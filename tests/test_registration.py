import html
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from patchright.sync_api import Page

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.event_log import RunLogger
from growthradar.evidence import EvidenceStore
from growthradar.registration import (
    _find_clickable_by_keywords,
    open_registration_entry_point,
    run_registration,
)
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

_FORM_WITH_FACEBOOK_OAUTH_BUTTON = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="document.title='oauth-clicked';">Continue with Facebook</button>
<button onclick="document.title='submitted';">Continue</button>
</body></html>
"""

# Unlike _FORM_WITH_OAUTH_BUTTON (whose Google click is a same-page no-op by
# design, so the loop is expected to move on to the real "Continue" button
# instead), this one's Google button clears the form -- standing in for a
# real OAuth redirect actually navigating away -- so a successful Google
# click is the run's one and only step, with nothing left behind for a later
# iteration to find and click.
_FORM_WITH_GOOGLE_OAUTH_BUTTON_THAT_NAVIGATES_AWAY = """
<html><body>
<div id="form">
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="document.title='oauth-clicked'; document.getElementById('form').remove();">
  Continue with Google
</button>
<button onclick="document.title='submitted';">Continue</button>
</div>
</body></html>
"""

# Regression (joinblink.com): a fillable email form and a bare "Google"
# button side by side -- unlike _FORM_WITH_OAUTH_BUTTON's "Continue with
# Google" (which _click_submit's phrase matching considers, then routes
# around), a button whose entire text is "Google" doesn't match any
# _SUBMIT_BUTTON_TEXTS phrase at all, so it's never found by the normal
# fill-then-submit path -- allow_google_oauth must click it before the email
# field ever gets filled, not compete with _click_submit for priority.
_FORM_WITH_EMAIL_FORM_AND_BARE_GOOGLE_BUTTON = """
<html><body>
<div id="form">
<input name="email" type="email" placeholder="name@company.com" />
<button onclick="document.title='submitted';">Start your free trial</button>
<button onclick="document.title='oauth-clicked'; document.getElementById('form').remove();">
  Google
</button>
</div>
</body></html>
"""

_FORM_WITH_INSERTED_WORD_BUTTON = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="document.title='submitted';">Start my free trial</button>
</body></html>
"""

# Regression (huddleup.ai): a hidden email input belonging to a later wizard
# step sorts before the real, visible step-1 email input in the DOM and
# shares the same type/placeholder keyword -- _visible_unclaimed must keep
# scanning past it instead of giving up at the first (invisible) match.
_FORM_WITH_HIDDEN_DECOY_EMAIL_FIELD = """
<html><body>
<input type="email" id="decoy" style="display:none" placeholder="Enter your work email" />
<input type="email" id="real" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""

# Regression (huddleup.ai): the submit button starts disabled and is only
# re-enabled by the email field's own onkeyup handler -- .fill() sets the
# value and fires input/change but never a real keystroke, so without a
# synthetic keyup afterward the button stays disabled forever.
_FORM_WITH_SUBMIT_GATED_BY_EMAIL_KEYUP = """
<html><body>
<input name="email" type="email" placeholder="Email"
  onkeyup="document.getElementById('go').disabled = this.value.length === 0;" />
<input name="password" type="password" placeholder="Password" />
<button id="go" disabled onclick="document.title='submitted';">Sign up</button>
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


