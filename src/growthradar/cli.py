"""Command-line entrypoint: `python -m growthradar <url>`.

Wires argparse to the run orchestrator (GRO-18) and renders the report as
Markdown or JSON, to stdout or a file. Never lets an exception crash with a
raw traceback -- failures are reported with a clear message and a non-zero
exit code (AGENTS.md: "Fail gracefully with meaningful error messages").
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from growthradar.config import Config, ConfigError
from growthradar.event_log import configure_console_logging
from growthradar.orchestrator import run_growthradar_session
from growthradar.report import to_json, to_markdown


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="growthradar",
        description="Explore a SaaS product and score it as a UserGuiding prospect.",
    )
    parser.add_argument("url", help="Target URL to explore (e.g. https://example.com)")

    headless_group = parser.add_mutually_exclusive_group()
    headless_group.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=True,
        help="Run the browser headless (default)",
    )
    headless_group.add_argument(
        "--headful",
        dest="headless",
        action="store_false",
        help="Run the browser with a visible window",
    )

    parser.add_argument(
        "--max-pages", type=int, default=None, help="Override GROWTHRADAR_MAX_PAGES"
    )
    parser.add_argument(
        "--output",
        choices=("json", "markdown"),
        default="markdown",
        help="Report output format (default: markdown)",
    )
    parser.add_argument(
        "--output-file", type=Path, default=None, help="Write the report to this file"
    )
    parser.add_argument(
        "--no-registration",
        dest="attempt_registration",
        action="store_false",
        default=True,
        help="Skip attempting to register/sign up",
    )
    parser.add_argument("--run-id", default=None, help="Explicit run id (default: auto-generated)")
    parser.add_argument(
        "--log-level", default="INFO", help="Console log level, e.g. DEBUG/INFO/WARNING"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    configure_console_logging(args.log_level)

    try:
        config = Config.from_env()
        if args.max_pages is not None:
            config = replace(config, max_pages=args.max_pages)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        outcome = run_growthradar_session(
            args.url,
            config=config,
            headless=args.headless,
            attempt_registration=args.attempt_registration,
            run_id=args.run_id,
        )
    except Exception as exc:  # last-resort safety net; the orchestrator itself shouldn't raise
        print(f"GrowthRadar run failed: {exc}", file=sys.stderr)
        return 1

    rendered = to_json(outcome.report) if args.output == "json" else to_markdown(outcome.report)

    if args.output_file:
        args.output_file.write_text(rendered, encoding="utf-8")
        print(f"Report written to {args.output_file}")
    else:
        print(rendered)

    if outcome.errors:
        print("\nWarnings during run:", file=sys.stderr)
        for err in outcome.errors:
            print(f"  - {err}", file=sys.stderr)

    return 0
