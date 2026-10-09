#!/usr/bin/env python3
"""GlobalDJMix RSS builder: safe 50-item test first, full crawl only when enabled."""
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://globaldjmix.com"
ARCHIVE_URL = BASE + "/dj-songs-mp3-download"
DATA_FILE = Path("data/items.json")
REPORT_FILE = Path("data/test-report.json")
OUTPUT_FILE = Path("rss.xml")

MODE = os.environ.get("FEED_MODE", "test").strip().lower()
if MODE not in {"test", "incremental", "full"}:
    print("Unrecognized FEED_MODE; falling back to test.", file=sys.stderr)
    MODE = "test"

TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "45" if MODE == "test" else "60"))
MAX_TEST_ARTICLES = 50
TEST_WORKERS = 3
INCREMENTAL_WORKERS = 3
FULL_WORKERS = 4
MIN_REQUEST_INTERVAL = 0.30
NL = "\n"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})
_request_lock = Lock()
_next_request_at = 0.0
_http_fallback_noted = set()


def throttle():
    """Space requests out globally, even when the worker pool is concurrent."""
    global _next_request_at
    with _request_lock:
        delay = _next_request_at - monotonic()
        if delay > 0:
            time.sleep(delay)
        _next_request_at = monotonic() + MIN_REQUEST_INTERVAL


def with_scheme(url, scheme):
    p = urlparse(url)
    return urlunparse((scheme, p.netloc, p.path, p.params, p.query, p.fragment))


def normalise_url(url, base=None):
    if not url:
        return None
    url = html.unescape(str(url)).strip().strip("\"'")
    if url.startswith("//"):
        url = "https:" + url
    elif base:
        url = urljoin(base, url)
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return url


def request_follow_redirects(url, *, headers=None, stream=False, timeout=None, max_redirects=8):
    """
    Follow redirects explicitly. If box.globaldjmix.com presents an invalid TLS
    certificate, try plain HTTP for that exact host only; never disable TLS
    certificate validation.
    """
    current = normalise_url(url)
    if not current:
        raise ValueError("Invalid URL: " + str(url))
    request_headers = dict(headers or {})
    timeout = timeout or TIMEOUT
    seen = set()

    for hop in range(max_redirects + 1):
        if current in seen:
            raise requests.TooManyRedirects("Redirect loop while resolving " + str(url))
        seen.add(current)
        try:
            throttle()
            response = session.get(
                current,
                headers=request_headers,
                timeout=(10, timeout),
                allow_redirects=False,
                stream=stream,
            )
        except requests.exceptions.SSLError:
            p = urlparse(current)
            if p.hostname and p.hostname.lower() == "box.globaldjmix.com" and p.scheme == "https":
                fallback = with_scheme(current, "http")
                if fallback not in _http_fallback_noted:
                    print("TLS certificate mismatch for box.globaldjmix.com; trying its HTTP URL "
                          "without disabling certificate checks.", file=sys.stderr)
                    _http_fallback_noted.add(fallback)
                current = fallback
                continue
            raise

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                response.raise_for_status()
                return response
            next_url = normalise_url(location, current)
            response.close()
            if not next_url:
                raise requests.RequestException("Invalid redirect Location from " + current)
            next_parts = urlparse(next_url)
            # Do not follow a redirect back to the known-bad HTTPS certificate
            # on this exact host. This is an HTTP fallback, not verify=False.
            if (next_parts.hostname or "").lower() == "box.globaldjmix.com" and next_parts.scheme == "https":
                next_url = with_scheme(next_url, "http")
            if next_url == current or next_url in seen:
                raise requests.TooManyRedirects("Redirect loop while resolving " + str(url))
            current = next_url
            continue

        response.raise_for_status()
        return response

    raise requests.TooManyRedirects("Too many redirects while resolving " + str(url))


def fetch(url, attempts=None, timeout=None, referer=None):
    if attempts is None:
        attempts = 2 if MODE == "test" else 3
    headers = {"Referer": referer} if referer else None
    last_error = None
    for attempt in range(attempts):
        try:
            return request_follow_redirects(url, headers=headers, timeout=timeout or TIMEOUT)
        except requests.exceptions.SSLError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise last_error


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


