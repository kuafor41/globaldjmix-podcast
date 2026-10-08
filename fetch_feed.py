#!/usr/bin/env python3
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE = "https://globaldjmix.com"
RSS_SOURCE = f"{BASE}/rss"
DATA_FILE = Path("data/items.json")
OUTPUT_FILE = Path("rss.xml")
MAX_ITEMS = 1000
TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GlobalDJMixPodcastRSS/1.0)"}

session = requests.Session()
session.headers.update(HEADERS)


def fetch(url, attempts=3):
    last = None
    for n in range(attempts):
        try:
            r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            r.raise_for_status()
            return r
        except Exception as exc:
            last = exc
            time.sleep(2 * (n + 1))
    raise last


def clean(value):
    return re.sub(r"s+", " ", value or "").strip()


def is_article_url(url):
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        path = p.path.strip("/")
        blocked = {
            "rss", "livedjsets", "topic", "best-mixes-by-month",
            "livesets", "podcasts", "news"
        }
        if host not in {"globaldjmix.com", "www.globaldjmix.com"} or not path:
            return False
        if "/" in path or path in blocked or len(path) <= 20:
            return False
        return True
    except Exception:
        return False


def discover():
    response = fetch(RSS_SOURCE)
    root = ET.fromstring(response.content)
    out = []
    for item in root.findall(".//item"):
        link = item.findtext("link", default="")
        link = urljoin(BASE, clean(link))
        if is_article_url(link):
            out.append(link)
    return list(dict.fromkeys(out))


