#!/usr/bin/env python3
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://globaldjmix.com"
SOURCE_RSS = BASE + "/rss"
DATA_FILE = Path("data/items.json")
OUTPUT_FILE = Path("rss.xml")
MAX_ITEMS = None
TIMEOUT = 30
NL = chr(10)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; GlobalDJMixPodcastRSS/1.0)"
})


def fetch(url, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            time.sleep(2 * (attempt + 1))
    raise last_error


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def article_url(url):
    try:
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        blocked = {
            "", "rss", "livedjsets", "topic", "best-mixes-by-month",
            "livesets", "podcasts", "news"
        }
        return (
            parsed.netloc.lower() in {"globaldjmix.com", "www.globaldjmix.com"}
            and path not in blocked
            and "/" not in path
            and len(path) > 20
        )
    except Exception:
        return False


def extract_page_urls(body):
    marker = re.search(r"Most popular DJ Mixes", body, flags=re.I)
    main_body = body[:marker.start()] if marker else body
    soup = BeautifulSoup(main_body, "html.parser")
    found = []
    for tag in soup.find_all("a", href=True):
        href = html.unescape(tag.get("href", "")).strip()
        url = urljoin(BASE, href)
        if article_url(url):
            found.append(url)
    return list(dict.fromkeys(found))


def archive_page_total(body):
    match = re.search(r"Page\s+1\s+of\s+(\d+)", body, flags=re.I)
    return int(match.group(1)) if match else 1401


def discover_urls(full_backfill=False):
    first_url = BASE + "/dj-songs-mp3-download"
    response = fetch(first_url)
    first_urls = extract_page_urls(response.text)

    if not full_backfill:
        return first_urls

    total_pages = archive_page_total(response.text)
    print("FULL BACKFILL: " + str(total_pages) + " archive pages")

    urls_by_page = {1: first_urls}
    page_numbers = list(range(2, total_pages + 1))

    def fetch_page(number):
        page_url = BASE + "/dj-songs-mp3-download?p=" + str(number)
        try:
            page_response = fetch(page_url)
            return number, extract_page_urls(page_response.text)
        except Exception as exc:
            print("Page " + str(number) + " failed: " + str(exc), file=sys.stderr)
            return number, []

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(fetch_page, number) for number in page_numbers]
        for future in as_completed(futures):
            number, page_urls = future.result()
            urls_by_page[number] = page_urls
            if number % 100 == 0:
                print("Fetched archive page " + str(number) + "/" + str(total_pages))

    found = []
    for number in range(1, total_pages + 1):
        page_urls = urls_by_page.get(number, [])
        found.extend(page_urls)
    return list(dict.fromkeys(found))

