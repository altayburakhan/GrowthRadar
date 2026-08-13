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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame, Locator, Page

from growthradar.browser import dismiss_overlays, retry, wait_for_stable
from growthradar.config import Config
from growthradar.event_log import RunLogger
from growthradar.evidence import EvidenceStore
from growthradar.exploration import _SIGNUP_KEYWORDS
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
    ("email", ("email",)),
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
    ("full_name", ("full name", "your name")),
)

_SUBMIT_BUTTON_TEXTS: tuple[str, ...] = (
    "Continue",
    "Next",
    "Sign up",
    "Create account",
    "Get started",
    "Start free trial",
    "Register",
    "Submit",
)

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
                box.check(timeout=2000)
            else:
                box.click(timeout=2000)
        except PlaywrightError:
            continue
        _claim(box)
        checked += 1
    return checked


def _clickable_text(locator: Locator) -> str:
    try:
        text = locator.inner_text(timeout=500)
        if text.strip():
            return text
    except PlaywrightError:
        pass
    try:
        return f"{locator.get_attribute('value') or ''} {locator.get_attribute('aria-label') or ''}"
    except PlaywrightError:
        return ""


def _words(text: str) -> set[str]:
    return {w for w in text.lower().split() if w}


_OAUTH_PHRASE_RE = re.compile(r"\b(with|via)\b")


def _is_oauth_button(locator: Locator) -> bool:
    lowered = _clickable_text(locator).lower()
    if any(keyword in lowered for keyword in _OAUTH_BUTTON_KEYWORDS):
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


def _click_submit(frame: Frame, *, allow_reclaim: bool = False) -> bool:
    # A CSS selector, not get_by_role(name=..., exact=False): Playwright's
    # substring matching there requires our target phrase to appear as an
    # unbroken run of words in the element's accessible name, which fails on
    # real copy like "Start MY free trial" (a real-world example) that
    # inserts a word into "Start free trial". Word-subset matching below
    # (every word of our phrase present, in any order/position) is more
    # forgiving without being so loose it clicks unrelated buttons.
    try:
        candidates = frame.locator(
            'button, a, input[type="submit"], input[type="button"], [role="button"]'
        )
        count = candidates.count()
    except PlaywrightError:
        return False

    for text in _SUBMIT_BUTTON_TEXTS:
        target_words = _words(text)
        for i in range(count):
            candidate = candidates.nth(i)
            try:
                if not candidate.is_visible():
                    continue
                if _is_claimed(candidate) and not allow_reclaim:
                    continue
            except PlaywrightError:
                continue
            if not target_words <= _words(_clickable_text(candidate)):
                continue
            if _is_oauth_button(candidate):
                continue
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
    *_LOGIN_KEYWORDS,
    *_SIGNUP_KEYWORDS,
)
# :is(...) wraps the comma-separated _CLICKABLE_SELECTOR into one compound
# selector so :not() excludes nav/header/footer descendants from all three
# alternatives -- `f"{_CLICKABLE_SELECTOR}:not(...)"` would silently attach
# :not() to only the last alternative ([role="button"]), leaving plain
# <button>/<a> elements inside <nav> unfiltered.
_CHOICE_CLICKABLE_SELECTOR = f":is({_CLICKABLE_SELECTOR}):not(nav *, header *, footer *)"


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


def _is_continue_with_email(text: str) -> bool:
    # Word-subset match (not a plain substring check), same reasoning as
    # _is_submit_like/_click_submit: real copy inserts words into the target
    # phrase ("Sign up with *your* email", seen live on dialpad.com), which
    # breaks a contiguous substring match even though every target word is
    # present.
    words = _words(text)
    return any(_words(keyword) <= words for keyword in _CONTINUE_WITH_EMAIL_KEYWORDS)


def _click_continue_with_email(frame: Frame) -> bool:
    try:
        candidates = frame.locator(_CLICKABLE_SELECTOR)
        count = candidates.count()
    except PlaywrightError:
        return False

    for i in range(count):
        candidate = candidates.nth(i)
        try:
            if not candidate.is_visible() or _is_claimed(candidate):
                continue
        except PlaywrightError:
            continue
        if not _is_continue_with_email(_clickable_text(candidate).strip()):
            continue
        try:
            candidate.click(timeout=3000)
        except PlaywrightError:
            continue
        _claim(candidate)
        return True
    return False


def _click_unclaimed_choice_option(page: Page) -> bool:
    try:
        candidates = page.locator(_CHOICE_CLICKABLE_SELECTOR)
        count = candidates.count()
    except PlaywrightError:
        return False

    for i in range(count):
        candidate = candidates.nth(i)
        try:
            if not candidate.is_visible() or _is_claimed(candidate):
                continue
        except PlaywrightError:
            continue
        text = _clickable_text(candidate).strip()
        if not text:
            continue
        lowered = text.lower()
        if _is_oauth_button(candidate) or _is_submit_like(lowered):
            continue
        if any(keyword in lowered for keyword in _CHOICE_EXCLUDE_KEYWORDS):
            continue
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
# -- including OAuth/integration buttons _is_oauth_button would otherwise
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


