from __future__ import annotations

import argparse
import sys

from growthradar.config import settings
from growthradar.core.pipeline import analyze_company
from growthradar.reporting.formatters import FORMATTERS
from growthradar.storage.repository import get_latest_for_domain, list_results, save_result
from growthradar.utils.logging import configure_logging


def _cmd_analyze(args: argparse.Namespace) -> int:
    result, evidence = analyze_company(args.url)
    if not args.no_store:
        save_result(settings.db_path, result, evidence)
    print(FORMATTERS[args.output](result))
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as fh:
        urls = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

    exit_code = 0
    for url in urls:
        try:
            result, evidence = analyze_company(url)
        except Exception as exc:  # noqa: BLE001 -- one bad company must not abort the batch
            print(f"[error] {url}: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        if not args.no_store:
            save_result(settings.db_path, result, evidence)
        print(FORMATTERS[args.output](result))
        print("-" * 60)
    return exit_code


def _cmd_list(args: argparse.Namespace) -> int:
    for result in list_results(settings.db_path, tier=args.tier, limit=args.limit):
        print(FORMATTERS["table"](result))
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    result = get_latest_for_domain(settings.db_path, args.domain)
    if result is None:
        print(f"No stored results for domain '{args.domain}'.", file=sys.stderr)
        return 1
    print(FORMATTERS[args.output](result))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="growthradar", description="AI Growth Intelligence platform for UserGuiding.")
    parser.add_argument("--log-level", default=settings.log_level)
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze a single company website.")
    analyze_parser.add_argument("url", help="Company domain or URL, e.g. example.com")
    analyze_parser.add_argument("--output", choices=list(FORMATTERS), default="table")
    analyze_parser.add_argument("--no-store", action="store_true", help="Don't persist the result to the database.")
    analyze_parser.set_defaults(func=_cmd_analyze)

    batch_parser = subparsers.add_parser("batch", help="Analyze a file of company URLs (one per line).")
    batch_parser.add_argument("file", help="Path to a text file with one domain/URL per line.")
    batch_parser.add_argument("--output", choices=list(FORMATTERS), default="table")
    batch_parser.add_argument("--no-store", action="store_true")
    batch_parser.set_defaults(func=_cmd_batch)

    list_parser = subparsers.add_parser("list", help="List previously scored leads.")
    list_parser.add_argument("--tier", choices=["hot", "warm", "cold", "excluded"], default=None)
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(func=_cmd_list)

    show_parser = subparsers.add_parser("show", help="Show the latest stored result for a domain.")
    show_parser.add_argument("domain", help="Domain as stored, e.g. example.com")
    show_parser.add_argument("--output", choices=list(FORMATTERS), default="markdown")
    show_parser.set_defaults(func=_cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
