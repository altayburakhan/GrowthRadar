from patchright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    context = browser.new_context()
    page = context.new_page()

    page.goto("https://app.clearstream.io/signup?step=info")
    page.wait_for_load_state("networkidle")
    page.screenshot(path="screenshot.png")
    browser.close()
