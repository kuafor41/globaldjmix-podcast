#!/usr/bin/env python3
"""Build and validate the GlobalDJMix podcast RSS feed."""
import csv
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from threading import Lock, local
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://globaldjmix.com"
ARCHIVE = BASE + "/dj-songs-mp3-download"
DATA = Path("data")
ITEMS_FILE = DATA / "items.json"
REPORT_FILE = DATA / "test-report.json"
RETRY_FILE = DATA / "retry-queue.json"
DAILY_FILE = DATA / "daily-additions.json"
HOURLY_REPORT_FILE = DATA / "hourly-success-report.csv"
RSS_FILE = Path("rss.xml")

MODE = os.getenv("FEED_MODE", "test").strip().lower()
if MODE not in {"test", "incremental", "since2025", "full", "finalretry"}:
    MODE = "test"
TEST_LIMIT = 50
TEST_PAGE_CAP = 5
INCREMENTAL_PAGE_LIMIT = 3
MAX_RETRIES_PER_RUN = 30
MAX_RETRY_QUEUE = 1000
WORKERS = 5
TIMEOUT = 18
REQUEST_GAP = 0.12
AGENT = "Mozilla/5.0 (compatible; GlobalDJMixPodcastRSS/3.0)"
THREAD = local()
RATE_LOCK = Lock()
NEXT_REQUEST = 0.0

BLOCKED = {
    "", "rss", "topic", "livedjsets", "livesets", "podcasts", "news",
    "best-mixes-by-month", "dj-songs-mp3-download", "dj-songs-download",
    "tomorrowland", "abgt", "asot", "new-dj-mixes", "djs-list", "dj-shows",
    "share-mix-web-article", "search", "contact", "about", "podcast", "mixes",
    "sitemap", "privacy-policy", "terms",
}
MEDIA_HOSTS = {"box.globaldjmix.com", "box.download"}
URL_PATTERN = re.compile(r"""(?:https?:)?//[^\s"'<>\\]+""", re.I)
MP3_PATTERN = re.compile(r"""https?://[^"'<> \s\\]+\.mp3(?:\?[^"'<> \s]*)?""", re.I)


def session():
    if not hasattr(THREAD, "session"):
        THREAD.session = requests.Session()
        THREAD.session.headers.update({
            "User-Agent": AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
    return THREAD.session


def wait_turn():
    global NEXT_REQUEST
    with RATE_LOCK:
        now = time.monotonic()
        if now < NEXT_REQUEST:
            time.sleep(NEXT_REQUEST - now)
        NEXT_REQUEST = max(time.monotonic(), NEXT_REQUEST) + REQUEST_GAP


def clean_url(value, base=None):
    if not value:
        return None
    value = html.unescape(str(value)).strip().strip("\"'").replace("\\/", "/")
    if value.startswith("//"):
        value = "https:" + value
    value = urljoin(base or BASE, value)
    p = urlparse(value)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def http_version(url):
    p = urlparse(url)
    return urlunparse(("http", p.netloc, p.path, p.params, p.query, ""))


def request(url, referer=None, stream=False, timeout=TIMEOUT, max_redirects=7):
    current = clean_url(url)
    if not current:
        raise requests.RequestException("invalid URL")
    seen = set()
    headers = {"Referer": referer} if referer else {}
    for _ in range(max_redirects + 1):
        if current in seen:
            raise requests.TooManyRedirects("redirect loop: " + current)
        seen.add(current)
        wait_turn()
        try:
            response = session().get(
                current, headers=headers, timeout=(6, timeout),
                stream=stream, allow_redirects=False,
            )
        except requests.exceptions.SSLError:
            p = urlparse(current)
            if p.scheme == "https" and (p.hostname or "").lower() == "box.globaldjmix.com":
                current = http_version(current)
                continue
            raise
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise requests.RequestException("redirect without Location")
            target = clean_url(location, current)
            p = urlparse(target or "")
            if (p.hostname or "").lower() == "box.globaldjmix.com" and p.scheme == "https":
                target = http_version(target)
            current = target
            continue
        response.raise_for_status()
        return response
    raise requests.TooManyRedirects("too many redirects: " + str(url))


def get_page(url, referer=None):
    response = request(url, referer=referer, timeout=TIMEOUT)
    try:
        return response.url, response.text
    finally:
        response.close()


def unique(values):
    return list(dict.fromkeys(x for x in values if x))


def is_article(url):
    try:
        p = urlparse(url)
        slug = p.path.strip("/").lower()
        return (
            p.scheme in {"http", "https"}
            and (p.hostname or "").lower() in {"globaldjmix.com", "www.globaldjmix.com"}
            and "/" not in slug and len(slug) >= 15 and slug not in BLOCKED
            and not slug.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".css", ".js", ".xml"))
        )
    except Exception:
        return False


def extract_posts(body):
    soup = BeautifulSoup(body, "html.parser")
    # Recent-mix entries come before the "Most popular" block. Find the visible
    # marker as DOM text (it is not always an H2/H3 tag in the current template).
    marker = None
    for text_node in soup.find_all(string=re.compile(r"Most popular DJ Mixes", re.I)):
        if text_node.parent and text_node.parent.name not in {"script", "style"}:
            marker = text_node.parent
            break

    result = []
    for anchor in soup.find_all("a", href=True):
        if marker is not None and marker in anchor.find_all_previous():
            continue
        href = clean_url(anchor.get("href"), BASE)
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if not href or len(title) < 12 or not is_article(href):
            continue
        if re.fullmatch(r"(?:page\s*)?\d+", title, re.I):
            continue
        result.append(urlunparse((*urlparse(href)[:3], "", "", "")))
    return unique(result)