def parse_date(text, label):
    m = re.search(
        re.escape(label) +
        r"s*:s*(d{1,2}-[A-Za-z]{3}-d{4}|d{1,2}/d{1,2}/d{4})",
        text,
        re.I,
    )
    if not m:
        return None
    for fmt in ("%d-%b-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def parse_size(text):
    m = re.search(r"FileSize:s*([0-9.,]+)s*(MB|GB)", text, re.I)
    if not m:
        return 0
    value = float(m.group(1).replace(",", "."))
    return int(value * (1024 ** 2 if m.group(2).upper() == "MB" else 1024 ** 3))


def find_enclosure(soup, source):
    for tag in soup.find_all(["a", "audio", "source"], href=True):
        url = html.unescape(tag.get("href", "")).strip()
        if "box.globaldjmix.com" in urlparse(url).netloc.lower() and url.startswith(("http://", "https://")):
            if ".mp3" in url.lower() or "media" in url.lower():
                return url

    for tag in soup.find_all(["audio", "source"], src=True):
        url = html.unescape(tag.get("src", "")).strip()
        if "box.globaldjmix.com" in urlparse(url).netloc.lower():
            return url

    patterns = [
        r"""https?://[^"'<>\s]+.mp3(?:?[^"'<>\s]*)?""",
        r"""https?:\/\/[^"'<>\s]+\.mp3(?:\?[^"'<>\s]*)?""",
    ]
    for pattern in patterns:
        m = re.search(pattern, source, re.I)
        if m:
            url = html.unescape(m.group(0)).replace("\/", "/")
            if "box.globaldjmix.com" in urlparse(url).netloc.lower():
                return url
    return None


def extract(url):
    response = fetch(url)
    soup = BeautifulSoup(response.text, "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))

    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else url
    enclosure = find_enclosure(soup, response.text)
    if not enclosure:
        return None

    duration_match = re.search(
        r"Duration:s*(.+?)(?=s*(?:Audio Bitrate|Bitrate|FileSize|Post Date|Rec Date):)",
        page_text,
        re.I,
    )
    bitrate_match = re.search(
        r"Audio Bitrate:s*(.+?)(?=s*(?:FileSize|Post Date|Rec Date):)",
        page_text,
        re.I,
    )
    genre_match = re.search(
        r"Genre:s*(.+?)(?=s*Duration:)",
        page_text,
        re.I,
    )

    duration = clean(duration_match.group(1)) if duration_match else ""
    bitrate = clean(bitrate_match.group(1)) if bitrate_match else ""
    genre = clean(genre_match.group(1)) if genre_match else "DJ Mix"
    pub_date = parse_date(page_text, "Post Date") or parse_date(page_text, "Rec Date") or datetime.now(timezone.utc)
    rec_date = parse_date(page_text, "Rec Date")

    return {
        "guid": url,
        "title": title,
        "link": url,
        "enclosure": enclosure,
        "pubDate": pub_date.isoformat(),
        "recDate": rec_date.isoformat() if rec_date else None,
        "genre": genre,
        "duration": duration,
        "bitrate": bitrate,
        "filesize": parse_size(page_text),
    }


def load_items():
    if not DATA_FILE.exists():
        return {}
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return {x["guid"]: x for x in raw if isinstance(x, dict) and x.get("guid")}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def sort_key(item):
    try:
        return datetime.fromisoformat(item["pubDate"])
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


def xml_escape(value, quote=False):
    return html.escape(str(value or ""), quote=quote)


def cdata(value):
    return "<![CDATA[" + str(value or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def build_rss(items):
    now = datetime.now(timezone.utc)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">',
        "  <channel>",
        "    <title>GlobalDJMix – DJ Mixes &amp; Live Sets</title>",
        f"    <link>{BASE}/livedjsets</link>",
        "    <description>GlobalDJMix releases with direct audio enclosures for podcast players.</description>",
        "    <language>en</language>",
        f"    <lastBuildDate>{format_datetime(now)}</lastBuildDate>",
        "    <itunes:author>GlobalDJMix</itunes:author>",
        "    <itunes:explicit>no</itunes:explicit>",
        "    <itunes:type>episodic</itunes:type>",
    ]

    for item in items:
        dt = datetime.fromisoformat(item["pubDate"])
        description = (
            f"Source: {item['link']}\n"
            f"Genre: {item.get('genre', '')}\n"
            f"Duration: {item.get('duration', '')}\n"
            f"Audio: {item.get('bitrate', '')}"
        )
        if item.get("filesize"):
            description += f"\nFile size: {item['filesize'] / (1024 ** 2):.2f} MB"

        lines.extend([
            "    <item>",
            f"      <title>{xml_escape(item['title'])}</title>",
            f"      <guid isPermaLink="true">{xml_escape(item['guid'])}</guid>",
            f"      <link>{xml_escape(item['link'])}</link>",
            f"      <pubDate>{format_datetime(dt)}</pubDate>",
            f"      <description>{cdata(description)}</description>",
            f"      <category>{xml_escape(item.get('genre') or 'DJ Mix')}</category>",
            f"      <enclosure url="{xml_escape(item['enclosure'], quote=True)}" length="{int(item.get('filesize') or 0)}" type="audio/mpeg" />",
            "      <itunes:episodeType>full</itunes:episodeType>",
        ])
        if item.get("duration"):
            lines.append(f"      <itunes:duration>{xml_escape(item['duration'])}</itunes:duration>")
        lines.append("    </item>")

    lines.extend(["  </channel>", "</rss>"])
    return "
".join(lines) + "
"


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    known = load_items()

    try:
        links = discover()
    except Exception as exc:
        print(f"Failed to read {RSS_SOURCE}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Discovered {len(links)} source items from {RSS_SOURCE}")

    for index, url in enumerate(links, 1):
        if url in known:
            continue
        try:
            item = extract(url)
            if item:
                known[url] = item
                print(f"[{index}/{len(links)}] added: {item['title']}")
            else:
                print(f"[{index}/{len(links)}] skipped (no direct MP3): {url}")
        except Exception as exc:
            print(f"[{index}/{len(links)}] failed: {url} -> {exc}", file=sys.stderr)

    ordered = sorted(known.values(), key=sort_key, reverse=True)[:MAX_ITEMS]
    DATA_FILE.write_text(json.dumps(ordered, ensure_ascii=False, indent=2) + "
", encoding="utf-8")
    OUTPUT_FILE.write_text(build_rss(ordered), encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE} with {len(ordered)} podcast episodes.")


if __name__ == "__main__":
    main()
