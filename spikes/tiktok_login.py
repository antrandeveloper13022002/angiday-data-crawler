"""DC-01: the owner logs the crawler's browser profile into the secondary TikTok account (BR-ACC-01).

    cd E:\\an\\data-crawler
    .venv\\Scripts\\python -I spikes\\tiktok_login.py

Opens Playwright's own Chromium as a normal, non-automated window on the crawler's profile
(data/browser-profile/tiktok — git-ignored, never the owner's browser) at TikTok's login page. The owner logs in by
hand and closes the window; nothing here types, clicks or reads the password. Afterwards it only checks that a
TikTok session cookie exists in the profile — by name, never printing a value.
"""

import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
PROFILE = Path(__file__).resolve().parents[1] / "data" / "browser-profile" / "tiktok"
PROFILE.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    chromium = playwright.chromium.executable_path

print("Cửa sổ Chromium đang mở trang đăng nhập TikTok. Đăng nhập bằng TÀI KHOẢN PHỤ, rồi đóng cửa sổ.")
subprocess.run([chromium, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                "https://www.tiktok.com/login"], check=False)

with sync_playwright() as playwright:
    context = playwright.chromium.launch_persistent_context(str(PROFILE), channel="chromium", headless=True)
    names = {cookie["name"] for cookie in context.cookies("https://www.tiktok.com")}
    context.close()
print("Đã đăng nhập — phiên được lưu trong profile của crawler." if "sessionid" in names
      else "Chưa thấy phiên đăng nhập. Chạy lại script và đăng nhập trước khi đóng cửa sổ.")
