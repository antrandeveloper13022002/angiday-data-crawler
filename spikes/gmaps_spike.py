"""DC-05 spike: what Google Maps gives a plain Playwright Chromium for a FoodReview search, and where the place data is.

    python spikes/gmaps_spike.py [--headed]

Opens the same search page Apify's compass~crawler-google-places opened (its `searchPageUrl`), keeps every search /
place response the page loads plus window.APP_INITIALIZATION_STATE, unfiltered, in data/raw/gmaps-spike/<stamp>.json.
Read-only: navigation only. No login, no Google account.
"""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "gmaps-spike"
APIFY = ROOT.parent / "FoodReview" / "data" / "raw" / "gmaps"
WANTED = ("/search?", "/maps/preview/place", "/maps/rpc/")


def apify_searches(count: int) -> list[dict]:
    """A few searches Apify ran, with what it found (rank 1..3), to compare against."""
    found: dict[str, dict] = {}
    for path in sorted(APIFY.glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            if item.get("searchPageUrl") and item.get("rank"):
                entry = found.setdefault(item["searchString"], {"url": item["searchPageUrl"], "apify": []})
                entry["apify"].append({k: item.get(k) for k in ("rank", "title", "placeId", "address", "location")})
    picked = [s for s, e in found.items() if len(e["apify"]) >= 3][:count - 1] + [
        s for s, e in found.items() if len(e["apify"]) == 1
    ][:1]
    return [{"search": s, **found[s]} for s in picked]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    headed = "--headed" in sys.argv
    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(locale="vi-VN", viewport={"width": 1280, "height": 900})
        page = context.new_page()
        for search in apify_searches(3):
            responses = []

            def keep(response, bucket=responses):
                if any(part in response.url for part in WANTED):
                    try:
                        bucket.append({"url": response.url, "status": response.status, "body": response.text()})
                    except Exception as error:  # noqa: BLE001 — spike: record and move on
                        bucket.append({"url": response.url, "status": response.status, "error": str(error)})

            page.on("response", keep)
            page.goto(search["url"], wait_until="domcontentloaded")
            time.sleep(6)
            state = page.evaluate("() => JSON.stringify(window.APP_INITIALIZATION_STATE || null)")
            links = page.eval_on_selector_all(
                "div[role=feed] div[jsaction]>a", "els => els.map(e => [e.getAttribute('aria-label'), e.href])"
            )
            page.remove_listener("response", keep)
            out.append({**search, "finalUrl": page.url, "title": page.title(), "feedLinks": links,
                        "appState": state, "responses": responses})
            print(f"{search['search']!r}: final {page.url[:90]}… feed {len(links)} links, "
                  f"{len(responses)} responses, appState {len(state or '')} chars")
            for rank in search["apify"][:3]:
                print(f"   apify #{rank['rank']}: {rank['title']} {rank['placeId']}")
            for label, href in links[:3]:
                print(f"   feed: {label} {href[:80]}")
            time.sleep(3)
        browser.close()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
