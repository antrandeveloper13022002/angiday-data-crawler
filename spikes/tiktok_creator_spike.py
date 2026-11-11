"""DC-08 spike: a creator's profile page — which call lists their videos, newest first? how many per page?

    python spikes/tiktok_creator_spike.py <username>

Headed, logged-in crawler profile (BR-ACC-01); read-only (navigate, scroll). Every list response is saved
unfiltered to data/raw/tiktok-spike/creator-<user>-<stamp>.json.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawler.sources import tiktok

LIST_API = "/api/post/item_list/"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    user = sys.argv[1]
    pages = []

    def keep(response):
        if LIST_API in response.url:
            try:
                payload = json.loads(response.body())
            except Exception as error:  # noqa: BLE001 — spike
                payload = {"error": str(error)}
            params = dict(parse_qsl(urlsplit(response.request.url).query))
            pages.append({"params": {k: params.get(k) for k in ("count", "cursor", "secUid", "coverFormat")}, "payload": payload})

    with sync_playwright() as playwright:
        context = tiktok.open_profile(playwright)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", keep)
        page.goto(f"https://www.tiktok.com/@{user}", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        if tiktok.wait_out_captcha(page):
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
        state = page.evaluate("() => { const s = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__'); return s ? s.textContent.length : 0 }")
        page.mouse.move(700, 600)
        for _ in range(2):  # one or two more pages
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(3000)
        context.close()
    print("rehydration state chars:", state)
    for entry in pages:
        items = entry["payload"].get("itemList") or []
        dates = [datetime.fromtimestamp(int(i["createTime"]), UTC).date().isoformat() for i in items if i.get("createTime")]
        pinned = [bool(i.get("isPinnedItem")) for i in items]
        print(f"cursor {entry['params']['cursor']} count {entry['params']['count']}: {len(items)} items, hasMore {entry['payload'].get('hasMore')}, "
              f"dates {dates[:6]}…{dates[-2:]}, pinned {sum(pinned)}")
    if pages and (pages[0]["payload"].get("itemList") or []):
        first = pages[0]["payload"]["itemList"][0]
        print("item keys:", sorted(first.keys())[:40])
    out = ROOT / "data" / "raw" / "tiktok-spike" / f"creator-{user}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    out.write_text(json.dumps({"user": user, "pages": pages}, ensure_ascii=False), encoding="utf-8")
    print("saved", out)


if __name__ == "__main__":
    main()
