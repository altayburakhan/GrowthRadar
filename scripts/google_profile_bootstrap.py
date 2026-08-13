"""One-time manual login for a persistent Google Chrome profile.

Usage:
    .venv/bin/python scripts/google_profile_bootstrap.py [profile_dir]

Opens a real, visible Google Chrome window (not Playwright's bundled
Chromium) at the given profile directory (default: GROWTHRADAR_GOOGLE_PROFILE_DIR
from .env) and navigates to accounts.google.com. Sign in there yourself --
this script never touches the login form, since Google's own 2FA/"verify
it's you" challenges need a real human and can't be scripted. Once signed
in, close the window; the session is saved in the profile directory and
every later automated run that points GROWTHRADAR_GOOGLE_PROFILE_DIR at the
same path reuses it (see browser.py's BrowserSession.start()).

Requires Google Chrome itself, not just Playwright's bundled Chromium:
    .venv/bin/python -m playwright install chrome

Run this again any time the session expires (Google eventually asks for
re-verification) or you want to switch which Google account is used.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

from growthradar.config import Config


def main() -> None:
    profile_dir = sys.argv[1] if len(sys.argv) > 1 else Config.from_env(".env").google_profile_dir
    if not profile_dir:
        print(
            "No profile directory given and GROWTHRADAR_GOOGLE_PROFILE_DIR isn't set.\n"
            "Usage: .venv/bin/python scripts/google_profile_bootstrap.py <profile_dir>"
        )
        sys.exit(1)

    print(f"profile dir: {profile_dir}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            profile_dir, channel="chrome", headless=False
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://accounts.google.com")
        input(
            "Sign in to the Google account you want GrowthRadar to use, then press "
            "Enter here to save the session and close the browser..."
        )
        context.close()

    print(f"Done. Set GROWTHRADAR_GOOGLE_PROFILE_DIR={profile_dir} in .env to use this profile.")


if __name__ == "__main__":
    main()
