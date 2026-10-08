#!/usr/bin/env python3
import html,json,re,sys,time
from datetime import datetime,timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin,urlparse
import requests
from bs4 import BeautifulSoup

BASE="https://globaldjmix.com"; RSS_SOURCE=f"{BASE}/rss"
DATA_FILE=Path("data/items.json"); OUTPUT_FILE=Path("rss.xml")
MAX_ITEMS=1000; TIMEOUT=30
HEADERS={"User-Agent":"Mozilla/5.0 (compatible; GlobalDJMixPodcastRSS/1.0)"}
s=requests.Session(); s.headers.update(HEADERS)

def fetch(url,attempts=3):
    last=None
    for n in range(attempts):
        try:
            r=s.get(url,timeout=TIMEOUT,allow_redirects=True); r.raise_for_status(); return r
        except Exception as e:
            last=e; time.sleep(2*(n+1))
    raise last

def clean(x): return re.sub(r"\s+"," ",x or "").strip()

def is_article_url(url):
    try:
        p=urlparse(url); host=p.netloc.lower(); path=p.path.strip("/")
        if host not in {"globaldjmix.com","www.globaldjmix.com"} or not path or "/" in path: return False
        return path not in {"rss","livedjsets","topic","best-mixes-by-month","livesets","podcasts","news"} and len(path)>20
    except: return False

def discover():
    root=BeautifulSoup(fetch(RSS_SOURCE).content,"xml"); out=[]
    for item in root.find_all("item"):
        n=item.find("link")
        if n:
            u=urljoin(BASE,clean(n.get_text(" ",strip=True)))
            if is_article_url(u): out.append(u)
    return list(dict.fromkeys(out))

def parse_date(text,label):
    m=re.search(re.escape(label)+r"\s*:\s*(\d{1,2}-[A-Za-z]{3}-\d{4}|\d{1,2}/\d{1,2}/\d{4})",text,re.I)
    if not m:return None
    for f in ("%d-%b-%Y","%d/%m/%Y"):
        try:return datetime.strptime(m.group(1),f).replace(tzinfo=timezone.utc)
        except ValueError:pass

def parse_size(text):
    m=re.search(r"FileSize:\s*([0-9.,]+)\s*(MB|GB)",text,re.I)
    if not m:return 0
    return int(float(m.group(1).replace(",","."))*(1024**2 if m.group(2).upper()=="MB" else 1024**3))

def extract(url):
    r=fetch(url); soup=BeautifulSoup(r.text,"html.parser"); text=clean(soup.get_text(" ",strip=True))
    h1=soup.find("h1"); title=clean(h1.get_text(" ",strip=True)) if h1 else url
    enclosure=None
    for a in soup.find_all("a",href=True):
        u=html.unescape(a["href"]).strip()
        if "box.globaldjmix.com" in u and u.startswith(("http://","https://")):
            enclosure=u; break
    if not enclosure:
        m=re.search(r'https?://[^"\'\s>]+\.mp3(?:\?[^"\'\s>]*)?',r.text,re.I)
        if m: enclosure=html.unescape(m.group(0))
    if not enclosure:return None
    m=re.search(r"Duration:\s*([^|]+?)(?=\s*(?:Audio Bitrate|FileSize|Post Date|Rec Date):)",text,re.I)
    duration=clean(m.group(1)) if m else ""
    m=re.search(r"Audio Bitrate:\s*([^|]+?)(?=\s*(?:FileSize|Post Date|Rec Date):)",text,re.I)
    bitrate=clean(m.group(1)) if m else ""
    m=re.search(r"Genre:\s*(.*?)(?=\s*Duration:)",text,re.I)
    genre=clean(m.group(1)) if m else "DJ Mix"
    pub=parse_date(text,"Post Date") or parse_date(text,"Rec Date") or datetime.now(timezone.utc)
    rec=parse_date(text,"Rec Date")
    return {"guid":url,"title":title,"link":url,"enclosure":enclosure,"pubDate":pub.isoformat(),"recDate":rec.isoformat() if rec else None,"genre":genre,"duration":duration,"bitrate":bitrate,"filesize":parse_size(text)}

def load():
    if not DATA_FILE.exists():return {}
    try:
        x=json.loads(DATA_FILE.read_text(encoding="utf-8"))
        return {i["guid"]:i for i in x if isinstance(i,dict) and i.get("guid")} if isinstance(x,list) else {}
    except:return {}

def esc(x):return html.escape(str(x or ""),quote=False)
def cdata(x):return "<![CDATA["+str(x or "").replace("]]>","]]]]><![CDATA[>")+"]]>"
def key(x):
    try:return datetime.fromisoformat(x["pubDate"])
    except:return datetime.min.replace(tzinfo=timezone.utc)

def build(items):
    now=datetime.now(timezone.utc)
    lines=['<?xml version="1.0" encoding="UTF-8"?>','<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">','  <channel>','    <title>GlobalDJMix – DJ Mixes &amp; Live Sets</title>',f'    <link>{BASE}/livedjsets</link>','    <description>GlobalDJMix releases with direct audio enclosures for podcast players.</description>','    <language>en</language>',f'    <lastBuildDate>{format_datetime(now)}</lastBuildDate>','    <itunes:author>GlobalDJMix</itunes:author>','    <itunes:explicit>no</itunes:explicit>','    <itunes:type>episodic</itunes:type>']
    for i in items:
        d=datetime.fromisoformat(i["pubDate"]); desc=f"Source: {i['link']}\nGenre: {i.get('genre','')}\nDuration: {i.get('duration','')}\nAudio: {i.get('bitrate','')}"
        if i.get("filesize"):desc+=f"\nFile size: {i['filesize']/(1024**2):.2f} MB"
        lines += ['    <item>',f'      <title>{esc(i["title"])}</title>',f'      <guid isPermaLink="true">{esc(i["guid"])}</guid>',f'      <link>{esc(i["link"])}</link>',f'      <pubDate>{format_datetime(d)}</pubDate>',f'      <description>{cdata(desc)}</description>',f'      <category>{esc(i.get("genre") or "DJ Mix")}</category>',f'      <enclosure url="{html.escape(i["enclosure"],quote=True)}" length="{int(i.get("filesize") or 0)}" type="audio/mpeg" />','      <itunes:episodeType>full</itunes:episodeType>']
        if i.get("duration"):lines.append(f'      <itunes:duration>{esc(i["duration"])}</itunes:duration>')
        lines.append('    </item>')
    return "\n".join(lines+['  </channel>','</rss>'])+"\n"

def main():
    DATA_FILE.parent.mkdir(parents=True,exist_ok=True); known=load()
    links=discover(); print(f"Discovered {len(links)} source items")
    for n,u in enumerate(links,1):
        if u in known:continue
        try:
            i=extract(u)
            if i:known[u]=i;print(f"[{n}] added {i['title']}")
            else:print(f"[{n}] skipped (no MP3)")
        except Exception as e:print(f"[{n}] failed {u}: {e}",file=sys.stderr)
    ordered=sorted(known.values(),key=key,reverse=True)[:MAX_ITEMS]
    DATA_FILE.write_text(json.dumps(ordered,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    OUTPUT_FILE.write_text(build(ordered),encoding="utf-8")
    print(f"Wrote {OUTPUT_FILE} with {len(ordered)} episodes")
if __name__=="__main__":main()
