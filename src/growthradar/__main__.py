"""Allows `python -m growthradar <url>`."""

import sys

from growthradar.cli import main

if __name__ == "__main__":
    sys.exit(main())
