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
MAX_ITEMS = 1000
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


def discover_urls(max_pages=10):
    # The current-release archive is paginated as /dj-songs-mp3-download?p=N.
    # Each page contains the main chronological listing followed by a
    # separate "Most popular DJ Mixes & Live Sets" block. We deliberately
    # ignore everything after that marker.
    found = []

    for page in range(1, max_pages + 1):
        page_url = BASE + "/dj-songs-mp3-download"
        if page > 1:
            page_url += "?p=" + str(page)

        try:
            response = fetch(page_url)
        except Exception as exc:
            print("Page unavailable: " + page_url + " -> " + str(exc), file=sys.stderr)
            continue

        soup = BeautifulSoup(response.text, "html.parser")

        # Find the "Most popular" marker and only inspect links appearing
        # before it in document order.
        marker = None
        for element in soup.find_all(string=re.compile(r"Most popular DJ Mixes", re.I)):
            marker = element.parent
            break

        if marker:
            all_links = soup.find_all("a", href=True)
            for link in all_links:
                if marker in list(link.parents):
                    # This test handles nested markup. We still need to stop
                    # only after passing the marker in document order below.
                    pass

        candidates = []
        for link in soup.find_all("a", href=True):
            # Stop once the marker element itself is reached.
            if marker is not None and link is marker:
                break
            href = html.unescape(link.get("href", "")).strip()
            url = urljoin(BASE, href)
            if article_url(url):
                candidates.append(url)

        # If the marker is not directly encountered by the anchor scan,
        # remove links belonging to the marker's following section using
        # document-order positions.
        if marker is not None:
            marker_index = None
            for index, tag in enumerate(soup.find_all(True)):
                if tag is marker or marker in list(tag.parents):
                    marker_index = index
                    break
            if marker_index is not None:
                tags = soup.find_all(True)
                candidates = []
                for index, tag in enumerate(tags):
                    if index >= marker_index:
                        break
                    if tag.name == "a" and tag.get("href"):
                        url = urljoin(BASE, html.unescape(tag.get("href", "")).strip())
                        if article_url(url):
                            candidates.append(url)

        page_found = list(dict.fromkeys(candidates))
        print("Archive page " + str(page) + ": " + str(len(page_found)) + " article URLs")
        found.extend(page_found)

        # Empty pages normally indicate the end of the archive.
        if not page_found:
            break

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


def extract_episode(url):
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    heading = soup.find("h1")
    title = clean(heading.get_text(" ", strip=True)) if heading else url

    # Extract the publication date from the final "(DD Month YYYY)"
    # portion of the article title.
    title_date = None
    title_date_match = re.search(
        r"\((\d{1,2})\s+([A-Za-z]+)\s+(20\d{2})\)\s*$",
        title
    )
    if title_date_match:
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12
        }
        month = months.get(title_date_match.group(2).lower())
        if month:
            title_date = datetime(
                int(title_date_match.group(3)),
                month,
                int(title_date_match.group(1)),
                tzinfo=timezone.utc,
            )

    mp3 = find_mp3(soup, response.text)
    if not mp3:
        return None

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

    pub_date = (
        title_date
        or parse_date(text, "Post Date")
        or parse_date(text, "Rec Date")
        or datetime.now(timezone.utc)
    )
    rec_date = parse_date(text, "Rec Date")

    current_year = datetime.now(timezone.utc).year
    if pub_date.year < current_year:
        return None

    return {
        "guid": url,
        "title": title,
        "link": url,
        "enclosure": mp3,
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
        "    <description>GlobalDJMix releases with direct audio enclosures for podcast players.</description>",
        "    <language>en</language>",
        "    <itunes:author>GlobalDJMix</itunes:author>",
        "    <itunes:explicit>no</itunes:explicit>",
        "    <itunes:type>episodic</itunes:type>",
        "    <itunes:category text=\"Music\" />",
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
            enclosure,
            "      <itunes:episodeType>full</itunes:episodeType>",
        ])
        if item.get("duration"):
            lines.append("      <itunes:duration>" + esc(item["duration"]) + "</itunes:duration>")
        lines.append("    </item>")

    lines.extend(["  </channel>", "</rss>"])
    return NL.join(lines) + NL


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Rebuild the feed from the current GlobalDJMix listing on each run.
    # This prevents the site's "Most popular" archive block from being carried
    # into the podcast feed.
    known = {}


    try:
        urls = discover_urls()
    except Exception as exc:
        print("Unable to read " + SOURCE_RSS + ": " + str(exc), file=sys.stderr)
        return 1

    print("Discovered " + str(len(urls)) + " source items")

    pending = [(index, url) for index, url in enumerate(urls, 1) if url not in known]

    with ThreadPoolExecutor(max_workers=12) as pool:
        future_map = {pool.submit(extract_episode, url): (index, url) for index, url in pending}
        for future in as_completed(future_map):
            index, url = future_map[future]
            try:
                episode = future.result()
                if episode:
                    known[url] = episode
                    print("[" + str(index) + "/" + str(len(urls)) + "] added: " + episode["title"])
                else:
                    print("[" + str(index) + "/" + str(len(urls)) + "] skipped (no direct MP3): " + url)
            except Exception as exc:
                print("[" + str(index) + "/" + str(len(urls)) + "] failed: " + url + " -> " + str(exc), file=sys.stderr)

    ordered = sorted(known.values(), key=sort_key, reverse=True)[:MAX_ITEMS]
    DATA_FILE.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + NL, encoding="utf-8")
    OUTPUT_FILE.write_text(build_rss(ordered), encoding="utf-8")
    print("Wrote " + str(OUTPUT_FILE) + " with " + str(len(ordered)) + " podcast episodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