# Mimics statusbrew.com's Angular Material-style checkbox: the native
# <input> is the real, checkable control (unlike _FORM_WITH_ARIA_CHECKBOX's
# pointer-events:none shadow input), but a custom visual indicator sits
# absolutely positioned directly on top of it for styling, intercepting a
# direct click/.check() at the input's own location. Only clicking the
# wrapping <label> (which forwards a real click to its input via standard
# browser behavior) gets through.
_FORM_WITH_OVERLAY_INTERCEPTED_CHECKBOX = """
<html><body>
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<label>
  <span style="position:relative; display:inline-block; width:20px; height:20px;">
    <input type="checkbox" id="terms"
      style="position:absolute; top:0; left:0; width:20px; height:20px; opacity:0;" />
    <span style="position:absolute; top:0; left:0; width:20px; height:20px;
      background:white; border:1px solid #ccc;"></span>
  </span>
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


# Regression (kamiapp.com): "Create a free account" (word-subset-matches
# _click_submit's "Create account" target, same as the real button) opens
# the actual app in a new tab (target="_blank") rather than navigating the
# current page. Without following it, the loop just kept clicking around
# whatever was left on the original page for the rest of its step budget --
# in one observed run, wandering as far as a help-center "Training Hub"
# page -- while wrongly recording the original click as a submission.
def test_follows_a_signup_cta_that_opens_a_new_tab(tmp_path: Path, page: Page) -> None:
    # A real http(s) URL via context.route(), not a data: URL -- modern
    # Chrome silently refuses to open a data: URL as a new top-level tab
    # (target="_blank"), which would make this test pass for the wrong
    # reason (no new tab ever opens, so there'd be nothing to fail to
    # follow).
    def handler(route):  # type: ignore[no-untyped-def]  # noqa: ANN001
        route.fulfill(
            status=200,
            content_type="text/html",
            body=(
                "<html><body>"
                '<input name="email" type="email" placeholder="Email" />'
                '<input name="password" type="password" placeholder="Password" />'
                "<button onclick=\"document.title='submitted';\">Sign up</button>"
                "</body></html>"
            ),
        )

    page.context.route("https://fake.growthradar.test/app", handler)
    page.set_content(
        '<html><body><a href="https://fake.growthradar.test/app" '
        'target="_blank">Create a free account</a></body></html>'
    )
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    new_tab = page.context.pages[-1]
    assert new_tab.title() == "submitted"
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


def test_fills_visible_email_field_past_a_hidden_decoy_earlier_in_the_dom(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_FORM_WITH_HIDDEN_DECOY_EMAIL_FIELD)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.eval_on_selector("#real", "el => el.value") == result.identity.email
    store.close()


def test_dispatches_keyup_after_fill_so_a_keyup_gated_submit_button_enables(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_FORM_WITH_SUBMIT_GATED_BY_EMAIL_KEYUP)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
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


def test_clicks_google_oauth_button_when_google_profile_is_configured(
    tmp_path: Path, page: Page, config: Config
) -> None:
    # With a signed-in google_profile_dir (browser.py) and explicit opt-in,
    # "Continue with Google" is no longer a dead end to route around -- it's
    # now the preferred path, since the whole point of the persistent profile
    # is to complete it instead of falling back to a generated identity.
    page.set_content(_FORM_WITH_GOOGLE_OAUTH_BUTTON_THAT_NAVIGATES_AWAY)
    store, run_logger = _store_and_logger(tmp_path)
    google_config = replace(config, google_profile_dir="/fake/profile-dir", allow_google_oauth=True)

    result = run_registration(page, store, run_logger, config=google_config)

    assert result.submitted is True
    assert page.title() == "oauth-clicked"
    store.close()


def test_still_skips_non_google_oauth_button_when_google_profile_is_configured(
    tmp_path: Path, page: Page, config: Config
) -> None:
    # allow_google_oauth is Google-specific, not a blanket "click any OAuth
    # button" switch -- the persistent profile only has a Google session, so
    # Facebook/Microsoft/etc. buttons must stay routed around exactly as
    # before.
    page.set_content(_FORM_WITH_FACEBOOK_OAUTH_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)
    google_config = replace(config, google_profile_dir="/fake/profile-dir", allow_google_oauth=True)

    result = run_registration(page, store, run_logger, config=google_config)

    assert result.submitted is True
    assert page.title() == "submitted"
    store.close()


def test_prefers_bare_google_button_over_filling_a_sibling_email_form(
    tmp_path: Path, page: Page, config: Config
) -> None:
    page.set_content(_FORM_WITH_EMAIL_FORM_AND_BARE_GOOGLE_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)
    google_config = replace(config, google_profile_dir="/fake/profile-dir", allow_google_oauth=True)

    result = run_registration(page, store, run_logger, config=google_config)

    assert result.submitted is True
    assert page.title() == "oauth-clicked"
    store.close()


def test_fills_email_form_when_google_oauth_not_allowed(
    tmp_path: Path, page: Page, config: Config
) -> None:
    # Without allow_google_oauth (the default), the same page must behave
    # exactly as it always has: fill the email field and submit through it,
    # never touching the Google button.
    page.set_content(_FORM_WITH_EMAIL_FORM_AND_BARE_GOOGLE_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, config=config)

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


def test_checks_native_checkbox_via_label_when_a_custom_indicator_intercepts_it(
    tmp_path: Path, page: Page
) -> None:
    # Regression (statusbrew.com): the native checkbox is the real control
    # (unlike the Radix/shadcn shadow-input case above), but a custom visual
    # indicator sits directly on top of it, so Playwright's own actionability
    # check correctly refuses a direct .check() ("intercepts pointer
    # events") -- without a fallback, the box stayed unchecked, the "Agree
    # and Sign up" button stayed disabled, and registration never submitted.
    page.set_content(_FORM_WITH_OVERLAY_INTERCEPTED_CHECKBOX)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.locator("#terms").is_checked() is True
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


# Regression (joinhomebase.com): a real field name="fullName" (camelCase, no
# space) wasn't matched by full_name's keyword list, which only had "full
# name" (with a space) and "your name" -- unlike first_name/last_name, which
# both already carry a no-space variant ("firstname"/"lastname") alongside
# the spaced one. The button stayed disabled with the field empty, and
# nothing else on the page for the loop to fall back to.
_FORM_WITH_CAMEL_CASE_FULL_NAME_FIELD = """
<html><body>
<p>What should we call you?</p>
<input name="fullName" placeholder="ex: Jane Smith" />
<button onclick="document.title='submitted';">Get started</button>
</body></html>
"""


def test_fills_camel_case_full_name_field(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_CAMEL_CASE_FULL_NAME_FIELD)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert page.locator('input[name="fullName"]').input_value() == result.identity.full_name
    assert result.submitted is True
    store.close()


# Regression (onlypult.com): a real email field named/labeled with "mail"
# instead of "email" -- name="RegisterForm[mail]", id="registerform-mail",
# label "E-mail" (hyphenated), type="text" (not type="email", so the
# input_type shortcut in _find_field doesn't catch it either). None of
# those attributes contain "email" as a contiguous substring, so the field
# was never matched or filled at all -- left empty, submission blocked.
_FORM_WITH_MAIL_NAMED_EMAIL_FIELD = """
<html><body>
<label for="registerform-mail">E-mail</label>
<input id="registerform-mail" name="RegisterForm[mail]" type="text"
  placeholder="example@onlypult.com" />
