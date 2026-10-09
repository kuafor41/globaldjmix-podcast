#!/usr/bin/env python3
"""Read-only GlobalDJMix tracklist feasibility test; does not modify RSS data."""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://globaldjmix.com"
ITEMS_PATH = Path("data/items.json")
REPORT_PATH = Path("tracklist-test-report.json")
GAP_SECONDS = 30
TIMEOUT = 20


def main():
    username = os.getenv("GLOBALDJMIX_USERNAME", "").strip()
    password = os.getenv("GLOBALDJMIX_PASSWORD", "")
    if not username or not password:
        raise SystemExit("Missing GLOBALDJMIX_USERNAME / GLOBALDJMIX_PASSWORD GitHub Secrets.")

    items = json.loads(ITEMS_PATH.read_text(encoding="utf-8"))
    items = [item for item in items if item.get("source_url")][:50]
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; GlobalDJMixPodcastRSS/3.0)",
        "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.8",
    })

    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "request_gap_seconds": GAP_SECONDS,
        "selected": len(items),
        "login_confirmed": False,
        "tracklists_found": 0,
        "rate_limited": 0,
        "button_missing": 0,
        "errors": 0,
        "results": [],
    }

    try:
        auth_response = session.get(BASE + "/get-auth-form", timeout=TIMEOUT)
        auth_response.raise_for_status()
        auth_soup = BeautifulSoup(auth_response.text, "html.parser")
        form = auth_soup.find("form")
        if not form:
            raise RuntimeError("Login form was not returned by /get-auth-form")

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
        login_response = session.post(
            action, data=data, headers={"Referer": auth_response.url},
            timeout=TIMEOUT, allow_redirects=True,
        )
        login_response.raise_for_status()
        login_text = login_response.text.lower()
        report["login_confirmed"] = any(
            marker in login_text for marker in ("logout", "log out", "sign out")
        )
        print("Login response received; explicit confirmation:", report["login_confirmed"])

        for index, item in enumerate(items, start=1):
            result = {
                "title": item.get("title", ""),
                "source_url": item["source_url"],
                "button_found": False,
                "track_count": 0,
                "tracklist": "",
                "status": "error",
                "error": None,
            }
            try:
                page = session.get(item["source_url"], timeout=TIMEOUT)
                page.raise_for_status()
                soup = BeautifulSoup(page.text, "html.parser")
                button = soup.select_one("button.show-tracklist[data-id]")
                if not button or not button.get("data-id"):
                    result["status"] = "button_missing"
                    report["button_missing"] += 1
                    report["results"].append(result)
                    print(f"{index}/{len(items)}: no tracklist button")
                    continue

                result["button_found"] = True
                # One tracklist endpoint request every 30 seconds; no concurrent calls.
                if index > 1:
                    time.sleep(GAP_SECONDS)
                response = session.get(
                    BASE + "/get-tracklist",
                    params={"id": button.get("data-id")},
                    headers={
                        "Referer": page.url,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=TIMEOUT,
                )
                response.raise_for_status()
                payload = response.json()
                tracklist_html = payload.get("tracklist") or ""
                track_soup = BeautifulSoup(tracklist_html, "html.parser")
                response_text = re.sub(r"\s+", " ", track_soup.get_text(" ", strip=True)).strip()

                if "many requests to tracklists" in response_text.lower():
                    result["status"] = "rate_limited"
                    report["rate_limited"] += 1
                elif payload.get("success") is not True:
                    result["status"] = "endpoint_unsuccessful"
                    result["error"] = "Endpoint did not return success=true"
                    report["errors"] += 1
                else:
                    nodes = track_soup.select(".track")
                    tracks = [
                        re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                        for node in nodes
                    ]
                    tracks = [track for track in tracks if track]
                    if not tracks and response_text:
                        tracks = [response_text]
                    if tracks:
                        result["status"] = "ok"
                        result["track_count"] = len(tracks)
                        result["tracklist"] = "\n".join(tracks)
                        report["tracklists_found"] += 1
                    else:
                        result["status"] = "empty"
                        result["error"] = "Successful response contained no track text"
                        report["errors"] += 1
                print(f"{index}/{len(items)}: {result['status']} ({result['track_count']} tracks)")
            except Exception as exc:
                result["error"] = type(exc).__name__ + ": " + str(exc)[:220]
                report["errors"] += 1
                print(f"{index}/{len(items)}: error - {result['error']}")
            report["results"].append(result)
    except Exception as exc:
        report["fatal_error"] = type(exc).__name__ + ": " + str(exc)[:300]
        print("Fatal test error:", report["fatal_error"])
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(
            "SUMMARY:",
            f"selected={report['selected']}",
            f"login_confirmed={report['login_confirmed']}",
            f"tracklists_found={report['tracklists_found']}",
            f"rate_limited={report['rate_limited']}",
            f"button_missing={report['button_missing']}",
            f"errors={report['errors']}",
        )


if __name__ == "__main__":
    main()