BLOCKED_SLUGS = {
    "", "rss", "livedjsets", "topic", "best-mixes-by-month", "livesets",
    "podcasts", "news", "dj-songs-mp3-download", "dj-songs-download",
    "tomorrowland", "abgt", "asot", "new-dj-mixes", "djs-list", "dj-shows",
    "share-mix-web-article", "search", "contact", "about", "top-aug-2026",
}


def article_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in {"http", "https"}:
            return False
        if (p.hostname or "").lower() not in {"globaldjmix.com", "www.globaldjmix.com"}:
            return False
        path = p.path.strip("/").lower()
        if path in BLOCKED_SLUGS or "/" in path or len(path) < 16:
            return False
        if path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".xml", ".css", ".js")):
            return False
        return True
    except Exception:
        return False


def extract_page_urls(body):
    # The archive list is before this page's separate popular-mixes section.
    marker = re.search(r"Most popular DJ Mixes", body, flags=re.I)
    main_body = body[:marker.start()] if marker else body
    soup = BeautifulSoup(main_body, "html.parser")
    found = []
    for tag in soup.find_all("a", href=True):
        label = clean(tag.get_text(" ", strip=True)).lower()
        if not label or label in {"download", "box.download", "more", "here"}:
            continue
        href = html.unescape(tag.get("href", "")).strip()
        url = normalise_url(href, BASE)
        if not url:
            continue
        p = urlparse(url)
        # Canonicalise article links without tracking parameters.
        url = urlunparse((p.scheme, p.netloc, p.path, "", "", ""))
        if article_url(url):
            found.append(url)
    return list(dict.fromkeys(found))


def archive_page_total(body):
    match = re.search(r"Page\s+1\s+of\s+(\d+)", body, flags=re.I)
    return int(match.group(1)) if match else 1387


def archive_page_url(number):
    return ARCHIVE_URL if number == 1 else ARCHIVE_URL + "?p=" + str(number)


def discover_urls(mode):
    first = fetch(ARCHIVE_URL)
    first_urls = extract_page_urls(first.text)
    total_pages = archive_page_total(first.text)
    print("Archive source: " + ARCHIVE_URL)
    print("Archive page count reported/fallback: " + str(total_pages))
    print("Archive page 1 produced " + str(len(first_urls)) + " unique post URLs.")

    if mode == "incremental":
        return first_urls, total_pages

    if mode == "test":
        urls = list(first_urls)
        page_number = 2
        # Only browse as many recent archive pages as are needed for a 50-post test.
        while len(urls) < MAX_TEST_ARTICLES and page_number <= min(total_pages, 12):
            response = fetch(archive_page_url(page_number))
            additions = extract_page_urls(response.text)
            urls.extend(additions)
            urls = list(dict.fromkeys(urls))
            print("Test discovery page " + str(page_number) + ": "
                  + str(len(additions)) + " new candidates; "
                  + str(min(len(urls), MAX_TEST_ARTICLES)) + "/" + str(MAX_TEST_ARTICLES))
            page_number += 1
        return urls[:MAX_TEST_ARTICLES], total_pages

    # Full mode exists for a later, explicitly approved crawl; this test rollout
    # does not invoke it. It uses low concurrency and retries missed page numbers.
    print("FULL BACKFILL: low-rate scan of " + str(total_pages) + " archive pages.")
    urls_by_page = {1: first_urls}
    failures = []

    def fetch_page(number):
        try:
            response = fetch(archive_page_url(number), attempts=3, timeout=60)
            return number, extract_page_urls(response.text), None
        except Exception as exc:
            return number, [], str(exc)

    with ThreadPoolExecutor(max_workers=FULL_WORKERS) as pool:
        futures = [pool.submit(fetch_page, n) for n in range(2, total_pages + 1)]
        for future in as_completed(futures):
            number, urls, error = future.result()
            urls_by_page[number] = urls
            if error:
                failures.append((number, error))
            if number % 100 == 0:
                print("Fetched archive page " + str(number) + "/" + str(total_pages))

    # Retry failed pages once after the first sweep.
    if failures:
        print("Retrying " + str(len(failures)) + " failed archive pages.")
        failed_numbers = [n for n, _ in failures]
        failures = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(fetch_page, n): n for n in failed_numbers}
            for future in as_completed(futures):
                n, urls, error = future.result()
                if urls:
                    urls_by_page[n] = urls
                if error:
                    failures.append((n, error))

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    Path("data/failed-archive-pages.json").write_text(
        json.dumps([{"page": n, "error": e} for n, e in sorted(failures)], ensure_ascii=False, indent=2) + NL,
        encoding="utf-8",
    )
    urls = []
    for number in range(1, total_pages + 1):
        urls.extend(urls_by_page.get(number, []))
    unique = list(dict.fromkeys(urls))
    print("Full discovery found " + str(len(unique)) + " unique post URLs; "
          + str(len(failures)) + " archive pages still failed after retry.")
    return unique, total_pages