<label for="registerform-password">Password</label>
<input id="registerform-password" name="RegisterForm[password]" type="password" />
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""


def test_fills_email_field_named_mail_instead_of_email(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_MAIL_NAMED_EMAIL_FIELD)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert page.locator("#registerform-mail").input_value() == result.identity.email
    assert result.submitted is True
    store.close()


# Regression (legalesign.com): right after a successful signup, a
# dismissible security prompt ("Set up a passkey") shows up with no field to
# fill -- just its own "commit" CTA and a "Skip for now" dismissal, in that
# DOM order. Without preferring the dismissal, the generic choice-picker
# would click "Set up a passkey" first (it's first in the DOM), sending the
# run into a WebAuthn flow with no way to complete it.
_FORM_WITH_PASSKEY_PROMPT_AFTER_SIGNUP = """
<html><body>
<div id="signup">
<input name="email" type="email" placeholder="Email" />
<input name="password" type="password" placeholder="Password" />
<button onclick="
    document.getElementById('signup').style.display='none';
    document.getElementById('passkey').style.display='block';
  ">Sign up</button>
</div>
<div id="passkey" style="display:none">
<button onclick="document.title='passkey-setup-clicked';">Set up a passkey</button>
<button onclick="document.title='reached-onboarding'; document.getElementById('passkey').remove();">
  Skip for now
</button>
</div>
</body></html>
"""


