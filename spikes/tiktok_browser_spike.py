"""DC-01 spike, browser engine: does a real browser get TikTok's video search from this IP, and how does it compare
with the Apify data FoodReview already has?

    cd E:\\an\\data-crawler
    .venv\\Scripts\\python -I spikes\\tiktok_browser_spike.py [--headed] [--one] [--top]

Opens https://www.tiktok.com/search/video?q=<keyword> in Playwright's Chromium with the crawler's own profile
(data/browser-profile/tiktok — git-ignored, never the owner's browser) and keeps the search JSON the page itself loads.
Read-only: it navigates, reloads and scrolls, never clicks. A captcha is solved by the owner in the window (--headed);
nothing here solves it (BR-DEP-01, security.md). Everything is saved unfiltered and labelled to
data/raw/tiktok-spike/browser-<stamp>.json (BR-RAW-01). Throwaway: deleted when DC-01 closes.
"""

import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
FOODREVIEW = ROOT.parent / "FoodReview"
QUERIES = ["review quán ăn nha trang", "ăn gì đà lạt", "quán ngon nha trang"][: 1 if "--one" in sys.argv else 3]
PAGES = 2
top = "--top" in sys.argv  # the Top tab (/search?q=) instead of the Video tab
SEARCH_API = "/api/search/general/full/" if top else "/api/search/item/full/"
SEARCH_URL = "https://www.tiktok.com/search?q=" if top else "https://www.tiktok.com/search/video?q="
headed = "--headed" in sys.argv
CAPTCHA_WAIT_S = 300
MAX_BLOCKS = 3  # performance.md: stop after 3 keywords in a row get nothing

apify: dict[str, list[dict]] = {}
for path in (FOODREVIEW / "data" / "raw" / "tiktok").glob("*.json"):
    for item in json.loads(path.read_text(encoding="utf-8"))["items"]:
        if item.get("searchQuery") and item.get("id"):
            apify.setdefault(item["searchQuery"], []).append(item)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def videos(payload: dict) -> list[dict]:
    """Video tab: item_list; Top tab: data[].item."""
    return payload.get("item_list") or [entry["item"] for entry in payload.get("data") or [] if entry.get("item")]


def rate(count: int, total: int) -> str:
    return f"{count}/{total}" + (f" ({100 * count // total}%)" if total else "")


stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
debug_dir = ROOT / "data" / "debug"
debug_dir.mkdir(parents=True, exist_ok=True)
pages, comparison, captchas, blocks = [], [], 0, 0
with sync_playwright() as playwright:
    context = playwright.chromium.launch_persistent_context(
        str(ROOT / "data" / "browser-profile" / "tiktok"),
        channel="chromium",  # full Chromium (new headless), not the stripped headless shell
        headless=not headed,
        locale="vi-VN",
        viewport={"width": 1366, "height": 900},
    )
    session = "logged-in" if any(c["name"] == "sessionid" for c in context.cookies("https://www.tiktok.com")) else "guest"
    page = context.pages[0] if context.pages else context.new_page()
    seen = []  # every search API response the page gets, read after it lands
    page.on("response", lambda response: seen.append(response) if SEARCH_API in response.url else None)
    captcha = page.locator("[id*='captcha' i], [class*='captcha' i]").or_(page.get_by_text("ghép hình"))

    def wait_for_payload(first: int, seconds: int) -> dict | None:
        """The first search response from index `first` on with a JSON body; logs the empty ones."""
        deadline, checked = time.time() + seconds, first
        while time.time() < deadline:
            page.wait_for_timeout(1000)
            while checked < len(seen):
                response = seen[checked]
                checked += 1
                body = response.body()
                try:
                    return json.loads(body)
                except ValueError:
                    print(f"    search response: HTTP {response.status}, {len(body)} bytes, not JSON")
        return None

    def captcha_cleared() -> bool:
        """Waits for the owner to solve a visible captcha (headed only). True if one was there and is gone."""
        global captchas
        if not captcha.first.is_visible():
            return False
        captchas += 1
        if not headed:
            print("    captcha shown — run with --headed so the owner can solve it")
            return False
        print(f"    CAPTCHA — please solve it in the browser window (waiting up to {CAPTCHA_WAIT_S // 60} min)")
        deadline = time.time() + CAPTCHA_WAIT_S
        while time.time() < deadline and captcha.first.is_visible():
            page.wait_for_timeout(2000)
        return not captcha.first.is_visible()

    for number, query in enumerate(QUERIES):
        if blocks >= MAX_BLOCKS:
            print(f"Stopping: {blocks} keywords in a row got nothing (performance.md).")
            break
        if number:
            time.sleep(random.uniform(5, 15))  # performance.md: between keywords
        ours: list[dict] = []
        for page_number in range(PAGES):
            started, first = time.time(), len(seen)
            if page_number == 0:
                page.goto(f"{SEARCH_URL}{quote(query)}", wait_until="domcontentloaded")
            else:
                time.sleep(random.uniform(2, 5))  # performance.md: between pages
                page.mouse.move(700, 600)  # over the results grid — the wheel scrolls whatever is under the pointer
                for _ in range(6):
                    page.mouse.wheel(0, 2500)
                    page.wait_for_timeout(700)
            payload = wait_for_payload(first, 20)
            if payload is None and captcha_cleared():
                first = len(seen)
                page.reload(wait_until="domcontentloaded")  # the page asks again once the captcha is gone
                payload = wait_for_payload(first, 30)
            if payload is None:
                shot = debug_dir / f"browser_{number}_{page_number}.png"
                page.screenshot(path=str(shot))
                print(f"{query!r} page {page_number}: no JSON search response — title {page.title()!r}, screenshot {shot}")
                pages.append({"label": {"keyword": query, "page": page_number, "scrapedAt": now()}, "error": "no JSON search response"})
                break
            items = videos(payload)
            ours += items
            pages.append({"label": {"keyword": query, "page": page_number, "scrapedAt": now(), "items": len(items)}, "payload": payload})
            print(f"{query!r} page {page_number}: {len(items)} items, has_more={payload.get('has_more')} ({time.time() - started:.1f}s)")
            if not payload.get("has_more"):
                break
        blocks = 0 if ours else blocks + 1

        # Compare with Apify on the same keyword: who found what, and how often each useful field is filled.
        ours_ids = {item.get("id") for item in ours}
        theirs = {item["id"]: item for item in apify.get(query, [])}
        row = {
            "keyword": query,
            "crawler": len(ours_ids), "apify": len(theirs), "both": len(ours_ids & theirs.keys()),
            "crawlerOnly": len(ours_ids - theirs.keys()), "apifyOnly": len(theirs.keys() - ours_ids),
            "crawlerFill": {
                "subtitles": sum(bool((item.get("video") or {}).get("subtitleInfos")) for item in ours),
                "location": sum(bool(item.get("poi")) for item in ours),
                "views": sum(bool((item.get("stats") or {}).get("playCount")) for item in ours),
            },
            "apifyFill": {
                "subtitles": sum(bool((item.get("videoMeta") or {}).get("subtitleLinks")) for item in theirs.values()),
                "location": sum(bool(item.get("locationMeta")) for item in theirs.values()),
                "views": sum(bool(item.get("playCount")) for item in theirs.values()),
            },
            "labels": {item_id: ("both" if item_id in theirs else "crawler-only") for item_id in ours_ids}
            | {item_id: "apify-only" for item_id in theirs.keys() - ours_ids},
        }
        comparison.append(row)
        print(f"  => {query!r}: crawler {row['crawler']} vs Apify {row['apify']} — both {row['both']}, "
              f"crawler-only {row['crawlerOnly']}, apify-only {row['apifyOnly']}")
        for field in ("subtitles", "location", "views"):
            print(f"     {field}: crawler {rate(row['crawlerFill'][field], len(ours))}, "
                  f"apify {rate(row['apifyFill'][field], len(theirs))}")
        print()
    context.close()

out = ROOT / "data" / "raw" / "tiktok-spike" / f"browser-{stamp}.json"
out.parent.mkdir(parents=True, exist_ok=True)
run_label = {"source": "data-crawler", "engine": "playwright-chromium", "session": session, "tab": "top" if top else "video",
             "headed": headed, "network": "owner PC, residential VN IP", "startedAt": stamp, "queries": QUERIES,
             "pagesPerKeyword": PAGES, "captchas": captchas}
tmp = out.with_suffix(".json.tmp")
tmp.write_text(json.dumps({"run": run_label, "pages": pages, "comparison": comparison}, ensure_ascii=False), encoding="utf-8")
tmp.replace(out)
print(f"captchas shown: {captchas}; raw + labels + comparison saved: {out}")
