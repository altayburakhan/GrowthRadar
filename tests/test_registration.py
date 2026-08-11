import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from playwright.sync_api import Page

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.event_log import RunLogger
from growthradar.evidence import EvidenceStore
from growthradar.registration import open_registration_entry_point, run_registration
from growthradar.temp_email import TempInbox

_MULTI_STEP_FORM = """
<html><body>
<div id="step1">
  <input name="email" type="email" placeholder="Email" />
  <input name="password" type="password" placeholder="Password" />
  <button onclick="
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
  ">Continue</button>
</div>
<div id="step2" style="display:none">
  <input name="company" placeholder="Company name" />
  <button onclick="
    document.getElementById('step2').style.display='none';
    document.body.insertAdjacentHTML('beforeend', '<div id=success>Registered!</div>');
  ">Create account</button>
</div>
</body></html>
"""

_SINGLE_STEP_FORM = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""

_FORM_WITH_UNMAPPED_REQUIRED_SELECT = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<select name="hdyfu">
  <option value="" disabled selected>-</option>
  <option value="recommendation">A recommendation</option>
  <option value="exhibition">An exhibition or show</option>
</select>
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""

_FORM_WITH_OAUTH_BUTTON = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="document.title='oauth-clicked';">Continue with Google</button>
<button onclick="document.title='submitted';">Continue</button>
</body></html>
"""

_FORM_WITH_COUNTRY_DROPDOWN = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<select name="country">
  <option value="">Select a country</option>
  <option value="TR">Turkey</option>
  <option value="US">United States of America</option>
</select>
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""

_FORM_WITH_INSERTED_WORD_BUTTON = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="document.title='submitted';">Start my free trial</button>
</body></html>
"""

_FORM_WITH_CONSENT_CHECKBOXES = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<label><input type="checkbox" id="terms" />
  I have read and accept the Terms and Conditions and Privacy Policy.</label>
<label><input type="checkbox" id="marketing" />
  Happy to receive email marketing about our product? (Opt-in)</label>
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""

# Mimics a Radix/shadcn-style checkbox: the real clickable control is a
# role="checkbox" button with aria-checked; the native input is a
# pointer-events:none shadow purely for form submission.
_FORM_WITH_ARIA_CHECKBOX = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<label>
  <button type="button" role="checkbox" aria-checked="false"
    onclick="
      const pressed = this.getAttribute('aria-checked') === 'true';
      this.setAttribute('aria-checked', pressed ? 'false' : 'true');
    "></button>
  <input aria-hidden="true" tabindex="-1" type="checkbox"
    style="position:absolute; pointer-events:none; opacity:0;" />
  I have read and accept the Terms and Conditions and Privacy Policy.
</label>
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


@pytest.fixture
def page(config: Config):
    with BrowserSession(config) as session:
        yield session.start()


def _store_and_logger(tmp_path: Path) -> tuple[EvidenceStore, RunLogger]:
    return EvidenceStore(db_path=tmp_path / "e.db"), RunLogger(run_id="run-1", log_dir=tmp_path)


def test_completes_multi_step_form(tmp_path: Path, page: Page) -> None:
    page.set_content(_MULTI_STEP_FORM)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 2
    assert result.submitted is True
    assert result.error is None
    assert page.query_selector("#success") is not None
    store.close()


def test_stops_when_no_fields_or_buttons_remain(tmp_path: Path, page: Page) -> None:
    page.set_content("<html><body><p>Nothing to fill here</p></body></html>")
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.steps_completed == 0
    assert result.submitted is False
    store.close()


def test_generated_identity_used_when_none_provided(tmp_path: Path, page: Page) -> None:
    page.set_content(_SINGLE_STEP_FORM)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert "@" in result.identity.email
    assert result.identity.password
    store.close()