def parse_date(text, label):
    month = r"(?:[A-Za-z]{3,9}|\d{1,2})"
    match = re.search(
        re.escape(label) + r"\s*:\s*(\d{1,2}[-/]" + month + r"[-/]\d{4})",
        text,
        re.I,
    )
    if not match:
        return None
    value = match.group(1)
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d/%b/%Y", "%d/%B/%Y"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_size(text):
    match = re.search(r"(?:FileSize|File Size|Size)\s*:\s*([0-9.,]+)\s*(MB|GB)", text, re.I)
    if not match:
        return 0
    try:
        value = float(match.group(1).replace(",", "."))
        return int(value * (1024 ** 2 if match.group(2).upper() == "MB" else 1024 ** 3))
    except ValueError:
        return 0


def find_image(soup, page_url):
    """Use only an image embedded/referenced by the episode's own page."""
    meta_selectors = [
        ("meta", {"property": "og:image"}, "content"),
        ("meta", {"property": "og:image:url"}, "content"),
        ("meta", {"name": "twitter:image"}, "content"),
        ("meta", {"name": "twitter:image:src"}, "content"),
        ("link", {"rel": "image_src"}, "href"),
    ]
    for tag_name, attrs, value_attr in meta_selectors:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get(value_attr):
            image_url = normalise_url(tag.get(value_attr), page_url)
            if image_url:
                return image_url

    # JSON-LD often stores the same official episode thumbnail.
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop(0)
            if isinstance(obj, dict):
                image = obj.get("image") or obj.get("thumbnailUrl")
                if isinstance(image, str):
                    image_url = normalise_url(image, page_url)
                    if image_url:
                        return image_url
                if isinstance(image, dict) and image.get("url"):
                    image_url = normalise_url(image["url"], page_url)
                    if image_url:
                        return image_url
                if isinstance(image, list):
                    stack.extend(image)
                stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
            elif isinstance(obj, list):
                stack.extend(obj)

    containers = []
    for selector in ("article", "main", ".post", ".entry-content", ".content", "#content"):
        try:
            containers.extend(soup.select(selector))
        except Exception:
            pass
    containers.append(soup)
    seen = set()
    for container in containers:
        for img in container.find_all("img", src=True):
            src = html.unescape(img.get("src", "")).strip()
            alt = clean(img.get("alt", "")).lower()
            classes = " ".join(img.get("class", [])).lower()
            combined = (src + " " + alt + " " + classes).lower()
            if any(word in combined for word in (
                "logo", "avatar", "favicon", "sprite", "emoji", "icon", "banner", "advert"
            )):
                continue
            image_url = normalise_url(src, page_url)
            if image_url and image_url not in seen:
                seen.add(image_url)
                return image_url
    return None


