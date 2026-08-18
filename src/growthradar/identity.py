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

# Real-looking US area codes to vary the generated phone number's prefix;
# the exchange+subscriber block below is always 555-01XX, the range every
# North American telecom permanently reserves as never assigned to a real
# subscriber (used in film/TV for exactly this reason) -- so this can never
# collide with an actual phone number regardless of which area code lands.
_PHONE_AREA_CODES = ("202", "212", "305", "312", "404", "415", "512", "617", "702", "818")


@dataclass(frozen=True)
class Identity:
    first_name: str
    last_name: str
    email: str
    password: str
    company_name: str
    country: str
    date_of_birth: str
    phone: str
    website: str

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


def generate_phone() -> str:
    """A plausible-looking, never-real US phone number (see _PHONE_AREA_CODES).

    No "+1" country code prefix -- a signup form's own client-side validator
    commonly strips non-digit characters and checks for exactly 10 digits
    (seen live on shippingeasy.com: "Please enter a 10 digit US phone
    number"), and a leading "+1" pushes the stripped digit count to 11,
    failing that check on every single run.
    """
    area = random.choice(_PHONE_AREA_CODES)
    subscriber = random.randint(100, 199)
    return f"{area}-555-0{subscriber}"


def generate_website(company_name: str) -> str:
    """A syntactically valid URL under example.com/.org/.net -- the TLDs
    IANA permanently reserves for documentation and testing, so this never
    resolves to (or gets mistaken for) a real, unrelated site."""
    slug = "".join(ch for ch in company_name.lower() if ch.isalnum()) or "company"
    return f"https://{slug}.example.com"


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
    password_override: str | None = None,
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

    `password_override` (see `Config.registrant_password`) pins the password
    too, same reasoning: a fresh random one every run (the old default) is
    never recorded anywhere -- not in Evidence, not in a log -- specifically
    so a secret never lands in the database or on disk, but that also means
    nobody could ever look up what password a given run actually used to
    manually check the created account. A known, fixed password makes every
    run's account log-in-able after the fact, at the accepted cost that all
    such accounts share one password.
    """
    first = first_name or random.choice(_FIRST_NAMES)
    last = last_name or random.choice(_LAST_NAMES)
    email = _build_email(first, last, email_domain, email_override)
    resolved_company_name = company_name or generate_company_name()
    return Identity(
        first_name=first,
        last_name=last,
        email=email,
        password=password_override or generate_password(),
        company_name=resolved_company_name,
        country=country,
        date_of_birth=_DEFAULT_DATE_OF_BIRTH,
        phone=generate_phone(),
        website=generate_website(resolved_company_name),
    )
