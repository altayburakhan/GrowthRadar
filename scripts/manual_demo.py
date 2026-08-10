"""Manual smoke test for the browser engine + structured logger built so far.

Usage:
    .venv/bin/python scripts/manual_demo.py [URL]

Opens a real (visible) browser window, navigates to URL (default: a public SaaS
homepage), dismisses cookie banners if present, and writes a structured run log to
logs/. Not part of any GRO task -- just a manual way to see GRO-6/GRO-7 working.
"""

from __future__ import annotations

import sys

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.event_log import RunLogger, configure_console_logging


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.userguiding.com"

    configure_console_logging("INFO")
    config = Config.from_env(".env")
    run_logger = RunLogger(log_dir="logs")

    print(f"run_id: {run_logger.run_id}")
    print(f"log file: {run_logger.path}")

    with BrowserSession(config, headless=False) as session:
        run_logger.action("goto", url=url)
        ok = session.goto(url)
        run_logger.page_visited(url, title=session.page.title() if session.page else None)

        if not ok:
            run_logger.error(f"failed to load {url}")
        else:
            run_logger.discovery(
                f"page loaded with {len(session.dialog_log)} dialog(s), "
                f"{len(session.extra_pages)} extra tab(s)"
            )

        input("Browser is open -- press Enter to close it...")

    print("\n--- log entries ---")
    for entry in run_logger.read_all():
        print(f"{entry.seq:>3} {entry.event_type.value:<14} {entry.message}")


if __name__ == "__main__":
    main()
