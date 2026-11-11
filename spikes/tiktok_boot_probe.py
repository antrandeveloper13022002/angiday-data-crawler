"""DC-01 probe: what does TikTok send back when the vendored boot (/@tiktok) finds no page data?

    cd E:\\an\\data-crawler
    python -I spikes\\tiktok_boot_probe.py [path ...]   (default: /@tiktok)

One request per path, made exactly like the vendored client's boot. Prints only safe facts (status, final URL path,
title, size, markers, script ids) — never cookies — and keeps the HTML in data/debug/ (git-ignored; delete after use).
Throwaway: deleted when DC-01 closes.
"""

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "tiktok-scraper"))

from tiktok import fetch  # noqa: E402  (vendored; path set above)

MARKERS = ["__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE", "captcha", "verify", "wafchallenge", "_wafchallengeid",
           "login", "not available", "unavailable", "slardar", "webmssdk"]

debug_dir = ROOT / "data" / "debug"
debug_dir.mkdir(parents=True, exist_ok=True)
for number, path in enumerate(sys.argv[1:] or ["/@tiktok"]):
    client = fetch._Client("vn")
    resp = client.session.get(fetch.WWW + path, headers=fetch.PAGE_HEADERS, timeout=fetch.PAGE_TIMEOUT, allow_redirects=True)
    html = resp.text or ""
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    print(f"{path}: HTTP {resp.status_code}, final path {resp.url.split('tiktok.com', 1)[-1][:80]!r}, {len(html)} chars")
    print(f"  title: {(title.group(1).strip() if title else None)!r}")
    print(f"  markers: {[marker for marker in MARKERS if marker.lower() in html.lower()]}")
    print(f"  script ids: {re.findall(r'<script[^>]+id=\"([^\"]+)\"', html)[:15]}")
    print(f"  content-type: {resp.headers.get('content-type')}, server: {resp.headers.get('server')}")
    out = debug_dir / f"boot_probe_{number}.html"
    out.write_text(html, encoding="utf-8")
    print(f"  html kept: {out}")
    client.close()
