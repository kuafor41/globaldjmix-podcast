#!/usr/bin/env python3
"""GlobalDJMix podcast feed. Push/manual runs test 50 episodes; full crawl is opt-in."""
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
from threading import Lock, local
from time import monotonic
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BASE = "https://globaldjmix.com"
ARCHIVE = BASE + "/dj-songs-mp3-download"
DATA_FILE = Path("data/items.json")
REPORT_FILE = Path("data/test-report.json")
OUTPUT_FILE = Path("rss.xml")

MODE = os.environ.get("FEED_MODE", "test").lower().strip()
if MODE not in {"test", "incremental", "full"}:
    MODE = "test"
TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20" if MODE == "test" else "60"))
TEST_LIMIT = 50
WORKERS = 2 if MODE == "test" else (3 if MODE == "incremental" else 4)
REQUEST_GAP = 0.20
NL = "\n"

_thread_state = local()
_rate_lock = Lock()
_next_request = 0.0
_logged_tls_fallback = False


def get_session():
    if not hasattr(_thread_state, "session"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; GlobalDJMixPodcastRSS/2.0)",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        _thread_state.session = s
    return _thread_state.session


def throttle():
    global _next_request
    with _rate_lock:
        delay = _next_request - monotonic()
        if delay > 0:
            time.sleep(delay)
        _next_request = monotonic() + REQUEST_GAP


def absolute_url(value, base=None):
    if not value:
        return None
    value = html.unescape(str(value)).strip().strip("\"'")
    if value.startswith("//"):
        value = "https:" + value
    if base:
        value = urljoin(base, value)
    p = urlparse(value)
    if p.scheme not in {"http", "https"} or not p.netloc:
        return None
    return value


def request_redirect_safe(url, headers=None, timeout=None, stream=False, max_redirects=6):
    """Follow redirects manually. Never disable certificate verification."""
    global _logged_tls_fallback
    current = absolute_url(url)
    if not current:
        raise requests.RequestException("Invalid URL: " + str(url))
    seen = set()
    sess = get_session()
    for _ in range(max_redirects + 1):
        if current in seen:
            raise requests.TooManyRedirects("Redirect loop: " + current)
        seen.add(current)
        throttle()
        try:
            response = sess.get(
                current,
                headers=headers or {},
                timeout=(8, timeout or TIMEOUT),
                allow_redirects=False,
                stream=stream,
            )
        except requests.exceptions.SSLError:
            p = urlparse(current)
            if (p.scheme == "https" and (p.hostname or "").lower() == "box.globaldjmix.com"):
                if not _logged_tls_fallback:
                    print("box.globaldjmix.com TLS name mismatch; trying HTTP for this host only.",
                          file=sys.stderr)
                    _logged_tls_fallback = True
                current = urlunparse(("http", p.netloc, p.path, p.params, p.query, p.fragment))
                continue
            raise

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location:
                response.raise_for_status()
                return response
            target = absolute_url(location, current)
            response.close()
            if not target:
                raise requests.RequestException("Invalid redirect from " + current)
            p = urlparse(target)
            if (p.hostname or "").lower() == "box.globaldjmix.com" and p.scheme == "https":
                target = urlunparse(("http", p.netloc, p.path, p.params, p.query, p.fragment))
            if target in seen or target == current:
                raise requests.TooManyRedirects("Redirect loop: " + current)
            current = target
            continue
        response.raise_for_status()
        return response
    raise requests.TooManyRedirects("Too many redirects for " + str(url))


def fetch_page(url, referer=None, attempts=2):
    last_error = None
    headers = {"Referer": referer} if referer else {}
    for attempt in range(attempts):
        try:
            return request_redirect_safe(url, headers=headers)
        except requests.exceptions.SSLError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    raise last_error


BLOCKED = {
    "", "rss", "livedjsets", "topic", "best-mixes-by-month", "livesets",
    "podcasts", "news", "dj-songs-mp3-download", "dj-songs-download",
    "tomorrowland", "abgt", "asot", "new-dj-mixes", "djs-list", "dj-shows",
    "share-mix-web-article", "search", "contact", "about", "podcast", "mixes",
}


