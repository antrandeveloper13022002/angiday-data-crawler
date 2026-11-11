"""DC-09 spike: does a plain Playwright Chromium (signed out, no proxy) get a GrabFood restaurant's menu?

    python spikes/grab_menu_spike.py [--headed] [--limit 12]

Opens the restaurant pages of the places FoodReview already matched (its data/raw/grab/matches.json), 5-10 s
apart, and keeps every JSON response plus the page's __NEXT_DATA__, unfiltered, in data/raw/grab-spike/<stamp>.json.
Read-only: navigate only — no account, cart or address (BR-SAFE-01).
"""

import argparse
import json
import random
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
APIFY = ROOT.parent / "FoodReview" / "data" / "raw" / "grab"  # read-only
OUT = ROOT / "data" / "raw" / "grab-spike"
PAGE_URL = "https://food.grab.com/vn/vi/restaurant/online-delivery/"
GRAB_ID = re.compile(r"\d-[A-Z0-9]{8,20}")
NEXT_DATA_JS = "() => document.getElementById('__NEXT_DATA__')?.textContent ?? null"


def apify_counts() -> dict[str, int | None]:
    counts = {}
    for path in APIFY.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data.get("items", []) if isinstance(data, dict) else []:
            counts[item.get("restaurantId")] = item.get("menuItemCount")
    return counts


def count_menu_items(value) -> int:
    """Largest list of dicts with a price-ish key anywhere in the value — a rough "where is the menu" probe."""
    best = 0
    if isinstance(value, dict):
        for child in value.values():
            best = max(best, count_menu_items(child))
    elif isinstance(value, list):
        if value and all(isinstance(x, dict) for x in value) and any("price" in str(list(x)).lower() for x in value):
            best = len(value)
        for child in value:
            best = max(best, count_menu_items(child))
    return best


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    matches = json.loads((APIFY / "matches.json").read_text(encoding="utf-8"))
    targets = [(slug, m["restaurantId"]) for slug, m in matches.items() if m["restaurantId"]][: args.limit]
    apify = apify_counts()
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="vi-VN", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        for number, (slug, grab_id) in enumerate(targets):
            if not GRAB_ID.fullmatch(grab_id):
                print(f"skip odd id {grab_id!r}")
                continue
            if number:
                time.sleep(random.uniform(5, 10))
            responses = []

            def keep(response, responses=responses):
                if "json" in (response.headers.get("content-type") or ""):
                    responses.append(response)

            page.on("response", keep)
            error = None
            try:
                page.goto(PAGE_URL + grab_id, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(6000)
            except PlaywrightError as caught:
                error = str(caught).splitlines()[0]
            page.remove_listener("response", keep)
            calls = []
            for response in responses:
                try:
                    body = response.json()
                except (PlaywrightError, ValueError):
                    body = None
                parts = urlsplit(response.url)
                calls.append({"host": parts.hostname, "path": parts.path, "status": response.status, "body": body})
            try:
                next_raw = page.evaluate(NEXT_DATA_JS)
            except PlaywrightError:
                next_raw = None
            next_data = json.loads(next_raw) if next_raw else None
            menu_calls = [(c["path"], count_menu_items(c["body"])) for c in calls if count_menu_items(c["body"])]
            row = {
                "slug": slug,
                "restaurantId": grab_id,
                "finalUrl": page.url,
                "title": page.title(),
                "error": error,
                "jsonCalls": [(c["host"], c["path"], c["status"]) for c in calls],
                "menuCalls": menu_calls,
                "nextDataMenu": count_menu_items(next_data),
                "apifyMenuItemCount": apify.get(grab_id),
            }
            results.append({"summary": row, "calls": calls, "nextData": next_data})
            print(
                f"{slug[:40]:<40} {grab_id} title={row['title'][:40]!r} err={error} json={len(calls)} "
                f"menuCalls={menu_calls} nextData={row['nextDataMenu']} apify={row['apifyMenuItemCount']}"
            )
        browser.close()
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{stamp}.json"
    out.write_text(json.dumps({"at": stamp, "headed": args.headed, "results": results}, ensure_ascii=False), "utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