def test_skips_a_passkey_prompt_instead_of_setting_one_up(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_WITH_PASSKEY_PROMPT_AFTER_SIGNUP)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

    assert result.submitted is True
    assert page.title() == "reached-onboarding"
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


# Regression (joinhomebase.com): a persona-picker screen offering "Set up a
# new business" (the real signup path) next to "Sign in with your phone or
# email" (an *existing employee's* login, an entirely different flow) --
# under plain word-subset matching, "sign in with email" (a target phrase
# meaning "prefer email over OAuth") matched the phone/email chooser too,
# since "your phone or" sitting between "with" and "email" doesn't stop all
# four target words from being present *somewhere*. Sent registration down
# the wrong branch of the screen every time, never reaching a fillable form.
_PERSONA_PICKER_WITH_UNRELATED_SIGN_IN_LINK = """
<html><body>
<a href="#" onclick="
    document.getElementById('picker').style.display='none';
    document.getElementById('owner-form').style.display='block';
  ">Set up a new business</a>
<a href="#" onclick="document.title='wrong-branch-clicked';">Sign in with your phone or email</a>
<div id="picker"></div>
<div id="owner-form" style="display:none">
  <input name="email" type="email" placeholder="Email" />
  <input name="password" type="password" placeholder="Password" />
  <button onclick="document.title='submitted';">Sign up</button>
</div>
</body></html>
"""


def test_persona_picker_prefers_new_business_over_unrelated_sign_in_link(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_PERSONA_PICKER_WITH_UNRELATED_SIGN_IN_LINK)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger)

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
    def __init__(
        self, address: str = "fake@temp.example", link: str | None = None, code: str | None = None
    ) -> None:
        self.address = address
        self.link = link
        self.code = code
        self.create_inbox_called = False

    def create_inbox(self) -> TempInbox | None:
        self.create_inbox_called = True
        return TempInbox(address=self.address, provider_data={})

    def wait_for_verification_link(
        self, inbox: TempInbox, *, timeout: float = 60.0, keyword: str = "verify"
    ) -> str | None:
        return self.link

    def wait_for_verification_code(self, inbox: TempInbox, *, timeout: float = 60.0) -> str | None:
        return self.code


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


# Regression (joinblink.com): lands directly on a "check your inbox, enter
# the 6-digit code" screen right after the initial signup step, with no
# link to open at all -- six separate maxlength="1" boxes, the standard
# widget shape for this pattern.
_OTP_BOX_CODE_ENTRY_PAGE = """
<html><body>
<h1>Enter your code</h1>
<p>We sent a verification code to your email. Enter it below.</p>
<input maxlength="1" />
<input maxlength="1" />
<input maxlength="1" />
<input maxlength="1" />
<input maxlength="1" />
<input maxlength="1" />
<button onclick="document.title='verified';">Continue</button>
</body></html>
"""

_SINGLE_FIELD_CODE_ENTRY_PAGE = """
<html><body>
<h1>Enter your code</h1>
<p>Please enter the one-time code we emailed you.</p>
<input name="otp" placeholder="One-time code" />
<button onclick="document.title='verified';">Verify</button>
</body></html>
"""

# Regression guard: page copy alone must not be enough -- no code field
# exists here (only a plain email form), so this must go through the
# ordinary fill+submit path untouched.
_PAGE_MENTIONING_CODE_WITH_NO_CODE_FIELD = """
<html><body>
<p>After signing up you'll receive a verification code by email.</p>
<input name="email" type="email" placeholder="Email" />
<button onclick="document.title='submitted';">Sign up</button>
</body></html>
"""


def test_enters_verification_code_into_otp_boxes(tmp_path: Path, page: Page) -> None:
    page.set_content(_OTP_BOX_CODE_ENTRY_PAGE)
    store, run_logger = _store_and_logger(tmp_path)
    provider = _FakeTempEmailProvider(code="483920")

    result = run_registration(page, store, run_logger, temp_email_provider=provider)

    boxes = page.locator('input[maxlength="1"]')
    values = [boxes.nth(i).input_value() for i in range(boxes.count())]
    assert values == list("483920")
    assert result.verification_code_entered is True
    assert result.submitted is True
    assert page.title() == "verified"
    store.close()