def archive_page_url(n):
    return ARCHIVE if n == 1 else ARCHIVE + "?p=" + str(n)


def total_pages(body):
    soup = BeautifulSoup(body, "html.parser")
    text = soup.get_text(" ", strip=True)
    for source in (text, body):
        match = re.search(r"Page\s+\d+\s+of\s+([\d,]+)", source, re.I)
        if match:
            return int(match.group(1).replace(",", ""))
    # Pagination links can still disclose the final page number when its label changes.
    values = []
    for href in re.findall(r"""href=["']([^"']*?)[?&]p=(\d+)[^"']*["']""", body, re.I):
        try:
            values.append(int(href[1]))
        except (ValueError, TypeError):
            pass
    return max(values) if values else 1

def archive_url_year(url):
    """Extract the listing/post date year from GlobalDJMix's trailing URL date."""
    slug = urlparse(url).path.strip("/").lower()
    match = re.search(
        r"-(20\d{2})-(january|february|march|april|may|june|july|august|september|october|november|december)-\d{1,2}$",
        slug,
    )
    if not match:
        match = re.search(r"-(20\d{2})-(\d{1,2})-(\d{1,2})$", slug)
    if match:
        return int(match.group(1))
    # Fallback for dated slugs whose month formatting differs.
    match = re.search(r"(?:^|-)(20\d{2})(?:-|$)", slug)
    if match:
        return int(match.group(1))
    return None


def discover_posts(mode):
    errors = []
    pages_checked = 0
    try:
        _, first_body = get_page(ARCHIVE)
        pages_checked = 1
    except Exception as exc:
        first_body = ""
        errors.append({"page": 1, "url": ARCHIVE, "error": type(exc).__name__ + ": " + str(exc)})

    reported = total_pages(first_body) if first_body else 1
    first_posts = extract_posts(first_body) if first_body else []
    print("Archive source:", ARCHIVE)
    print("Archive page 1:", len(first_posts), "posts; reported pages:", reported)
    if not first_posts and first_body:
        info = page_debug(first_body)
        errors.append({"page": 1, "url": ARCHIVE, "error": "No episode links extracted", "response": info})
        print("Archive page 1 diagnostic:", json.dumps(info, ensure_ascii=False)[:900])
        try:
            _, home_body = get_page(BASE + "/")
            home_posts = extract_posts(home_body)
            print("Home page fallback:", len(home_posts), "posts")
            if home_posts:
                first_posts.extend(home_posts)
            else:
                home_info = page_debug(home_body)
                errors.append({"page": "home", "url": BASE + "/", "error": "No episode links extracted", "response": home_info})
                print("Home page diagnostic:", json.dumps(home_info, ensure_ascii=False)[:900])
        except Exception as exc:
            errors.append({"page": "home", "url": BASE + "/", "error": type(exc).__name__ + ": " + str(exc)})

    found = unique(first_posts)
    older_consecutive = 0

    def keep_since_2025(url):
        year = archive_url_year(url)
        return year is None or year >= 2025

    if mode == "since2025":
        found = [url for url in found if keep_since_2025(url)]
        print("Episodes from 2025 onward found on page 1:", len(found))
        # Archive is newest-first. Stop after two consecutive pages whose dated
        # episode URLs are all older than 2025, rather than scanning the entire archive.
        for n in range(2, reported + 1):
            try:
                _, body = get_page(archive_page_url(n))
                pages_checked += 1
                posts = unique(extract_posts(body))
                years = [archive_url_year(url) for url in posts]
                has_since_2025 = any(keep_since_2025(url) for url in posts)
                known_years = [year for year in years if year is not None]
                if posts and not has_since_2025 and known_years and max(known_years) < 2025:
                    older_consecutive += 1
                else:
                    older_consecutive = 0
                found.extend(url for url in posts if keep_since_2025(url))
                found = unique(found)
                print(
                    "Archive page", n, ":", len(posts), "posts;",
                    "2025+ total:", len(found), "| older pages in a row:", older_consecutive,
                )
                if not posts:
                    errors.append({"page": n, "url": archive_page_url(n), "error": "No episode links extracted", "response": page_debug(body)})
                if older_consecutive >= 2:
                    print("Reached archive entries before 2025; stopping archive discovery.")
                    break
            except Exception as exc:
                errors.append({"page": n, "url": archive_page_url(n), "error": type(exc).__name__ + ": " + str(exc)})
                print("Archive page", n, "failed:", str(exc))
                # Do not silently assume older pages are reached after a network failure.
                break
    elif mode == "full":
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(get_page, archive_page_url(n)): n for n in range(2, reported + 1)}
            for future in as_completed(futures):
                n = futures[future]
                try:
                    _, body = future.result()
                    posts = extract_posts(body)
                    found.extend(posts)
                    pages_checked += 1
                    print("Archive page", n, ":", len(posts), "posts")
                    if not posts:
                        errors.append({"page": n, "url": archive_page_url(n), "error": "No episode links extracted", "response": page_debug(body)})
                except Exception as exc:
                    errors.append({"page": n, "url": archive_page_url(n), "error": type(exc).__name__ + ": " + str(exc)})
        found = unique(found)
    else:
        page_limit = min(reported, TEST_PAGE_CAP if mode == "test" else INCREMENTAL_PAGE_LIMIT)
        for n in range(2, page_limit + 1):
            try:
                _, body = get_page(archive_page_url(n))
                pages_checked += 1
                posts = extract_posts(body)
                found.extend(posts)
                found = unique(found)
                print("Archive page", n, ":", len(posts), "posts; unique total:", len(found))
                if not posts:
                    info = page_debug(body)
                    errors.append({"page": n, "url": archive_page_url(n), "error": "No episode links extracted", "response": info})
                    print("Page diagnostic:", json.dumps(info, ensure_ascii=False)[:700])
                if mode == "test" and len(found) >= TEST_LIMIT:
                    break
            except Exception as exc:
                errors.append({"page": n, "url": archive_page_url(n), "error": type(exc).__name__ + ": " + str(exc)})
                print("Archive page", n, "failed:", str(exc))
                break

    if mode == "test":
        found = found[:TEST_LIMIT]
    return found, reported, errors

