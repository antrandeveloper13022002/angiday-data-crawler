"""DC-05 spike 2: where price, review count and closed state live — place pages of places Apify saw with a price,
permanently closed or temporarily closed.

    python spikes/gmaps_place_spike.py

Saves every place / search response and APP_INITIALIZATION_STATE unfiltered to data/raw/gmaps-spike/<stamp>-place.json.
Read-only: navigation only, no Google account.
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


def samples() -> list[dict]:
    items = {}
    for path in sorted(APIFY.glob("*.json")):
        for item in json.loads(path.read_text(encoding="utf-8")):
            items.setdefault(item.get("placeId"), item)
    groups = {
        "price": lambda i: i.get("price") and "ĐÓNG" not in i["price"] and not i.get("permanentlyClosed"),
        "permanentlyClosed": lambda i: i.get("permanentlyClosed"),
        "temporarilyClosed": lambda i: i.get("temporarilyClosed"),
    }
    picked = []
    for name, test in groups.items():
        hits = [i for i in items.values() if i.get("placeId") and test(i)][:2]
        picked += [{"why": name, **{k: i.get(k) for k in ("title", "placeId", "url", "price", "reviewsCount",
                                                         "totalScore", "permanentlyClosed", "temporarilyClosed")}}
                   for i in hits]
    return picked


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(locale="vi-VN", viewport={"width": 1280, "height": 900}).new_page()
        for sample in samples():
            responses = []

            def keep(response, bucket=responses):
                if any(part in response.url for part in WANTED):
                    try:
                        bucket.append({"url": response.url, "status": response.status, "body": response.text()})
                    except Exception as error:  # noqa: BLE001 — spike: record and move on
                        bucket.append({"url": response.url, "status": response.status, "error": str(error)})

            page.on("response", keep)
            page.goto(sample["url"] + "&hl=vi", wait_until="domcontentloaded")
            time.sleep(6)
            state = page.evaluate("() => JSON.stringify(window.APP_INITIALIZATION_STATE || null)")
            text = page.inner_text("div[role=main]") if page.query_selector("div[role=main]") else ""
            page.remove_listener("response", keep)
            out.append({**sample, "finalUrl": page.url, "appState": state, "mainText": text, "responses": responses})
            print(f"{sample['why']}: {sample['title']} → {page.url[:80]}… {len(responses)} responses; "
                  f"main text {text[:160]!r}")
            time.sleep(3)
        browser.close()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-place.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"saved {path}")


if __name__ == "__main__":
    main()