def test_enters_verification_code_into_a_single_field(tmp_path: Path, page: Page) -> None:
    page.set_content(_SINGLE_FIELD_CODE_ENTRY_PAGE)
    store, run_logger = _store_and_logger(tmp_path)
    provider = _FakeTempEmailProvider(code="192837")

    result = run_registration(page, store, run_logger, temp_email_provider=provider)

    assert page.eval_on_selector('input[name="otp"]', "el => el.value") == "192837"
    assert result.verification_code_entered is True
    assert result.submitted is True
    assert page.title() == "verified"
    store.close()


def test_does_not_treat_a_plain_email_form_as_a_code_entry_step(tmp_path: Path, page: Page) -> None:
    page.set_content(_PAGE_MENTIONING_CODE_WITH_NO_CODE_FIELD)
    store, run_logger = _store_and_logger(tmp_path)
    provider = _FakeTempEmailProvider(code="000000")

    result = run_registration(page, store, run_logger, temp_email_provider=provider)

    assert result.verification_code_entered is False
    assert result.submitted is True
    assert page.title() == "submitted"
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


def test_find_clickable_by_keywords_is_fast_on_a_link_heavy_page(page: Page) -> None:
    # Regression (mirro.io): a marketing landing page with 188 clickable
    # elements (mega-menus, footer links, ...) made a single
    # _find_clickable_by_keywords search take well over a minute -- each
    # candidate cost a separate .is_visible() plus a .inner_text(timeout=500)
    # call, and each of those carries its own Playwright actionability-wait
    # overhead. Two such searches (signup, then login) alone ate almost the
    # entire per-site session budget before registration ever got a chance
    # to start. A single-evaluate-per-candidate fast path (see
    # _fast_visible_text) must still find the real match, just quickly.
    filler_links = "".join(f'<a href="#">Learn more {i}</a>' for i in range(200))
    page.set_content(f"<html><body>{filler_links}<a href='#'>Log in</a></body></html>")

    start = time.monotonic()
    found = _find_clickable_by_keywords(page, ("log in", "login", "sign in"))
    elapsed = time.monotonic() - start

    assert found is not None
    assert found.inner_text().strip() == "Log in"
    assert elapsed < 10.0, f"took {elapsed:.1f}s -- the slow per-candidate path regressed"


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


# Regression (kamiapp.com's "Try for free" page): several product cards,
# each its own link -- the choice-picker's first unclaimed candidate
# ("Start for free") doesn't match any _SUBMIT_BUTTON_TEXTS phrase, but a
# *different* card further down ("Create a free account") does. Without a
# same-iteration guard, _click_submit ran right after the choice-picker on
# the same stale `page` reference and clicked that second, unrelated card
# too -- two different navigations from one iteration, only the second
# ever actually followed.
_MULTI_CARD_PAGE_WITH_AMBIGUOUS_SUBMIT_MATCH = """
<html><body>
<a onclick="document.title='card-a-clicked';return false;">Start for free</a>
<a onclick="document.title='card-b-clicked';return false;">Create a free account</a>
</body></html>
"""


def test_choice_picker_click_is_not_followed_by_a_same_iteration_submit_click(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_MULTI_CARD_PAGE_WITH_AMBIGUOUS_SUBMIT_MATCH)
    store, run_logger = _store_and_logger(tmp_path)

    run_registration(page, store, run_logger, max_steps=1)

    # Whichever card the choice-picker reaches first, only one click should
    # have happened this iteration -- never both.
    assert page.title() in ("card-a-clicked", "card-b-clicked")
    store.close()