def noise(value):
    value = (value or "").lower()
    return any(word in value for word in (
        "logo", "favicon", "avatar", "sprite", "placeholder", "transparent",
        "emoji", "icon-", "banner-ad", "gravatar", "social-icon",
    ))


def images_from_jsonld(data, page_url):
    found, stack = [], [data]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            image = value.get("image") or value.get("thumbnailUrl")
            if isinstance(image, str):
                found.append(clean_url(image, page_url))
            elif isinstance(image, dict):
                found.append(clean_url(image.get("url") or image.get("@id"), page_url))
            elif isinstance(image, list):
                for item in image:
                    if isinstance(item, str):
                        found.append(clean_url(item, page_url))
                    elif isinstance(item, dict):
                        found.append(clean_url(item.get("url") or item.get("@id"), page_url))
            stack.extend(x for x in value.values() if isinstance(x, (dict, list)))
        elif isinstance(value, list):
            stack.extend(value)
    return found


def find_image(soup, page_url):
    candidates = []
    for attrs in (
        {"property": "og:image"}, {"property": "og:image:url"},
        {"name": "twitter:image"}, {"name": "twitter:image:src"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            candidates.append(clean_url(tag["content"], page_url))
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            candidates.extend(images_from_jsonld(json.loads(script.string or script.get_text()), page_url))
        except Exception:
            pass

    containers = soup.select("article, main, .entry-content, .post, .content, .mix-content")
    for container in containers or [soup]:
        for img in container.find_all("img"):
            label = " ".join([
                str(img.get("alt", "")), " ".join(img.get("class", [])),
                str(img.get("src", "")),
            ])
            if noise(label):
                continue
            for attr in ("data-src", "data-lazy-src", "data-original", "data-image", "src", "srcset", "data-srcset"):
                raw = img.get(attr)
                if raw:
                    if "srcset" in attr:
                        raw = raw.split(",")[0].strip().split(" ")[0]
                    candidates.append(clean_url(raw, page_url))
                    break
    for candidate in unique(candidates):
        if candidate and not noise(candidate):
            return candidate
    return None


def audio_candidates(soup, base_url, raw_body="", intermediate=False):
    scored, seen = [], set()

    def add(raw, label="", attr_name=""):
        url = clean_url(raw, base_url)
        if not url or url in seen or noise(url):
            return
        p = urlparse(url)
        host = (p.hostname or "").lower()
        lower = url.lower()
        label_lower = label.lower()
        score = 0
        if host == "box.globaldjmix.com":
            score = 130
        elif host == "box.download":
            score = 125
        elif ".mp3" in lower:
            score = 120
        elif any(ext in lower for ext in (".m4a", ".aac", ".ogg", ".wav")):
            score = 115
        elif "download" in label_lower and host not in {"globaldjmix.com", "www.globaldjmix.com"}:
            score = 100
        elif intermediate and host not in {"globaldjmix.com", "www.globaldjmix.com"} and p.scheme in {"http", "https"}:
            score = 30
        if host in {"globaldjmix.com", "www.globaldjmix.com"} and (p.path.strip("/") in BLOCKED or url.rstrip("/") == base_url.rstrip("/")):
            score = 0
        if score:
            seen.add(url)
            scored.append((score, len(scored), url))

    attrs = ("href", "src", "data-href", "data-url", "data-download", "data-src", "data-link", "formaction", "onclick")
    for tag in soup.find_all(True):
        label_parts = [tag.get_text(" ", strip=True)[:120] if tag.name in {"a", "button", "source", "audio", "iframe"} else ""]
        for name in ("id", "class", "title", "aria-label", "download"):
            value = tag.get(name)
            if isinstance(value, list):
                value = " ".join(value)
            if value:
                label_parts.append(str(value))
        label = " ".join(label_parts)
        for attr in attrs:
            value = tag.get(attr)
            if not value:
                continue
            for raw in (value if isinstance(value, list) else [str(value)]):
                add(raw, label, attr)
                if attr in {"onclick", "data-url", "data-href", "data-download", "data-link"}:
                    for match in URL_PATTERN.findall(html.unescape(str(raw)).replace("\\/", "/")):
                        add(match, label, attr)

    raw = html.unescape(raw_body or "").replace("\\/", "/")
    for match in MP3_PATTERN.findall(raw):
        add(match, "mp3", "raw")
    for match in URL_PATTERN.findall(raw):
        candidate = match.rstrip(");,]")
        if any(host in candidate.lower() for host in MEDIA_HOSTS):
            add(candidate, "box.download", "raw")

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [url for _, _, url in scored[:7]]


def get_response_size(response):
    content_range = response.headers.get("Content-Range", "")
    match = re.search(r"/(\d+)\s*$", content_range)
    if match:
        return int(match.group(1))
    value = response.headers.get("Content-Length", "")
    return int(value) if value.isdigit() else 0


def probe_audio(url, referer):
    last_error = None
    for attempt in range(2):
        response = None
        try:
            response = request(
                url, referer=referer, stream=True, timeout=10, max_redirects=5,
            )
            final_url = response.url
            content_type = (response.headers.get("Content-Type") or "").lower()
            disposition = (response.headers.get("Content-Disposition") or "").lower()
            size = get_response_size(response)
            if "text/html" in content_type or "xhtml" in content_type:
                body = response.text[:300000]
                return "html", final_url, body, size, None

            prefix = next(response.iter_content(chunk_size=512), b"")
            if prefix.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                body = prefix.decode("utf-8", "ignore")
                return "html", final_url, body, size, None

            audio_like = (
                content_type.startswith("audio/")
                or any(x in content_type for x in (
                    "application/octet-stream", "application/x-download",
                    "application/download", "application/force-download", "binary/octet-stream",
                ))
                or ".mp3" in disposition
                or any(ext in final_url.lower() for ext in (".mp3", ".m4a", ".aac", ".ogg", ".wav"))
            )
            if audio_like:
                return "audio", final_url, None, size, None
            return "other", final_url, None, size, "content-type=" + (content_type or "missing")
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = type(exc).__name__ + ": " + str(exc)
            if attempt == 0:
                time.sleep(0.5)
                continue
            return "error", None, None, 0, last_error
        except Exception as exc:
            return "error", None, None, 0, type(exc).__name__ + ": " + str(exc)
        finally:
            if response is not None:
                response.close()
    return "error", None, None, 0, last_error or "probe failed"

def resolve_audio(soup, raw_body, article_url):
    candidates = audio_candidates(soup, article_url, raw_body)
    errors = []
    for candidate in candidates[:5]:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        pixeldrain = re.fullmatch(r"/u/([A-Za-z0-9_-]+)", parsed.path.rstrip("/"))
        if host in {"pixeldrain.com", "www.pixeldrain.com"} and pixeldrain:
            # Turn a Pixeldrain sharing page into its generic file API URL using
            # the ID discovered from that page; no episode-specific token is fixed.
            direct = "https://pixeldrain.com/api/file/" + pixeldrain.group(1) + "?download"
            kind, final_url, body, size, error = probe_audio(direct, article_url)
            if kind == "audio":
                return direct, "intermediate", size, candidates, None
            if error:
                errors.append("Pixeldrain API: " + error)

        kind, final_url, body, size, error = probe_audio(candidate, article_url)
        if kind == "audio":
            return candidate, "direct", size, candidates, None
        if kind == "html" and body:
            mid_soup = BeautifulSoup(body, "html.parser")
            inner = audio_candidates(mid_soup, final_url, body, intermediate=True)
            for url in inner[:6]:
                inner_kind, inner_url, _, inner_size, inner_error = probe_audio(url, final_url)
                if inner_kind == "audio":
                    return url, "intermediate", inner_size, candidates, None
                if inner_error:
                    errors.append(inner_error)

            # Last-resort: JavaScript/HTML may spell the actual link without an anchor.
            raw = html.unescape(body).replace("\\/", "/")
            raw_urls = unique([clean_url(x, final_url) for x in URL_PATTERN.findall(raw)])
            raw_urls += unique([clean_url(x, final_url) for x in MP3_PATTERN.findall(raw)])
            for url in raw_urls:
                if not url:
                    continue
                p = urlparse(url)
                if (p.hostname or "").lower() not in MEDIA_HOSTS and ".mp3" not in url.lower():
                    continue
                inner_kind, inner_url, _, inner_size, inner_error = probe_audio(url, final_url)
                if inner_kind == "audio":
                    return inner_url, "intermediate", inner_size, candidates, None
                if inner_error:
                    errors.append(inner_error)
        if error:
            errors.append(error)

    detail = "No audio URL resolved. candidates=" + json.dumps(candidates[:5])
    if errors:
        detail += "; " + " | ".join(errors[:4])
    return None, None, 0, candidates, detail

def verify_image(url, referer):
    if not url:
        return False, "No image URL found in this episode page."
    response = None
    try:
        response = request(url, referer=referer, stream=True, timeout=10)
        content_type = (response.headers.get("Content-Type") or "").lower()
        suffix = urlparse(response.url).path.lower()
        ok = content_type.startswith("image/") or (
            suffix.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))
            and ("octet-stream" in content_type or not content_type)
        )
        if not ok:
            return False, "image returned content-type=" + (content_type or "missing")
        return True, None
    except Exception as exc:
        return False, type(exc).__name__ + ": " + str(exc)
    finally:
        if response is not None:
            response.close()


