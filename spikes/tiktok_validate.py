"""DC-01 validation, second pass: is the data the browser crawler got right?

    cd E:\\an\\data-crawler
    .venv\\Scripts\\python -I -u spikes\\tiktok_validate.py

First pass (validate-20261031T201635Z): oEmbed says every crawler and Apify video still exists with the right id —
but TikTok's oEmbed no longer returns author or caption, and one scroll loaded several pages of which only the first
was kept. This pass:
1. Integrity — 10 random crawler videos and 10 random Apify videos opened on their own TikTok page; the page's own
   data must show the same id, author and caption we stored.
2. Ranking — every search page the browser loads is kept (one scroll step at a time) up to ~120 videos per keyword:
   where are Apify's videos, if anywhere?
Read-only: navigate, scroll. A captcha waits for the owner. Raw pages are saved labelled (BR-RAW-01); the report goes
to data/reports/. Throwaway: deleted when DC-01 closes.
"""

import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
FOODREVIEW = ROOT.parent / "FoodReview"
EARLIER = ["browser-20261031T200521Z.json", "browser-20261031T200801Z.json"]
TARGET_VIDEOS = 120
SAMPLE = 10
SEARCH_API = "/api/search/item/full/"
KEPT_PARAMS = {"keyword", "offset", "count", "sort_type", "publish_time", "is_filter_search", "from_page", "search_source"}
VIDEO_ID = re.compile(r"^\d{1,25}$")
USERNAME = re.compile(r"^[\w.]{1,64}$")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def kept_params(url: str) -> dict:
    """Only the search's own params — never msToken, signatures or device ids."""
    return {key: value for key, value in parse_qsl(urlsplit(url).query) if key in KEPT_PARAMS}


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# What we already have: the earlier browser runs (TikTok's own item shape) and Apify's items.
earlier: dict[str, dict[str, dict]] = {}
for name in EARLIER:
    run = json.loads((ROOT / "data" / "raw" / "tiktok-spike" / name).read_text(encoding="utf-8"))
    for raw_page in run["pages"]:
        for item in (raw_page.get("payload") or {}).get("item_list") or []:
            earlier.setdefault(raw_page["label"]["keyword"], {}).setdefault(item["id"], item)
KEYWORDS = list(earlier)
apify: dict[str, dict[str, dict]] = {keyword: {} for keyword in KEYWORDS}
for path in (FOODREVIEW / "data" / "raw" / "tiktok").glob("*.json"):
    for item in json.loads(path.read_text(encoding="utf-8"))["items"]:
        if item.get("searchQuery") in apify and item.get("id"):
            apify[item["searchQuery"]][item["id"]] = item

# (source, id, author, caption) samples for the integrity check.
crawler_videos = [("crawler", item["id"], (item.get("author") or {}).get("uniqueId", ""), item.get("desc", ""))
                  for videos in earlier.values() for item in videos.values()]
apify_videos = [("apify", item["id"], (item.get("authorMeta") or {}).get("name", ""), item.get("text", ""))
                for videos in apify.values() for item in videos.values()]
random.seed(20261101)
samples = random.sample(crawler_videos, SAMPLE) + random.sample(apify_videos, SAMPLE)

stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
raw_pages, ranking, integrity = [], {}, []
with sync_playwright() as playwright:
    context = playwright.chromium.launch_persistent_context(
        str(ROOT / "data" / "browser-profile" / "tiktok"), channel="chromium", headless=False, locale="vi-VN",
        viewport={"width": 1366, "height": 900}, args=["--mute-audio"],
    )
    page = context.pages[0] if context.pages else context.new_page()
    seen = []  # every search API response the page gets, read after it lands
    page.on("response", lambda response: seen.append(response) if SEARCH_API in response.url else None)
    captcha = page.locator("[id*='captcha' i], [class*='captcha' i]").or_(page.get_by_text("ghép hình"))

    def wait_out_captcha() -> None:
        if captcha.first.is_visible():
            print("    CAPTCHA — please solve it in the browser window (waiting up to 5 min)")
            deadline = time.time() + 300
            while time.time() < deadline and captcha.first.is_visible():
                page.wait_for_timeout(2000)

    # 2. Ranking: keep every page the browser loads, one scroll step at a time.
    for number, keyword in enumerate(KEYWORDS):
        if number:
            time.sleep(random.uniform(5, 15))  # performance.md: between keywords
        by_offset: dict[int, dict] = {}
        read, idle, has_more = len(seen), 0, True
        page.goto(f"https://www.tiktok.com/search/video?q={quote(keyword)}", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        wait_out_captcha()
        while has_more and idle < 3 and sum(len(entry["items"]) for entry in by_offset.values()) < TARGET_VIDEOS:
            new = 0
            while read < len(seen):
                response = seen[read]
                read += 1
                try:
                    payload = json.loads(response.body())
                except ValueError:
                    print(f"    empty search response (HTTP {response.status})")
                    continue
                params = kept_params(response.request.url)
                offset = int(params.get("offset", -1))
                if offset not in by_offset:
                    items = payload.get("item_list") or []
                    by_offset[offset] = {"params": params, "items": [item["id"] for item in items]}
                    raw_pages.append({"label": {"keyword": keyword, "offset": offset, "scrapedAt": now(), "params": params,
                                                "items": len(items)}, "payload": payload})
                    has_more = bool(payload.get("has_more"))
                    new += 1
            idle = 0 if new else idle + 1
            time.sleep(random.uniform(2, 5))  # performance.md: between pages
            page.mouse.move(700, 600)  # over the results grid
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(2500)
        ordered = [video_id for offset in sorted(by_offset) for video_id in by_offset[offset]["items"]]
        position = {}
        for video_id in ordered:
            position.setdefault(video_id, len(position) + 1)
        found = sorted(position[video_id] for video_id in apify[keyword] if video_id in position)
        ranking[keyword] = {"offsets": sorted(by_offset), "ourVideos": len(position), "apifyVideos": len(apify[keyword]),
                            "apifyFoundAt": found}
        print(f"{keyword!r}: offsets {sorted(by_offset)} → {len(position)} distinct videos; "
              f"Apify's {len(apify[keyword])}: {len(found)} found, at positions {found}")

    # 1. Integrity: open each sampled video's own page and compare with what we stored.
    print(f"\nOpening {len(samples)} sampled video pages…")
    for source, video_id, author, caption in samples:
        time.sleep(random.uniform(2, 5))  # performance.md
        result = {"source": source, "id": video_id}
        if not (VIDEO_ID.match(video_id) and USERNAME.match(author)):  # security.md: validated before use
            integrity.append(result | {"note": "invalid id or author"})
            continue
        page.goto(f"https://www.tiktok.com/@{author}/video/{video_id}", wait_until="domcontentloaded")
        wait_out_captcha()
        try:
            data = json.loads(page.locator("#__UNIVERSAL_DATA_FOR_REHYDRATION__").text_content(timeout=15_000) or "")
            item = data["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
        except Exception as error:  # noqa: BLE001 — a spike records the miss and moves on
            integrity.append(result | {"note": f"no video data on the page: {type(error).__name__}"})
            print(f"  {source:<7} {video_id}: no video data on the page")
            continue
        result |= {
            "sameId": str(item.get("id")) == video_id,
            "sameAuthor": norm((item.get("author") or {}).get("uniqueId")) == norm(author),
            "sameCaption": norm(item.get("desc"))[:60] == norm(caption)[:60],
        }
        integrity.append(result)
        print(f"  {source:<7} {video_id}: id {result['sameId']}, author {result['sameAuthor']}, caption {result['sameCaption']}")
    context.close()

print("\n== Integrity (video's own page)")
summary = {}
for source in ("crawler", "apify"):
    rows = [row for row in integrity if row["source"] == source]
    checked = [row for row in rows if "sameId" in row]
    summary[source] = {"sampled": len(rows), "checked": len(checked),
                       **{field: sum(bool(row[field]) for row in checked) for field in ("sameId", "sameAuthor", "sameCaption")}}
    print(f"  {source:<7}: {len(checked)}/{len(rows)} pages read — same id {summary[source]['sameId']}, "
          f"same author {summary[source]['sameAuthor']}, same caption {summary[source]['sameCaption']}")

label = {"source": "data-crawler", "engine": "playwright-chromium", "session": "logged-in", "headed": True,
         "network": "owner PC, residential VN IP", "startedAt": stamp, "keywords": KEYWORDS, "targetVideos": TARGET_VIDEOS}
write_json(ROOT / "data" / "raw" / "tiktok-spike" / f"validate-{stamp}.json", {"run": label, "pages": raw_pages})
write_json(ROOT / "data" / "reports" / f"tiktok-validation-{stamp}.json",
           {"run": label, "integrity": {"summary": summary, "rows": integrity}, "ranking": ranking})
print(f"\nsaved: data/raw/tiktok-spike/validate-{stamp}.json, data/reports/tiktok-validation-{stamp}.json")