# Regression (kamiapp.com's "try for free" page): four product-choice cards,
# no plain form. A promotional banner link sits earlier in the DOM than any
# real CTA and, before this fix, was clicked first every time, landing on an
# unrelated HubSpot resource page. "talk to us" (a real, same-origin, non-
# excluded link) isn't signup-flavored either and shouldn't win over "Create
# a free account", which is both submit-like (matches "Create account") and
# signup-flavored despite the inserted words. All the real hrefs here point
# off-page (a data: URL's own scheme never matches https -- see
# exploration.py's _in_scope) the same way kamiapp.com's actual CTAs route
# through a third-party click-tracking redirect rather than kamiapp.com
# itself, so this also exercises the offsite check's signup-flavored
# exemption, not just the exclusion.
_KAMI_STYLE_CHOICE_SCREEN = """
<html><body>
<a href="https://unrelated-hub.example/learn"
   onclick="event.preventDefault(); document.title='clicked-banner';">
  Find answers in the Learning Hub
</a>
<a href="https://tracker.example/click?x=1"
   onclick="event.preventDefault(); document.title='clicked-sales';">
  talk to us
</a>
<a href="https://tracker.example/click?x=2"
   onclick="event.preventDefault(); document.title='clicked-create-account';">
  Create a free account
</a>
</body></html>
"""


def test_choice_picker_prefers_a_signup_flavored_submit_like_option(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_KAMI_STYLE_CHOICE_SCREEN)
    store, run_logger = _store_and_logger(tmp_path)

    run_registration(page, store, run_logger, max_steps=1)

    assert page.title() == "clicked-create-account"
    store.close()


_KAMI_STYLE_CHOICE_SCREEN_WITHOUT_SUBMIT_MATCH = """
<html><body>
<a href="https://unrelated-hub.example/learn"
   onclick="event.preventDefault(); document.title='clicked-banner';">
  Find answers in the Learning Hub
</a>
<a href="#contact"
   onclick="event.preventDefault(); document.title='clicked-sales';">
  talk to us
</a>
<a href="https://tracker.example/click?x=3"
   onclick="event.preventDefault(); document.title='clicked-trial';">
  Sign your whole team up for free
</a>
</body></html>
"""


def test_choice_picker_excludes_offsite_banner_and_prefers_signup_wording(
    tmp_path: Path, page: Page
) -> None:
    # "talk to us" is same-origin here (a relative href), same as it was on
    # the real kamiapp.com page -- unaffected by the offsite check either
    # way, but a plain, non-excluded candidate on its own merits.
    # "Sign your whole team up for free" is deliberately worded so "sign"
    # and "up" aren't adjacent: still word-subset-matches "sign up", but
    # doesn't contain it as a literal substring, so it survives
    # _CHOICE_EXCLUDE_KEYWORDS's existing signup-keyword exclusion (which
    # predates this fix and always removes a literal "trial"/"sign up"/etc.
    # substring match) and reaches this fix's new signup-flavor preference
    # instead of merely being first-in-DOM-order.
    page.set_content(_KAMI_STYLE_CHOICE_SCREEN_WITHOUT_SUBMIT_MATCH)
    store, run_logger = _store_and_logger(tmp_path)

    run_registration(page, store, run_logger, max_steps=1)

    assert page.title() == "clicked-trial"
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


# A reCAPTCHA challenge can also present as just the small, un-solved
# checkbox badge (well under _captcha_challenge_visible's 100px size floor)
# paired with the site's own error copy -- seen live on paperbell.com:
# "Sorry, you have triggered a security warning. Please click the box above
# and try again." Without a phrase-based check alongside the size-based one,
# the loop kept clicking "Create my account" every remaining iteration,
# each click re-triggering the same warning and resetting the form, while
# steps_completed/submitted still reported a normal successful click.
_PAGE_WITH_SMALL_CAPTCHA_BADGE_AND_WARNING = """
<html><body>
<p>Sorry, you have triggered a security warning. Please click the box above and try again.</p>
<iframe src="data:text/html,recaptcha-challenge-fixture" width="304" height="78"></iframe>
<button onclick="document.title='submitted';">Create my account</button>
</body></html>
"""


def test_stops_registration_when_captcha_warning_text_appears(tmp_path: Path, page: Page) -> None:
    page.set_content(_PAGE_WITH_SMALL_CAPTCHA_BADGE_AND_WARNING)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.steps_completed == 0
    assert result.submitted is False
    assert page.title() != "submitted"

    evidence = store.for_run(run_logger.run_id)
    captcha_shots = [
        e for e in evidence if e.label == "registration blocked by anti-bot challenge (captcha)"
    ]
    assert len(captcha_shots) == 1
    store.close()


