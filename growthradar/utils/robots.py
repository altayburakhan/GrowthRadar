from __future__ import annotations

import logging
import urllib.request
import urllib.robotparser as robotparser
from urllib.error import URLError
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


def load_robots(base_url: str, user_agent: str, timeout: float) -> robotparser.RobotFileParser:
    """Fetches and parses robots.txt for a domain. Defaults to allow-all if it
    cannot be fetched, matching robots.txt's own convention for a missing file."""
    parser = robotparser.RobotFileParser()
    robots_url = urljoin(base_url, "/robots.txt")
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read().decode("utf-8", errors="ignore")
        parser.parse(content.splitlines())
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        logger.info("Could not fetch robots.txt for %s (%s); defaulting to allow.", base_url, exc)
        parser.parse([])
    return parser


def can_fetch(parser: robotparser.RobotFileParser, user_agent: str, url: str) -> bool:
    try:
        return parser.can_fetch(user_agent, url)
    except Exception:
        return True
