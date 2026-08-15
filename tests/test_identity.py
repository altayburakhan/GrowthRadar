import re
import string

import pytest

from growthradar.identity import (
    generate_company_name,
    generate_identity,
    generate_password,
    generate_phone,
    generate_website,
)


def test_generate_identity_produces_plausible_fields() -> None:
    identity = generate_identity()

    assert identity.first_name
    assert identity.last_name
    assert "@" in identity.email
    assert identity.email.startswith(identity.first_name.lower())
    assert len(identity.company_name.split()) == 2
    assert identity.full_name == f"{identity.first_name} {identity.last_name}"


def test_generate_identity_is_not_deterministic() -> None:
    identities = {generate_identity().email for _ in range(20)}
    assert len(identities) == 20


def test_generate_identity_respects_email_domain() -> None:
    identity = generate_identity(email_domain="tempmail.example")
    assert identity.email.endswith("@tempmail.example")


def test_generate_identity_defaults_country_to_united_states() -> None:
    identity = generate_identity()
    assert identity.country == "United States"


def test_generate_identity_respects_country_override() -> None:
    identity = generate_identity(country="Turkey")
    assert identity.country == "Turkey"


def test_generate_identity_respects_company_name_override() -> None:
    identity = generate_identity(company_name="Acme Analytics")
    assert identity.company_name == "Acme Analytics"


def test_generate_identity_uses_email_override_verbatim() -> None:
    identity = generate_identity(email_override="levent@userguidingnow.com")
    assert identity.email == "levent@userguidingnow.com"


def test_generate_identity_email_override_without_at_sign_used_verbatim() -> None:
    identity = generate_identity(email_override="not-an-email")
    assert identity.email == "not-an-email"


def test_generate_identity_respects_first_and_last_name_override() -> None:
    identity = generate_identity(first_name="Levent", last_name="Aksan")
    assert identity.first_name == "Levent"
    assert identity.last_name == "Aksan"
    assert identity.full_name == "Levent Aksan"


def test_generate_identity_name_override_used_in_default_email() -> None:
    identity = generate_identity(first_name="Levent", last_name="Aksan")
    assert identity.email.startswith("levent.aksan.")


def test_generate_identity_has_a_date_of_birth() -> None:
    identity = generate_identity()
    assert identity.date_of_birth


def test_generate_phone_matches_reserved_fictional_block() -> None:
    # <area>-555-01XX -- the block North American telecom permanently
    # reserves as never assigned to a real subscriber. No "+1" country-code
    # prefix -- see generate_phone's docstring for why.
    for _ in range(20):
        phone = generate_phone()
        assert re.fullmatch(r"\d{3}-555-01\d{2}", phone)
        assert sum(c.isdigit() for c in phone) == 10


def test_generate_website_builds_url_from_company_name() -> None:
    website = generate_website("Acme Analytics")
    assert website == "https://acmeanalytics.example.com"


def test_generate_website_falls_back_when_company_name_has_no_alnum_chars() -> None:
    website = generate_website("!!!")
    assert website == "https://company.example.com"


def test_generate_identity_has_phone_and_website() -> None:
    identity = generate_identity(company_name="Acme Analytics")
    assert sum(c.isdigit() for c in identity.phone) == 10
    assert identity.website == "https://acmeanalytics.example.com"


def test_generate_password_meets_complexity_requirements() -> None:
    password = generate_password()

    assert len(password) == 16
    assert any(c.islower() for c in password)
    assert any(c.isupper() for c in password)
    assert any(c.isdigit() for c in password)
    assert any(c in "!@#$%^&*-_" for c in password)
    assert all(c in string.ascii_letters + string.digits + "!@#$%^&*-_" for c in password)


def test_generate_password_is_not_deterministic() -> None:
    passwords = {generate_password() for _ in range(20)}
    assert len(passwords) == 20


def test_generate_password_rejects_too_short_length() -> None:
    with pytest.raises(ValueError):
        generate_password(length=4)


def test_generate_company_name_is_two_words() -> None:
    name = generate_company_name()
    assert len(name.split()) == 2