def episode_date(text, title):
    # Prefer the publication/listing date so Podcast Addict shows episodes
    # near their actual release date. Rec Date is only a fallback.
    date_formats = [
        (r"(\d{1,2}-[A-Za-z]{3,9}-\d{4})", ("%d-%b-%Y", "%d-%B-%Y")),
        (r"(\d{1,2}/\d{1,2}/\d{4})", ("%d/%m/%Y", "%m/%d/%Y")),
    ]
    for label in ("Post Date", "Rec Date"):
        for source in (text, title):
            for pattern, formats in date_formats:
                match = re.search(label + r"\s*:?\s*" + pattern, source, re.I)
                if not match:
                    continue
                value = match.group(1)
                for fmt in formats:
                    try:
                        return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
                    except ValueError:
                        pass

    # Fallback for pages that expose only an ISO-formatted date.
    for source in (text, title):
        match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", source)
        if match:
            try:
                return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                pass
    return datetime.now(timezone.utc).isoformat()


def parse_episode(url):
    diag = {
        "url": url, "article_fetched": False, "title": "", "audio_ok": False,
        "audio_method": None, "audio_candidates": [], "audio_url": None,
        "audio_attempts": 0, "image_found": False, "image_url": None, "image_http_ok": False,
        "audio_error": None, "image_error": None, "error": None,
    }
    try:
        final_url, body = get_page(url)
        diag["article_fetched"] = True
        soup = BeautifulSoup(body, "html.parser")
        heading = soup.find("h1")
        title = re.sub(r"\s+", " ", heading.get_text(" ", strip=True)).strip() if heading else ""
        if not title:
            meta = soup.find("meta", attrs={"property": "og:title"})
            title = (meta.get("content") or "").strip() if meta else ""
        if not title and soup.title:
            title = soup.title.get_text(" ", strip=True)
        diag["title"] = title or url

        image_url = find_image(soup, final_url)
        diag["image_found"] = bool(image_url)
        diag["image_url"] = image_url
        diag["image_http_ok"], diag["image_error"] = verify_image(image_url, final_url)

        audio_url, method, size, candidates, error = resolve_audio(soup, body, final_url)
        diag["audio_attempts"] = 1
        # One delayed retry catches transient timeouts and server failures without
        # allowing a single episode to stall the entire archive scan.
        if not audio_url:
            time.sleep(1.2)
            audio_url, method, size, retry_candidates, retry_error = resolve_audio(soup, body, final_url)
            diag["audio_attempts"] = 2
            candidates = unique(candidates + retry_candidates)
            error = retry_error or error
        diag["audio_candidates"] = candidates
        diag["audio_error"] = error
        if not audio_url:
            return None, diag

        diag["audio_ok"] = True
        diag["audio_method"] = method
        diag["audio_url"] = audio_url
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        date = episode_date(text, title)
        description = ""
        for selector in ("meta[name=description]", "meta[property='og:description']"):
            meta = soup.select_one(selector)
            if meta and meta.get("content"):
                description = meta["content"].strip()
                break
        item = {
            "title": title, "source_url": final_url, "audio_url": audio_url,
            "image_url": image_url, "pub_date": date, "length": size,
            "description": description or title,
        }
        return item, diag
    except Exception as exc:
        diag["error"] = type(exc).__name__ + ": " + str(exc)
        return None, diag


