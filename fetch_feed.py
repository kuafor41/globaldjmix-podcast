#!/usr/bin/env python3
"""GlobalDJMix podcast RSS builder; new runs test 50 recent episodes first."""
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from threading import Lock, local
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://globaldjmix.com"
ARCHIVE = BASE + "/livedjsets"
DATA = Path("data")
ITEMS_FILE = DATA / "items.json"
REPORT_FILE = DATA / "test-report.json"
RSS_FILE = Path("rss.xml")

MODE = os.getenv("FEED_MODE", "test").strip().lower()
if MODE not in {"test", "incremental", "full"}:
    MODE = "test"
TEST_LIMIT = 50
ARCHIVE_PAGE_CAP = 3
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
        THREAD.login_attempted = False

    if not THREAD.login_attempted:
        THREAD.login_attempted = True
        username = os.getenv("GLOBALDJMIX_USERNAME", "").strip()
        password = os.getenv("GLOBALDJMIX_PASSWORD", "")
        if username and password:
            try:
                auth_response = THREAD.session.get(BASE + "/get-auth-form", timeout=(6, TIMEOUT))
                auth_soup = BeautifulSoup(auth_response.text, "html.parser")
                form = auth_soup.find("form")
                if not form:
                    print("GlobalDJMix login form unavailable; tracklists will be skipped.")
                else:
                    data = {}
                    for field in form.find_all("input"):
                        name = field.get("name")
                        if not name:
                            continue
                        field_type = (field.get("type") or "text").lower()
                        if field_type == "password":
                            data[name] = password
                        elif name.lower() in {"login", "username", "user", "email"}:
                            data[name] = username
                        else:
                            data[name] = field.get("value", "")
                    action = urljoin(auth_response.url, form.get("action") or auth_response.url)
                    login_response = THREAD.session.post(
                        action, data=data, headers={"Referer": auth_response.url},
                        timeout=(6, TIMEOUT), allow_redirects=True,
                    )
                    body = login_response.text.lower()
                    logged_in = any(marker in body for marker in ("logout", "log out", "sign out"))
                    if logged_in:
                        print("GlobalDJMix login succeeded; tracklist retrieval enabled.")
                    else:
                        print("GlobalDJMix login could not be confirmed; tracklists may be unavailable.")
            except Exception as exc:
                print("GlobalDJMix login error; continuing without tracklists:", type(exc).__name__)

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
    found = extract_posts(first_body) if first_body else []
    print("Archive source:", ARCHIVE)
    print("Archive page 1:", len(found), "posts; reported pages:", reported)
    if not found and first_body:
        info = page_debug(first_body)
        errors.append({"page": 1, "url": ARCHIVE, "error": "No episode links extracted", "response": info})
        print("Archive page 1 diagnostic:", json.dumps(info, ensure_ascii=False)[:900])
        # The home page also lists the newest mixes. This fallback avoids the separate
        # DJ-song search page and gives the test a second legitimate listing source.
        try:
            _, home_body = get_page(BASE + "/")
            home_posts = extract_posts(home_body)
            print("Home page fallback:", len(home_posts), "posts")
            if home_posts:
                found.extend(home_posts)
            else:
                home_info = page_debug(home_body)
                errors.append({"page": "home", "url": BASE + "/", "error": "No episode links extracted", "response": home_info})
                print("Home page diagnostic:", json.dumps(home_info, ensure_ascii=False)[:900])
        except Exception as exc:
            errors.append({"page": "home", "url": BASE + "/", "error": type(exc).__name__ + ": " + str(exc)})

    found = unique(found)
    if mode == "full":
        page_numbers = range(2, reported + 1)
    else:
        page_numbers = range(2, max(reported, ARCHIVE_PAGE_CAP) + 1)

    if mode == "full":
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(get_page, archive_page_url(n)): n for n in page_numbers}
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
        for n in page_numbers:
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
    patterns = [
        (r"(?:Post Date|Rec Date)\s*:?\s*(\d{1,2}-[A-Za-z]{3,9}-\d{4})", ("%d-%b-%Y", "%d-%B-%Y")),
        (r"(?:Post Date|Rec Date)\s*:?\s*(\d{1,2}/\d{1,2}/\d{4})", ("%d/%m/%Y", "%m/%d/%Y")),
        (r"\b(\d{4}-\d{2}-\d{2})\b", ("%Y-%m-%d",)),
    ]
    for pattern, formats in patterns:
        values = re.findall(pattern, text, re.I) or re.findall(pattern, title, re.I)
        for value in values:
            for fmt in formats:
                try:
                    return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    pass
    return datetime.now(timezone.utc).isoformat()