def test_skips_oauth_button_and_clicks_the_real_submit_button(tmp_path: Path, page: Page) -> None:
    # Regression (100hires.com): "Continue" substring-matches "Continue with
    # Google", which was clicked instead of the real submit button, sending
    # the whole run into a third-party OAuth flow.
    page.set_content(_FORM_WITH_OAUTH_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.title() == "submitted"
    store.close()


def test_clicks_submit_button_with_a_word_inserted_into_the_target_phrase(
    tmp_path: Path, page: Page
) -> None:
    # Regression (10to8.com/signinapp.com): real button text was "Start my
    # free trial" -- Playwright's substring name matching against our
    # "Start free trial" pattern fails because "my" breaks the contiguous
    # substring, even though every target word is present.
    page.set_content(_FORM_WITH_INSERTED_WORD_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.title() == "submitted"
    store.close()


def test_checks_required_consent_checkbox_but_not_marketing_optin(
    tmp_path: Path, page: Page
) -> None:
    # Regression (signinapp.com): a mandatory "I agree to Terms and Privacy
    # Policy" checkbox isn't marked HTML `required`, so it silently blocks
    # submission unless we recognize it by label text. A nearby opt-in
    # marketing checkbox must be left unchecked.
    page.set_content(_FORM_WITH_CONSENT_CHECKBOXES)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.locator("#terms").is_checked() is True
    assert page.locator("#marketing").is_checked() is False
    store.close()


def test_checks_aria_role_checkbox_when_native_input_is_a_shadow_control(
    tmp_path: Path, page: Page
) -> None:
    # Regression (signinapp.com): the real signup form uses a Radix/shadcn-
    # style checkbox where clicking the native <input> does nothing (it's a
    # pointer-events:none shadow); the actual toggle is a role="checkbox"
    # button tracked via aria-checked.
    page.set_content(_FORM_WITH_ARIA_CHECKBOX)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.locator('[role="checkbox"]').get_attribute("aria-checked") == "true"
    store.close()


def test_fills_unmapped_required_select_with_first_real_option(tmp_path: Path, page: Page) -> None:
    # Regression (signinapp.com): "How did you hear about us?" has no
    # identity-data mapping and no HTML `required` attribute (validated via
    # JS instead), so it silently blocked submission. Any real answer
    # satisfies that validation.
    page.set_content(_FORM_WITH_UNMAPPED_REQUIRED_SELECT)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.locator("select[name=hdyfu]").input_value() == "recommendation"
    store.close()


def test_uses_email_override_verbatim(tmp_path: Path, page: Page) -> None:
    page.set_content(_SINGLE_STEP_FORM)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, email_override="levent@userguidingnow.com")

    assert result.identity.email == "levent@userguidingnow.com"
    store.close()


# Mirrors blocksurvey.io's real signup form: a single "Full name" field (no
# separate first/last inputs) plus a native date-of-birth input.
_FORM_WITH_FULL_NAME_AND_DOB = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="fullname" placeholder="Full name" />
<input name="dob" type="date" placeholder="Date of birth" />
<button onclick="document.title='submitted';">Continue</button>
</body></html>
"""


def test_uses_first_and_last_name_override_for_full_name_field(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_FULL_NAME_AND_DOB)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(
        page, store, run_logger, first_name_override="Levent", last_name_override="Aksan"
    )

    assert result.identity.full_name == "Levent Aksan"
    assert page.locator("input[name=fullname]").input_value() == "Levent Aksan"
    store.close()


def test_fills_date_of_birth_field(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_FULL_NAME_AND_DOB)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert page.locator("input[name=dob]").input_value() == result.identity.date_of_birth
    store.close()


# Mirrors a real church-management SaaS signup form: an industry-specific
# "Church Name"/"Church Website" pair _FIELD_PATTERNS' company_name/website
# keywords can't enumerate by name, a first/last split, a "Username" field
# (which must NOT be swept up as a name field -- see _NAME_WORD_RE), a phone
# field, and an "Administrator Password" field (already covered by the
# existing substring "password" keyword).
_FORM_WITH_CHURCH_FIELDS = """
<html><body>
<input name="churchName" placeholder="Church Name" />
<input name="churchWebsite" placeholder="Church Website" />
<input name="username" placeholder="Username" />
<input name="firstName" type="text" placeholder="First Name" />
<input name="lastName" type="text" placeholder="Last Name" />
<input name="email" type="email" placeholder="Email" />
<input name="phone" placeholder="Phone" />
<input name="adminPassword" type="password" placeholder="Administrator Password" />
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""


def test_fills_industry_specific_name_and_website_fields_with_no_errors(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_FORM_WITH_CHURCH_FIELDS)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(
        page, store, run_logger, first_name_override="Levent", last_name_override="Aksan"
    )

    identity = result.identity
    assert page.locator("input[name=churchName]").input_value() == identity.company_name
    assert page.locator("input[name=churchWebsite]").input_value() == identity.website
    assert page.locator("input[name=firstName]").input_value() == "Levent"
    assert page.locator("input[name=lastName]").input_value() == "Aksan"
    assert page.locator("input[name=email]").input_value() == identity.email
    assert page.locator("input[name=phone]").input_value() == identity.phone
    assert page.locator("input[name=adminPassword]").input_value() == identity.password
    # "Username" contains "name" as a substring but isn't a name field --
    # must stay untouched, not filled with a company name (which can
    # contain a space and would fail that field's validation).
    assert page.locator("input[name=username]").input_value() == ""
    assert result.submitted is True
    store.close()


# Mirrors conceptboard.com's real signup form: a bare "Name" field labeled
# just "Name" with an example-value placeholder ("Jane Doe") that must NOT
# be read as an organization qualifier, plus separate Password/Confirm
# password fields -- leaving Confirm password empty triggers the site's own
# "Passwords do not match" validation error.
_FORM_WITH_BARE_NAME_LABEL_AND_CONFIRM_PASSWORD = """
<html><body>
<label for="name">Name</label>
<input id="name" name="name" type="text" placeholder="Jane Doe" />
<label for="email">Email</label>
<input id="email" name="email" type="email" placeholder="jane.doe@company.com" />
<label for="password">Password</label>
<input id="password" name="Password" type="password" />
<label for="confirm">Confirm password</label>
<input id="confirm" name="Confirm password" type="password" />
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""


def test_fills_bare_name_label_and_confirm_password_field(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_BARE_NAME_LABEL_AND_CONFIRM_PASSWORD)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    identity = result.identity
    # A label of just "Name" plus an example-value placeholder ("Jane Doe")
    # is the person's name, not identity.company_name -- the placeholder
    # must not be misread as an organization qualifier like "Church".
    assert page.locator("#name").input_value() == identity.full_name
    assert page.locator("#password").input_value() == identity.password
    assert page.locator("#confirm").input_value() == identity.password
    assert result.submitted is True
    store.close()


# Mirrors Synder's real signup form (reached from cloudbusinesshq.com): an
# auth-method chooser with OAuth/SSO/integration buttons plus one "Continue
# with Email" option. Clicking it reveals -- on the *same* page, no
# navigation -- a bare "Name"/Email/Password form and a "Sign up" button,
# while the OAuth buttons stay visible right above it. "Sign up" and
# "Continue with Xero" both word-subset-match "Continue" in _click_submit, so
# without _is_oauth_button's with/via rule the wrong one could get clicked.
_FORM_WITH_OAUTH_CHOOSER_AND_EMAIL_OPTION = """
<html><body>
<div id="chooser">
  <button onclick="document.title='oauth-clicked';">Sign in with Intuit</button>
  <button onclick="document.title='oauth-clicked';">Continue with Xero</button>
  <button onclick="document.title='oauth-clicked';">Continue with Google</button>
  <button onclick="document.getElementById('form').style.display='block';">
    Continue with Email
  </button>
</div>
<div id="form" style="display:none">
  <input placeholder="Name" />
  <input placeholder="Email" />
  <input type="password" placeholder="Password" />
  <button onclick="document.title='submitted';">Sign up</button>
</div>
</body></html>
"""


def test_prefers_continue_with_email_over_oauth_chooser_buttons(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_OAUTH_CHOOSER_AND_EMAIL_OPTION)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    identity = result.identity
    assert page.locator("#form input").nth(1).input_value() == identity.email
    assert page.locator("#form input").nth(2).input_value() == identity.password
    # A bare "Name" field with no organization qualifier and no separate
    # company field anywhere on the page -- the person's name, not
    # identity.company_name (see _is_bare_name_hint).
    assert page.locator("#form input").nth(0).input_value() == identity.full_name
    assert result.submitted is True
    # Confirms neither Intuit/Xero/Google was ever actually clicked -- if one
    # had been, this title would be "oauth-clicked" instead and submitted
    # would still be False, since only the real "Sign up" button sets it.
    assert page.title() == "submitted"
    store.close()


# Mirrors dialpad.com's real signup form: "Sign up with your email" has an
# inserted word breaking a plain substring match against our "sign up with
# email" keyword (same class of bug as _click_submit's "Start MY free
# trial" precedent), plus OAuth buttons that must stay unclicked.
_FORM_WITH_INSERTED_WORD_EMAIL_BUTTON = """
<html><body>
<div id="chooser">
  <button onclick="document.title='oauth-clicked';">Continue with Google</button>
  <button onclick="document.title='oauth-clicked';">Continue with Microsoft</button>
  <button onclick="document.getElementById('form').style.display='block';">
    Sign up with your email
  </button>
</div>
<div id="form" style="display:none">
  <input placeholder="Work Email" />
  <input placeholder="First Name" />
  <input placeholder="Last Name" />
  <input type="password" placeholder="Create Password" />
  <button onclick="document.title='submitted';">Continue</button>
</div>
</body></html>
"""


def test_clicks_continue_with_email_button_with_a_word_inserted_into_the_target_phrase(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_FORM_WITH_INSERTED_WORD_EMAIL_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(
        page, store, run_logger, first_name_override="Levent", last_name_override="Aksan"
    )

    assert page.locator('#form input[placeholder="Work Email"]').input_value() == (
        result.identity.email
    )
    assert result.submitted is True
    assert page.title() == "submitted"
    store.close()


def test_fills_country_select_dropdown_via_exact_label_match(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_COUNTRY_DROPDOWN)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, country="Turkey")

    assert result.identity.country == "Turkey"
    assert page.locator("select[name=country]").input_value() == "TR"
    store.close()


def test_fills_country_select_dropdown_via_best_effort_substring_match(
    tmp_path: Path, page: Page
) -> None:
    # The stored value is "United States" but the only matching option's label
    # is "United States of America" -- must still match via substring fallback.
    page.set_content(_FORM_WITH_COUNTRY_DROPDOWN)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, country="United States")

    assert result.identity.country == "United States"
    assert page.locator("select[name=country]").input_value() == "US"
    store.close()


def test_records_registration_evidence_and_screenshots(tmp_path: Path, page: Page) -> None:
    page.set_content(_SINGLE_STEP_FORM)
    store, run_logger = _store_and_logger(tmp_path)

    run_registration(page, store, run_logger)

    evidence = store.for_run("run-1")
    assert any(
        e.visible_ui and e.visible_ui.get("screenshot_kind") == "registration" for e in evidence
    )
    final = next(e for e in evidence if e.label == "registration attempt")
    assert final.visible_ui["submitted"] is True
    store.close()


class _FakeTempEmailProvider:
    def __init__(self, address: str = "fake@temp.example", link: str | None = None) -> None:
        self.address = address
        self.link = link
        self.create_inbox_called = False

    def create_inbox(self) -> TempInbox | None:
        self.create_inbox_called = True
        return TempInbox(address=self.address, provider_data={})

    def wait_for_verification_link(
        self, inbox: TempInbox, *, timeout: float = 60.0, keyword: str = "verify"
    ) -> str | None:
        return self.link


def test_opens_verification_link_when_provided(tmp_path: Path, page: Page) -> None:
    page.set_content(_SINGLE_STEP_FORM)
    store, run_logger = _store_and_logger(tmp_path)
    verify_url = _data_url("<html><body><h1>You're verified!</h1></body></html>")
    provider = _FakeTempEmailProvider(link=verify_url)

    result = run_registration(page, store, run_logger, temp_email_provider=provider)

    assert provider.create_inbox_called is True
    assert result.identity.email == "fake@temp.example"
    assert result.verification_link_opened is True
    assert "verified" in page.content().lower()
    store.close()


def test_missing_verification_email_does_not_fail_registration(tmp_path: Path, page: Page) -> None:
    page.set_content(_SINGLE_STEP_FORM)
    store, run_logger = _store_and_logger(tmp_path)
    provider = _FakeTempEmailProvider(link=None)

    result = run_registration(page, store, run_logger, temp_email_provider=provider)

    assert result.submitted is True
    assert result.verification_link_opened is False
    assert result.error is None
    store.close()


def test_never_raises_on_internal_failure(tmp_path: Path, page: Page) -> None:
    page.set_content(_SINGLE_STEP_FORM)
    _, run_logger = _store_and_logger(tmp_path)

    class _BrokenStore:
        def add(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("db exploded")

    result = run_registration(page, _BrokenStore(), run_logger)  # type: ignore[arg-type]

    assert result.error is not None
    assert result.steps_completed == 0
    assert result.submitted is False

    log_entries = run_logger.read_all()
    assert any("registration flow failed" in entry.message for entry in log_entries)


# --- open_registration_entry_point (GRO-30) ---------------------------------

_PAGE_WITH_SIGNUP_AND_LOGIN = """
<html><body>
<a href="#" onclick="document.title='clicked-signup'; return false;">Sign up</a>
<a href="#" onclick="document.title='clicked-login'; return false;">Login</a>
</body></html>
"""

_PAGE_WITH_LOGIN_ONLY_REVEALS_EMAIL_MODAL = """
<html><body>
<button onclick="document.getElementById('modal').style.display='block';">Sign in</button>
<div id="modal" style="display:none">
  <button onclick="document.title='oauth-clicked';">Continue with Google</button>
  <input type="email" placeholder="Email" />
  <button>Continue</button>
</div>
</body></html>
"""

_PAGE_WITH_LOGIN_REVEALING_SECONDARY_SIGNUP_LINK = """
<html><body>
<button onclick="document.getElementById('login-form').style.display='block';">Sign in</button>
<div id="login-form" style="display:none">
  <input type="email" placeholder="Email" />
  <input type="password" placeholder="Password" />
  <a href="#" onclick="document.title='clicked-register-link'; return false;">
    New here? Register
  </a>
</div>
</body></html>
"""

_PAGE_WITH_NOTHING_CLICKABLE = """
<html><body><p>Just some text, no signup or login anywhere.</p></body></html>
"""


def test_entry_point_prefers_signup_over_login(tmp_path: Path, page: Page) -> None:
    page.set_content(_PAGE_WITH_SIGNUP_AND_LOGIN)
    _, run_logger = _store_and_logger(tmp_path)

    found = open_registration_entry_point(page, run_logger)

    assert found is True
    assert page.title() == "clicked-signup"


def test_entry_point_falls_back_to_login_and_reveals_email_modal(
    tmp_path: Path, page: Page
) -> None:
    # Mirrors allevents.in: no signup control on the page at all, only
    # "Sign in", which opens a modal with OAuth buttons plus a bare email
    # input and generic "Continue" button (the actual signup entry point).
    page.set_content(_PAGE_WITH_LOGIN_ONLY_REVEALS_EMAIL_MODAL)
    _, run_logger = _store_and_logger(tmp_path)

    found = open_registration_entry_point(page, run_logger)

    assert found is True
    assert page.locator("#modal input[type=email]").is_visible()
    # Must not have clicked the OAuth button while looking for a secondary
    # signup link -- _find_clickable_by_keywords skips OAuth-labeled controls.
    assert page.title() != "oauth-clicked"


def test_entry_point_drills_into_secondary_signup_link(tmp_path: Path, page: Page) -> None:
    # A login form that itself contains a "Register" link -- must be found
    # and followed on the second pass, not just the initial login click.
    page.set_content(_PAGE_WITH_LOGIN_REVEALING_SECONDARY_SIGNUP_LINK)
    _, run_logger = _store_and_logger(tmp_path)

    found = open_registration_entry_point(page, run_logger)

    assert found is True
    assert page.title() == "clicked-register-link"


def test_entry_point_returns_false_when_nothing_matches(tmp_path: Path, page: Page) -> None:
    page.set_content(_PAGE_WITH_NOTHING_CLICKABLE)
    _, run_logger = _store_and_logger(tmp_path)

    found = open_registration_entry_point(page, run_logger)

    assert found is False


# --- "Continue with email" fill+submit (GRO-31) -----------------------------

# Mirrors allevents.in's real sign-in modal: OAuth buttons plus a bare email
# input and generic "Continue" button, which reveals a second step asking
# for First/Last Name and an email verification code before "Register".
_CONTINUE_WITH_EMAIL_THEN_NAME_FORM = """
<html><body>
<div id="step1">
  <button onclick="document.title='oauth-clicked';">Continue with Facebook</button>
  <button onclick="document.title='oauth-clicked';">Continue with Google</button>
  <input type="email" placeholder="Email" />
  <button onclick="
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
  ">Continue</button>
</div>
<div id="step2" style="display:none">
  <input type="text" placeholder="First Name" />
  <input type="text" placeholder="Last Name" />
  <input type="number" placeholder="Verification Code" />
  <button onclick="document.title='submitted';">Register</button>
</div>
</body></html>
"""


def test_continue_with_email_flow_fills_name_fields_and_registers(
    tmp_path: Path, page: Page
) -> None:
    # GRO-31: once already on this modal (GRO-30's open_registration_entry_point
    # is what gets a real run there), the normal fill+submit loop must fill the
    # email, click the plain "Continue" (never the OAuth buttons), then fill
    # First/Last Name on the revealed second step and click "Register".
    page.set_content(_CONTINUE_WITH_EMAIL_THEN_NAME_FORM)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 2
    assert result.submitted is True
    assert page.title() == "submitted"  # never became "oauth-clicked"
    assert page.locator('#step2 input[placeholder="First Name"]').input_value() == (
        result.identity.first_name
    )
    assert page.locator('#step2 input[placeholder="Last Name"]').input_value() == (
        result.identity.last_name
    )
    # Verification code has no identity-data mapping -- out of scope for
    # GRO-31, correctly left untouched rather than guessed at.
    assert page.locator('#step2 input[placeholder="Verification Code"]').input_value() == ""
    store.close()


# --- Choice-wizard steps with no fillable fields (GRO-34) -------------------

# Mirrors ansarada.com's "Who needs the data room?" step: two persona cards,
# no <input>/<select>, no submit-text button -- clicking either one reveals
# the real form.
_CHOICE_WIZARD_FORM = """
<html><body>
<div id="step1">
  <button onclick="
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
  ">Companies</button>
  <button onclick="
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
  ">Advisors</button>
</div>
<div id="step2" style="display:none">
  <input name="email" type="email" placeholder="Email" />
  <input name="password" type="password" placeholder="Password" />
  <button onclick="document.title='submitted';">Sign up</button>
</div>
</body></html>
"""


def test_clicks_first_choice_option_when_no_fields_or_submit_button_present(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_CHOICE_WIZARD_FORM)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 2
    assert result.submitted is True
    assert page.title() == "submitted"
    store.close()


# Distractors that a naive "click any visible clickable" heuristic would
# wrongly pick: a nav link (excluded structurally) and standalone "Back"/
# "Contact us" controls (excluded via _CHOICE_EXCLUDE_KEYWORDS).
_CHOICE_WIZARD_WITH_DISTRACTORS = """
<html><body>
<nav><a href="#" onclick="document.title='wrongly-clicked';return false;">Blog</a></nav>
<a href="#" onclick="document.title='wrongly-clicked';return false;">Contact us</a>
<button onclick="document.title='wrongly-clicked';">Back</button>
<div id="step1">
  <button onclick="
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
  ">Companies</button>
</div>
<div id="step2" style="display:none">
  <input name="email" type="email" placeholder="Email" />
  <button onclick="document.title='submitted';">Sign up</button>
</div>
</body></html>
"""


def test_choice_picker_skips_nav_links_and_back_contact_buttons(tmp_path: Path, page: Page) -> None:
    page.set_content(_CHOICE_WIZARD_WITH_DISTRACTORS)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 2
    assert result.submitted is True
    assert page.title() == "submitted"  # never became "wrongly-clicked"
    store.close()


# Mirrors ansarada.com's real failure mode before this fix: the wizard's
# "Next" button is a single DOM node reused across steps (disabled until a
# choice is picked, then re-purposed as the step-2 submit once fields are
# filled). The old claim mechanism (GRO-31) would permanently block
# re-clicking it after its first legitimate use, stalling the wizard on step
# 2 with fields filled but never submitted.
_CHOICE_WIZARD_WITH_SHARED_NEXT_BUTTON = """
<html><body>
<div id="step1">
  <button onclick="
    document.getElementById('next').disabled=false;
    document.getElementById('step1').style.display='none';
    document.getElementById('step2').style.display='block';
  ">Companies</button>
</div>
<div id="step2" style="display:none">
  <input name="email" type="email" placeholder="Email" />
  <input name="password" type="password" placeholder="Password" />
</div>
<button id="next" disabled onclick="
  var email = document.querySelector('input[name=email]').value;
  if (document.getElementById('step2').style.display === 'block' && email) {
    document.body.insertAdjacentHTML('beforeend', '<div id=success>Registered!</div>');
  }
">Next</button>
</body></html>
"""


def test_shared_next_button_can_be_reclicked_after_fields_are_filled(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_CHOICE_WIZARD_WITH_SHARED_NEXT_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 2
    assert page.query_selector("#success") is not None
    store.close()


# --- Screenshot + vision-LLM fallback wiring (GRO-35) ------------------------


def _vision_config() -> Config:
    return replace(
        Config.from_env(env_path="/nonexistent/.env"),
        groq_api_key="gsk-test-123",
        groq_vision_model="qwen/qwen3.6-27b",
    )


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


# The DOM-based choice-picker (_click_unclaimed_choice_option) deliberately
# excludes anything matching _CHOICE_EXCLUDE_KEYWORDS (e.g. "contact") so it
# never wanders off into a nav/contact link. This button's label happens to
# contain that word without actually being one -- exactly the kind of
# element only the vision fallback (which has no such keyword exclusion list
# of its own) can recognize and click.
_STUCK_PAGE_ONLY_VISION_CAN_RESOLVE = """
<html><body>
<button onclick="document.title='submitted';">Contact us now</button>
</body></html>
"""


def test_vision_fallback_clicks_element_the_dom_picker_had_excluded(
    tmp_path: Path, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        return _FakeResponse(
            {"choices": [{"message": {"content": '{"choice": "Contact us now"}'}}]}
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    page.set_content(_STUCK_PAGE_ONLY_VISION_CAN_RESOLVE)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5, config=_vision_config())

    assert result.steps_completed == 1
    assert page.title() == "submitted"
    store.close()


def test_vision_fallback_not_attempted_without_config(tmp_path: Path, page: Page) -> None:
    # No config passed (the default) -- the DOM heuristics stall on this
    # page with nothing to fill/check/pick, and registration must stop there
    # rather than raise for lack of a vision model, exactly like before
    # GRO-35 existed.
    page.set_content(_STUCK_PAGE_ONLY_VISION_CAN_RESOLVE)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 0
    assert page.title() != "submitted"
    store.close()


def test_vision_fallback_stops_registration_when_it_cannot_help(
    tmp_path: Path, page: Page, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unhelpful vision response ends registration immediately rather than
    # re-querying the same unchanged screenshot -- _MAX_VISION_ATTEMPTS caps
    # distinct stuck points across a run (e.g. a wizard that stalls twice at
    # two different steps), not retries at one point: nothing about the page
    # would differ on a second identical call.
    call_count = 0

    def fake_urlopen(request, timeout: float = 30):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        return _FakeResponse({"choices": [{"message": {"content": "no clear answer"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    page.set_content(_STUCK_PAGE_ONLY_VISION_CAN_RESOLVE)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5, config=_vision_config())

    assert call_count == 1
    assert result.steps_completed == 0
    assert page.title() != "submitted"
    store.close()


# --- CAPTCHA/anti-bot challenge stops registration cleanly (GRO-42) ---------

# A CAPTCHA iframe's own controls are unreachable (cross-origin, and we don't
# solve them anyway), but a leftover OAuth-style button sitting right next to
# it stays in the DOM and "visible" per Playwright even though a human
# couldn't really click it under the overlay -- mirrors what happened live on
# Synder's signup (reached from cloudbusinesshq.com): a reCAPTCHA challenge
# appeared after "Sign up", and the choice-wizard fallback went on to click
# "Continue with Xero" instead of stopping.
_PAGE_WITH_CAPTCHA_AND_LEFTOVER_OAUTH_BUTTON = """
<html><body>
<button onclick="document.title='oauth-clicked';">Continue with Xero</button>
<iframe src="data:text/html,recaptcha-challenge-fixture" width="300" height="400"></iframe>
</body></html>
"""


def test_stops_registration_when_captcha_challenge_appears(tmp_path: Path, page: Page) -> None:
    page.set_content(_PAGE_WITH_CAPTCHA_AND_LEFTOVER_OAUTH_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 0
    assert result.submitted is False
    # Never fell through to the choice-wizard fallback and clicked the
    # leftover button underneath the overlay.
    assert page.title() != "oauth-clicked"

    evidence = store.for_run(run_logger.run_id)
    captcha_shots = [
        e for e in evidence if e.label == "registration blocked by anti-bot challenge (captcha)"
    ]
    assert len(captcha_shots) == 1
    assert captcha_shots[0].screenshot is not None
    store.close()
