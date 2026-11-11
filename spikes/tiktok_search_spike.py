"""DC-01 spike: does the vendored TikTok search answer from this machine's IP, and how does it compare with Apify?

    cd E:\\an\\data-crawler
    python -I spikes\\tiktok_search_spike.py [path to FoodReview, default ..\\FoodReview]

Saves every raw payload to data/raw/tiktok-spike/<UTC stamp>.json (unfiltered, BR-RAW-01), then prints, per query and
region: items per page, has_more, overlap with Apify's ids for the same query, and which fields the items carry.
Throwaway: deleted when DC-01 closes.
"""

import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
FOODREVIEW = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "FoodReview"
sys.path.insert(0, str(ROOT / "vendor" / "tiktok-scraper"))

from tiktok import search  # noqa: E402  (vendored; path set above)

QUERIES = ["review quán ăn nha trang", "ăn gì đà lạt", "quán ngon nha trang"]
REGIONS = ["vn", "us"]
PAGES = 2

apify: dict[str, set[str]] = {}
for path in (FOODREVIEW / "data" / "raw" / "tiktok").glob("*.json"):
    for item in json.loads(path.read_text(encoding="utf-8"))["items"]:
        if item.get("searchQuery") and item.get("id"):
            apify.setdefault(item["searchQuery"], set()).add(item["id"])

stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
pages = []
for query in QUERIES:
    for region in REGIONS:
        cursor, ids = None, []
        for page in range(PAGES):
            time.sleep(random.uniform(1, 3))  # performance.md pacing
            started = time.time()
            try:
                payload, cursor = search._search(
                    "/api/search/item/full/", query, cursor, countries=(region,), extra={"count": 20}
                )
            except Exception as error:  # noqa: BLE001 — a spike reports every failure and moves on
                print(f"{query!r} [{region}] page {page}: {type(error).__name__}: {error}")
                pages.append({"query": query, "region": region, "page": page, "error": f"{type(error).__name__}: {error}"})
                break
            items = payload.get("item_list") or []
            ids += [item.get("id") for item in items]
            pages.append({"query": query, "region": region, "page": page, "payload": payload})
            fields = {
                "subtitles": sum(bool((item.get("video") or {}).get("subtitleInfos")) for item in items),
                "poi": sum(bool(item.get("poi")) for item in items),
                "stats": sum(bool(item.get("stats") or item.get("statsV2")) for item in items),
            }
            print(
                f"{query!r} [{region}] page {page}: {len(items)} items, has_more={payload.get('has_more')}, "
                f"{time.time() - started:.1f}s, with subtitles/poi/stats {fields}"
            )
            if not cursor:
                break
        known = apify.get(query, set())
        print(f"  => {query!r} [{region}]: {len(ids)} ids, {len(set(ids) & known)}/{len(known)} of Apify's also found\n")

out = ROOT / "data" / "raw" / "tiktok-spike" / f"{stamp}.json"
out.parent.mkdir(parents=True, exist_ok=True)
tmp = out.with_suffix(".json.tmp")
tmp.write_text(json.dumps({"run": {"at": stamp, "queries": QUERIES, "regions": REGIONS, "pages": PAGES}, "pages": pages}, ensure_ascii=False), encoding="utf-8")
tmp.replace(out)
print("raw saved:", out)
