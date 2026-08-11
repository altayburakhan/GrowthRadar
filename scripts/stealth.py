from playwright.sync_api import sync_playwright
from playwright.stealth import stealth_sync

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context()
    page = context.new_page()

    stealth_sync(page)
    page.goto("https://app.clearstream.io/signup?step=info")
    page.wait_for_load_state("networkidle")
    page.screenshot(path="screenshot.png")
    browser.close()