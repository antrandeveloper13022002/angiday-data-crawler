"""DC-07 spike: how TikTok's search page filters by posting date, and what it sends when it does.

    python spikes/tiktok_recent_spike.py probe            find the filter control (screenshot + candidates)
    python spikes/tiktok_recent_spike.py pick "<label>"   open the filter, click the option with that text

Headed, on the crawler's logged-in profile (BR-ACC-01); read-only: it only navigates and opens the filter.
Every search request's params and every payload are saved unfiltered to data/raw/tiktok-spike/recent-<stamp>.json,
screenshots next to it. A captcha waits for the owner (crawler.sources.tiktok.wait_out_captcha).
"""

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from crawler.sources import tiktok  # noqa: E402

OUT = ROOT / "data" / "raw" / "tiktok-spike"
KEYWORD = "ăn gì quận 1"
CANDIDATES_JS = """() => [...document.querySelectorAll('button, [role=button], [data-e2e], [aria-label], svg')]
  .filter((el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top < 140; })
  .filter((el) => !/^(search-card|new-desc|video-)/.test(el.getAttribute('data-e2e') || ''))
  .map((el) => ({ tag: el.tagName, e2e: el.getAttribute('data-e2e'), aria: el.getAttribute('aria-label'),
                  text: (el.innerText || '').trim().slice(0, 40) }))
"""


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    mode, label = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    search_url = "https://www.tiktok.com/search?q=" if mode.startswith("top") else tiktok.SEARCH_URL
    stamp = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    OUT.mkdir(parents=True, exist_ok=True)
    requests, payloads = [], []

    def keep(response):
        if tiktok.SEARCH_API in response.url:
            params = dict(parse_qsl(urlsplit(response.request.url).query))
            requests.append({k: v for k, v in params.items() if k in tiktok.SEARCH_PARAMS or k == "search_source"})
            try:
                payloads.append(json.loads(response.body()))
            except Exception as error:  # noqa: BLE001 — spike: record and move on
                payloads.append({"error": str(error)})

    with sync_playwright() as playwright:
        context = tiktok.open_profile(playwright)
        page = context.pages[0] if context.pages else context.new_page()
        page.on("response", keep)
        page.goto(search_url + quote(KEYWORD), wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        if tiktok.wait_out_captcha(page):
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
        page.screenshot(path=str(OUT / f"recent-{stamp}-1.png"))
        found = page.evaluate(CANDIDATES_JS)
        print("filter candidates:", json.dumps(found, ensure_ascii=False, indent=1)[:3000])
        if mode.endswith("pick"):
            opener = page.locator("[data-e2e*='filter' i], button:has-text('Bộ lọc'), button:has-text('Lọc')").first
            opener.click()
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / f"recent-{stamp}-2.png"))
            panel = page.evaluate(CANDIDATES_JS)
            print("after opening:", json.dumps(panel, ensure_ascii=False, indent=1)[:3000])
            if label:
                page.get_by_text(label, exact=True).first.click()
                page.wait_for_timeout(1500)
                apply = page.get_by_role("button", name="Áp dụng")
                if apply.count():
                    apply.first.click()
                page.wait_for_timeout(6000)
                page.screenshot(path=str(OUT / f"recent-{stamp}-3.png"))
        time.sleep(1)
        context.close()
    dates = []
    for payload in payloads:
        for item in payload.get("item_list") or [] if isinstance(payload, dict) else []:
            if item.get("createTime"):
                dates.append(datetime.fromtimestamp(int(item["createTime"]), UTC).date().isoformat())
    out = OUT / f"recent-{stamp}.json"
    out.write_text(json.dumps({"keyword": KEYWORD, "mode": mode, "label": label, "requests": requests,
                               "payloads": payloads}, ensure_ascii=False), encoding="utf-8")
    print("requests:", json.dumps(requests, ensure_ascii=False))
    print("video dates:", sorted(dates, reverse=True)[:30])
    print("saved", out)


if __name__ == "__main__":
    main()