def is_article_url(url):
    try:
        p = urlparse(url)
        slug = p.path.strip("/").lower()
        return (
            p.scheme in {"http", "https"}
            and (p.hostname or "").lower() in {"globaldjmix.com", "www.globaldjmix.com"}
            and "/" not in slug and slug not in BLOCKED and len(slug) >= 16
            and not slug.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".css", ".js", ".xml"))
        )
    except Exception:
        return False


def unique(values):
    return list(dict.fromkeys(values))


def extract_post_urls(page_html):
    marker = re.search(r"Most popular DJ Mixes", page_html, re.I)
    main_html = page_html[:marker.start()] if marker else page_html
    soup = BeautifulSoup(main_html, "html.parser")
    found = []

    # Archive titles are the headings; this keeps menus, genre links and footer links out.
    for a in soup.select("h2 a[href], h3 a[href], h4 a[href], .post-title a[href], .entry-title a[href]"):
        href = absolute_url(a.get("href"), BASE)
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if href and title and is_article_url(href):
            p = urlparse(href)
            found.append(urlunparse((p.scheme, p.netloc, p.path, "", "", "")))

    found = unique(found)
    if len(found) >= 10:
        return found

    # Fallback only if heading markup differs: require a nearby post-metadata pattern.
    for a in soup.find_all("a", href=True):
        href = absolute_url(a.get("href"), BASE)
        title = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()
        if not href or len(title) < 10 or not is_article_url(href):
            continue
        parent = a
        context = ""
        for _ in range(4):
            if parent.parent is None:
                break
            parent = parent.parent
            context = parent.get_text(" ", strip=True)
            if re.search(r"Bitrate\s*:|Duration\s*:|File\s*Size\s*:|Genre\s*:", context, re.I):
                break
        if re.search(r"Bitrate\s*:|Duration\s*:|File\s*Size\s*:|Genre\s*:", context, re.I):
            p = urlparse(href)
            found.append(urlunparse((p.scheme, p.netloc, p.path, "", "", "")))
    return unique(found)


def get_archive_total(body):
    m = re.search(r"Page\s+1\s+of\s+(\d+)", body, re.I)
    return int(m.group(1)) if m else 1387


def discover(mode):
    first = fetch_page(ARCHIVE)
    first_urls = extract_post_urls(first.text)
    total = get_archive_total(first.text)
    print("Archive: " + ARCHIVE)
    print("Archive reports " + str(total) + " pages; first page yielded " + str(len(first_urls)) + " posts.")

    if mode == "incremental":
        return first_urls, total
    if mode == "test":
        posts = list(first_urls)
        # A test may read at most three archive pages; it never starts the full crawl.
        for page_no in (2, 3):
            if len(posts) >= TEST_LIMIT:
                break
            page = fetch_page(ARCHIVE + "?p=" + str(page_no))
            added = extract_post_urls(page.text)
            posts = unique(posts + added)
            print("Test discovery page " + str(page_no) + ": " + str(len(posts)) + "/" + str(TEST_LIMIT) + " posts.")
        return posts[:TEST_LIMIT], total

    # Full mode is intentionally separate and only selected explicitly in Actions.
    by_page = {1: first_urls}
    failed = []

    def load_archive_page(n):
        try:
            r = fetch_page(ARCHIVE + "?p=" + str(n), attempts=3)
            return n, extract_post_urls(r.text), None
        except Exception as exc:
            return n, [], str(exc)

    print("Full crawl selected: 4 workers, 60-second timeout, failed pages retried once.")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(load_archive_page, n) for n in range(2, total + 1)]
        for i, future in enumerate(as_completed(futures), 1):
            n, urls, error = future.result()
            by_page[n] = urls
            if error:
                failed.append(n)
            if i % 100 == 0:
                print("Archive pages completed: " + str(i) + "/" + str(total - 1))

    if failed:
        print("Retrying " + str(len(failed)) + " archive pages once.")
        retry_failed = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(load_archive_page, n): n for n in failed}
            for future in as_completed(futures):
                n, urls, error = future.result()
                if not error:
                    by_page[n] = urls
                else:
                    retry_failed.append({"page": n, "error": error})
        Path("data/failed-archive-pages.json").write_text(
            json.dumps(retry_failed, ensure_ascii=False, indent=2) + NL, encoding="utf-8"
        )

    collected = []
    for n in range(1, total + 1):
        collected.extend(by_page.get(n, []))
    return unique(collected), total


