"""Realistic user identity, password, and workspace-name generation for
registration flows. Built from small curated word lists rather than a
third-party name-generation dependency -- output looks plausible without
representing (or depending on data about) real people.
"""

from __future__ import annotations

import random
import secrets
import string
from dataclasses import dataclass

_FIRST_NAMES = (
    "Alex",
    "Jordan",
    "Taylor",
    "Morgan",
    "Casey",
    "Riley",
    "Avery",
    "Jamie",
    "Cameron",
    "Drew",
    "Elliot",
    "Reese",
    "Sam",
    "Quinn",
    "Harper",
    "Rowan",
    "Skyler",
    "Blake",
    "Dana",
    "Emerson",
)

_LAST_NAMES = (
    "Carter",
    "Bennett",
    "Hayes",
    "Foster",
    "Coleman",
    "Reyes",
    "Simmons",
    "Patel",
    "Nguyen",
    "Kim",
    "Morrison",
    "Walsh",
    "Fischer",
    "Novak",
    "Sullivan",
    "Ellis",
    "Whitfield",
    "Marsh",
    "Bardot",
    "Okafor",
)

_COMPANY_WORDS = (
    "North",
    "Summit",
    "Bright",
    "Clear",
    "Swift",
    "Blue",
    "Silver",
    "Cedar",
    "Vertex",
    "Nimbus",
    "Harbor",
    "Anchor",
    "Compass",
    "Beacon",
    "Orbit",
    "Pioneer",
    "Alloy",
    "Crest",
    "Ember",
    "Ridge",
)

_COMPANY_SUFFIXES = (
    "Labs",
    "Works",
    "Studio",
    "Analytics",
    "Systems",
    "Group",
    "Software",
    "Digital",
)

_PASSWORD_SYMBOLS = "!@#$%^&*-_"

# Some signup forms ask for a date of birth (e.g. an age-gate or profile
# field) with no real-world stake in the specific value -- any plausible
# adult date satisfies it. Fixed rather than randomized: nothing needs it to
# vary per run, and a constant is one less thing to reason about when
# reading evidence later. ISO format fills a native <input type="date">
# correctly; text-based fields expecting another format are best-effort,
# same tradeoff as _select_option_best_effort's country matching.
_DEFAULT_DATE_OF_BIRTH = "1990-01-01"


@dataclass(frozen=True)
class Identity:
    first_name: str
    last_name: str
    email: str
    password: str
    company_name: str
    country: str
    date_of_birth: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


def generate_password(length: int = 16) -> str:
    """A cryptographically random password guaranteed to mix case, digits, symbols."""
    if length < 8:
        raise ValueError("password length must be at least 8")

    alphabet = string.ascii_letters + string.digits + _PASSWORD_SYMBOLS
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in _PASSWORD_SYMBOLS for c in password)
        ):
            return password


def generate_company_name() -> str:
    return f"{random.choice(_COMPANY_WORDS)} {random.choice(_COMPANY_SUFFIXES)}"


def _build_email(first: str, last: str, email_domain: str, email_override: str | None) -> str:
    """A random @example.com-style address by default (derived from the
    already-chosen `first`/`last` name), or -- when `email_override` (see
    `Config.registrant_email`) is set -- that exact address, used verbatim.
    Deliberately not plus-tagged: the caller wants every registration to use
    this one real, monitored inbox as-is, not a distinct variant per run --
    re-registering against the same target site with the same address can
    hit "email already in use" on a second attempt, which is an accepted
    tradeoff here rather than a bug.
    """
    if not email_override:
        tag = secrets.token_hex(3)
        return f"{first.lower()}.{last.lower()}.{tag}@{email_domain}"

    return email_override


def generate_identity(
    *,
    email_domain: str = "example.com",
    email_override: str | None = None,
    country: str = "United States",
    company_name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> Identity:
    """A fresh, plausible identity. Not deterministic -- every call differs,
    except `country`, `company_name`, `first_name`, and `last_name`, which a
    caller can pin (see `Config.registrant_country` / `Config.registrant_company`
    / `Config.registrant_first_name` / `Config.registrant_last_name`) when a
    signup flow needs consistent, real values -- e.g. a full name typed into
    a "Full name" field should read as one real, chosen person, not a random
    one picked per run -- rather than a random name/company each time.
    """
    first = first_name or random.choice(_FIRST_NAMES)
    last = last_name or random.choice(_LAST_NAMES)
    email = _build_email(first, last, email_domain, email_override)
    return Identity(
        first_name=first,
        last_name=last,
        email=email,
        password=generate_password(),
        company_name=company_name or generate_company_name(),
        country=country,
        date_of_birth=_DEFAULT_DATE_OF_BIRTH,
    )
