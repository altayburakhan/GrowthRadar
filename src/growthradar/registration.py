"""Best-effort registration/onboarding flow automation.

Fills sign-up forms across multiple steps using resilient heuristic selectors
(role/label/attribute matching, not one site's specific markup), submits, and --
if a TempEmailProvider is supplied -- waits for and opens the verification link.
Never raises: a failed registration is logged and returned as a result, so
exploration can continue elsewhere (Linear.md "Error Recovery").
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from typing import Any
from urllib.parse import urljoin

from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import Frame, Locator, Page

from growthradar.browser import dismiss_overlays, retry, wait_for_stable
from growthradar.config import Config
from growthradar.event_log import RunLogger
from growthradar.evidence import EvidenceStore
from growthradar.exploration import _SIGNUP_KEYWORDS, _in_scope
from growthradar.identity import Identity, generate_identity
from growthradar.screenshot import ScreenshotKind, capture_and_record
from growthradar.temp_email import TempEmailProvider, TempInbox
from growthradar.vision_fallback import suggest_click_target

logger = logging.getLogger(__name__)

# Choice-wizard steps (GRO-34) can need more iterations than a plain 2-3
# step form: a persona pick, a project-type pick, a region check, and the
# real field-filling step can each consume one iteration on their own
# (seen on ansarada.com's signup wizard).
MAX_FORM_STEPS = 8
# A step-count budget alone doesn't bound wall-clock time -- each iteration's
# own actions are individually timeout-bounded, but on a real, heavily
# anti-bot-instrumented signup page (seen live on squareup.com: the tab's own
# JS pegged a CPU core, and the browser stopped responding to our commands in
# any timely way) those per-action timeouts can each still take their full
# duration back-to-back, and MAX_FORM_STEPS=8 iterations of that adds up to
# a run that never converges within a sane amount of time. This is the
# backstop: once elapsed, stop trying no matter which step we're on.
_MAX_REGISTRATION_SECONDS = 180.0
_MAX_VISION_ATTEMPTS = 2
# See the "nothing found anywhere yet" branch in _run_registration's loop --
# a lazily-loaded embedded form (e.g. HubSpot) can take several seconds
# after the page otherwise looks settled to actually inject its fields.
_MAX_EMPTY_RETRIES = 2
_EMPTY_RETRY_WAIT_MS = 2000
_CLAIMED_MARKER = "data-growthradar-filled"

# Ordered most-specific first so a broad pattern (e.g. "first name") is tried
# before a narrower one could ever mis-claim its field.
# "fname"/"lname" are deliberately excluded despite being a real convention
# some forms use: _find_field's fallback selector matches them as a raw CSS
# attribute substring, and "lname" is itself a substring of "fullname" --
# a single "Full name" field (name="fullname", e.g. blocksurvey.io) would
# otherwise get wrongly claimed here as a last-name field before full_name's
# own pattern ever runs.
# full_name deliberately excludes a bare "name" keyword: a plain substring
# check would also match any "<X> Name" organization field (e.g. "Church
# Name"), wrongly filling it with the person's name instead of the company
# name. Those fields are left for _fill_unclaimed_generic_name_fields, which
# runs after this and fills anything still-unclaimed containing the word
# "name" with identity.company_name -- including a genuinely bare "Name"
# field with no "full"/"your" qualifier, an accepted tradeoff since that
# phrasing is rare and the field still gets a plausible, non-empty value.
# confirm_password must precede password: password's input_type="password"
# shortcut in _find_field grabs the first unclaimed type=password field
# regardless of keyword, so without this ordering it would claim whichever
# password field comes first in the DOM -- possibly "Confirm password"
# itself -- before confirm_password's own label-text match ever gets a
# chance, leaving the *other* password field unfilled (seen live on
# conceptboard.com: "Confirm password" was left empty, and the site's own
# "Passwords do not match" validation blocked submission).
_FIELD_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # "e-mail"/"mail" alongside "email" -- seen live on onlypult.com:
    # name="RegisterForm[mail]", id="registerform-mail", label "E-mail"
    # (hyphenated), input type="text" (not type="email", so the input_type
    # shortcut in _find_field doesn't catch it either). None of those
    # attributes contain "email" as a contiguous substring, so the field was
    # never matched or filled at all.
    ("email", ("email", "e-mail", "mail")),
    (
        "confirm_password",
        (
            "confirm password",
            "confirm your password",
            "repeat password",
            "verify password",
            "re-enter password",
            "re enter password",
            "password confirmation",
        ),
    ),
    ("password", ("password", "pwd")),
    ("first_name", ("first name", "firstname", "first-name", "given name")),
    ("last_name", ("last name", "lastname", "last-name", "surname", "family name")),
    ("company_name", ("company", "organization", "organisation", "workspace", "business name")),
    ("country", ("country",)),
    ("date_of_birth", ("date of birth", "birth date", "birthday", "dob")),
    ("phone", ("phone", "mobile", "telephone")),
    ("website", ("website", "web site", "url")),
    ("full_name", ("full name", "fullname", "full-name", "your name")),
)

_SUBMIT_BUTTON_TEXTS: tuple[str, ...] = (
    "Continue",
    "Next",
    "Sign up",
    "Create account",
    "Get started",
    "Start trial",
    "Start free trial",
    "Register",
    "Submit",
)
# A code-confirmation screen's button almost never says any of the above --
# "Verify"/"Confirm" specifically, not a generic signup CTA -- so
# _fill_verification_code's submit click needs its own list layered on top,
# not a global change to _SUBMIT_BUTTON_TEXTS (which would risk a "Verify"
# button winning a match somewhere it shouldn't, e.g. an unrelated identity
# check earlier in a wizard).
_CODE_CONFIRM_BUTTON_TEXTS: tuple[str, ...] = (*_SUBMIT_BUTTON_TEXTS, "Verify", "Confirm")

# See _check_consent_checkboxes: only check a box whose label matches one of
# these AND none of _SKIP_CHECKBOX_KEYWORDS -- mandatory legal consent, never
# an opt-in.
_CONSENT_CHECKBOX_KEYWORDS: tuple[str, ...] = (
    "agree",
    "accept",
    "terms",
    "privacy policy",
    "consent",
)
_SKIP_CHECKBOX_KEYWORDS: tuple[str, ...] = (
    "opt-in",
    "opt in",
    "optional",
    "marketing",
    "newsletter",
    "subscribe",
    "promotional",
)

# A generic label like "Continue" substring-matches "Continue with Google" --
# without this check, `_click_submit` would launch a third-party OAuth flow
# instead of submitting the actual form (observed on 100hires.com: it clicked
# "Continue with Google", got rejected by Google's bot detection, and the run
# wandered onto google.com's own pages instead of the target site).
_OAUTH_BUTTON_KEYWORDS: tuple[str, ...] = (
    "google",
    "microsoft",
    "apple",
    "github",
    "gitlab",
    "linkedin",
    "facebook",
    "twitter",
    "sso",
    "saml",
    "okta",
)


@dataclass(frozen=True)
class RegistrationResult:
    identity: Identity
    steps_completed: int
    submitted: bool
    verification_link_opened: bool
    email_verification_required: bool = False
    phone_verification_required: bool = False
    verification_code_entered: bool = False
    error: str | None = None


def _is_claimed(locator: Locator) -> bool:
    try:
        return bool(locator.get_attribute(_CLAIMED_MARKER))
    except PlaywrightError:
        return True


def _claim(locator: Locator) -> None:
    with suppress(PlaywrightError):
        locator.evaluate(f"el => el.setAttribute('{_CLAIMED_MARKER}', 'true')")


def _visible_unclaimed(locator: Locator) -> Locator | None:
    """First visible, unclaimed match -- scans every match, not just the
    first, since a later step's hidden field (still `display:none` until its
    step is reached) can share the same selector/label and sort earlier in
    DOM order than the field actually on screen right now (seen live on
    huddleup.ai's onboarding: a hidden "email" input for step 2 precedes the
    visible step-1 email input in the DOM, so stopping at `.first` found
    nothing and the visible field was never filled)."""
    try:
        count = locator.count()
    except PlaywrightError:
        return None
    for i in range(count):
        candidate = locator.nth(i)
        try:
            if candidate.is_visible() and not _is_claimed(candidate):
                return candidate
        except PlaywrightError:
            continue
    return None


def _find_field(
    frame: Frame, keywords: tuple[str, ...], *, input_type: str | None = None
) -> Locator | None:
    if input_type:
        candidate = _visible_unclaimed(frame.locator(f'input[type="{input_type}"]'))
        if candidate is not None:
            return candidate

    for keyword in keywords:
        candidate = _visible_unclaimed(frame.get_by_label(keyword, exact=False))
        if candidate is not None:
            return candidate

        selector = (
            f'input[name*="{keyword}" i], input[id*="{keyword}" i], '
            f'input[placeholder*="{keyword}" i], input[aria-label*="{keyword}" i], '
            f'select[name*="{keyword}" i], select[id*="{keyword}" i], '
            f'select[aria-label*="{keyword}" i]'
        )
        candidate = _visible_unclaimed(frame.locator(selector))
        if candidate is not None:
            return candidate

    return None


def _select_option_best_effort(locator: Locator, value: str) -> bool:
    """Country (and similar) fields are almost always a <select>, where a plain
    `.fill()` doesn't apply. Try an exact option-label match first, then fall
    back to the first option whose visible text contains `value` -- dropdown
    option text often differs slightly from our stored value (e.g. "USA" vs
    "United States")."""
    try:
        locator.select_option(label=value, timeout=2000)
        return True
    except PlaywrightError:
        pass

    try:
        option_texts = locator.locator("option").all_inner_texts()
    except PlaywrightError:
        return False
    for option_text in option_texts:
        if value.lower() in option_text.lower():
            try:
                locator.select_option(label=option_text, timeout=2000)
                return True
            except PlaywrightError:
                return False
    return False


def _set_field_value(locator: Locator, value: str) -> bool:
    try:
        tag = locator.evaluate("el => el.tagName.toLowerCase()")
    except PlaywrightError:
        return False
    if tag == "select":
        return _select_option_best_effort(locator, value)
    try:
        locator.fill(value, timeout=2000)
        # `.fill()` sets the value and fires input/change but never a real
        # keystroke -- some sites gate submit-button enablement on
        # `onkeyup` specifically (seen live on huddleup.ai: the field fills
        # fine, but the submit button stays disabled forever without this,
        # since its enable handler only runs on keyup). A synthetic keyup
        # with no key data is enough wherever the handler just re-reads the
        # input's current value, which covers this pattern generally.
        with suppress(PlaywrightError):
            locator.dispatch_event("keyup")
        return True
    except PlaywrightError:
        return False


def _fill_visible_fields(frame: Frame, identity: Identity) -> int:
    values = {
        "email": identity.email,
        "password": identity.password,
        # Same value as "password" -- a confirm/repeat field must match the
        # original for the site's own validation to accept it.
        "confirm_password": identity.password,
        "first_name": identity.first_name,
        "last_name": identity.last_name,
        "company_name": identity.company_name,
        "country": identity.country,
        "date_of_birth": identity.date_of_birth,
        "phone": identity.phone,
        "website": identity.website,
        "full_name": identity.full_name,
    }
    filled = 0
    for field_key, keywords in _FIELD_PATTERNS:
        input_type = {
            "email": "email",
            "password": "password",
            "phone": "tel",
            "website": "url",
        }.get(field_key)
        locator = _find_field(frame, keywords, input_type=input_type)
        if locator is None:
            continue
        if not _set_field_value(locator, values[field_key]):
            continue
        _claim(locator)
        filled += 1
    return filled


def _fill_unclaimed_empty_selects(frame: Frame) -> int:
    """Some forms have a <select> with no mapping to our identity data --
    e.g. "How did you hear about us?" -- left at its empty placeholder value.
    Many such fields are validated as required via JS rather than the HTML
    `required` attribute (seen on trial.signinapp.com's react-hook-form-style
    validation), so there's no attribute to detect this by; leaving it unset
    silently blocks submission. Any real answer satisfies that validation, so
    pick the first non-disabled, non-empty option -- low risk since this is a
    generated identity with no real-world stake in the answer.
    """
    try:
        selects = frame.locator("select")
        count = selects.count()
    except PlaywrightError:
        return 0

    filled = 0
    for i in range(count):
        select = selects.nth(i)
        try:
            if not select.is_visible() or _is_claimed(select):
                continue
            current_value = select.evaluate("el => el.value")
            if current_value:
                continue
            option_values = select.locator("option").evaluate_all(
                "opts => opts.filter(o => !o.disabled).map(o => o.value).filter(v => v)"
            )
        except PlaywrightError:
            continue
        if not option_values:
            continue
        try:
            select.select_option(value=option_values[0], timeout=2000)
        except PlaywrightError:
            continue
        _claim(select)
        filled += 1
    return filled


def _input_hint_text(locator: Locator) -> str:
    """name/id/placeholder/aria-label plus any associated <label>'s text --
    a broader "what is this field called" signal than _find_field's per-
    keyword selectors, used by _fill_unclaimed_generic_name_fields to
    inspect a field it already has a handle on rather than search for one."""
    try:
        return str(
            locator.evaluate(
                "el => [el.name, el.id, el.placeholder, el.getAttribute('aria-label'), "
                "(el.labels && el.labels[0] && el.labels[0].innerText) || ''].join(' ')"
            )
        )
    except PlaywrightError:
        return ""


def _qualifier_hint_text(locator: Locator) -> str:
    """Same signal as _input_hint_text but excludes the placeholder --
    used only for the bare-vs-qualified decision in _is_bare_name_hint, not
    the initial "is this a name field at all" gate (which still needs
    placeholder, since some fields -- e.g. Synder's bare "Name" input -- have
    no other signal at all). A placeholder is example VALUE text ("Jane
    Doe", "jane.doe@company.com"), not a qualifier describing what the field
    IS; folding it into the qualifier check read Conceptboard's real "Name"
    field (label "Name", placeholder "Jane Doe") as organization-qualified
    and filled it with the company name instead of the person's.
    """
    try:
        return str(
            locator.evaluate(
                "el => [el.name, el.id, el.getAttribute('aria-label'), "
                "(el.labels && el.labels[0] && el.labels[0].innerText) || ''].join(' ')"
            )
        )
    except PlaywrightError:
        return ""


_NAME_WORD_RE = re.compile(r"\bname\b")
_NAME_QUALIFIER_STRIP_RE = re.compile(r"\b(name|your|full)\b")
_ALPHA_WORD_RE = re.compile(r"[a-z]{3,}")


def _is_bare_name_hint(hint: str) -> bool:
    """True for a field whose only signal is "Name"/"Your Name"/"Full Name"
    -- no organization-style qualifier like "Church" or "Company". Stripping
    "name"/"your"/"full" as whole words and checking what's left tells the
    two apart: "Church Name" leaves "church" behind, a bare "Name" field
    leaves nothing. Ambiguous fields default to a person's name (not a
    company one) -- seen live on Synder's post-"Continue with Email" form
    (cloudbusinesshq.com): a single "Name" input beside Email/Password, no
    company field anywhere on screen at all. `hint` should come from
    _qualifier_hint_text, not _input_hint_text -- see there for why.
    """
    residual = _NAME_QUALIFIER_STRIP_RE.sub("", hint)
    return not _ALPHA_WORD_RE.search(residual)


def _fill_unclaimed_generic_name_fields(frame: Frame, identity: Identity) -> int:
    """Catch-all for name-shaped fields _FIELD_PATTERNS' fixed keyword lists
    can't enumerate in advance: organization-style "<Industry> Name" fields
    ("Church Name", "Clinic Name", ...) get `identity.company_name`; a bare,
    unqualified "Name" field (see `_is_bare_name_hint`) gets
    `identity.full_name` instead. By the time this runs, anything
    _fill_visible_fields' more specific first/last/full-name patterns should
    have matched is already claimed, so this only reaches fields those
    patterns didn't recognize. Matching "name" as a whole word (not a plain
    substring) is deliberate: a compound like "Username" contains "name" as a
    substring but isn't one -- filling it with a value that can contain a
    space would break that field's validation, the exact class of error this
    whole feature exists to avoid. Same low-risk, no-real-world-stake
    reasoning as _fill_unclaimed_empty_selects.
    """
    try:
        inputs = frame.locator('input[type="text"], input:not([type])')
        count = inputs.count()
    except PlaywrightError:
        return 0

    filled = 0
    for i in range(count):
        candidate = inputs.nth(i)
        try:
            if not candidate.is_visible() or _is_claimed(candidate):
                continue
            if candidate.input_value():
                continue
        except PlaywrightError:
            continue
        hint = _input_hint_text(candidate).lower()
        if not _NAME_WORD_RE.search(hint):
            continue
        qualifier_hint = _qualifier_hint_text(candidate).lower()
        value = identity.full_name if _is_bare_name_hint(qualifier_hint) else identity.company_name
        if not _set_field_value(candidate, value):
            continue
        _claim(candidate)
        filled += 1
    return filled


def _checkbox_label_text(checkbox: Locator) -> str:
    try:
        text = checkbox.evaluate(
            "el => {"
            ' const byFor = el.id && document.querySelector(`label[for="${el.id}"]`);'
            " const wrap = el.closest('label');"
            " return (byFor && byFor.innerText) || (wrap && wrap.innerText) || ''; }"
        )
        return str(text or "")
    except PlaywrightError:
        return ""


def _is_checkbox_checked(box: Locator, *, native: bool) -> bool:
    if native:
        try:
            return box.is_checked()
        except PlaywrightError:
            return True
    try:
        return box.get_attribute("aria-checked") == "true"
    except PlaywrightError:
        return True


def _check_consent_checkboxes(frame: Frame) -> int:
    """Check unchecked, visible checkboxes that look like a mandatory
    "I agree to the Terms/Privacy Policy" consent gate -- common on signup
    forms and not always marked HTML `required` (seen on trial.signinapp.com),
    so submission silently fails without this. Explicitly skips anything that
    reads like an opt-in marketing/newsletter checkbox: never subscribe a
    generated identity to marketing on the target's behalf.

    Matches both a native `<input type="checkbox">` and a `role="checkbox"`
    element (Radix/shadcn-style component libraries render the actual
    clickable control as a `<button role="checkbox">` with `aria-checked`,
    leaving a `pointer-events: none`, `aria-hidden` shadow `<input>` purely
    for form submission -- `.check()` on that shadow input times out, so the
    two need different check-state and click strategies).

    A native `<input>` can still fail to `.check()` even though it's the
    real control: some component libraries (seen live on statusbrew.com, an
    Angular Material-style checkbox) render a custom visual indicator
    directly on top of the native input for styling, which intercepts
    pointer events at the input's own screen location -- Playwright
    correctly refuses to click there rather than risk clicking the wrong
    thing. Falling back to a click on the wrapping `<label>` (which forwards
    a real click to its input via standard browser behavior, and is present
    here specifically because that's also where this function's own label
    text came from) recovers cleanly instead of leaving the box unchecked
    and the "I agree..." gate permanently blocking submission.
    """
    try:
        boxes = frame.locator('input[type="checkbox"], [role="checkbox"]')
        count = boxes.count()
    except PlaywrightError:
        return 0

    checked = 0
    for i in range(count):
        box = boxes.nth(i)
        try:
            native = box.evaluate("el => el.tagName.toLowerCase()") == "input"
            if not box.is_visible() or _is_checkbox_checked(box, native=native) or _is_claimed(box):
                continue
        except PlaywrightError:
            continue

        label = _checkbox_label_text(box).lower()
        if not label or any(kw in label for kw in _SKIP_CHECKBOX_KEYWORDS):
            continue
        if not any(kw in label for kw in _CONSENT_CHECKBOX_KEYWORDS):
            continue

        try:
            if native:
                try:
                    box.check(timeout=2000)
                except PlaywrightError:
                    wrapping_label = box.locator("xpath=ancestor::label[1]")
                    if wrapping_label.count() == 0:
                        raise
                    wrapping_label.first.click(timeout=2000)
                    if not box.is_checked():
                        # The label click landed on a different control
                        # instead (seen live with the Radix/shadcn shadow-
                        # input pattern above: the same label also wraps a
                        # role="checkbox" button, and browsers forward a
                        # label click to the first labelable descendant --
                        # the button, not this input -- toggling *its*
                        # state instead of this shadow input's, which never
                        # reflects a checked state at all). Nothing useful
                        # happened to *this* box, so treat it the same as
                        # any other failed attempt rather than claiming a
                        # false success.
                        raise PlaywrightError("label click did not check this box") from None
            else:
                box.click(timeout=2000)
        except PlaywrightError:
            continue
        _claim(box)
        checked += 1
    return checked


def _words(text: str) -> set[str]:
    return {w for w in text.lower().split() if w}


_OAUTH_PHRASE_RE = re.compile(r"\b(with|via)\b")


def _is_oauth_text(lowered: str, *, allow_google: bool = False) -> bool:
    matched_providers = [keyword for keyword in _OAUTH_BUTTON_KEYWORDS if keyword in lowered]
    if matched_providers:
        # Google-only opt-in (config.allow_google_oauth + a signed-in
        # google_profile_dir, see browser.py): a button naming Google *and*
        # some other provider ("Google Workspace SSO") still isn't one we
        # can complete, so only an exact single-provider match is exempted.
        if allow_google and matched_providers == ["google"]:
            return False
        return True
    # "Sign in/up/continue with/via <provider>" for a provider this project
    # doesn't (and can't exhaustively) enumerate in _OAUTH_BUTTON_KEYWORDS --
    # Xero, Intuit, Shopify, Salesforce, ... (seen live on Synder's signup,
    # reached from cloudbusinesshq.com: "Continue with Xero"/"Sign in with
    # Intuit" sit right next to the "Continue with Email" button we DO want
    # to click). Any "with"/"via" phrase not naming email is treated as a
    # third-party connector -- same enumeration-avoidance reasoning as
    # _fill_unclaimed_generic_name_fields (GRO-40).
    return bool(_OAUTH_PHRASE_RE.search(lowered)) and "email" not in lowered


# Reuses exploration.py's _SIGNUP_KEYWORDS (imported above) so "what counts as
# a signup CTA" is defined in exactly one place.
_LOGIN_KEYWORDS: tuple[str, ...] = ("log in", "login", "sign in")

_CLICKABLE_SELECTOR = 'button, a, [role="button"]'


@dataclass(frozen=True)
class _ClickCandidate:
    """One clickable element's already-fetched text/visibility/claimed/href
    -- everything every click-matching function below needs, without going
    back to the browser per candidate."""

    index: int
    text: str
    visible: bool
    claimed: bool
    href: str | None


# Combines what used to be several separate per-candidate Playwright calls
# (.is_visible(), .get_attribute(claimed marker), .inner_text()/.get_attribute
# fallback for text, .get_attribute('href')) into the fields every caller
# below needs, computed entirely in the page's own JS engine.
_CANDIDATE_SCAN_JS = f"""
(els) => els.map((el) => {{
    const style = getComputedStyle(el);
    const visible = style.visibility !== 'hidden' && style.display !== 'none'
        && el.getClientRects().length > 0;
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    return {{
        visible,
        text,
        claimed: el.hasAttribute('{_CLAIMED_MARKER}'),
        href: el.getAttribute('href'),
    }};
}})
"""


def _scan_clickable(scope: Frame | Page, selector: str) -> tuple[Locator, list[_ClickCandidate]]:
    """One `evaluate_all()` round trip for every element matching `selector`,
    instead of a separate Playwright call per candidate per caller.

    The functions below each used to call .is_visible()/.get_attribute()/
    .inner_text() (the latter with its own up-to-500ms actionability wait)
    once per candidate, and _click_submit did that *again* for every one of
    its ~8 target phrases -- fine on a small form, but O(button_texts *
    candidates) round trips stalls for tens of seconds to minutes on a
    content-heavy page (seen live on web.hr/pricing: ~80 buttons/links/FAQ-
    toggles alongside the two real signup buttons burned almost the entire
    180s registration budget on scanning alone, never reaching a real click).
    mirro.io's 188-link landing page did the same to _find_clickable_by_
    keywords even after an earlier partial fix (_fast_visible_text) cut it
    to one round trip per candidate -- still O(candidates), just with a
    smaller constant.

    The returned Locator is the same live `selector` query -- `.nth(index)`
    re-resolves fresh against the current DOM at click time, so this is
    never any *more* stale than the per-candidate approach it replaces (if
    anything, less: the whole scan now takes one round trip instead of
    however many seconds/minutes the old version spent between the first
    candidate check and the eventual click).
    """
    candidates = scope.locator(selector)
    try:
        raw: list[dict[str, Any]] = candidates.evaluate_all(_CANDIDATE_SCAN_JS)
    except PlaywrightError:
        return candidates, []
    return candidates, [
        _ClickCandidate(
            index=i,
            text=str(item.get("text") or ""),
            visible=bool(item.get("visible")),
            claimed=bool(item.get("claimed")),
            href=item.get("href"),
        )
        for i, item in enumerate(raw)
    ]


def _click_submit(
    frame: Frame,
    *,
    allow_reclaim: bool = False,
    allow_google_oauth: bool = False,
    button_texts: tuple[str, ...] = _SUBMIT_BUTTON_TEXTS,
) -> bool:
    # A CSS selector, not get_by_role(name=..., exact=False): Playwright's
    # substring matching there requires our target phrase to appear as an
    # unbroken run of words in the element's accessible name, which fails on
    # real copy like "Start MY free trial" (a real-world example) that
    # inserts a word into "Start free trial". Word-subset matching below
    # (every word of our phrase present, in any order/position) is more
    # forgiving without being so loose it clicks unrelated buttons.
    candidates, infos = _scan_clickable(
        frame, 'button, a, input[type="submit"], input[type="button"], [role="button"]'
    )

    for text in button_texts:
        target_words = _words(text)
        for info in infos:
            if not info.visible:
                continue
            if info.claimed and not allow_reclaim:
                continue
            if not target_words <= _words(info.text):
                continue
            if _is_oauth_text(info.text.lower(), allow_google=allow_google_oauth):
                continue
            candidate = candidates.nth(info.index)
            try:
                candidate.click(timeout=3000)
            except PlaywrightError:
                continue
            # Claim it like a filled field (see _fill_visible_fields): a
            # multi-step form's submit button often stays visible with
            # identical text after a click that didn't actually advance
            # anything (e.g. a validation error re-shows the same "Register"
            # button) -- without this, the loop just re-clicks the same
            # element every remaining iteration up to max_steps, inflating
            # steps_completed with no real progress. A genuinely new button
            # on a later step (a different DOM node) is unclaimed and still
            # gets found normally. `allow_reclaim` (set by the caller only
            # when this iteration made real progress elsewhere -- a field
            # filled or a choice-group option picked) is the escape hatch for
            # SPA wizards that reuse the very same "Next" node across steps
            # (seen on ansarada.com): without it, that node stays claimed
            # forever after its first legitimate click and the wizard can
            # never advance past its second step.
            _claim(candidate)
            return True
    return False


# Multi-step "choice wizard" onboarding (seen on ansarada.com: "Who needs the
# data room?" -> persona cards -> project-type cards -- none of them <input>
# or <select>, just clickable divs/buttons with no mapping to identity data)
# has no field to fill and no submit-text button to click, so the fill loop
# would otherwise stall immediately with steps_completed=0 even though a
# working signup flow is right there. Any one answer is enough to satisfy the
# gate (this is a generated identity with no real-world stake in the
# answer), so as a last resort -- only tried once nothing else in this
# iteration matched -- click the first plausible, unclaimed option.
_CHOICE_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "back",
    "cancel",
    "close",
    "contact",
    "demo",
    "help",
    "home",
    "menu",
    # Legal-document links sitting right next to a signup form's own submit
    # button (seen live on onlypult.com: "By clicking Sign up you agree to
    # the Terms of Use, Privacy Policy, License Agreement" inline under the
    # form, not inside a <footer>, so the nav/footer exclusion in
    # _CHOICE_CLICKABLE_SELECTOR below doesn't catch them) were an otherwise
    # unexcluded, always-present "plausible option" -- once the real submit
    # button got claimed after one attempt, later iterations kept finding
    # and clicking these instead, each opening a new tab (see
    # _switch_to_new_page) and abandoning the actual, still-unsubmitted
    # form.
    "terms",
    "privacy policy",
    "license agreement",
    "cookie policy",
    *_LOGIN_KEYWORDS,
    # Deliberately NOT *_SIGNUP_KEYWORDS: a real primary signup CTA living in
    # the page body rather than <nav>/<header> (so _CHOICE_CLICKABLE_SELECTOR's
    # own nav/header/footer exclusion doesn't already catch it) almost always
    # *is* signup-flavored text ("Sign Up on yearly plan and save", "Free
    # Sign Up") -- excluding it here made the `best` bucket below (submit-like
    # AND signup-flavored, meant to be the top pick) permanently unreachable,
    # so this fell through to clicking the first unrelated eligible element
    # instead (seen live on web.hr/pricing: clicked a "Payroll" module-price
    # toggle, then the next FAQ accordion, etc., one per iteration, never the
    # two real "Sign Up" buttons two rows above them -- registration never
    # progressed past this page at all).
)
# :is(...) wraps the comma-separated _CLICKABLE_SELECTOR into one compound
# selector so :not() excludes nav/header/footer descendants from all three
# alternatives -- `f"{_CLICKABLE_SELECTOR}:not(...)"` would silently attach
# :not() to only the last alternative ([role="button"]), leaving plain
# <button>/<a> elements inside <nav> unfiltered.
#
# The `[class*="language" i]`/`[class*="locale" i]` clauses exclude
# language-switcher widgets and the locale options they reveal (seen live on
# onlypult.com: <button class="... languages-popover-link"> inside
# <div class="language-picker">, sitting in the page body outside nav/header/
# footer). These aren't excludable by keyword, since the toggle's own visible
# text is just the current locale's name ("English") -- a value keyword-matching
# can't anticipate. Left unexcluded, the choice-picker fallback clicked this
# toggle (the first unclaimed candidate in DOM order), which opened a popover
# of other locale names; the next iteration then clicked one of *those*
# ("Deutsch"), switching the entire page to German and derailing registration
# instead of re-submitting the still-unsubmitted form.
_CHOICE_CLICKABLE_SELECTOR = (
    f":is({_CLICKABLE_SELECTOR}):not("
    "nav *, header *, footer *, "
    '[class*="language" i], [class*="language" i] *, '
    '[class*="locale" i], [class*="locale" i] *'
    ")"
)


def _is_submit_like(text: str) -> bool:
    words = _words(text)
    return any(_words(t) <= words for t in _SUBMIT_BUTTON_TEXTS)


# An auth-method chooser screen (OAuth/SSO/integration connect buttons plus
# one email option -- seen live on Synder's signup, reached from
# cloudbusinesshq.com) has no field to fill and no plain submit button
# either, so without this it would fall through to _click_unclaimed_choice_
# option and could pick any of the OAuth buttons essentially at random. Tried
# as its own step, before that generic fallback, so "Continue with Email" is
# always preferred over "Continue with Xero"/"Sign in with Intuit"/etc.
_CONTINUE_WITH_EMAIL_KEYWORDS: tuple[str, ...] = (
    "continue with email",
    "sign up with email",
    "sign in with email",
    "use email",
    "email instead",
)


def _words_in_order_near(
    target_words: list[str], candidate_words: list[str], *, max_gap: int = 1
) -> bool:
    """True if target_words all appear in candidate_words, in the same
    relative order, with at most `max_gap` other words between each
    consecutive pair. Stricter than a plain set-subset match: still
    tolerates a real site inserting a word into the target phrase ("Sign up
    with *your* email", seen live on dialpad.com -- one word, matches with
    max_gap=1), but rejects a candidate that merely happens to contain the
    same words scattered across an unrelated sentence (seen live on
    joinhomebase.com: "Sign in with your phone or email" -- a phone/email
    login chooser for an *existing* employee account, not an OAuth-vs-email
    chooser -- matched target "sign in with email" under plain set-subset
    matching purely because "your phone or" doesn't stop any of those four
    words from being present *somewhere*, and sent registration down the
    wrong branch of a 2-option screen entirely)."""
    idx = 0
    last_pos: int | None = None
    for word in target_words:
        try:
            pos = candidate_words.index(word, idx)
        except ValueError:
            return False
        if last_pos is not None and pos - last_pos - 1 > max_gap:
            return False
        last_pos = pos
        idx = pos + 1
    return True


def _is_continue_with_email(text: str) -> bool:
    candidate_words = text.lower().split()
    return any(
        _words_in_order_near(keyword.split(), candidate_words)
        for keyword in _CONTINUE_WITH_EMAIL_KEYWORDS
    )


def _click_continue_with_email(frame: Frame) -> bool:
    candidates, infos = _scan_clickable(frame, _CLICKABLE_SELECTOR)
    for info in infos:
        if not info.visible or info.claimed:
            continue
        if not _is_continue_with_email(info.text.strip()):
            continue
        candidate = candidates.nth(info.index)
        try:
            candidate.click(timeout=3000)
        except PlaywrightError:
            continue
        _claim(candidate)
        return True
    return False


# A post-signup security/preference interstitial (seen live on
# legalesign.com: "Set up a passkey") has no field to fill, just a "commit"
# button and a dismissal one -- without preferring the dismissal, the
# generic choice-picker below would click whichever comes first in the DOM,
# which is often the "commit" CTA (it's usually the visually primary
# button), sending the run into a WebAuthn/biometric flow it has no way to
# complete. We're here to reach the onboarding screen, not to opt into every
# optional security prompt along the way.
_SKIP_PROMPT_KEYWORDS: tuple[str, ...] = (
    "skip for now",
    "not now",
    "maybe later",
    "remind me later",
    "no thanks",
    "not right now",
    "ill do this later",
    "skip",
)


def _is_skip_prompt(text: str) -> bool:
    candidate_words = text.lower().replace("'", "").split()
    return any(
        _words_in_order_near(keyword.split(), candidate_words) for keyword in _SKIP_PROMPT_KEYWORDS
    )


def _click_skip_prompt(frame: Frame) -> bool:
    candidates, infos = _scan_clickable(frame, _CLICKABLE_SELECTOR)
    for info in infos:
        if not info.visible or info.claimed:
            continue
        if not _is_skip_prompt(info.text.strip()):
            continue
        candidate = candidates.nth(info.index)
        try:
            candidate.click(timeout=3000)
        except PlaywrightError:
            continue
        _claim(candidate)
        return True
    return False


def _is_google_oauth_candidate(text: str) -> bool:
    # Same "names Google and only Google" test _is_oauth_text uses to
    # decide whether to stop excluding a button -- factored out so the
    # priority click below and the exclusion check agree on what counts.
    lowered = text.lower()
    matched_providers = [keyword for keyword in _OAUTH_BUTTON_KEYWORDS if keyword in lowered]
    return matched_providers == ["google"]


def _click_google_oauth_button(frame: Frame) -> bool:
    """Click a bare "Google" (or "Continue with Google") button before
    anything else gets a chance to run this iteration.

    Only called when allow_google_oauth is on (see _run_registration).
    Without this, a page offering *both* a fillable email form and a Google
    button (seen live on joinblink.com: "Start your free trial" next to a
    plain "Google" button) always went the email route -- _fill_visible_
    fields fills the email field unconditionally, and _click_submit's
    phrase-matching finds "Start your free trial" before ever considering a
    button whose entire text is just "Google", which matches none of
    _SUBMIT_BUTTON_TEXTS at all. The whole point of allow_google_oauth is to
    complete the real Google identity instead of a generated one that then
    dead-ends on a verification code sent to a fake inbox, so this has to
    run before the fill step, not compete with it.
    """
    candidates, infos = _scan_clickable(frame, _CLICKABLE_SELECTOR)
    for info in infos:
        if not info.visible or info.claimed:
            continue
        if not _is_google_oauth_candidate(info.text):
            continue
        candidate = candidates.nth(info.index)
        try:
            candidate.click(timeout=3000)
        except PlaywrightError:
            continue
        _claim(candidate)
        return True
    return False


# Deliberately more specific than just "code" -- a bare "code" hint would
# also match "zip code"/"postal code"/"promo code"/"country code" fields,
# none of which are an email-verification step.
_CODE_FIELD_HINTS: tuple[str, ...] = (
    "verification code",
    "one-time code",
    "one time code",
    "confirmation code",
    "security code",
    "otp",
)
_CODE_PAGE_PHRASES: tuple[str, ...] = (
    "verification code",
    "enter your code",
    "enter the code",
    "one-time code",
    "one time code",
    "confirmation code",
    "digit code",
)


def _otp_box_candidates(frame: Frame) -> list[Locator]:
    # A row of single-character inputs (maxlength="1") is the standard
    # "6 separate boxes" OTP widget (seen live on joinblink.com) -- there's
    # no name/placeholder/label to match on for these, just this shape.
    try:
        candidates = frame.locator('input[maxlength="1"]')
        count = candidates.count()
    except PlaywrightError:
        return []
    boxes: list[Locator] = []
    for i in range(count):
        candidate = candidates.nth(i)
        try:
            if candidate.is_visible() and not _is_claimed(candidate):
                boxes.append(candidate)
        except PlaywrightError:
            continue
    return boxes


def _code_entry_visible(page: Page) -> bool:
    """Both a code-flavored phrase in the page text AND a matching field --
    same "never conclude from one signal" reasoning as
    _email_verification_required: page copy alone ("verification code")
    could still be marketing text on a page with nothing to fill."""
    try:
        text = page.inner_text("body").lower()
    except PlaywrightError:
        return False
    if not any(phrase in text for phrase in _CODE_PAGE_PHRASES):
        return False
    for frame in page.frames:
        if _otp_box_candidates(frame):
            return True
        if _find_field(frame, _CODE_FIELD_HINTS) is not None:
            return True
    return False


def _fill_verification_code(page: Page, code: str) -> bool:
    for frame in page.frames:
        boxes = _otp_box_candidates(frame)
        if len(boxes) == len(code):
            for box, digit in zip(boxes, code, strict=True):
                if not _set_field_value(box, digit):
                    return False
                _claim(box)
            return True

    for frame in page.frames:
        field = _find_field(frame, _CODE_FIELD_HINTS)
        if field is not None and _set_field_value(field, code):
            _claim(field)
            return True

    return False


def _is_offsite_href(href: str | None, page_url: str) -> bool:
    """True for an `<a href>` pointing off the current site entirely (a
    different registrable domain, not just a different path/subdomain --
    see exploration.py's _in_scope, which already treats a same-site
    subdomain like space.statusbrew.com as in-scope, so a genuine signup
    flow redirecting to an app subdomain is never excluded by this).

    Seen live on kamiapp.com's "Start free" choice screen (four product
    cards, no single form): a promotional banner link, "Find answers in the
    Kami Learning Hub", sits earlier in the DOM than any of the real
    product CTAs and matches none of _CHOICE_EXCLUDE_KEYWORDS, so the
    choice-picker clicked it first, landing on an unrelated HubSpot-hosted
    resource page instead of ever reaching a signup flow.
    """
    if not href:
        return False
    try:
        return not _in_scope(page_url, urljoin(page_url, href))
    except ValueError:
        return False


def _is_signup_flavored(text: str) -> bool:
    """True if every word of some known signup/trial phrase (_SIGNUP_KEYWORDS)
    appears in this candidate's own text, in any order/position -- the same
    forgiving word-subset matching _click_submit uses, needed here for the
    same reason: real button copy inserts words a fixed phrase list can't
    anticipate (e.g. kamiapp.com's "Create a free account", which doesn't
    contain "create account" as a contiguous substring)."""
    candidate_words = _words(text)
    return any(_words(keyword) <= candidate_words for keyword in _SIGNUP_KEYWORDS)


def _click_unclaimed_choice_option(page: Page, *, allow_google_oauth: bool = False) -> bool:
    candidates, infos = _scan_clickable(page, _CHOICE_CLICKABLE_SELECTOR)

    page_url = page.url
    eligible: list[tuple[int, str]] = []
    # Submit-like AND signup-flavored (e.g. kamiapp.com's "Create a free
    # account", which matches _SUBMIT_BUTTON_TEXTS' "Create account" via
    # word-subset) is unambiguously the best possible pick -- always
    # preferred outright, unlike a merely submit-like button with no
    # signup wording ("Continue"/"Next"), which is deliberately left alone
    # here for _click_submit to find on a later iteration instead (see the
    # `picked` guard below _click_unclaimed_choice_option's call site: this
    # function and _click_submit never both click within the same
    # iteration, to avoid clicking two different cards -- seen live on an
    # earlier kamiapp.com regression).
    best: list[tuple[int, str]] = []
    for info in infos:
        if not info.visible or info.claimed:
            continue
        text = info.text.strip()
        if not text:
            continue
        lowered = text.lower()
        if _is_oauth_text(lowered, allow_google=allow_google_oauth):
            continue
        if any(keyword in lowered for keyword in _CHOICE_EXCLUDE_KEYWORDS):
            continue
        signup_flavored = _is_signup_flavored(text)
        # Only applied to non-signup-flavored candidates: a real signup CTA
        # can itself route through a third-party click-tracking redirect
        # (seen live on kamiapp.com: "Create a free account"'s href is a
        # cta-service-cms2.hubspot.com tracking link, not kamiapp.com
        # itself, yet it's exactly the button we want) -- the offsite check
        # exists to catch genuinely unrelated content links like the
        # "Learning Hub" banner, not to second-guess text that already
        # reads as a clear signup CTA.
        if not signup_flavored and _is_offsite_href(info.href, page_url):
            continue
        if _is_submit_like(lowered):
            if signup_flavored:
                best.append((info.index, text))
            continue
        eligible.append((info.index, text))

    # Among the rest, a signup/trial-flavored option (seen live on
    # kamiapp.com: "Book a 30-day trial") still beats whatever happens to
    # sit first in DOM order (there: a "talk to us" sales-contact link,
    # which matches no exclusion keyword but isn't a self-serve signup
    # either). sorted() is stable, so this only reorders when a
    # signup-flavored option actually exists among `eligible`.
    ordered = best + sorted(eligible, key=lambda pair: not _is_signup_flavored(pair[1]))
    for index, _text in ordered:
        candidate = candidates.nth(index)
        try:
            candidate.click(timeout=3000)
        except PlaywrightError:
            continue
        _claim(candidate)
        return True
    return False


# A CAPTCHA/anti-bot challenge widget (Google reCAPTCHA, hCaptcha, Cloudflare
# Turnstile) renders in an iframe we neither can nor try to interact with --
# see registration_blocked_by_captcha below. Its own controls are invisible
# to a plain page.locator() (Playwright doesn't reach into cross-origin
# iframes), but the underlying page's own buttons behind/around the overlay
# -- including OAuth/integration buttons _is_oauth_text would otherwise
# exclude -- stay in the DOM and register as "visible" even though a human
# couldn't actually click most of them. Without this check, the choice-
# wizard/vision fallback below can find and click one of those instead (seen
# live on Synder's signup, reached from cloudbusinesshq.com: a reCAPTCHA
# image challenge appeared after "Sign up", and the fallback went on to
# click "Continue with Xero", wandering off to Xero's own site for the rest
# of the run's step budget).
_CAPTCHA_SELECTOR = (
    'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], '
    'iframe[src*="challenges.cloudflare.com"], .g-recaptcha, .h-captcha, .cf-turnstile'
)
# reCAPTCHA (and, empirically, most other providers) renders two distinct
# things matching the selector above: a small, always-present "protected by
# reCAPTCHA" anchor/checkbox badge (~256x60px, harmless, present on nearly
# every reCAPTCHA-using page whether or not a human will ever see a real
# challenge) and, only once actually triggered, a much larger challenge
# surface (~400x580px on Synder's image-grid puzzle). Without a size floor,
# the badge alone -- present the instant "Continue with Email" reveals
# Synder's form, long before Sign up is even clicked -- would trip this
# check on essentially every reCAPTCHA-protected signup form, most of which
# never show a real challenge at all. 100px comfortably separates a checkbox
# from an actual puzzle across the providers observed live so far.
_MIN_CAPTCHA_CHALLENGE_PX = 100


def _captcha_challenge_visible(page: Page) -> bool:
    try:
        widgets = page.locator(_CAPTCHA_SELECTOR)
        count = widgets.count()
    except PlaywrightError:
        return False
    for i in range(count):
        widget = widgets.nth(i)
        try:
            if not widget.is_visible():
                continue
            box = widget.bounding_box()
        except PlaywrightError:
            continue
        if (
            box is not None
            and box["width"] >= _MIN_CAPTCHA_CHALLENGE_PX
            and box["height"] >= _MIN_CAPTCHA_CHALLENGE_PX
        ):
            return True
    return False


# A reCAPTCHA challenge can present as just the small, un-solved "I'm not a
# robot" checkbox -- well under _captcha_challenge_visible's size floor --
# paired with the site's own error copy telling the visitor to solve it
# (seen live on paperbell.com: "Sorry, you have triggered a security
# warning. Please click the box above and try again."). Without this,
# _captcha_challenge_visible alone missed it and the loop kept re-clicking
# "Create my account" every remaining iteration, each click re-triggering
# the same warning and resetting the form, while steps_completed/submitted
# still reported a normal successful click -- a false-positive "registration
# completed" for a signup that never actually went through.
_CAPTCHA_WARNING_PHRASES: tuple[str, ...] = (
    "triggered a security warning",
    "verify you are human",
    "verify you're human",
    "unusual traffic",
    "captcha verification failed",
)


def _captcha_warning_visible(page: Page) -> bool:
    """Same "never conclude from one signal" reasoning as
    _code_entry_visible: requires the CAPTCHA widget selector to also be
    present, not the warning phrase alone."""
    try:
        text = page.inner_text("body").lower()
    except PlaywrightError:
        return False
    if not any(phrase in text for phrase in _CAPTCHA_WARNING_PHRASES):
        return False
    try:
        return page.locator(_CAPTCHA_SELECTOR).count() > 0
    except PlaywrightError:
        return False


# "Verify your email"/"Check your email"-style gates block further progress
# just as completely as a CAPTCHA -- there's no automated way to open a link
# sitting in a real inbox from here (registration.py's TempEmailProvider path
# only fires when a caller explicitly supplies one; the orchestrator doesn't
# wire one in for the real GROWTHRADAR_EMAIL flow). Detected by phrase, not
# by a specific selector, since this is arbitrary site copy rather than a
# recognizable third-party widget.
_EMAIL_VERIFICATION_PHRASES: tuple[str, ...] = (
    "verify your email",
    "confirm your email",
    "check your email",
    "verify your account",
    "confirm your account",
    "verification email",
    "we sent you a link",
    "we've sent you a link",
    "click the link we sent",
    "click the link in your email",
)


def _email_verification_required(page: Page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=1000).lower()
    except PlaywrightError:
        return False
    return any(phrase in body_text for phrase in _EMAIL_VERIFICATION_PHRASES)


# A phone/SMS verification gate blocks progress the same way an email one
# does, but this project has no way to solve it even in principle: unlike
# email (a real, readable Gmail inbox, see GmailImapProvider), there's no
# receivable phone number configured anywhere. Flagged as its own distinct
# evidence/report field (never lumped in with the email flag) purely so the
# dashboard can call out *which* kind of gate stopped the run, rather than
# it reading as a generic, unexplained failure -- seen live on
# shippingeasy.com, reached right after its email-verification link was
# successfully opened ("Success! Your email is now verified.") -- the two
# gates can chain, so this is checked independently of the email one, not
# as an alternative to it.
_PHONE_VERIFICATION_PHRASES: tuple[str, ...] = (
    "verify your phone",
    "verify your phone number",
    "verify your mobile",
    "phone verification code",
    "verification code to your phone",
    "text message with a verification code",
    "send you a text",
)


def _phone_verification_required(page: Page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=1000).lower()
    except PlaywrightError:
        return False
    return any(phrase in body_text for phrase in _PHONE_VERIFICATION_PHRASES)


def _click_by_exact_text(page: Page, text: str) -> bool:
    """Click the visible, unclaimed clickable element whose text exactly
    matches `text` -- used only for a vision-fallback suggestion (see
    vision_fallback.suggest_click_target), which is itself constrained to
    return one of the exact candidate strings a DOM scan already found. This
    stays a separate, stricter match than _click_unclaimed_choice_option's
    first-available heuristic: vision was shown a specific set of options and
    picked one, so we click exactly that one, not merely "the first"."""
    candidates, infos = _scan_clickable(page, _CHOICE_CLICKABLE_SELECTOR)
    for info in infos:
        if not info.visible or info.claimed:
            continue
        if info.text.strip() != text:
            continue
        candidate = candidates.nth(info.index)
        try:
            candidate.click(timeout=3000)
        except PlaywrightError:
            continue
        _claim(candidate)
        return True
    return False


def _visible_credential_field_count(page: Page) -> int:
    """Count of visible email/password inputs -- a reasonably universal
    signal that a login/signup form is now on screen, used to detect whether
    clicking a signup/login trigger actually did anything (see
    open_registration_entry_point's retry)."""
    try:
        fields = page.locator('input[type="email"], input[type="password"]')
        count = fields.count()
    except PlaywrightError:
        return 0
    visible = 0
    for i in range(count):
        with suppress(PlaywrightError):
            if fields.nth(i).is_visible():
                visible += 1
    return visible


def _find_clickable_by_keywords(page: Page, keywords: tuple[str, ...]) -> Locator | None:
    # Substring matching (not _click_submit's word-subset matching) --
    # consistent with how exploration.py already matches these same keyword
    # lists against link text; these are short, generic phrases ("sign up",
    # "login") where a plain substring check is enough and keeps this in
    # sync with the crawler's own notion of "a signup/login control".
    candidates, infos = _scan_clickable(page, _CLICKABLE_SELECTOR)
    for info in infos:
        if not info.visible or not info.text:
            continue
        lowered = info.text.lower()
        if _is_oauth_text(lowered):
            continue
        if any(keyword in lowered for keyword in keywords):
            return candidates.nth(info.index)
    return None


def open_registration_entry_point(page: Page, run_logger: RunLogger) -> bool:
    """Reach a signup/login flow that has no distinct URL of its own -- many
    sites (e.g. allevents.in) open a client-side modal instead of navigating
    anywhere, so exploration.py's crawl never finds a page it can classify as
    "the registration page" and registration is skipped entirely even though
    a working signup flow exists right on the current page.

    Priority mirrors a real first-time user: prefer a visible signup-styled
    control; if there isn't one, use a login-styled control instead (many
    sites only expose "Sign in", with signup reachable from inside it). After
    that click, scan once more for a signup link nested in whatever just
    appeared (a "New here? Sign up" link inside a login form is common) and
    follow it too if present.

    Only navigates/clicks -- never fills or submits anything itself. Returns
    True if it reached a plausibly-different screen, so the caller can run
    its normal fill+submit loop from here; False (never raises) if it found
    nothing to click, leaving the page untouched.
    """
    trigger = _find_clickable_by_keywords(page, _SIGNUP_KEYWORDS)
    if trigger is None:
        trigger = _find_clickable_by_keywords(page, _LOGIN_KEYWORDS)
    if trigger is None:
        return False

    # A trigger can be visible and pass Playwright's actionability checks
    # before a JS framework has finished attaching its click handler after
    # (re)hydrating -- observed reliably on allevents.in specifically on a
    # *second* visit to the same page within one browser session (e.g. after
    # the crawler explored elsewhere and this fallback navigated back). A
    # short settle pause before the first click attempt avoids the race in
    # the common case; the retry below is a second line of defense for
    # slower sites where even this isn't enough.
    with suppress(PlaywrightError):
        page.wait_for_timeout(1500)

    # Proactively clear a delayed popup/consent overlay before it can
    # intercept the click below (seen live on mirro.io: a HubSpot marketing
    # popup appears on its own ~2.5s timer after page load and, once up,
    # blocks every click on the page beneath it -- including this exact
    # "Login" link. dismiss_overlays was previously only called *after*
    # this click, too late to help a click that's already failing).
    dismiss_overlays(page)

    fields_before = _visible_credential_field_count(page)
    try:
        trigger.click(timeout=3000)
    except PlaywrightError:
        # The popup runs on its own timer, not ours -- it can still appear
        # mid-attempt even after the proactive dismiss above. One more
        # dismiss-and-retry before giving up entirely.
        dismiss_overlays(page)
        try:
            trigger.click(timeout=3000)
        except PlaywrightError:
            return False

    wait_for_stable(page)
    dismiss_overlays(page)

    if _visible_credential_field_count(page) <= fields_before:
        # On JS-heavy sites, a trigger that's visually present and passes
        # Playwright's actionability checks can still have its click handler
        # attached slightly after the page "settles" per wait_for_stable --
        # the click is reported successful but is a no-op (observed on
        # allevents.in specifically on a *second* visit to the homepage in
        # the same browser session, e.g. after the crawler explored
        # elsewhere and the registration fallback navigated back to it: the
        # very same click worked on a first, fresh visit). One retry after a
        # short beat recovers cleanly; if it still didn't help, proceed
        # anyway -- the caller's fill loop will simply find nothing to do.
        page.wait_for_timeout(500)
        with suppress(PlaywrightError):
            trigger.click(timeout=3000)
            wait_for_stable(page)
            dismiss_overlays(page)

    secondary = _find_clickable_by_keywords(page, _SIGNUP_KEYWORDS)
    if secondary is not None:
        with suppress(PlaywrightError):
            secondary.click(timeout=3000)
            wait_for_stable(page)

    run_logger.action("registration_entry_point_opened", url=page.url)
    return True


# Many real signup forms render inside a third-party-embedded iframe rather
# than the page's own document -- e.g. digifabster.com/getstarted/'s form is
# a lazily-loaded HubSpot embed (`<div id="hubspotLazyForm">` that injects an
# `<iframe>` only once its own script runs). `page.locator()` never looks
# inside an iframe's content document, so every field-finding function above
# is scoped to a single `Frame` and dispatched across `page.frames` (which
# already includes the main frame) below, rather than being called on `page`
# directly -- otherwise a form embedded this way is invisible to us: zero
# fields found, nothing filled, and the loop either stalls or (worse) clicks
# a real submit-like button before any required field has a value.
#
# The one exception is _click_unclaimed_choice_option, deliberately left
# page-only (not dispatched here): its whole job is "click the first
# plausible thing" as a last resort, which is safe for same-site
# choice-wizard UI but too broad a net to risk running inside an arbitrary
# third-party iframe (a video player embed, an ad, a chat widget) that
# happens to share the page -- seen live on this same digifabster.com page,
# which embeds an "Overview video" player alongside the HubSpot form.
def _fill_across_frames(page: Page, fn: Callable[[Frame], int]) -> int:
    total = 0
    for frame in page.frames:
        try:
            total += fn(frame)
        except PlaywrightError:
            continue
    return total


def _click_across_frames(page: Page, fn: Callable[[Frame], bool]) -> bool:
    for frame in page.frames:
        try:
            if fn(frame):
                return True
        except PlaywrightError:
            continue
    return False


def _switch_to_new_page(page: Page, pages_before: int, store: EvidenceStore, run_id: str) -> Page:
    """If the last click opened a new tab/window (page.context.pages grew),
    continue on the newest one instead of the stale original.

    A signup CTA opening target="_blank" (seen live on kamiapp.com's "Try
    for free"/"Create a free account" buttons, which open the actual app at
    a different domain in a new tab) otherwise leaves the loop clicking
    around whatever's left on the original page -- nav/footer links, in one
    observed run wandering as far as a help-center "Training Hub" page --
    for the rest of its step budget, having already wrongly recorded the
    click as a successful submission.

    Screenshots the new tab right after switching -- without this, the only
    screenshots on record are "before any clicks" and "after the whole
    step budget", so the handoff itself (what the new tab actually looked
    like the moment we arrived) was otherwise never visible in the
    dashboard/report even when following it worked correctly.
    """
    try:
        pages = page.context.pages
    except PlaywrightError:
        return page
    if len(pages) <= pages_before:
        return page
    new_page = pages[-1]
    with suppress(PlaywrightError):
        new_page.bring_to_front()
    with suppress(PlaywrightError):
        new_page.wait_for_load_state("domcontentloaded", timeout=10000)
    capture_and_record(
        new_page, store, run_id, ScreenshotKind.REGISTRATION, "registration opened a new tab"
    )
    return new_page


# Deliberately not folded into _SUBMIT_BUTTON_TEXTS: a login form's button
# almost never says "Sign up"/"Register"/"Start trial" (those would be the
# *wrong* action here), and conversely a signup form's submit shouldn't match
# "Log in"/"Sign in" either -- the two flows need disjoint phrase lists so
# _click_submit's word-subset matching can't cross-click the wrong one.
# "Next" is required, not optional: a same-origin email-first login (email
# screen -> "Next" -> separate password screen, seen live on
# conceptboard.com) is exactly as common as a single-page login form, and
# without it the email gets filled but the click that would reveal the
# password field never happens.
_LOGIN_BUTTON_TEXTS: tuple[str, ...] = ("Log in", "Login", "Sign in", "Next", "Continue", "Submit")
# A login flow is short by nature (email screen, password screen, maybe a
# "stay signed in?" prompt) -- a small bound catches the common 1-3 screen
# cases without risking the same runaway-loop shape registration.py's own
# MAX_FORM_STEPS guards against.
_MAX_LOGIN_STEPS = 4


def attempt_login(page: Page, store: EvidenceStore, run_id: str, identity: Identity) -> bool:
    """Fill and submit whatever login form is visible on `page` with
    `identity`'s email/password, following an email-then-password style
    multi-step flow the same way run_registration follows a multi-step
    signup (see _LOGIN_BUTTON_TEXTS' "Next").

    Only meaningful right after a successful `run_registration` with this
    same `identity` -- that's the one case where the account genuinely
    exists and the password is known, since it's never read back from
    anywhere (not even the target site's own confirmation email). Used by
    orchestrator.py when the post-registration crawl lands on a login page
    (seen live on conceptboard.com: exploration visited
    app.conceptboard.com/login-redirect after signup and, since nothing
    called this, left it as a blank, never-interacted-with screenshot).

    Never raises; records the attempt as its own Evidence row regardless of
    outcome (Linear.md "Evidence first"), mirroring run_registration's own
    "registration attempt". Returns whether at least one login-button click
    actually happened -- not proof the login itself succeeded, since a wrong
    password/rejected account looks identical from here (still on some page
    with a clicked button); the caller's own next screenshot/DOM evidence is
    what shows whether it actually worked.
    """
    total_filled = 0
    logged_in = False
    for _ in range(_MAX_LOGIN_STEPS):
        dismiss_overlays(page)
        filled = _fill_across_frames(page, lambda f: _fill_visible_fields(f, identity))
        total_filled += filled
        clicked = _click_across_frames(
            page,
            partial(_click_submit, allow_reclaim=filled > 0, button_texts=_LOGIN_BUTTON_TEXTS),
        )
        if not clicked:
            break
        wait_for_stable(page)
        logged_in = True

    if logged_in:
        capture_and_record(page, store, run_id, ScreenshotKind.LOGIN, "login attempt result")

    store.add(
        run_id,
        "login attempt",
        url=page.url,
        visible_ui={"email": identity.email, "fields_filled": total_filled, "clicked": logged_in},
    )
    return logged_in


def run_registration(
    page: Page,
    store: EvidenceStore,
    run_logger: RunLogger,
    *,
    identity: Identity | None = None,
    temp_email_provider: TempEmailProvider | None = None,
    max_steps: int = MAX_FORM_STEPS,
    country: str = "United States",
    company_name: str | None = None,
    email_override: str | None = None,
    password_override: str | None = None,
    first_name_override: str | None = None,
    last_name_override: str | None = None,
    config: Config | None = None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RegistrationResult:
    """Fill and submit a (possibly multi-step) registration form. Never raises.

    `country`/`company_name`/`email_override`/`password_override`/
    `first_name_override`/`last_name_override` are only used when `identity`
    isn't supplied -- they let a caller pin these values (see
    `Config.registrant_country` / `Config.registrant_company` /
    `Config.registrant_email` / `Config.registrant_password` /
    `Config.registrant_first_name` / `Config.registrant_last_name`) instead of
    getting a random name/company/email domain/password on every run.
    `email_override` is used verbatim (see `identity._build_email`) -- every
    target site sees the same real, monitored inbox, not a distinct variant
    per run. `password_override` likewise (see `identity.generate_identity`)
    -- a known, fixed password instead of one generated and then never
    recorded anywhere, so a just-created account can actually be logged back
    into later (see `attempt_login`) or by hand.

    `config` is only needed for the screenshot+vision-LLM fallback (see
    vision_fallback.py) -- omitted (the default), that fallback is simply
    never attempted, same as when no vision model is configured.

    `deadline` is a `time.monotonic()` timestamp (see orchestrator.py's
    shared session deadline, Config.session_deadline_seconds) after which the
    loop stops no matter how many steps remain. Omitted (the default), this
    call gets its own standalone `_MAX_REGISTRATION_SECONDS` budget instead.

    `cancelled`, if given, is polled the same way each iteration -- a
    callable rather than a plain flag so the dashboard's Stop button (see
    web.py) can request cancellation from another thread via a
    `threading.Event` without this function needing to know about threads.
    """
    identity = identity or generate_identity(
        country=country,
        company_name=company_name,
        email_override=email_override,
        password_override=password_override,
        first_name=first_name_override,
        last_name=last_name_override,
    )

    try:
        return _run_registration(
            page,
            store,
            run_logger,
            identity=identity,
            temp_email_provider=temp_email_provider,
            max_steps=max_steps,
            config=config,
            deadline=deadline,
            cancelled=cancelled,
        )
    except Exception as exc:
        logger.exception("registration flow failed unexpectedly")
        run_logger.error(f"registration flow failed: {exc}")
        return RegistrationResult(
            identity=identity,
            steps_completed=0,
            submitted=False,
            verification_link_opened=False,
            error=str(exc),
        )


def _run_registration(
    page: Page,
    store: EvidenceStore,
    run_logger: RunLogger,
    *,
    identity: Identity,
    temp_email_provider: TempEmailProvider | None,
    max_steps: int,
    config: Config | None,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RegistrationResult:
    allow_google_oauth = bool(config and config.allow_google_oauth)

    inbox: TempInbox | None = None
    if temp_email_provider is not None:
        inbox = temp_email_provider.create_inbox()
        if inbox is not None:
            identity = replace(identity, email=inbox.address)
            run_logger.action("temp_email_created", address=inbox.address)
        else:
            run_logger.error("failed to create temporary inbox; using generated email instead")

    run_logger.action("registration_started", email=identity.email, company=identity.company_name)
    capture_and_record(
        page, store, run_logger.run_id, ScreenshotKind.REGISTRATION, "registration form"
    )

    steps_completed = 0
    submitted = False
    vision_attempts = 0
    empty_retries = 0
    email_verification_required = False
    phone_verification_required = False
    code_verification_attempted = False
    verification_code_entered = False
    # An externally-supplied deadline (orchestrator.py's shared session
    # budget) takes priority; falling back to this call's own standalone
    # budget only when nothing shared was provided (e.g. tests, or
    # registration.py used outside the orchestrator).
    effective_deadline = (
        deadline if deadline is not None else time.monotonic() + _MAX_REGISTRATION_SECONDS
    )
    for _ in range(max_steps):
        if cancelled is not None and cancelled():
            run_logger.action("registration_cancelled", steps=steps_completed)
            break
        if time.monotonic() > effective_deadline:
            run_logger.error(
                f"registration timed out after {_MAX_REGISTRATION_SECONDS:.0f}s "
                f"({steps_completed} step(s) completed)"
            )
            break

        dismiss_overlays(page)
        pages_before = len(page.context.pages)

        if _captcha_challenge_visible(page) or _captcha_warning_visible(page):
            # Nothing safe left to do: we don't solve CAPTCHAs, and letting
            # the loop continue risks the choice-wizard/vision fallback
            # clicking whatever's left underneath the overlay instead (see
            # _captcha_challenge_visible). Capture this exact moment --
            # useful evidence in its own right (the target blocks automated
            # signup here) -- and stop.
            run_logger.action("registration_blocked_by_captcha", url=page.url)
            capture_and_record(
                page,
                store,
                run_logger.run_id,
                ScreenshotKind.REGISTRATION,
                "registration blocked by anti-bot challenge (captcha)",
            )
            break

        if allow_google_oauth and _click_across_frames(page, _click_google_oauth_button):
            # Beats the fill step below on purpose (see
            # _click_google_oauth_button): a page offering both a fillable
            # email form and a Google button must not fill+submit the
            # generated identity first. Treated the same as a successful
            # _click_submit click further down -- this *is* the submission
            # action for an OAuth-based signup, and whatever Google shows
            # next (an account chooser, a consent screen) is just more pages
            # for this same loop to keep handling on a later iteration.
            steps_completed += 1
            page = _switch_to_new_page(page, pages_before, store, run_logger.run_id)
            wait_for_stable(page)
            submitted = True
            continue

        if (
            temp_email_provider is not None
            and inbox is not None
            and not code_verification_attempted
            and _code_entry_visible(page)
        ):
            # A site can email a 6-digit code instead of (or on top of) a
            # clickable link -- seen live on joinblink.com, which lands
            # directly on this screen right after the initial signup step,
            # no link to open at all. Tried once per run, same reasoning as
            # _MAX_VISION_ATTEMPTS: if the code never arrives or the fields
            # can't be filled, retrying the exact same screen every
            # iteration just burns the step budget for no reason.
            code_verification_attempted = True
            code = temp_email_provider.wait_for_verification_code(inbox)
            if code and _fill_verification_code(page, code):
                verification_code_entered = True
                run_logger.action("verification_code_entered", code_length=len(code))
                clicked = _click_across_frames(
                    page,
                    partial(
                        _click_submit,
                        allow_reclaim=True,
                        allow_google_oauth=allow_google_oauth,
                        button_texts=_CODE_CONFIRM_BUTTON_TEXTS,
                    ),
                )
                steps_completed += 1
                page = _switch_to_new_page(page, pages_before, store, run_logger.run_id)
                wait_for_stable(page)
                if clicked:
                    submitted = True
                continue
            run_logger.error("verification code not received or code field not found")

        filled = _fill_across_frames(page, lambda f: _fill_visible_fields(f, identity))
        filled += _fill_across_frames(page, _fill_unclaimed_empty_selects)
        filled += _fill_across_frames(
            page, lambda f: _fill_unclaimed_generic_name_fields(f, identity)
        )
        checked_boxes = _fill_across_frames(page, _check_consent_checkboxes)

        picked = False
        clicked_email = False
        skipped_prompt = False
        if filled == 0 and checked_boxes == 0:
            # Nothing to fill or check -- either an auth-method chooser (see
            # _click_continue_with_email, tried first so it's always
            # preferred over an OAuth/SSO button), a dismissible security/
            # preference prompt (see _click_skip_prompt, e.g. "Set up a
            # passkey" -- tried next so it's preferred over accidentally
            # committing to it), or a choice-wizard step (see
            # _click_unclaimed_choice_option) rather than a plain form.
            clicked_email = _click_across_frames(page, _click_continue_with_email)
            if not clicked_email:
                skipped_prompt = _click_across_frames(page, _click_skip_prompt)
            if not clicked_email and not skipped_prompt:
                picked = _click_unclaimed_choice_option(page, allow_google_oauth=allow_google_oauth)

        made_progress = filled > 0 or checked_boxes > 0 or picked or clicked_email or skipped_prompt
        # Clicking "Continue with Email"/a skip prompt only reveals the next
        # screen (see cloudbusinesshq.com/Synder) -- nothing to submit yet.
        # Skip _click_submit this same iteration so it can't find and click
        # a leftover OAuth button, or the target site's own submit button,
        # before those fields are filled on the next iteration; that would
        # submit an empty form and trigger the exact validation errors this
        # project exists to avoid.
        #
        # `picked` needs the same guard: _click_unclaimed_choice_option
        # already clicked *something* (the first unexcluded candidate in DOM
        # order) this iteration -- seen live on kamiapp.com's "try for free"
        # page, offering 4 product cards with no submit-phrase match on the
        # first one ("Start for free"): the choice-picker clicked it,
        # opening a new tab, but _click_submit then ran anyway on the same
        # (now-stale) `page` reference and found a *different* card's button
        # ("Create a free account", which does match "Create account"),
        # clicking that too and opening a second, unrelated new tab. Only
        # the second click's tab ever got followed, wandering off to
        # whichever product that button happened to launch instead of the
        # one the choice-picker actually meant to pick.
        clicked = (
            False
            if (clicked_email or skipped_prompt or picked)
            else _click_across_frames(
                page,
                partial(
                    _click_submit,
                    allow_reclaim=made_progress,
                    allow_google_oauth=allow_google_oauth,
                ),
            )
        )
        page = _switch_to_new_page(page, pages_before, store, run_logger.run_id)

        if not made_progress and not clicked:
            # Nothing found anywhere yet -- could be genuinely stuck, or
            # could be a lazily-loaded embedded form still initializing
            # (seen live on digifabster.com/getstarted/: a HubSpot form
            # injects its <iframe> and fields several seconds after the page
            # otherwise looks settled). A couple of short, cheap retries
            # before falling to the vision fallback below covers this
            # without meaningfully slowing down the genuinely-stuck case.
            if empty_retries < _MAX_EMPTY_RETRIES:
                empty_retries += 1
                page.wait_for_timeout(_EMPTY_RETRY_WAIT_MS)
                continue

            if _email_verification_required(page):
                # Checked only once genuinely stuck (nothing left to fill or
                # click), not on every iteration -- the trigger phrases
                # ("verification email", "confirm your account", ...) can
                # also show up as ordinary marketing copy on a signup page
                # that's still fully fillable, and checking unconditionally
                # would abort registration before it even started. Same dead
                # end as a CAPTCHA otherwise: we can't open a link sitting in
                # a real inbox from here. Recorded as its own distinct
                # evidence/flag (not lumped in with a generic "stuck"
                # screenshot) so the dashboard can call this out
                # specifically, and skips the vision fallback below since
                # there's nothing useful it could point at either.
                email_verification_required = True
                run_logger.action("registration_pending_email_verification", url=page.url)
                capture_and_record(
                    page,
                    store,
                    run_logger.run_id,
                    ScreenshotKind.REGISTRATION,
                    "registration pending email verification",
                )
                break

            if _phone_verification_required(page):
                # Same reasoning as the email branch above, but there's no
                # equivalent solve path even in principle -- no receivable
                # phone number is configured anywhere in this project.
                phone_verification_required = True
                run_logger.action("registration_pending_phone_verification", url=page.url)
                capture_and_record(
                    page,
                    store,
                    run_logger.run_id,
                    ScreenshotKind.REGISTRATION,
                    "registration pending phone verification",
                )
                break

            # The DOM-based heuristics above (fill/check/pick/submit) found
            # nothing at all -- genuinely stuck. Screenshot the page and ask
            # a vision-capable LLM to point at one of the same clickable
            # elements the DOM already found (see vision_fallback.py); it
            # never gets to invent a target of its own. Only reached when
            # everything cheaper already failed -- a network round-trip per
            # attempt. An unhelpful response (nothing found, or a choice
            # outside the offered candidates) ends registration right here
            # rather than re-querying the exact same unchanged screenshot;
            # _MAX_VISION_ATTEMPTS instead caps how many *separate* stuck
            # points (e.g. a wizard that stalls at two different steps) get
            # a vision attempt over the whole registration.
            if config is not None and vision_attempts < _MAX_VISION_ATTEMPTS:
                vision_attempts += 1
                suggestion = suggest_click_target(page, config, selector=_CHOICE_CLICKABLE_SELECTOR)
                if suggestion is not None and _click_by_exact_text(page, suggestion):
                    run_logger.action("vision_fallback_clicked", text=suggestion)
                    wait_for_stable(page)
                    steps_completed += 1
                    continue
                run_logger.action("vision_fallback_no_target", attempt=vision_attempts)
            break

        steps_completed += 1

        if not clicked:
            wait_for_stable(page)
            continue

        wait_for_stable(page)
        submitted = True

    capture_and_record(
        page,
        store,
        run_logger.run_id,
        ScreenshotKind.REGISTRATION,
        f"registration form after {steps_completed} step(s)",
    )
    run_logger.action("registration_steps_completed", steps=steps_completed, submitted=submitted)

    verification_opened = (
        False
        if verification_code_entered
        else _maybe_open_verification_link(
            page,
            store,
            run_logger,
            inbox,
            temp_email_provider,
            submitted=submitted,
            email_verification_required=email_verification_required,
        )
    )

    if verification_opened and _phone_verification_required(page):
        # The two gates can chain (seen live on shippingeasy.com: opening
        # the email link lands directly on "Please Verify Your Phone
        # Number") -- this only runs after a link was actually opened, so
        # it's checking the page the link led to, not re-checking the page
        # that was already handled by the loop's own phone-verification
        # branch above.
        phone_verification_required = True
        run_logger.action("registration_pending_phone_verification", url=page.url)
        capture_and_record(
            page,
            store,
            run_logger.run_id,
            ScreenshotKind.REGISTRATION,
            "registration pending phone verification",
        )

    store.add(
        run_logger.run_id,
        "registration attempt",
        url=page.url,
        visible_ui={
            "steps_completed": steps_completed,
            "submitted": submitted,
            "verification_link_opened": verification_opened,
            "verification_code_entered": verification_code_entered,
            "email_verification_required": email_verification_required,
            "phone_verification_required": phone_verification_required,
            "email": identity.email,
            "company_name": identity.company_name,
            "country": identity.country,
        },
    )

    return RegistrationResult(
        identity=identity,
        steps_completed=steps_completed,
        submitted=submitted,
        verification_link_opened=verification_opened,
        email_verification_required=email_verification_required,
        phone_verification_required=phone_verification_required,
        verification_code_entered=verification_code_entered,
    )


def _maybe_open_verification_link(
    page: Page,
    store: EvidenceStore,
    run_logger: RunLogger,
    inbox: TempInbox | None,
    temp_email_provider: TempEmailProvider | None,
    *,
    submitted: bool,
    email_verification_required: bool,
) -> bool:
    # `submitted` alone missed a real case live on phishingbox.com: its
    # signup form's own submit click was never recognized as one of
    # _SUBMIT_BUTTON_TEXTS, so `submitted` stayed False even though the site
    # had already emailed a verification link -- the page itself showed
    # "check your email" text with nothing left to fill or click
    # (_email_verification_required, gated on the same "genuinely stuck"
    # branch that avoids false-triggering on marketing copy, see
    # _run_registration). That phrase-plus-stuck signal is just as strong
    # evidence the email was actually sent as a recognized submit click, so
    # either one is enough to justify waiting on a real, already-configured
    # inbox instead of giving up with the verification link sitting unread.
    if (
        not (submitted or email_verification_required)
        or inbox is None
        or temp_email_provider is None
    ):
        return False

    link = temp_email_provider.wait_for_verification_link(inbox)
    if not link:
        run_logger.error("no verification email received in time")
        return False

    run_logger.action("verification_link_found", link=link)
    try:
        retry(lambda: page.goto(link, wait_until="domcontentloaded"))
    except PlaywrightError as exc:
        run_logger.error(f"failed to open verification link: {exc}")
        return False

    wait_for_stable(page)
    dismiss_overlays(page)
    capture_and_record(
        page, store, run_logger.run_id, ScreenshotKind.ONBOARDING, "post-verification page"
    )
    return True