def find_image(soup, page_url):
    for tag_name, attrs, attr in (
        ("meta", {"property": "og:image"}, "content"),
        ("meta", {"property": "og:image:url"}, "content"),
        ("meta", {"name": "twitter:image"}, "content"),
        ("meta", {"name": "twitter:image:src"}, "content"),
        ("link", {"rel": "image_src"}, "href"),
    ):
        tag = soup.find(tag_name, attrs=attrs)
        url = absolute_url(tag.get(attr), page_url) if tag and tag.get(attr) else None
        if url:
            return url

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text())
        except Exception:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                image = item.get("image") or item.get("thumbnailUrl")
                if isinstance(image, str):
                    url = absolute_url(image, page_url)
                    if url:
                        return url
                if isinstance(image, dict) and image.get("url"):
                    url = absolute_url(image["url"], page_url)
                    if url:
                        return url
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))
            elif isinstance(item, list):
                stack.extend(item)

    for container in soup.select("article, main, .entry-content, .post, .content"):
        for img in container.find_all("img", src=True):
            src = img.get("src", "")
            details = (src + " " + img.get("alt", "") + " " + " ".join(img.get("class", []))).lower()
            if any(skip in details for skip in ("logo", "avatar", "favicon", "sprite", "emoji", "icon", "banner", "advert")):
                continue
            url = absolute_url(src, page_url)
            if url:
                return url
    return None


def article_download_candidates(soup, base_url, include_raw=False, raw=""):
    candidates = []
    def add(value):
        value = absolute_url(value, base_url)
        if value and value not in candidates:
            candidates.append(value)

    # Prioritise the actual visible Download/box.download buttons rather than every page link.
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip().lower()
        title = str(a.get("title", "")).lower()
        aria = str(a.get("aria-label", "")).lower()
        classes = " ".join(a.get("class", [])).lower()
        host = (urlparse(urljoin(base_url, href)).hostname or "").lower()
        is_download = (
            "download" in text or "box.download" in text or "download" in title
            or "download" in aria or "download" in classes or a.has_attr("download")
            or ".mp3" in href.lower() or host in {"box.download", "box.globaldjmix.com"}
        )
        if is_download:
            add(href)
            if len(candidates) >= (5 if include_raw else 3):
                break

    if include_raw and raw:
        for pattern in (
            r'''https?://[^"'<> \s]+\.mp3(?:\?[^"'<> \s]*)?''',
            r'''https?:\\/\\/[^"'<> \s]+\.mp3(?:\?[^"'<> \s]*)?''',
        ):
            for match in re.findall(pattern, raw, re.I):
                add(match.replace("\\/", "/"))
                if len(candidates) >= 5:
                    break
    return candidates[:(5 if include_raw else 3)]


def probe(url, referer):
    """Return (audio/html/other/error, final_url, html_text, error)."""
    response = None
    try:
        headers = {"Range": "bytes=0-511", "Referer": referer}
        response = request_redirect_safe(url, headers=headers, stream=True, timeout=TIMEOUT)
        final_url = response.url
        content_type = (response.headers.get("content-type") or "").lower()
        disposition = (response.headers.get("content-disposition") or "").lower()

        # If response looks like HTML, read the small interstitial page to locate its Download button.
        if "text/html" in content_type or "xhtml" in content_type:
            page_text = response.text
            return "html", final_url, page_text, None

        prefix = b""
        try:
            prefix = response.raw.read(512, decode_content=True).lstrip().lower()
        except Exception:
            pass
        if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
            return "html", final_url, prefix.decode("utf-8", "ignore"), None

        audio_like = (
            content_type.startswith("audio/")
            or any(t in content_type for t in (
                "application/octet-stream", "application/x-download", "application/download",
                "application/force-download", "binary/octet-stream",
            ))
            or ".mp3" in disposition
            or ".mp3" in final_url.lower()
        )
        if audio_like:
            return "audio", final_url, None, None
        return "other", final_url, None, "non-audio response (" + (content_type or "unknown content-type") + ")"
    except Exception as exc:
        return "error", None, None, str(exc)
    finally:
        if response is not None:
            response.close()