# --- Iframe-embedded forms and lazily-loaded widgets (GRO-45) ---------------

# Mirrors digifabster.com/getstarted/: the real signup form renders inside a
# third-party-embedded iframe (a HubSpot form there), invisible to a plain
# page.locator() call, which only ever searches the main document.
_IFRAME_INNER_FORM_HTML = (
    "<html><body>"
    '<input placeholder="First Name" />'
    '<input placeholder="Last Name" />'
    '<input type="email" placeholder="Email" />'
    "<button onclick=\"document.title='submitted'\">Sign up</button>"
    "</body></html>"
)
_PAGE_WITH_FORM_INSIDE_IFRAME = (
    f'<html><body><iframe srcdoc="{html.escape(_IFRAME_INNER_FORM_HTML)}"></iframe></body></html>'
)


def test_fills_form_embedded_inside_an_iframe(tmp_path: Path, page: Page) -> None:
    page.set_content(_PAGE_WITH_FORM_INSIDE_IFRAME)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(
        page, store, run_logger, first_name_override="Levent", last_name_override="Aksan"
    )

    frame = page.frame_locator("iframe")
    assert frame.locator('input[placeholder="First Name"]').input_value() == "Levent"
    assert frame.locator('input[placeholder="Last Name"]').input_value() == "Aksan"
    assert frame.locator('input[placeholder="Email"]').input_value() == result.identity.email
    assert result.submitted is True
    # The onclick handler sets *the iframe's own* document.title, not the
    # outer page's -- confirm the click actually landed there.
    iframe_frame = next(f for f in page.frames if f != page.main_frame)
    assert iframe_frame.title() == "submitted"
    store.close()


# Mirrors digifabster.com's lazily-loaded HubSpot embed: nothing is present
# at all when the page first settles, and the real form only appears a few
# seconds later once the widget's own script finishes initializing.
_PAGE_WITH_DELAYED_LAZY_FORM = """
<html><body>
<div id="lazy"></div>
<script>
setTimeout(() => {
  document.getElementById('lazy').innerHTML =
    '<input placeholder="Email" type="email" />' +
    '<input placeholder="Password" type="password" />' +
    '<button onclick="document.title=\\'submitted\\'">Sign up</button>';
}, 2500);
</script>
</body></html>
"""


def test_retries_before_giving_up_on_a_lazily_loaded_form(tmp_path: Path, page: Page) -> None:
    page.set_content(_PAGE_WITH_DELAYED_LAZY_FORM)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.submitted is True
    assert page.title() == "submitted"
    store.close()


# --- Email verification gate (user-requested: flag it, never guess a link) --

# Mirrors a common post-signup gate: the form submits fine, but the account
# isn't usable until a link in a real inbox is clicked -- something this
# project can't do on its own (see registration.py's TempEmailProvider note).
_FORM_THEN_EMAIL_VERIFICATION_GATE = """
<html><body>
<div id="form">
  <input type="email" placeholder="Email" />
  <input type="password" placeholder="Password" />
  <button onclick="
    document.getElementById('form').style.display='none';
    document.getElementById('verify').style.display='block';
  ">Sign up</button>
</div>
<div id="verify" style="display:none">
  <h2>Check your email</h2>
  <p>We sent you a link to verify your account. Please check your email to confirm your account.</p>
</div>
</body></html>
"""


def test_flags_email_verification_gate_and_stops(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_THEN_EMAIL_VERIFICATION_GATE)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.submitted is True
    assert result.email_verification_required is True

    evidence = store.for_run(run_logger.run_id)
    verify_shots = [e for e in evidence if e.label == "registration pending email verification"]
    assert len(verify_shots) == 1
    assert verify_shots[0].screenshot is not None

    attempt = next(e for e in evidence if e.label == "registration attempt")
    assert attempt.visible_ui["email_verification_required"] is True
    store.close()


