"""DC-07 spike 2: do search page URL parameters reach the search API (publish_time / sort_type)?

    python spikes/tiktok_recent_url_spike.py

Headed, logged-in crawler profile; one keyword, a few URL variants, paced. Each variant's request params and the
videos' posting dates are printed; payloads saved unfiltered to data/raw/tiktok-spike/recent-url-<stamp>.json.
"""

import json
import random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawler.sources import tiktok  # noqa: E402

KEYWORD = "ăn gì quận 1"
VARIANTS = ["", "&publish_time=7", "&publish_time=30&sort_type=0", "&sort_type=2", "&t=7"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    seen: list[dict] = []

    def keep(response):
        if tiktok.SEARCH_API in response.url:
            params = dict(parse_qsl(urlsplit(response.request.url).query))
            try:
                payload = json.loads(response.body())
            except Exception:  # noqa: BLE001 — spike
                payload = {}
            seen.append({"params": {k: v for k, v in params.items() if k in tiktok.SEARCH_PARAMS or k == "search_source"}, "payload": payload})

    out = []
    with sync_playwright() as playwright:
        context = tiktok.open_profile(playwright)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", keep)
        for variant in VARIANTS:
            seen.clear()
            url = tiktok.SEARCH_URL + quote(KEYWORD) + variant
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            if tiktok.wait_out_captcha(page):
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
            dates = sorted({datetime.fromtimestamp(int(i["createTime"]), UTC).date().isoformat()
                            for s in seen for i in (s["payload"].get("item_list") or []) if i.get("createTime")}, reverse=True)
            print(f"{variant or '(none)'}: page url {page.url[len(tiktok.SEARCH_URL):][:60]} | params {[s['params'] for s in seen][:2]}")
            print(f"   newest {dates[:4]} oldest {dates[-2:]} ({len(dates)} dates)")
            out.append({"variant": variant, "pageUrl": page.url, "responses": list(seen)})
            time.sleep(random.uniform(5, 10))
        context.close()
    path = ROOT / "data" / "raw" / "tiktok-spike" / f"recent-url-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps({"keyword": KEYWORD, "variants": out}, ensure_ascii=False), encoding="utf-8")
    print("saved", path)


if __name__ == "__main__":
    main()