def download_candidates(soup, base_url, raw_source=""):
    candidates = []

    def add(value):
        candidate = normalise_url(value, base_url)
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for tag in soup.find_all("a", href=True):
        href = html.unescape(tag.get("href", "")).strip()
        if not href:
            continue
        label = clean(tag.get_text(" ", strip=True)).lower()
        title = clean(tag.get("title", "")).lower()
        aria = clean(tag.get("aria-label", "")).lower()
        classes = " ".join(tag.get("class", [])).lower()
        absolute = urljoin(base_url, href)
        host = (urlparse(absolute).hostname or "").lower()
        signal = (
            "download" in label or "download" in title or "download" in aria
            or "download" in classes or tag.has_attr("download")
            or ".mp3" in href.lower()
            or host == "box.globaldjmix.com"
            or host == "box.download"
        )
        if signal:
            add(href)
        for attr in ("data-href", "data-url", "data-download", "data-download-url"):
            value = tag.get(attr)
            if value and ("download" in attr or ".mp3" in str(value).lower()
                          or "box." in str(value).lower()):
                add(value)

    for tag in soup.find_all(["form", "audio", "source", "button"]):
        value = tag.get("action") or tag.get("src") or tag.get("data-href") or tag.get("data-url")
        label = clean(tag.get_text(" ", strip=True)).lower()
        onclick = tag.get("onclick", "")
        if value and ("download" in str(value).lower() or ".mp3" in str(value).lower()
                      or "box." in str(value).lower() or "download" in label):
            add(value)
        if onclick:
            for match in re.findall(r"""['"]((?:https?:)?//[^'"]+|/[^'"]+)['"]""", onclick):
                if "download" in match.lower() or ".mp3" in match.lower() or "box." in match.lower():
                    add(match)

    patterns = (
        r'''https?://[^"'<>\\s]+\\.mp3(?:\\?[^"'<>\\s]*)?''',
        r'''https?:\\/\\/[^"'<>\\s]+\\.mp3(?:\\?[^"'<>\\s]*)?''',
        r'''https?://box\\.globaldjmix\\.com/[^"'<>\\s]+''',
        r'''https?://box\\.download/[^"'<>\\s]+''',
    )
    for pattern in patterns:
        for match in re.findall(pattern, raw_source or "", flags=re.I):
            add(match.replace("\\/", "/").rstrip(".,);]"))
    return candidates


def probe_url(url, referer=None):
    headers = {"Range": "bytes=0-511"}
    if referer:
        headers["Referer"] = referer
    response = None
    try:
        response = request_follow_redirects(
            url, headers=headers, stream=True, timeout=TIMEOUT
        )
        final_url = response.url
        content_type = (response.headers.get("content-type") or "").lower()
        disposition = (response.headers.get("content-disposition") or "").lower()
        prefix = b""
        try:
            prefix = response.raw.read(512, decode_content=True).lstrip().lower()
        except Exception:
            pass
        html_payload = (
            "text/html" in content_type
            or "application/xhtml" in content_type
            or prefix.startswith(b"<!doctype html")
            or prefix.startswith(b"<html")
        )
        if html_payload:
            return "html", final_url, None
        filename_mp3 = ".mp3" in disposition
        binary_types = (
            "application/octet-stream", "application/x-download",
            "binary/octet-stream", "application/download", "application/force-download",
        )
        audio = (
            content_type.startswith("audio/")
            or any(item in content_type for item in binary_types)
            or filename_mp3
            or (".mp3" in final_url.lower() and not html_payload)
        )
        if audio:
            return "audio", final_url, None
        return "other", final_url, "Response was not identified as audio (Content-Type="
               + (content_type or "missing") + ")"
    except Exception as exc:
        return "error", None, str(exc)
    finally:
        if response is not None:
            response.close()