# Regression (phishingbox.com): the button that reveals the "check your
# email" gate doesn't match any of _SUBMIT_BUTTON_TEXTS, so _click_submit
# never fires and the click instead goes through the choice-picker fallback
# -- `submitted` stays False even though the site plainly already emailed a
# verification link. A configured, working inbox must still get used here,
# not skipped just because our own submit-button wording heuristic missed
# the click.
_FORM_THEN_EMAIL_VERIFICATION_GATE_VIA_UNRECOGNIZED_BUTTON = """
<html><body>
<div id="form">
  <button onclick="
    document.getElementById('form').style.display='none';
    document.getElementById('verify').style.display='block';
  ">Go</button>
</div>
<div id="verify" style="display:none">
  <h2>Check your email</h2>
  <p>We sent you a link to verify your account. Please check your email to confirm your account.</p>
</div>
</body></html>
"""


def test_opens_verification_link_when_stuck_on_gate_without_a_recognized_submit_click(
    tmp_path: Path, page: Page
) -> None:
    page.set_content(_FORM_THEN_EMAIL_VERIFICATION_GATE_VIA_UNRECOGNIZED_BUTTON)
    store, run_logger = _store_and_logger(tmp_path)
    verify_url = _data_url("<html><body><h1>You're verified!</h1></body></html>")
    provider = _FakeTempEmailProvider(link=verify_url)

    result = run_registration(page, store, run_logger, max_steps=5, temp_email_provider=provider)

    assert result.submitted is False
    assert result.email_verification_required is True
    assert result.verification_link_opened is True
    assert "verified" in page.content().lower()
    store.close()


# --- Phone verification gate (GRO-??: flag it, we have no way to solve it) --

# Mirrors a phone/SMS gate with no email step at all -- the form submits
# fine, but the account needs a code texted/called to a real phone number,
# something this project has no way to receive.
_FORM_THEN_PHONE_VERIFICATION_GATE = """
<html><body>
<div id="form">
  <input type="email" placeholder="Email" />
  <input type="password" placeholder="Password" />
  <button onclick="
    document.getElementById('form').style.display='none';
    document.getElementById('verify').style.display='block';
  ">Sign up</button>
</div>
<div id="verify" style="display:none">
  <h2>Please Verify Your Phone Number</h2>
  <p>To protect your account, we need to send a phone verification code.</p>
</div>
</body></html>
"""


def test_flags_phone_verification_gate_and_stops(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_THEN_PHONE_VERIFICATION_GATE)
    store, run_logger = _store_and_logger(tmp_path)

    result = run_registration(page, store, run_logger, max_steps=5)

    assert result.submitted is True
    assert result.phone_verification_required is True

    evidence = store.for_run(run_logger.run_id)
    verify_shots = [e for e in evidence if e.label == "registration pending phone verification"]
    assert len(verify_shots) == 1

    attempt = next(e for e in evidence if e.label == "registration attempt")
    assert attempt.visible_ui["phone_verification_required"] is True
    store.close()


# Regression (shippingeasy.com): the two gates chain -- the email
# verification link opens successfully ("Success! Your email is now
# verified.") but lands directly on a phone-verification screen. This must
# still get flagged even though it's discovered only after
# _maybe_open_verification_link's own goto, not by the main loop's stuck
# branch (there's nothing "stuck" about it -- the email link genuinely
# worked).
def test_flags_phone_verification_gate_reached_via_email_link(tmp_path: Path, page: Page) -> None:
    page.set_content(_FORM_THEN_EMAIL_VERIFICATION_GATE)
    store, run_logger = _store_and_logger(tmp_path)
    phone_gate_url = _data_url(
        "<html><body><h2>Please Verify Your Phone Number</h2>"
        "<p>we need to send a phone verification code</p></body></html>"
    )
    provider = _FakeTempEmailProvider(link=phone_gate_url)

    result = run_registration(page, store, run_logger, max_steps=5, temp_email_provider=provider)

    assert result.verification_link_opened is True
    assert result.phone_verification_required is True
    store.close()