def parse_date(text, label):
    match = re.search(
        re.escape(label) + r"\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        text,
        re.I,
    )
    if not match:
        return None
    for fmt in ("%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(match.group(1), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_size(text):
    match = re.search(r"FileSize:\s*([0-9.,]+)\s*(MB|GB)", text, re.I)
    if not match:
        return 0
    value = float(match.group(1).replace(",", "."))
    return int(value * (1024 ** 2 if match.group(2).upper() == "MB" else 1024 ** 3))


def normalize_media_url(url):
    if not url:
        return None
    url = html.unescape(str(url)).strip()
    parsed = urlparse(url)
    if parsed.scheme == "http" and "box.globaldjmix.com" in parsed.netloc.lower():
        url = "https://" + parsed.netloc + parsed.path
        if parsed.query:
            url += "?" + parsed.query
    return url


def verify_media_url(url):
    url = normalize_media_url(url)
    if not url:
        return None

    try:
        response = session.get(
            url,
            headers={"Range": "bytes=0-0"},
            timeout=25,
            allow_redirects=True,
            stream=True,
        )
        final_url = response.url
        content_type = (response.headers.get("content-type") or "").lower()
        response.close()

        if "audio/" in content_type or content_type.startswith("application/octet-stream"):
            return final_url

        print(
            "Rejected media URL: " + url + " -> " + final_url
            + " [" + content_type + "]",
            file=sys.stderr,
        )
    except Exception as exc:
        print("Media probe failed: " + url + " -> " + str(exc), file=sys.stderr)

    return None


def find_mp3(soup, source):
    candidates = []
    for tag in soup.find_all(["a", "audio", "source"], href=True):
        candidates.append(html.unescape(tag.get("href", "")).strip())
    for tag in soup.find_all(["audio", "source"], src=True):
        candidates.append(html.unescape(tag.get("src", "")).strip())

    for url in candidates:
        host = urlparse(url).netloc.lower()
        if "box.globaldjmix.com" in host and url.startswith(("http://", "https://")):
            if ".mp3" in url.lower() or "media" in url.lower():
                return url

    patterns = [
        r'''https?://[^"'<>\s]+\.mp3(?:\?[^"'<>\s]*)?''',
        r'''https?:\\/\\/[^"'<>\s]+\.mp3(?:\\?[^"'<>\s]*)?''',
    ]
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            url = html.unescape(match.group(0)).replace("\\/", "/")
            if "box.globaldjmix.com" in urlparse(url).netloc.lower():
                return url
    return None



def normalize_image_url(value, page_url):
    if not value:
        return None
    value = html.unescape(str(value)).strip()
    if not value or value.startswith(("data:", "javascript:", "#")):
        return None
    url = urljoin(page_url, value)
    parsed = urlparse(url)
    if parsed.scheme == "http" and parsed.netloc:
        url = "https://" + parsed.netloc + parsed.path
        if parsed.query:
            url += "?" + parsed.query
    return url


def find_image(soup, page_url):
    # 1) Social/SEO metadata is normally the exact featured image for the post.
    meta_selectors = [
        ("meta", {"property": "og:image"}),
        ("meta", {"property": "og:image:url"}),
        ("meta", {"name": "twitter:image"}),
        ("meta", {"name": "twitter:image:src"}),
    ]
    for tag_name, attrs in meta_selectors:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get("content"):
            image = normalize_image_url(tag.get("content"), page_url)
            if image:
                return image

    # 2) Common link-based featured image declaration.
    link_tag = soup.find("link", rel=lambda value: value and "image_src" in value)
    if link_tag and link_tag.get("href"):
        image = normalize_image_url(link_tag.get("href"), page_url)
        if image:
            return image

    # 3) JSON-LD article metadata.
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        objects = payload if isinstance(payload, list) else [payload]
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            value = obj.get("image")
            if isinstance(value, dict):
                value = value.get("url")
            elif isinstance(value, list):
                value = value[0] if value else None
                if isinstance(value, dict):
                    value = value.get("url")
            image = normalize_image_url(value, page_url)
            if image:
                return image

    # 4) Fallback to the first sufficiently sized content image.
    for img in soup.find_all("img"):
        for attr in ("src", "data-src", "data-lazy-src", "data-original"):
            value = img.get(attr)
            image = normalize_image_url(value, page_url)
            if image:
                lower = image.lower()
                if any(skip in lower for skip in (
                    "logo", "icon", "avatar", "favicon", "sprite", "emoji"
                )):
                    continue
                return image

    return None


def extract_episode(url):
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    heading = soup.find("h1")
    title = clean(heading.get_text(" ", strip=True)) if heading else url

    mp3 = find_mp3(soup, response.text)
    mp3 = verify_media_url(mp3)
    if not mp3:
        return None

    image = find_image(soup, url)

    duration_match = re.search(
        r"Duration:\s*(.+?)(?=\s*(?:Audio Bitrate|Bitrate|FileSize|Post Date|Rec Date):)",
        text, re.I
    )
    bitrate_match = re.search(
        r"Audio Bitrate:\s*(.+?)(?=\s*(?:FileSize|Post Date|Rec Date):)",
        text, re.I
    )
    genre_match = re.search(
        r"Genre:\s*(.+?)(?=\s*Duration:)",
        text, re.I
    )

    post_date = parse_date(text, "Post Date")
    rec_date = parse_date(text, "Rec Date")
    pub_date = post_date or rec_date or datetime.now(timezone.utc)

    return {
        "guid": url,
        "title": title,
        "link": url,
        "enclosure": mp3,
        "image": image,
        "pubDate": pub_date.isoformat(),
        "recDate": rec_date.isoformat() if rec_date else None,
        "genre": clean(genre_match.group(1)) if genre_match else "DJ Mix",
        "duration": clean(duration_match.group(1)) if duration_match else "",
        "bitrate": clean(bitrate_match.group(1)) if bitrate_match else "",
        "filesize": parse_size(text),
    }



def load_items():
    if not DATA_FILE.exists():
        return {}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if isinstance(raw, list):
        return {
            item["guid"]: item
            for item in raw
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
        "    <link>" + BASE + "/livedjsets</link>",
        "    <description>GlobalDJMix releases with direct audio enclosures and episode artwork for podcast players.</description>",
        "    <language>en</language>",
        "    <itunes:author>GlobalDJMix</itunes:author>",
        "    <itunes:explicit>no</itunes:explicit>",
        "    <itunes:type>episodic</itunes:type>",
        '    <itunes:category text="Music" />',
        "    <lastBuildDate>" + format_datetime(datetime.now(timezone.utc)) + "</lastBuildDate>",
    ]

    for item in items:
        dt = datetime.fromisoformat(item["pubDate"])
        description = (
            "Source: " + item["link"] + NL
            + "Genre: " + item.get("genre", "") + NL
            + "Duration: " + item.get("duration", "") + NL
            + "Audio: " + item.get("bitrate", "")
        )
        if item.get("filesize"):
            description += NL + "File size: " + f'{item["filesize"] / (1024 ** 2):.2f} MB'

        enclosure = (
            '      <enclosure url="' + esc(item["enclosure"], quote=True)
            + '" length="' + str(int(item.get("filesize") or 0))
            + '" type="audio/mpeg" />'
        )

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
            lines.append(
                '      <itunes:image href="' + esc(item["image"], quote=True) + '" />'
            )

        lines.extend([
            enclosure,
            "      <itunes:episodeType>full</itunes:episodeType>",
        ])
        if item.get("duration"):
            lines.append(
                "      <itunes:duration>" + esc(item["duration"]) + "</itunes:duration>"
            )
        lines.append("    </item>")

    lines.extend(["  </channel>", "</rss>"])
    return NL.join(lines) + NL



def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = load_items()
    full_backfill = not bool(existing)

    print("Mode: " + ("FULL BACKFILL" if full_backfill else "INCREMENTAL"))
    try:
        # Full first run: all archive pages. Later runs: newest page only,
        # because new content enters the front of the archive.
        urls = discover_urls(full_backfill=full_backfill)
    except Exception as exc:
        print("Unable to discover archive URLs: " + str(exc), file=sys.stderr)
        return 1

    print("Discovered " + str(len(urls)) + " source items")

    known = existing
    pending = [(index, url) for index, url in enumerate(urls, 1) if url not in known]
    print("Article pages to inspect: " + str(len(pending)))

    with ThreadPoolExecutor(max_workers=16) as pool:
        future_map = {
            pool.submit(extract_episode, url): (index, url)
            for index, url in pending
        }
        for future in as_completed(future_map):
            index, url = future_map[future]
            try:
                episode = future.result()
                if episode:
                    known[url] = episode
            except Exception as exc:
                print("Failed: " + url + " -> " + str(exc), file=sys.stderr)

    ordered = sorted(known.values(), key=sort_key, reverse=True)
    if MAX_ITEMS is not None:
        ordered = ordered[:MAX_ITEMS]

    DATA_FILE.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=2) + NL,
        encoding="utf-8",
    )
    OUTPUT_FILE.write_text(build_rss(ordered), encoding="utf-8")
    print("Wrote " + str(OUTPUT_FILE) + " with " + str(len(ordered)) + " podcast episodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