def resolve_candidate(url, article_url, depth=0, visited=None):
    if visited is None:
        visited = set()
    url = normalise_url(url)
    if not url or url in visited:
        return None, None, "invalid or repeated candidate URL"
    visited.add(url)

    kind, final_url, error = probe_url(url, referer=article_url)
    if kind == "audio":
        return final_url, ("direct" if depth == 0 else "intermediate"), None
    if kind != "html" or depth >= 2:
        return None, None, error or "URL did not return playable audio or a download page"

    try:
        page = fetch(final_url, attempts=1, referer=article_url)
    except Exception as exc:
        return None, None, "could not open download page: " + str(exc)

    soup = BeautifulSoup(page.text, "html.parser")
    candidates = download_candidates(soup, page.url, page.text)
    # Actual file links are often labelled Download; try obvious files first.
    candidates.sort(key=lambda candidate: (
        0 if ".mp3" in candidate.lower() else 1,
        0 if "download" in candidate.lower() else 1,
    ))
    if not candidates:
        return None, None, "intermediate page exposed no download link"
    errors = []
    for child in candidates:
        if child in visited:
            continue
        audio_url, method, child_error = resolve_candidate(
            child, article_url=page.url, depth=depth + 1, visited=visited
        )
        if audio_url:
            return audio_url, "intermediate", None
        if child_error:
            errors.append(child_error)
    return None, None, "inner Download links did not resolve to audio: " + "; ".join(errors[:3])


def resolve_audio(soup, raw_source, article_page_url):
    candidates = download_candidates(soup, article_page_url, raw_source)
    candidates.sort(key=lambda candidate: (
        0 if ".mp3" in candidate.lower() else 1,
        0 if "download" in candidate.lower() else 1,
        0 if "box." in (urlparse(candidate).hostname or "").lower() else 1,
    ))
    if not candidates:
        return None, None, "No download candidates found on the episode page."

    errors = []
    for candidate in candidates:
        audio_url, method, error = resolve_candidate(candidate, article_url=article_page_url)
        if audio_url:
            return audio_url, method, None
        if error:
            errors.append(candidate + " => " + error)
    return None, None, "No candidate resolved to audio. " + " | ".join(errors[:4])


def parse_episode(url):
    diagnostic = {
        "url": url, "title": "", "audio_ok": False, "audio_method": None,
        "image_found": False, "image_http_ok": False, "image_url": None, "error": None,
    }
    try:
        response = fetch(url)
    except Exception as exc:
        diagnostic["error"] = "Article fetch failed: " + str(exc)
        return None, diagnostic

    soup = BeautifulSoup(response.text, "html.parser")
    plain_text = clean(soup.get_text(" ", strip=True))
    heading = soup.find("h1")
    title = clean(heading.get_text(" ", strip=True)) if heading else url
    diagnostic["title"] = title

    image_url = find_image(soup, response.url)
    diagnostic["image_url"] = image_url
    diagnostic["image_found"] = bool(image_url)

    audio_url, method, audio_error = resolve_audio(soup, response.text, response.url)
    if not audio_url:
        diagnostic["error"] = audio_error or "No audio URL resolved."
        return None, diagnostic

    diagnostic["audio_ok"] = True
    diagnostic["audio_method"] = method

    filesize = parse_size(plain_text)
    duration_match = re.search(
        r"Duration\s*:\s*(.+?)(?=\s*(?:Audio Bitrate|Bitrate|FileSize|File Size|Post Date|Rec Date|Genre)\s*:|$)",
        plain_text, re.I
    )
    bitrate_match = re.search(
        r"(?:Audio Bitrate|Bitrate)\s*:\s*(.+?)(?=\s*(?:FileSize|File Size|Post Date|Rec Date|Genre)\s*:|$)",
        plain_text, re.I
    )
    genre_match = re.search(r"Genre\s*:\s*(.+?)(?=\s*Duration\s*:|$)", plain_text, re.I)
    post_date = parse_date(plain_text, "Post Date")
    rec_date = parse_date(plain_text, "Rec Date")
    pub_date = post_date or rec_date or datetime.now(timezone.utc)

    return {
        "guid": url,
        "title": title,
        "link": url,
        "enclosure": audio_url,
        "image": image_url,
        "pubDate": pub_date.isoformat(),
        "recDate": rec_date.isoformat() if rec_date else None,
        "genre": clean(genre_match.group(1)) if genre_match else "DJ Mix",
        "duration": clean(duration_match.group(1)) if duration_match else "",
        "bitrate": clean(bitrate_match.group(1)) if bitrate_match else "",
        "filesize": filesize,
    }, diagnostic


