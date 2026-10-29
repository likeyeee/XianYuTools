"""为使用手册截图：python tools/capture_screens.py [http://127.0.0.1:8765]"""
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parent.parent / "docs" / "images"
SHOTS = [
    ("01-dashboard", "#/dashboard", None),
    ("02-accounts", "#/accounts", None),
    ("03-items", "#/items", None),
    ("04-orders", "#/orders", None),
    ("05-ship", "#/orders", "button[data-ship]"),
    ("06-im", "#/im", ".conv"),
    ("07-refund", "#/refunds", "button[data-d]"),
    ("08-tasks", "#/tasks", None),
    ("09-settings", "#/settings?tab=rules", None),
]


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="chrome")
        page = await b.new_page(viewport={"width": 1366, "height": 800})
        await page.goto(BASE + "/")
        await page.wait_for_timeout(1500)
        for name, route, click in SHOTS:
            await page.goto(BASE + "/" + route)
            await page.wait_for_function("() => !document.querySelector('#view .loading, #convs .loading')", timeout=30000)
            if click:
                await page.locator(click).first.click()
                target = ".bubble" if route == "#/im" else "#modal:not(.hidden)"
                await page.wait_for_selector(target, timeout=30000)
                await page.wait_for_timeout(600)
            await page.screenshot(path=str(OUT / f"{name}.png"))
            await page.keyboard.press("Escape")
            await page.evaluate("() => document.getElementById('modal').classList.add('hidden')")
            print("saved", name)
        await b.close()

asyncio.run(main())