def fetch_tracklist(soup, article_url):
    button = soup.select_one("button.show-tracklist[data-id]")
    if not button or not button.get("data-id"):
        return None, "Tracklist button not found"
    if not (os.getenv("GLOBALDJMIX_USERNAME") and os.getenv("GLOBALDJMIX_PASSWORD")):
        return None, "Login credentials not configured"

    response = None
    try:
        wait_turn()
        response = session().get(
            BASE + "/get-tracklist",
            params={"id": button.get("data-id")},
            headers={"Referer": article_url, "X-Requested-With": "XMLHttpRequest"},
            timeout=(6, TIMEOUT),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") is not True:
            return None, "Tracklist endpoint did not return success"
        tracklist_html = payload.get("tracklist") or ""
        track_soup = BeautifulSoup(tracklist_html, "html.parser")
        track_nodes = track_soup.select(".track")
        tracks = [
            re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
            for node in track_nodes
        ]
        tracks = [track for track in tracks if track]
        if not tracks:
            plain = re.sub(r"\s+", " ", track_soup.get_text(" ", strip=True)).strip()
            if plain:
                tracks = [plain]
        if not tracks:
            return None, "Tracklist response was successful but contained no tracks"
        return "\n".join(tracks), None
    except Exception as exc:
        return None, type(exc).__name__ + ": " + str(exc)[:220]
    finally:
        if response is not None:
            response.close()


def parse_episode(url):
    diag = {
        "url": url, "article_fetched": False, "title": "", "audio_ok": False,
        "audio_method": None, "audio_candidates": [], "audio_url": None,
        "image_found": False, "image_url": None, "image_http_ok": False,
        "tracklist_button_found": False, "tracklist_found": False, "tracklist_error": None,
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
        description = description or title
        diag["tracklist_button_found"] = bool(soup.select_one("button.show-tracklist[data-id]"))
        tracklist, tracklist_error = fetch_tracklist(soup, final_url)
        diag["tracklist_found"] = bool(tracklist)
        diag["tracklist_error"] = tracklist_error
        if tracklist:
            description += "\n\nTracklist:\n" + tracklist
        item = {
            "title": title, "source_url": final_url, "audio_url": audio_url,
            "image_url": image_url, "pub_date": date, "length": size,
            "description": description,
        }
        return item, diag
    except Exception as exc:
        diag["error"] = type(exc).__name__ + ": " + str(exc)
        return None, diag


def sort_key(item):
    try:
        return datetime.fromisoformat(item.get("pub_date", "").replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def load_items():
    try:
        parsed = json.loads(ITEMS_FILE.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, list) else []
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


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    existing = load_items()
    print("Mode:", MODE, "| timeout:", TIMEOUT, "| workers:", WORKERS)
    posts, reported_pages, archive_errors = discover_posts(MODE)
    print("Selected article pages:", len(posts))

    existing_by_url = {item.get("source_url"): item for item in existing if item.get("source_url")}
    if MODE == "full":
        selected = posts
        pending = [url for url in selected if url not in existing_by_url]
    else:
        selected = posts[:TEST_LIMIT] if MODE == "test" else posts
        pending = [url for url in selected if url not in existing_by_url]
    if MODE == "test":
        # A test always performs the requested checks instead of skipping previously saved rows.
        pending = selected

    diagnostics = []
    new_items = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(parse_episode, url): url for url in pending}
        for number, future in enumerate(as_completed(futures), start=1):
            try:
                item, diag = future.result()
            except Exception as exc:
                item = None
                diag = {"url": futures[future], "error": type(exc).__name__ + ": " + str(exc)}
            diagnostics.append(diag)
            if item:
                new_items.append(item)
            print(
                "Episode", number, "/", len(pending),
                "| audio", bool(item),
                "| image", bool(diag.get("image_http_ok")),
                "| tracklist", bool(diag.get("tracklist_found")),
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
    tracklists_found = sum(bool(d.get("tracklist_found")) for d in diagnostics)
    tracklists_missing = len(diagnostics) - tracklists_found
    tracklist_buttons_found = sum(bool(d.get("tracklist_button_found")) for d in diagnostics)
    tracklist_buttonless_pages = len(diagnostics) - tracklist_buttons_found
    tracklist_button_pages_missing_tracklist = tracklist_buttons_found - tracklists_found
    tracklist_required = bool(os.getenv("GLOBALDJMIX_USERNAME") and os.getenv("GLOBALDJMIX_PASSWORD"))
    tracklist_minimum = tracklist_buttons_found if tracklist_required else 0
    tracklist_test_passed = (
        not tracklist_required
        or (tracklist_buttons_found > 0 and tracklists_found == tracklist_buttons_found)
    )
    methods = {}
    for diag in diagnostics:
        if diag.get("audio_method"):
            methods[diag["audio_method"]] = methods.get(diag["audio_method"], 0) + 1

    passed = bool(selected) and audio_ok == len(selected) and image_ok == len(selected) and tracklist_test_passed
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": MODE, "archive_source": ARCHIVE,
        "archive_pages_reported": reported_pages,
        "archive_pages_failed": archive_errors,
        "selected_articles": len(selected),
        "articles_checked": len(diagnostics),
        "audio_resolved": audio_ok,
        "images_found": image_found,
        "images_verified": image_ok,
        "tracklists_found": tracklists_found,
        "tracklists_missing": tracklists_missing,
        "tracklist_buttons_found": tracklist_buttons_found,
        "tracklist_buttonless_pages": tracklist_buttonless_pages,
        "tracklist_button_pages_missing_tracklist": tracklist_button_pages_missing_tracklist,
        "tracklist_minimum_for_test": tracklist_minimum,
        "tracklist_test_passed": tracklist_test_passed if MODE == "test" else None,
        "audio_resolution_methods": methods,
        "test_passed": passed if MODE == "test" else None,
        "saved_episode_count": 0,
        "diagnostics": diagnostics,
    }

    if MODE == "test":
        # Do not publish a partial/empty feed. Keep current RSS/items until all 50 pass.
        if passed:
            items = sorted(new_items, key=sort_key, reverse=True)[:TEST_LIMIT]
            ITEMS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            RSS_FILE.write_text(rss_xml(items), encoding="utf-8")
            report["saved_episode_count"] = len(items)
        else:
            report["saved_episode_count"] = len(existing)
            print("TEST DID NOT PASS; keeping the previously published RSS/items unchanged.")
    else:
        merged = dict(existing_by_url)
        for item in new_items:
            merged[item["source_url"]] = item
        items = sorted(merged.values(), key=sort_key, reverse=True)
        ITEMS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        RSS_FILE.write_text(rss_xml(items), encoding="utf-8")
        report["saved_episode_count"] = len(items)

    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "SUMMARY: articles=", len(selected),
        "| audio=", str(audio_ok) + "/" + str(len(selected)),
        "| artwork found=", str(image_found) + "/" + str(len(selected)),
        "| artwork verified=", str(image_ok) + "/" + str(len(selected)),
        "| tracklists=", str(tracklists_found) + "/" + str(tracklist_buttons_found) + " buttons",
        "| pages without tracklist button=", tracklist_buttonless_pages,
        "| button pages missing tracklist=", tracklist_button_pages_missing_tracklist,
        "| methods=", json.dumps(methods),
        "| test passed=", passed,
        "| saved episodes=", report["saved_episode_count"],
    )
    # A zero-success test is still a useful diagnostic run; workflow commits report regardless.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