def verify_image_url(url, referer):
    if not url:
        return None, "no image URL found"
    response = None
    try:
        headers = {"Range": "bytes=0-255", "Referer": referer}
        response = request_follow_redirects(url, headers=headers, stream=True, timeout=TIMEOUT)
        final_url = response.url
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type.startswith("image/"):
            return final_url, None
        # Some hosts serve image files as application/octet-stream.
        suffix = urlparse(final_url).path.lower()
        if any(suffix.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif")) \
                and ("octet-stream" in content_type or not content_type):
            return final_url, None
        return None, "image URL returned non-image Content-Type: " + (content_type or "missing")
    except Exception as exc:
        return None, str(exc)
    finally:
        if response is not None:
            response.close()


def load_items():
    if not DATA_FILE.exists():
        return {}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, list):
        return {
            item["guid"]: item for item in raw
            if isinstance(item, dict) and item.get("guid")
        }
    return raw if isinstance(raw, dict) else {}


def sort_key(item):
    try:
        return datetime.fromisoformat(item["pubDate"])
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def esc(value, quote=False):
    return html.escape(str(value or ""), quote=quote)


def cdata(value):
    return "<![CDATA[" + str(value or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_rss(items):
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        "  <channel>",
        "    <title>GlobalDJMix - DJ Mixes &amp; Live Sets</title>",
        "    <link>" + BASE + "/dj-songs-mp3-download</link>",
        "    <description>GlobalDJMix DJ mixes, with direct audio enclosures and episode artwork.</description>",
        "    <language>en</language>",
        "    <itunes:author>GlobalDJMix</itunes:author>",
        "    <itunes:explicit>no</itunes:explicit>",
        '    <itunes:type>episodic</itunes:type>',
        '    <itunes:category text="Music" />',
        "    <lastBuildDate>" + format_datetime(datetime.now(timezone.utc)) + "</lastBuildDate>",
    ]
    for item in items:
        try:
            dt = datetime.fromisoformat(item["pubDate"])
        except Exception:
            dt = datetime.now(timezone.utc)
        description = (
            "Source: " + item["link"] + NL
            + "Genre: " + item.get("genre", "") + NL
            + "Duration: " + item.get("duration", "") + NL
            + "Audio: " + item.get("bitrate", "")
        )
        if item.get("filesize"):
            description += NL + "File size: " + f'{item["filesize"] / (1024 ** 2):.2f} MB'
        lines.extend([
            "    <item>",
            "      <title>" + esc(item["title"]) + "</title>",
            '      <guid isPermaLink="true">' + esc(item["guid"]) + "</guid>",
            "      <link>" + esc(item["link"]) + "</link>",
            "      <pubDate>" + format_datetime(dt) + "</pubDate>",
            "      <description>" + cdata(description) + "</description>",
            "      <category>" + esc(item.get("genre") or "DJ Mix") + "</category>",
        ])
        if item.get("image"):
            lines.append('      <itunes:image href="' + esc(item["image"], quote=True) + '" />')
        lines.extend([
            '      <enclosure url="' + esc(item["enclosure"], quote=True)
            + '" length="' + str(int(item.get("filesize") or 0))
            + '" type="audio/mpeg" />',
            "      <itunes:episodeType>full</itunes:episodeType>",
        ])
        if item.get("duration"):
            lines.append("      <itunes:duration>" + esc(item["duration"]) + "</itunes:duration>")
        lines.append("    </item>")
    lines.extend(["  </channel>", "</rss>"])
    return NL.join(lines) + NL


