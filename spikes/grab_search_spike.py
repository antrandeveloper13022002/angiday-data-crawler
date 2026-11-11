"""DC-10 spike: can a headed Chromium, signed out, find a place on GrabFood by name around its coordinates?

    python spikes/grab_search_spike.py

The search page searches around Grab's own `location` cookie (by IP otherwise; browser geolocation and URL params are
ignored), so the cookie is set to the place's coordinates first — the one exception in security.md (owner,
2026-11-11). Cases: the 10 places FoodReview's APP-42 matched by search on Apify, with the id it found. Every search
answer is kept, unfiltered, in data/raw/grab-spike/search-<stamp>.json.
"""

import json
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, unquote

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "raw" / "grab-spike"
SEARCH_API = "/foodweb/guest/v2/search"
CASES = [  # restaurant id APP-42 matched, place lat, lng, the search term it used
    ("5-C75WJN2JA4KFPA", 20.992679, 105.799252, "Aha Food"),
    ("5-C6KUTK3TVUNKL2", 10.744291, 106.634924, "Quán 220 BÚN MẮM - BÚN THÁI"),
    ("5-C6CWC33TPB62RX", 10.033902, 105.7854578, "Đồng Khởi Food & Drink"),
    ("5-C4NTTFTWGRBXTX", 10.769732, 106.684352, "A Bowl Of Noodle - Mì Bò Đài Loan"),
    ("5-C8DTJNTTJN4ZJT", 10.833706, 106.689434, "Ăn Vặt Báo"),
    ("5-C7KZSANBVBMHGT", 10.761441, 106.700665, "Ăn Vặt Chóp Chép"),
    ("5-C73HV8CZAZD3HA", 10.7521996, 106.6652712, "138 Asian Cuisine"),
    ("5-C8JUMEU1KBB3WE", 21.038474, 105.785753, "Ăn vặt Hiệp Gà"),
    ("5-C8D1GUXXPFKATX", 16.068743, 108.201679, "Quán Ăn Bà Hồng"),
    ("5-C8BBWB3JCZAFEN", 10.82873, 106.77437, "Ăn Vặt Heo Thỳ"),
]


def main() -> None:
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context(locale="vi-VN", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.goto("https://food.grab.com/vn/vi/restaurants", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        cookie = next(c for c in context.cookies() if c["name"] == "location")  # Grab's own, by IP
        base = json.loads(unquote(cookie["value"]))
        keep = ("domain", "path", "secure", "httpOnly", "sameSite", "expires")
        for number, (grab_id, lat, lng, term) in enumerate(CASES):
            if number:
                time.sleep(random.uniform(5, 10))
            # an empty address makes the page fall back to its IP location (spike run 1): any text will do
            location = {**base, "latitude": lat, "longitude": lng, "isAccurate": True, "address": f"{lat},{lng}"}
            context.clear_cookies(name="location")
            value = quote(json.dumps(location, separators=(",", ":")))
            context.add_cookies([{**{k: cookie[k] for k in keep}, "name": "location", "value": value}])
            answers = []

            def collect(response, answers=answers):
                if response.url.endswith(SEARCH_API):
                    try:
                        answers.append({"sent": json.loads(response.request.post_data or "{}"), "body": response.json()})
                    except ValueError:
                        answers.append({"sent": None, "body": None, "status": response.status})

            page.on("response", collect)
            page.goto("https://food.grab.com/vn/vi/restaurants?search=" + quote(term), wait_until="domcontentloaded")
            page.wait_for_timeout(7000)
            page.remove_listener("response", collect)
            hits = [
                m
                for a in answers
                for m in ((a.get("body") or {}).get("searchResult") or {}).get("searchMerchants") or []
            ]
            unique = list({m["id"]: m for m in hits}.values())
            rank = next((i for i, m in enumerate(unique) if m["id"] == grab_id), None)
            distance = (unique[rank].get("merchantBrief") or {}).get("distanceInKm") if rank is not None else None
            sent = next((a["sent"]["latlng"] for a in answers if a.get("sent")), None)
            summary = {"term": term, "expected": grab_id, "sentLatlng": sent, "hits": len(unique), "rank": rank,
                       "distanceKm": distance, "top": [(m["id"], m["address"]["name"]) for m in unique[:3]]}
            results.append({"summary": summary, "answers": answers})
            print(f"{term[:32]:<32} sent={sent} hits={len(unique):>3} rank={rank} dist={distance}")
        browser.close()
    found = sum(r["summary"]["rank"] is not None for r in results)
    print(f"found {found}/{len(CASES)}")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"search-{stamp}.json"
    out.write_text(json.dumps({"at": stamp, "results": results}, ensure_ascii=False), encoding="utf-8")
    print(f"saved {out}")


if __name__ == "__main__":
    main()