def _click_by_exact_text(page: Page, text: str) -> bool:
    """Click the visible, unclaimed clickable element whose text exactly
    matches `text` -- used only for a vision-fallback suggestion (see
    vision_fallback.suggest_click_target), which is itself constrained to
    return one of the exact candidate strings a DOM scan already found. This
    stays a separate, stricter match than _click_unclaimed_choice_option's
    first-available heuristic: vision was shown a specific set of options and
    picked one, so we click exactly that one, not merely "the first"."""
    try:
        candidates = page.locator(_CHOICE_CLICKABLE_SELECTOR)
        count = candidates.count()
    except PlaywrightError:
        return False
    for i in range(count):
        candidate = candidates.nth(i)
        try:
            if not candidate.is_visible() or _is_claimed(candidate):
                continue
            if _clickable_text(candidate).strip() != text:
                continue
        except PlaywrightError:
            continue
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
    try:
        candidates = page.locator(_CLICKABLE_SELECTOR)
        count = candidates.count()
    except PlaywrightError:
        return None
    for i in range(count):
        candidate = candidates.nth(i)
        try:
            if not candidate.is_visible():
                continue
        except PlaywrightError:
            continue
        text = _clickable_text(candidate).lower()
        if not text or _is_oauth_button(candidate):
            continue
        if any(keyword in text for keyword in keywords):
            return candidate
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

    fields_before = _visible_credential_field_count(page)
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
    first_name_override: str | None = None,
    last_name_override: str | None = None,
    config: Config | None = None,
) -> RegistrationResult:
    """Fill and submit a (possibly multi-step) registration form. Never raises.

    `country`/`company_name`/`email_override`/`first_name_override`/
    `last_name_override` are only used when `identity` isn't supplied -- they
    let a caller pin these values (see `Config.registrant_country` /
    `Config.registrant_company` / `Config.registrant_email` /
    `Config.registrant_first_name` / `Config.registrant_last_name`) instead of
    getting a random name/company/email domain on every run. `email_override`
    is used verbatim (see `identity._build_email`) -- every target site sees
    the same real, monitored inbox, not a distinct variant per run.

    `config` is only needed for the screenshot+vision-LLM fallback (see
    vision_fallback.py) -- omitted (the default), that fallback is simply
    never attempted, same as when no vision model is configured.
    """
    identity = identity or generate_identity(
        country=country,
        company_name=company_name,
        email_override=email_override,
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
) -> RegistrationResult:
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
    for _ in range(max_steps):
        dismiss_overlays(page)

        if _captcha_challenge_visible(page):
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

        filled = _fill_across_frames(page, lambda f: _fill_visible_fields(f, identity))
        filled += _fill_across_frames(page, _fill_unclaimed_empty_selects)
        filled += _fill_across_frames(
            page, lambda f: _fill_unclaimed_generic_name_fields(f, identity)
        )
        checked_boxes = _fill_across_frames(page, _check_consent_checkboxes)

        picked = False
        clicked_email = False
        if filled == 0 and checked_boxes == 0:
            # Nothing to fill or check -- either an auth-method chooser (see
            # _click_continue_with_email, tried first so it's always
            # preferred over an OAuth/SSO button) or a choice-wizard step
            # (see _click_unclaimed_choice_option) rather than a plain form.
            clicked_email = _click_across_frames(page, _click_continue_with_email)
            if not clicked_email:
                picked = _click_unclaimed_choice_option(page)

        made_progress = filled > 0 or checked_boxes > 0 or picked or clicked_email
        # Clicking "Continue with Email" only reveals the real form (see
        # cloudbusinesshq.com/Synder) -- it's still empty. Skip _click_submit
        # this same iteration so it can't find and click a leftover OAuth
        # button, or the target site's own submit button, before those
        # fields are filled on the next iteration; that would submit an
        # empty form and trigger the exact validation errors this project
        # exists to avoid.
        clicked = (
            False
            if clicked_email
            else _click_across_frames(page, partial(_click_submit, allow_reclaim=made_progress))
        )

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

    verification_opened = _maybe_open_verification_link(
        page, store, run_logger, inbox, temp_email_provider, submitted=submitted
    )

    store.add(
        run_logger.run_id,
        "registration attempt",
        url=page.url,
        visible_ui={
            "steps_completed": steps_completed,
            "submitted": submitted,
            "verification_link_opened": verification_opened,
            "email_verification_required": email_verification_required,
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
    )


def _maybe_open_verification_link(
    page: Page,
    store: EvidenceStore,
    run_logger: RunLogger,
    inbox: TempInbox | None,
    temp_email_provider: TempEmailProvider | None,
    *,
    submitted: bool,
) -> bool:
    if not submitted or inbox is None or temp_email_provider is None:
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