def write_report(total_candidates, diagnostics, items, total_pages):
    audio_ok = [d for d in diagnostics if d.get("audio_ok")]
    image_found = [d for d in diagnostics if d.get("image_found")]
    image_ok = [d for d in diagnostics if d.get("image_http_ok")]
    method_counts = {}
    for d in audio_ok:
        method = d.get("audio_method") or "unknown"
        method_counts[method] = method_counts.get(method, 0) + 1
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": MODE,
        "archive_source": ARCHIVE_URL,
        "archive_pages_reported": total_pages,
        "source_articles_attempted": total_candidates,
        "audio_resolved": len(audio_ok),
        "images_found_on_episode_pages": len(image_found),
        "image_urls_http_verified": len(image_ok),
        "audio_resolution_methods": method_counts,
        "rss_episode_count": len(items),
        "full_archive_started": MODE == "full",
        "diagnostics": diagnostics,
    }
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + NL, encoding="utf-8")
    print(
        "TEST SUMMARY: attempted=" + str(total_candidates)
        + ", audio_resolved=" + str(len(audio_ok))
        + ", images_found=" + str(len(image_found))
        + ", image_urls_verified=" + str(len(image_ok))
        + ", methods=" + json.dumps(method_counts)
        + ", rss_items=" + str(len(items))
    )


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = load_items()
    print("FEED_MODE: " + MODE)
    print("Archive source fixed to: " + ARCHIVE_URL)

    try:
        urls, total_pages = discover_urls(MODE)
    except Exception as exc:
        print("Archive discovery failed: " + str(exc), file=sys.stderr)
        diagnostics = [{"url": ARCHIVE_URL, "error": "Archive discovery failed: " + str(exc)}]
        write_report(0, diagnostics, [], 0)
        DATA_FILE.write_text("[]\n", encoding="utf-8")
        OUTPUT_FILE.write_text(build_rss([]), encoding="utf-8")
        return 0

    if MODE == "test":
        known = {}
        pending = list(enumerate(urls, 1))
    else:
        known = existing
        if MODE == "incremental":
            pending = [(i, url) for i, url in enumerate(urls, 1) if url not in known]
        else:
            pending = [(i, url) for i, url in enumerate(urls, 1) if url not in known]

    print("Article URLs selected: " + str(len(urls)))
    print("Article pages to inspect: " + str(len(pending)))

    diagnostics = []
    workers = TEST_WORKERS if MODE == "test" else (
        INCREMENTAL_WORKERS if MODE == "incremental" else FULL_WORKERS
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(parse_episode, url): (index, url)
            for index, url in pending
        }
        finished = 0
        for future in as_completed(future_map):
            index, url = future_map[future]
            try:
                episode, diagnostic = future.result()
            except Exception as exc:
                episode = None
                diagnostic = {
                    "url": url, "title": "", "audio_ok": False, "audio_method": None,
                    "image_found": False, "image_http_ok": False, "image_url": None,
                    "error": "Unhandled parser error: " + str(exc),
                }

            if episode:
                verified_image, image_error = verify_image_url(
                    episode.get("image"), episode["link"]
                )
                if verified_image:
                    episode["image"] = verified_image
                    diagnostic["image_url"] = verified_image
                    diagnostic["image_http_ok"] = True
                else:
                    diagnostic["image_http_ok"] = False
                    if diagnostic.get("image_found"):
                        diagnostic["image_error"] = image_error
                known[url] = episode

            diagnostics.append(diagnostic)
            finished += 1
            if finished % 10 == 0 or finished == len(pending):
                print("Article progress: " + str(finished) + "/" + str(len(pending))
                      + "; valid RSS items so far: " + str(len(known)))

    ordered = sorted(known.values(), key=sort_key, reverse=True)
    if MODE == "test":
        ordered = ordered[:MAX_TEST_ARTICLES]

    # Always generate a readable RSS plus a diagnostic report, even when no
    # audio link resolves, so failures do not silently masquerade as success.
    DATA_FILE.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + NL, encoding="utf-8")
    OUTPUT_FILE.write_text(build_rss(ordered), encoding="utf-8")
    write_report(len(urls), diagnostics, ordered, total_pages)
    print("Wrote rss.xml with " + str(len(ordered)) + " podcast episodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