def resolve_audio(article_soup, article_html, article_url):
    candidates = article_download_candidates(article_soup, article_url, include_raw=True, raw=article_html)
    errors = []
    for candidate in candidates[:5]:
        kind, final_url, body, error = probe(candidate, article_url)
        if kind == "audio":
            return final_url, "direct", None
        if kind == "html" and body:
            intermediate = BeautifulSoup(body, "html.parser")
            inner_candidates = article_download_candidates(
                intermediate, final_url, include_raw=True, raw=body
            )
            # The inner page's explicit Download button is first priority.
            for inner in inner_candidates[:3]:
                inner_kind, inner_final, _, inner_error = probe(inner, final_url)
                if inner_kind == "audio":
                    return inner_final, "intermediate", None
                if inner_error:
                    errors.append(inner_error)
        if error:
            errors.append(error)
    return None, None, "No candidate resolved to audio. " + "; ".join(errors[:3])


def verify_image(url, referer):
    if not url:
        return False
    response = None
    try:
        response = request_redirect_safe(
            url, headers={"Range": "bytes=0-255", "Referer": referer},
            stream=True, timeout=min(TIMEOUT, 15)
        )
        ctype = (response.headers.get("content-type") or "").lower()
        suffix = urlparse(response.url).path.lower()
        return ctype.startswith("image/") or (
            any(suffix.endswith(x) for x in (".jpg", ".jpeg", ".png", ".webp", ".gif"))
            and ("octet-stream" in ctype or not ctype)
        )
    except Exception:
        return False
    finally:
        if response is not None:
            response.close()


def parse_date(text, label):
    m = re.search(re.escape(label) + r"\s*:\s*(\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{4}|\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    if not m:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def parse_episode(url):
    report = {
        "url": url, "title": "", "audio_ok": False, "audio_method": None,
        "image_found": False, "image_http_ok": False, "image_url": None, "error": None,
    }
    try:
        response = fetch_page(url)
        soup = BeautifulSoup(response.text, "html.parser")
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()
        h = soup.find("h1")
        title = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip() if h else url
        report["title"] = title

        image_url = find_image(soup, response.url)
        report["image_found"] = bool(image_url)
        report["image_url"] = image_url
        audio_url, method, error = resolve_audio(soup, response.text, response.url)
        if not audio_url:
            report["error"] = error
            return None, report

        report["audio_ok"] = True
        report["audio_method"] = method
        report["image_http_ok"] = verify_image(image_url, response.url)
        post_date = parse_date(text, "Post Date")
        rec_date = parse_date(text, "Rec Date")
        pub_date = post_date or rec_date or datetime.now(timezone.utc)

        size_match = re.search(r"(?:FileSize|File Size|Size)\s*:\s*([0-9.,]+)\s*(MB|GB)", text, re.I)
        size_bytes = 0
        if size_match:
            try:
                amount = float(size_match.group(1).replace(",", "."))
                size_bytes = int(amount * (1024 ** 2 if size_match.group(2).upper() == "MB" else 1024 ** 3))
            except ValueError:
                pass
        duration = re.search(r"Duration\s*:\s*(.+?)(?=\s*(?:Audio Bitrate|Bitrate|FileSize|File Size|Post Date|Rec Date|Genre)\s*:|$)", text, re.I)
        bitrate = re.search(r"(?:Audio Bitrate|Bitrate)\s*:\s*(.+?)(?=\s*(?:FileSize|File Size|Post Date|Rec Date|Genre)\s*:|$)", text, re.I)
        genre = re.search(r"Genre\s*:\s*(.+?)(?=\s*Duration\s*:|$)", text, re.I)

        return {
            "guid": url, "title": title, "link": url,
            "enclosure": audio_url, "image": image_url,
            "pubDate": pub_date.isoformat(),
            "genre": duration.group(1) if False else (genre.group(1).strip() if genre else "DJ Mix"),
            "duration": duration.group(1).strip() if duration else "",
            "bitrate": bitrate.group(1).strip() if bitrate else "",
            "filesize": size_bytes,
        }, report
    except Exception as exc:
        report["error"] = "Article failed: " + str(exc)
        return None, report


def load_items():
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, list):
        return {x["guid"]: x for x in raw if isinstance(x, dict) and x.get("guid")}
    if isinstance(raw, dict):
        return raw
    return {}


