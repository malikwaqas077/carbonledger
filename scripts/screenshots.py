"""Capture README screenshots of each dashboard tab. Run with the app on :8611."""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8611"
OUT = Path(__file__).resolve().parent.parent / "docs"
OUT.mkdir(exist_ok=True)
TABS = ["Portfolio", "Abatement curve", "Investment plan", "Design options", "Data & governance", "Ask the data"]

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    page = b.new_page(viewport={"width": 1500, "height": 1000})
    page.goto(URL)
    page.get_by_text("Signed in as").wait_for(timeout=120_000)
    page.wait_for_timeout(2500)
    for i, name in enumerate(TABS):
        page.get_by_role("tab", name=name).click()
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / f"tab{i}.png"), full_page=True)
        print("saved", name)
    b.close()