def sort_key(item):
    try:
        dt = datetime.fromisoformat(item.get("pub_date", "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def load_items():
    try:
        parsed = json.loads(ITEMS_FILE.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []

def load_daily_additions():
    try:
        parsed = json.loads(DAILY_FILE.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) and isinstance(parsed.get("days"), dict) else {"timezone": "Europe/Istanbul", "days": {}}
    except Exception:
        return {"timezone": "Europe/Istanbul", "days": {}}


def save_daily_additions(data, today):
    # Keep the last 365 calendar days to prevent the history file growing forever.
    from datetime import timedelta
    cutoff = today - timedelta(days=364)
    kept = {}
    for day, entry in data.get("days", {}).items():
        try:
            parsed_day = datetime.strptime(day, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if parsed_day >= cutoff:
            kept[day] = entry
    data["timezone"] = "Europe/Istanbul"
    data["days"] = kept
    DAILY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_retry_queue():
    try:
        parsed = json.loads(RETRY_FILE.read_text(encoding="utf-8"))
        # An existing empty list is an intentional "queue cleared" state.
        # Only recover from the report when the queue file is missing or invalid.
        if isinstance(parsed, list):
            return unique(parsed)[:MAX_RETRY_QUEUE]
    except Exception:
        pass

    # Recovery path: if the queue file is missing or invalid, seed it from the
    # latest diagnostic report so a prior full import's failures are not forgotten.
    try:
        report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        failed = [
            diag.get("url") for diag in report.get("diagnostics", [])
            if not diag.get("audio_ok") and diag.get("url")
        ]
        return unique(failed)[:MAX_RETRY_QUEUE]
    except Exception:
        return []


def rss_xml(items):
    ET.register_namespace("itunes", "http://www.itunes.com/dtds/podcast-1.0.dtd")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "GlobalDJMix Podcast"
    ET.SubElement(channel, "link").text = ARCHIVE
    ET.SubElement(channel, "description").text = "DJ mixes, podcasts and live sets from GlobalDJMix."
    ET.SubElement(channel, "language").text = "en"
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}author").text = "GlobalDJMix"
    ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}explicit").text = "no"
    channel_image = next((item.get("image_url") for item in sorted(items, key=sort_key, reverse=True) if item.get("image_url")), None)
    if channel_image:
        ET.SubElement(channel, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image", {"href": channel_image})

    for item in sorted(items, key=sort_key, reverse=True):
        if not item.get("audio_url"):
            continue
        node = ET.SubElement(channel, "item")
        ET.SubElement(node, "title").text = item.get("title") or item.get("source_url") or "DJ Mix"
        ET.SubElement(node, "link").text = item.get("source_url", "")
        ET.SubElement(node, "guid", {"isPermaLink": "false"}).text = item.get("source_url", item.get("audio_url", ""))
        try:
            dt = datetime.fromisoformat(item["pub_date"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        ET.SubElement(node, "pubDate").text = format_datetime(dt)
        ET.SubElement(node, "description").text = item.get("description") or item.get("title") or ""
        enclosure_type = "audio/mpeg"
        ET.SubElement(node, "enclosure", {
            "url": item["audio_url"], "length": str(max(0, int(item.get("length") or 0))),
            "type": enclosure_type,
        })
        if item.get("duration"):
            ET.SubElement(node, "{http://www.itunes.com/dtds/podcast-1.0.dtd}duration").text = item["duration"]
        if item.get("image_url"):
            ET.SubElement(node, "{http://www.itunes.com/dtds/podcast-1.0.dtd}image", {"href": item["image_url"]})

    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


def validate_feed(xml_text, expected_count, live_audio_checks=3):
    """Validate RSS structure/order/retention and probe a few published enclosures."""
    errors, warnings, audio_probe_failures = [], [], []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return {
            "passed": False, "errors": ["Malformed XML: " + str(exc)],
            "warnings": [], "audio_probe_failures": [], "item_count": 0,
            "live_audio_checked": 0,
        }

    if root.tag != "rss" or root.get("version") != "2.0":
        errors.append("Root must be RSS 2.0.")
    channel = root.find("channel")
    if channel is None:
        return {
            "passed": False, "errors": errors + ["Missing channel element."],
            "warnings": warnings, "audio_probe_failures": [],
            "item_count": 0, "live_audio_checked": 0,
        }

    feed_items = channel.findall("item")
    if len(feed_items) != expected_count:
        errors.append(f"RSS item count {len(feed_items)} does not match expected {expected_count}.")

    seen_guids = set()
    parsed_dates = []
    enclosure_urls = []
    for index, node in enumerate(feed_items, start=1):
        title = (node.findtext("title") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        pub_date = (node.findtext("pubDate") or "").strip()
        enclosure = node.find("enclosure")
        if not title:
            errors.append(f"Item {index} has no title.")
        if not guid:
            errors.append(f"Item {index} has no GUID.")
        elif guid in seen_guids:
            errors.append(f"Duplicate GUID at item {index}: {guid}")
        seen_guids.add(guid)

        try:
            dt = parsedate_to_datetime(pub_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt = dt.astimezone(timezone.utc)
            parsed_dates.append(dt)
            if dt < datetime(2025, 1, 1, tzinfo=timezone.utc):
                errors.append(f"Item {index} is older than 2025: {pub_date}")
        except Exception:
            errors.append(f"Item {index} has invalid pubDate: {pub_date}")

        if enclosure is None:
            errors.append(f"Item {index} has no enclosure.")
            continue
        url = enclosure.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append(f"Item {index} has an invalid enclosure URL.")
        if enclosure.get("type") != "audio/mpeg":
            errors.append(f"Item {index} enclosure type is not audio/mpeg.")
        if not url:
            errors.append(f"Item {index} has an empty enclosure URL.")
        else:
            enclosure_urls.append((url, node.findtext("link") or ""))

    if any(parsed_dates[i] < parsed_dates[i + 1] for i in range(len(parsed_dates) - 1)):
        errors.append("RSS items are not sorted by pubDate descending.")

    checked = 0
    for url, referer in enclosure_urls[:max(0, live_audio_checks)]:
        checked += 1
        kind, final_url, _, _, error = probe_audio(url, referer or ARCHIVE)
        if kind != "audio":
            audio_probe_failures.append({
                "url": url, "result": kind, "final_url": final_url, "error": error,
            })

    if audio_probe_failures:
        warnings.append(
            f"{len(audio_probe_failures)} of {checked} sampled published audio links failed a live probe."
        )
    return {
        # Structural errors fail validation; transient live-probe failures are warnings.
        "passed": not errors,
        "errors": errors, "warnings": warnings,
        "audio_probe_failures": audio_probe_failures,
        "item_count": len(feed_items), "live_audio_checked": checked,
    }



def main():
    global MODE
    # A report marker arms exactly one cleanup run without changing the hourly schedule.
    try:
        previous_report = json.loads(REPORT_FILE.read_text(encoding="utf-8"))
        if MODE == "incremental" and previous_report.get("one_time_legacy_retry_pending"):
            MODE = "finalretry"
            print("One-time cleanup enabled: checking the entire legacy retry queue.")
    except Exception:
        pass
    DATA.mkdir(parents=True, exist_ok=True)
    existing = load_items()
    retry_queue = load_retry_queue()
    daily_data = load_daily_additions()
    local_now = datetime.now(ZoneInfo("Europe/Istanbul"))
    today_key = local_now.date().isoformat()
    today_entry = daily_data.get("days", {}).get(today_key, {"added_count": 0, "episodes": []})
    print("Mode:", MODE, "| timeout:", TIMEOUT, "| workers:", WORKERS)
    posts, reported_pages, archive_errors = discover_posts(MODE)
    print("Selected archive article pages:", len(posts))

    existing_by_url = {item.get("source_url"): item for item in existing if item.get("source_url")}
    selected = posts[:TEST_LIMIT] if MODE == "test" else posts

    if MODE == "test":
        # Test mode checks recent entries but never touches production data.
        pending = selected
    elif MODE == "since2025":
        # Re-parse retained archive pages so stored publication dates are refreshed.
        pending = selected
    elif MODE == "full":
        pending = [url for url in selected if url not in existing_by_url]
    elif MODE == "finalretry":
        # One-time cleanup run: check every legacy queued URL exactly once.
        # Keep discovery of recent new episodes active during the cleanup.
        pending = unique(
            [url for url in selected if url not in existing_by_url] + retry_queue
        )
    else:
        # Hourly mode scans only three archive pages plus a bounded retry batch.
        retry_batch = retry_queue[:MAX_RETRIES_PER_RUN]
        pending = unique(
            [url for url in selected if url not in existing_by_url] + retry_batch
        )

    diagnostics = []
    new_items = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(parse_episode, url): url for url in pending}
        for number, future in enumerate(as_completed(futures), start=1):
            try:
                item, diag = future.result()
            except Exception as exc:
                item = None
                diag = {"url": futures[future], "audio_ok": False, "error": type(exc).__name__ + ": " + str(exc)}
            diagnostics.append(diag)
            if item:
                new_items.append(item)
            print(
                "Episode", number, "/", len(pending),
                "| audio", bool(item),
                "| image", bool(diag.get("image_http_ok")),
                "| audio attempts", diag.get("audio_attempts", 0),
                "| method", diag.get("audio_method") or "-",
                "|", (diag.get("title") or diag.get("url") or "")[:92],
            )
            if not item:
                print("  Diagnostic:", (diag.get("audio_error") or diag.get("error") or "audio unresolved")[:700])
            if diag.get("image_error"):
                print("  Image diagnostic:", str(diag["image_error"])[:250])

    audio_ok = sum(bool(d.get("audio_ok")) for d in diagnostics)
    image_ok = sum(bool(d.get("image_http_ok")) for d in diagnostics)
    image_found = sum(bool(d.get("image_found")) for d in diagnostics)
    methods = {}
    for diag in diagnostics:
        if diag.get("audio_method"):
            methods[diag["audio_method"]] = methods.get(diag["audio_method"], 0) + 1

    merged = dict(existing_by_url)
    if MODE != "test":
        for item in new_items:
            merged[item["source_url"]] = item

        def item_year(item):
            year = archive_url_year(item.get("source_url", ""))
            if year is not None:
                return year
            try:
                return datetime.fromisoformat(item.get("pub_date", "").replace("Z", "+00:00")).year
            except Exception:
                return 0

        # Enforce retention on every production run so older entries cannot return.
        merged = {url: item for url, item in merged.items() if item_year(item) >= 2025}
        candidate_items = sorted(merged.values(), key=sort_key, reverse=True)

        # Remove legacy duplicate episodes before publishing. Keep the newest
        # record when the same episode appears under multiple source URLs.
        deduplicated_items = []
        seen_episode_keys = set()
        for item in candidate_items:
            title_key = re.sub(r"\\s+", " ", (item.get("title") or "").strip()).casefold()
            source_key = (item.get("source_url") or "").rstrip("/").casefold()
            audio_key = (item.get("audio_url") or "").split("?")[0].rstrip("/").casefold()
            keys = [("source", source_key)] if source_key else []
            if title_key:
                keys.append(("title", title_key))
            if audio_key:
                keys.append(("audio", audio_key))
            if any(key in seen_episode_keys for key in keys):
                continue
            deduplicated_items.append(item)
            seen_episode_keys.update(keys)
        removed_duplicates = len(candidate_items) - len(deduplicated_items)
        if removed_duplicates:
            print("Removed duplicate episode records:", removed_duplicates)
        candidate_items = deduplicated_items
    else:
        candidate_items = sorted(new_items, key=sort_key, reverse=True)[:TEST_LIMIT]

    candidate_xml = rss_xml(candidate_items)
    expected_count = sum(bool(item.get("audio_url")) for item in candidate_items)
    validation = validate_feed(candidate_xml, expected_count, live_audio_checks=3)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": MODE, "archive_source": ARCHIVE,
        "one_time_legacy_retry_pending": False,
        "archive_pages_reported": reported_pages,
        "archive_pages_failed": archive_errors,
        "selected_articles": len(selected),
        "articles_checked": len(diagnostics),
        "audio_resolved": audio_ok,
        "images_found": image_found,
        "images_verified": image_ok,
        "audio_resolution_methods": methods,
        "test_passed": None,
        "saved_episode_count": len(existing),
        "new_episodes_added": 0,
        "new_episode_titles": [],
        "daily_date": today_key,
        "daily_added_count": len(today_entry.get("episodes", [])),
        "daily_episode_titles": [entry.get("title", "") for entry in today_entry.get("episodes", [])],
        "validation": validation,
        "retry_queue_before": len(retry_queue),
        "retry_queue_after": len(retry_queue),
        "retry_queue_added": 0,
        "retry_queue_processed": 0,
        "diagnostics": diagnostics,
    }

    if MODE == "test":
        # Test mode writes only its report; it cannot replace the production feed.
        report["test_passed"] = (
            bool(selected)
            and audio_ok == len(selected)
            and image_ok == len(selected)
            and validation["passed"]
        )
        report["saved_episode_count"] = len(existing)
        print("TEST MODE: production RSS/items/retry queue left unchanged.")
    else:
        processed_urls = set(pending)
        failed_urls = unique([
            d.get("url") for d in diagnostics
            if not d.get("audio_ok") and d.get("url")
        ])
        remaining_queue = [url for url in retry_queue if url not in processed_urls]
        if MODE == "finalretry":
            # Do not re-queue failures from the legacy backlog after its one final check.
            # Only failures from newly discovered episodes stay queued for future retries.
            fresh_episode_urls = {
                url for url in selected
                if url not in existing_by_url and url not in retry_queue
            }
            failed_fresh_urls = [url for url in failed_urls if url in fresh_episode_urls]
            next_retry_queue = unique(failed_fresh_urls)[:MAX_RETRY_QUEUE]
            report["legacy_retry_urls_retired"] = sum(url in retry_queue for url in processed_urls)
            report["legacy_retry_failures_retired"] = sum(url in retry_queue for url in failed_urls)
        else:
            next_retry_queue = unique(remaining_queue + failed_urls)[:MAX_RETRY_QUEUE]
        report["retry_queue_processed"] = sum(url in processed_urls for url in retry_queue)
        report["retry_queue_added"] = sum(url not in retry_queue for url in failed_urls)
        report["retry_queue_after"] = len(next_retry_queue)

        # Structural failures preserve the last known-good RSS. Sampled audio
        # probe failures are warnings because remote hosts can be transient.
        if validation["errors"]:
            print("VALIDATION ERROR: keeping previously published RSS/items unchanged.")
            report["publish_blocked"] = True
            report["saved_episode_count"] = len(existing)
            if MODE == "finalretry":
                # Do not retire the legacy backlog unless the recovered items can be published.
                next_retry_queue = retry_queue
                report["retry_queue_after"] = len(retry_queue)
                report["legacy_retry_urls_retired"] = 0
                report["legacy_retry_failures_retired"] = 0
        else:
            ITEMS_FILE.write_text(json.dumps(candidate_items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            RSS_FILE.write_text(candidate_xml, encoding="utf-8")
            report["publish_blocked"] = False
            report["saved_episode_count"] = len(candidate_items)

            # Count only genuinely new source URLs after the validated feed was published.
            added_by_url = {}
            for item in new_items:
                source_url = item.get("source_url")
                if source_url and source_url not in existing_by_url:
                    added_by_url[source_url] = item
            newly_added = list(added_by_url.values())
            report["new_episodes_added"] = len(newly_added)
            report["new_episode_titles"] = [item.get("title") or item.get("source_url", "") for item in newly_added]

            day_entry = daily_data.get("days", {}).get(today_key, {"added_count": 0, "episodes": []})
            day_episodes = day_entry.get("episodes", [])
            recorded_urls = {entry.get("source_url") for entry in day_episodes}
            for item in newly_added:
                if item.get("source_url") not in recorded_urls:
                    day_episodes.append({
                        "title": item.get("title") or item.get("source_url", ""),
                        "source_url": item.get("source_url", ""),
                        "added_at": local_now.isoformat(),
                    })
                    recorded_urls.add(item.get("source_url"))
            daily_data.setdefault("days", {})[today_key] = {
                "added_count": len(day_episodes),
                "episodes": day_episodes,
            }
            save_daily_additions(daily_data, local_now.date())
            report["daily_added_count"] = len(day_episodes)
            report["daily_episode_titles"] = [entry.get("title", "") for entry in day_episodes]

            # Record only successful scheduled hourly checks, including checks that add zero episodes.
            if os.getenv("HOURLY_REPORT_ENABLED", "").strip().lower() == "true" and MODE == "incremental":
                HOURLY_REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
                report_header = ["date_turkey", "time_turkey", "added_count", "episode_titles"]
                previous_rows = []
                if HOURLY_REPORT_FILE.exists() and HOURLY_REPORT_FILE.stat().st_size > 0:
                    with HOURLY_REPORT_FILE.open("r", encoding="utf-8", newline="") as history_file:
                        previous_rows = list(csv.reader(history_file))
                # Keep one rolling file, with the newest successful scheduled check first.
                old_entries = previous_rows[1:] if previous_rows and previous_rows[0] == report_header else []
                new_entry = [
                    local_now.strftime("%Y-%m-%d"),
                    local_now.strftime("%H:%M:%S"),
                    str(len(newly_added)),
                    " | ".join(item.get("title") or item.get("source_url", "") for item in newly_added),
                ]
                with HOURLY_REPORT_FILE.open("w", encoding="utf-8", newline="") as history_file:
                    writer = csv.writer(history_file)
                    writer.writerow(report_header)
                    writer.writerow(new_entry)
                    writer.writerows(old_entries)
                print("Hourly success report updated (newest first):", str(HOURLY_REPORT_FILE))

        RETRY_FILE.write_text(json.dumps(next_retry_queue, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "SUMMARY: archive selected=", len(selected),
        "| checked=", len(diagnostics),
        "| audio resolved=", str(audio_ok) + "/" + str(len(diagnostics)),
        "| artwork found=", str(image_found) + "/" + str(len(diagnostics)),
        "| artwork verified=", str(image_ok) + "/" + str(len(diagnostics)),
        "| methods=", json.dumps(methods),
        "| validation passed=", validation["passed"],
        "| validation errors=", len(validation["errors"]),
        "| sampled audio failures=", len(validation["audio_probe_failures"]),
        "| saved episodes=", report["saved_episode_count"],
        "| newly added=", report.get("new_episodes_added", 0),
        "| added today=", report.get("daily_added_count", 0),
        "| retry queue=", report["retry_queue_after"],
    )
    return 1 if MODE != "test" and validation["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