def date_key(item):
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
        "    <link>" + ARCHIVE + "</link>",
        "    <description>GlobalDJMix DJ mixes with direct audio enclosures and episode artwork.</description>",
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
        description = "Source: " + item["link"] + NL + "Genre: " + item.get("genre", "") + NL \
            + "Duration: " + item.get("duration", "") + NL + "Audio: " + item.get("bitrate", "")
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
            + '" length="' + str(int(item.get("filesize") or 0)) + '" type="audio/mpeg" />',
            "      <itunes:episodeType>full</itunes:episodeType>",
        ])
        if item.get("duration"):
            lines.append("      <itunes:duration>" + esc(item["duration"]) + "</itunes:duration>")
        lines.append("    </item>")
    lines.extend(["  </channel>", "</rss>"])
    return NL.join(lines) + NL


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = load_items()
    print("FEED_MODE=" + MODE + "; timeout=" + str(TIMEOUT) + "s; workers=" + str(WORKERS))
    print("Archive source: " + ARCHIVE)
    try:
        urls, total_pages = discover(MODE)
    except Exception as exc:
        print("Archive discovery error: " + str(exc), file=sys.stderr)
        REPORT_FILE.write_text(json.dumps({
            "mode": MODE, "archive_source": ARCHIVE, "error": str(exc),
            "source_articles_attempted": 0, "rss_episode_count": 0, "diagnostics": [],
        }, ensure_ascii=False, indent=2) + NL, encoding="utf-8")
        DATA_FILE.write_text("[]\n", encoding="utf-8")
        OUTPUT_FILE.write_text(build_rss([]), encoding="utf-8")
        return 0

    if MODE == "test":
        base_items = {}
        selected = urls[:TEST_LIMIT]
    else:
        base_items = existing
        selected = urls if MODE == "full" else urls[:]
    pending = [u for u in selected if u not in base_items]
    print("Selected post URLs: " + str(len(selected)) + "; new pages to inspect: " + str(len(pending)))

    diagnostics = []
    result = dict(base_items)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(parse_episode, url): url for url in pending}
        done = 0
        for future in as_completed(futures):
            url = futures[future]
            try:
                episode, detail = future.result()
            except Exception as exc:
                episode, detail = None, {"url": url, "error": str(exc)}
            diagnostics.append(detail)
            if episode:
                result[url] = episode
            done += 1
            print(
                "Episode test " + str(done) + "/" + str(len(pending))
                + ": audio=" + str(bool(episode))
                + ", image=" + str(bool(detail.get("image_http_ok")))
                + ", method=" + str(detail.get("audio_method") or "-")
                + ", title=" + (detail.get("title") or url)[:90]
            )

    items = sorted(result.values(), key=date_key, reverse=True)
    if MODE == "test":
        items = items[:TEST_LIMIT]
    DATA_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2) + NL, encoding="utf-8")
    OUTPUT_FILE.write_text(build_rss(items), encoding="utf-8")

    methods = {}
    for d in diagnostics:
        if d.get("audio_ok"):
            m = d.get("audio_method") or "unknown"
            methods[m] = methods.get(m, 0) + 1
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": MODE, "archive_source": ARCHIVE, "archive_pages_reported": total_pages,
        "source_articles_attempted": len(selected),
        "audio_resolved": sum(bool(d.get("audio_ok")) for d in diagnostics),
        "images_found_on_episode_pages": sum(bool(d.get("image_found")) for d in diagnostics),
        "image_urls_http_verified": sum(bool(d.get("image_http_ok")) for d in diagnostics),
        "audio_resolution_methods": methods, "rss_episode_count": len(items),
        "diagnostics": diagnostics,
    }
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2) + NL, encoding="utf-8")
    print("SUMMARY: audio=" + str(report["audio_resolved"]) + "/"
          + str(len(selected)) + ", images verified="
          + str(report["image_urls_http_verified"]) + "/" + str(len(selected))
          + ", methods=" + json.dumps(methods) + ", rss=" + str(len(items)))
    print("Wrote rss.xml with " + str(len(items)) + " podcast episodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
