"""One-time manual login for a persistent Google Chrome profile.

Usage:
    .venv/bin/python scripts/google_profile_bootstrap.py [profile_dir]

Launches a real Google Chrome window as an ordinary process (not through
Playwright) at the given profile directory (default:
GROWTHRADAR_GOOGLE_PROFILE_DIR from .env) and opens accounts.google.com. Sign
in there yourself -- this script never touches the login form, since
Google's own 2FA/"verify it's you" challenges need a real human and can't be
scripted. Once signed in, close the window; the session is saved in the
profile directory and every later automated run that points
GROWTHRADAR_GOOGLE_PROFILE_DIR at the same path reuses it (see browser.py's
BrowserSession.start()).

Deliberately NOT launched via Playwright: Playwright always starts Chrome
with automation flags (--enable-automation, a CDP debugging port, etc.), and
Google's sign-in page detects those and blocks with "This browser or app may
not be secure" -- true of `channel="chrome"` too, since that's still a
Playwright-controlled launch. This script starts Chrome exactly the way
double-clicking its icon would, so the actual sign-in step never looks
automated. The saved session is later reused by Playwright's
launch_persistent_context for the (already-authenticated) automated runs --
those never touch Google's login form, only "Continue with Google" consent
screens, which is a different, less-scrutinized step.

Run this again any time the session expires (Google eventually asks for
re-verification) or you want to switch which Google account is used.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from growthradar.config import Config

_CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium",
    "chromium-browser",
)


def _find_chrome() -> str:
    for name in _CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    raise SystemExit(
        "Could not find a Google Chrome/Chromium binary on PATH "
        f"(looked for: {', '.join(_CHROME_CANDIDATES)}). Install Chrome and try again."
    )


def main() -> None:
    profile_dir = sys.argv[1] if len(sys.argv) > 1 else Config.from_env(".env").google_profile_dir
    if not profile_dir:
        print(
            "No profile directory given and GROWTHRADAR_GOOGLE_PROFILE_DIR isn't set.\n"
            "Usage: .venv/bin/python scripts/google_profile_bootstrap.py <profile_dir>"
        )
        sys.exit(1)

    chrome = _find_chrome()
    profile_path = Path(profile_dir).expanduser().resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    print(f"chrome: {chrome}")
    print(f"profile dir: {profile_path}")
    print("Opening Chrome -- sign in, then just close the window when you're done.")
    subprocess.run(
        [chrome, f"--user-data-dir={profile_path}", "https://accounts.google.com"], check=False
    )

    print(f"Done. Set GROWTHRADAR_GOOGLE_PROFILE_DIR={profile_path} in .env to use this profile.")


if __name__ == "__main__":
    main()
