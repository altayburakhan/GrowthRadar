"""Run orchestrator: the single entrypoint that wires every module built so
far (browser GRO-6, logging GRO-7, evidence GRO-8, screenshots GRO-9, DOM
GRO-10, exploration GRO-11, registration GRO-12, JS/network GRO-13, onboarding
heuristics GRO-14, scoring GRO-16, report GRO-17, run history GRO-20) into one
call: `run_growthradar_session(url) -> RunOutcome`.

Each phase (exploration, registration) is isolated behind its own try/except
-- a failure in one never prevents the others from running or a report from
being generated from whatever evidence was collected (Linear.md "Error
Recovery": "Never stop because one path failed. Continue gathering
evidence."). Callable independently of any CLI; resources (browser, evidence
store) are always closed via try/finally regardless of outcome.

GRO-25: when registration succeeds, a second, separately-budgeted exploration
pass starts from wherever registration left the browser (the post-signup /
post-verification page) to discover the now-authenticated app -- dashboard,
settings, and the onboarding UI real products typically show only to
brand-new logged-in users. That's the single most valuable evidence for this
agent's stated purpose, and the pre-GRO-25 pipeline never visited it.

AI Vision (previously GRO-15/GRO-27) has been removed: it depended on an
Anthropic API key, which this project no longer uses. Screenshots are still
captured on every page (screenshot.py) so a human can review them manually --
GrowthRadar just no longer sends them to an LLM itself.

llm_summary.py runs as its own isolated phase after scoring: it asks Groq for
a short plain-English explanation of the (already-computed, 100% rule-based)
verdict. It never influences the score itself and is skipped -- not
raised -- when no Groq key is configured.

GRO-32: when the crawl never finds a page it can classify as "the
registration page" (many sites open a client-side signup/login modal with no
URL of its own -- see allevents.in), `_attempt_registration` falls back to
`registration.open_registration_entry_point` on the landing page before
giving up, instead of skipping registration outright.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from patchright.sync_api import Error as PlaywrightError

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.event_log import RunLogger
from growthradar.evidence import Evidence, EvidenceStore
from growthradar.exploration import (
    _SIGNUP_KEYWORDS,
    DEFAULT_MAX_DEPTH,
    ExplorationEngine,
    ExplorationResult,
    _in_scope,
)
from growthradar.history import RunHistoryStore, RunSummary
from growthradar.llm_summary import record_llm_summary
from growthradar.registration import (
    RegistrationResult,
    attempt_login,
    open_registration_entry_point,
    run_registration,
)
from growthradar.report import FinalReport, generate_report, record_report
from growthradar.scoring import score_run
from growthradar.screenshot import ScreenshotKind
from growthradar.temp_email import GmailImapProvider, TempEmailProvider

logger = logging.getLogger(__name__)

DEFAULT_MAX_POST_REGISTRATION_PAGES = 5
DEFAULT_POST_REGISTRATION_MAX_DEPTH = 2


def _normalize_target_url(url: str) -> str:
    """Infer a scheme the way a browser address bar does: "example.com" typed
    into the dashboard or CLI should behave like "https://example.com" rather
    than fail Playwright's navigation outright, which rejects schemeless URLs
    (Linear.md: fail gracefully, don't make the user guess the right syntax).

    `data:` URLs (used throughout this project's test suite) never contain
    "://" and must pass through untouched.
    """
    stripped = url.strip()
    if not stripped or "://" in stripped or stripped.startswith("data:"):
        return stripped
    return f"https://{stripped}"


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    report: FinalReport
    exploration: ExplorationResult | None
    registration: RegistrationResult | None
    post_registration_exploration: ExplorationResult | None
    history: RunSummary | None
    errors: tuple[str, ...]


def run_growthradar_session(
    url: str,
    *,
    config: Config | None = None,
    headless: bool = True,
    attempt_registration: bool = True,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_post_registration_pages: int = DEFAULT_MAX_POST_REGISTRATION_PAGES,
    post_registration_max_depth: int = DEFAULT_POST_REGISTRATION_MAX_DEPTH,
    run_id: str | None = None,
    log_dir: str | Path = "logs",
    cancel_event: threading.Event | None = None,
) -> RunOutcome:
    """Explore `url`, attempt registration, explore the authenticated app,
    score, and report.

    Never raises: every phase is isolated, so this always returns a
    RunOutcome -- with a report built from whatever evidence was collected,
    even if some or all phases failed.

    `cancel_event`, if given, is checked alongside the session deadline at
    every phase's own checkpoints (see ExplorationEngine.run/registration.py's
    loop) -- a caller running this on a background thread (the dashboard's
    job executor, see web.py) can set it from the request-handling thread to
    stop an in-progress run early. `threading.Event` is used specifically
    because it's safe to signal across threads; Playwright's sync API itself
    is not, so nothing here calls back into the browser session directly.
    """
    url = _normalize_target_url(url)
    config = config or Config.from_env()
    run_logger = RunLogger(run_id=run_id, log_dir=log_dir)
    store = EvidenceStore.from_config(config)
    history_store = RunHistoryStore.from_config(config)
    errors: list[str] = []
    exploration_result: ExplorationResult | None = None
    registration_result: RegistrationResult | None = None
    post_registration_result: ExplorationResult | None = None
    history_summary: RunSummary | None = None

    try:
        try:
            with BrowserSession(config, headless=headless) as session:
                (
                    exploration_result,
                    registration_result,
                    post_registration_result,
                    phase_errors,
                ) = _run_browser_phases(
                    session,
                    store,
                    run_logger,
                    url,
                    config,
                    attempt_registration=attempt_registration,
                    max_depth=max_depth,
                    max_post_registration_pages=max_post_registration_pages,
                    post_registration_max_depth=post_registration_max_depth,
                    cancel_event=cancel_event,
                )
                errors.extend(phase_errors)
        except Exception as exc:
            logger.exception("browser session failed")
            run_logger.error(f"browser session failed: {exc}")
            errors.append(f"browser: {exc}")

        evidence = store.for_run(run_logger.run_id)
        score = score_run(run_logger.run_id, evidence, config)

        try:
            record_llm_summary(
                store,
                run_logger.run_id,
                "llm summary",
                evidence=evidence,
                score=score,
                config=config,
            )
        except Exception as exc:
            logger.exception("LLM summary phase failed")
            run_logger.error(f"LLM summary phase failed: {exc}")
            errors.append(f"llm summary: {exc}")
        evidence = store.for_run(run_logger.run_id)

        final_report = generate_report(run_logger.run_id, evidence, score, run_errors=tuple(errors))
        try:
            record_report(store, final_report)
        except Exception as exc:
            logger.exception("failed to persist final report")
            run_logger.error(f"failed to persist final report: {exc}")
            errors.append(f"report persistence: {exc}")

        try:
            history_summary = history_store.record(final_report)
        except Exception as exc:
            logger.exception("failed to record run history")
            run_logger.error(f"failed to record run history: {exc}")
            errors.append(f"history: {exc}")
    finally:
        store.close()
        history_store.close()

    return RunOutcome(
        run_id=run_logger.run_id,
        report=final_report,
        exploration=exploration_result,
        registration=registration_result,
        post_registration_exploration=post_registration_result,
        history=history_summary,
        errors=tuple(errors),
    )


def run_growthradar_batch(
    urls: list[str],
    *,
    config: Config | None = None,
    headless: bool = True,
    attempt_registration: bool = True,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_post_registration_pages: int = DEFAULT_MAX_POST_REGISTRATION_PAGES,
    post_registration_max_depth: int = DEFAULT_POST_REGISTRATION_MAX_DEPTH,
    log_dir: str | Path = "logs",
) -> list[RunOutcome]:
    """Run the pipeline for each URL in turn.

    One URL's total failure never stops the batch: `run_growthradar_session`
    already never raises, but this is the explicit outer safety net the
    "pipeline-level resilience" requirement asks for -- every URL in `urls`
    gets exactly one RunOutcome back, even in the (should-be-impossible) case
    that a session call raises anyway.
    """
    resolved_config = config or Config.from_env()
    outcomes: list[RunOutcome] = []

    for url in urls:
        try:
            outcomes.append(
                run_growthradar_session(
                    url,
                    config=resolved_config,
                    headless=headless,
                    attempt_registration=attempt_registration,
                    max_depth=max_depth,
                    max_post_registration_pages=max_post_registration_pages,
                    post_registration_max_depth=post_registration_max_depth,
                    log_dir=log_dir,
                )
            )
        except Exception as exc:
            logger.exception("run_growthradar_session raised unexpectedly for %s", url)
            outcomes.append(_emergency_outcome(url, exc, resolved_config))

    return outcomes


def _emergency_outcome(url: str, exc: Exception, config: Config) -> RunOutcome:
    """Last-resort RunOutcome for the case run_growthradar_session itself raised."""
    run_id = f"failed-{uuid.uuid4().hex[:8]}"
    error = f"fatal: {url}: {exc}"
    score = score_run(run_id, [], config)
    report = generate_report(run_id, [], score, run_errors=(error,))
    return RunOutcome(
        run_id=run_id,
        report=report,
        exploration=None,
        registration=None,
        post_registration_exploration=None,
        history=None,
        errors=(error,),
    )


def _run_browser_phases(
    session: BrowserSession,
    store: EvidenceStore,
    run_logger: RunLogger,
    url: str,
    config: Config,
    *,
    attempt_registration: bool,
    max_depth: int,
    max_post_registration_pages: int,
    post_registration_max_depth: int,
    cancel_event: threading.Event | None = None,
) -> tuple[
    ExplorationResult | None,
    RegistrationResult | None,
    ExplorationResult | None,
    list[str],
]:
    errors: list[str] = []
    exploration_result: ExplorationResult | None = None
    registration_result: RegistrationResult | None = None
    post_registration_result: ExplorationResult | None = None
    # One wall-clock ceiling shared across exploration + registration +
    # post-registration exploration (Config.session_deadline_seconds) -- a
    # per-phase-only budget let a slow-but-not-failing site (seen live on
    # squareup.com) pad out its page/step budgets' worth of time several
    # times over, phase after phase, with no ceiling on the total.
    deadline = time.monotonic() + config.session_deadline_seconds
    cancelled = cancel_event.is_set if cancel_event is not None else None

    try:
        engine = ExplorationEngine.from_config(
            session, store, run_logger, config, max_depth=max_depth
        )
        exploration_result = engine.run(url, deadline=deadline, cancelled=cancelled)
    except Exception as exc:
        logger.exception("exploration phase failed")
        run_logger.error(f"exploration phase failed: {exc}")
        errors.append(f"exploration: {exc}")

    if attempt_registration and not (cancelled is not None and cancelled()):
        try:
            registration_result = _attempt_registration(
                session, store, run_logger, config, url, deadline=deadline, cancelled=cancelled
            )
        except Exception as exc:
            logger.exception("registration phase failed")
            run_logger.error(f"registration phase failed: {exc}")
            errors.append(f"registration: {exc}")

        if (
            registration_result is not None
            and registration_result.submitted
            and not (cancelled is not None and cancelled())
        ):
            try:
                post_registration_result = _explore_authenticated_app(
                    session,
                    store,
                    run_logger,
                    config,
                    original_url=url,
                    max_pages=max_post_registration_pages,
                    max_depth=post_registration_max_depth,
                    deadline=deadline,
                    cancelled=cancelled,
                )
            except Exception as exc:
                logger.exception("post-registration exploration failed")
                run_logger.error(f"post-registration exploration failed: {exc}")
                errors.append(f"post-registration exploration: {exc}")

            if not (cancelled is not None and cancelled()):
                try:
                    _attempt_login_if_reached(session, store, run_logger, registration_result)
                except Exception as exc:
                    logger.exception("login attempt failed")
                    run_logger.error(f"login attempt failed: {exc}")
                    errors.append(f"login attempt: {exc}")

    return (
        exploration_result,
        registration_result,
        post_registration_result,
        errors,
    )


def _explore_authenticated_app(
    session: BrowserSession,
    store: EvidenceStore,
    run_logger: RunLogger,
    config: Config,
    *,
    original_url: str,
    max_pages: int,
    max_depth: int,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ExplorationResult | None:
    """After a successful signup, keep exploring from wherever registration
    left the browser -- the now-authenticated app -- with its own bounded
    budget. A fresh ExplorationEngine means URLs seen before registration
    (e.g. a publicly-linked dashboard preview) are legitimately revisited,
    since their content can differ once authenticated.

    Guards against exploring a domain the registration flow was never meant to
    reach: a "Continue" button can accidentally trigger third-party OAuth (see
    registration.py's OAuth-button skip) or some other redirect can land the
    browser off-site. Continuing to crawl there would contaminate onboarding
    detection and scoring with evidence about an unrelated site (observed:
    a 100hires.com run that got redirected to accounts.google.com scored
    "hot" based on Google's own UI, not 100hires').
    """
    page = session.page
    if page is None:
        return None

    start_url = page.url
    if not _in_scope(original_url, start_url):
        run_logger.error(
            f"post-registration exploration skipped: browser ended up off-site "
            f"({start_url}, expected same domain as {original_url})"
        )
        return None

    run_logger.action("post_registration_exploration_started", url=start_url)
    engine = ExplorationEngine(
        session,
        store,
        run_logger,
        max_pages=max_pages,
        max_depth=max_depth,
        crawl_delay=config.crawl_delay,
    )
    return engine.run(
        start_url, initial_kind=ScreenshotKind.ONBOARDING, deadline=deadline, cancelled=cancelled
    )


def _signup_keyword_hits(e: Evidence) -> int:
    haystack = f"{e.label} {e.url}".lower().replace("-", " ").replace("_", " ")
    return sum(keyword in haystack for keyword in _SIGNUP_KEYWORDS)


def _find_login_page_url(store: EvidenceStore, run_id: str) -> str | None:
    for e in store.for_run(run_id):
        if (
            e.label.startswith("screenshot:")
            and isinstance(e.visible_ui, dict)
            and e.visible_ui.get("screenshot_kind") == "login"
            and e.url
        ):
            return e.url
    return None


def _attempt_login_if_reached(
    session: BrowserSession,
    store: EvidenceStore,
    run_logger: RunLogger,
    registration_result: RegistrationResult,
) -> None:
    """If exploration (pre- or post-registration) landed on a page classified
    as a login screen, actually try logging in with the identity that was
    just used to register -- that's the one point in a run where a real
    account with a known password exists (see registration.attempt_login).
    Without this, a login page found mid-crawl was only ever screenshotted,
    never interacted with (seen live on conceptboard.com's
    /login-redirect)."""
    login_url = _find_login_page_url(store, run_logger.run_id)
    if login_url is None:
        return

    page = session.page
    if page is None:
        return
    if page.url != login_url and not session.goto(login_url):
        run_logger.error(f"could not navigate back to login page {login_url}")
        return

    run_logger.action("login_attempt_started", url=page.url)
    logged_in = attempt_login(page, store, run_logger.run_id, registration_result.identity)
    run_logger.action("login_attempt_finished", url=page.url, clicked=logged_in)


def _find_registration_page_url(store: EvidenceStore, run_id: str) -> str | None:
    candidates = [
        e
        for e in store.for_run(run_id)
        if (
            e.label.startswith("screenshot:")
            and isinstance(e.visible_ui, dict)
            and e.visible_ui.get("screenshot_kind") == "registration"
            and e.url
        )
    ]
    if not candidates:
        return None
    # More than one visited page can get classified "registration" just from
    # a nav link's text (e.g. a "Free Trial" button pointing at a pricing
    # page) even when the actual signup FORM lives elsewhere entirely (seen
    # live on statusbrew.com: a "Free Trial" link led to /pricing -- a plan
    # comparison page with no form on it at all -- while the real target,
    # space.statusbrew.com/get-started, titled "Sign up | Statusbrew", was
    # visited moments later and never picked because the first match always
    # won). Prefer whichever candidate's own URL+title most directly names
    # the signup flow, not just the first one the crawl happened to reach;
    # ties keep list order (max() is stable), so this is a pure improvement
    # when nothing outranks the old first-match behavior.
    best = max(candidates, key=_signup_keyword_hits)
    return best.url


def _temp_email_provider_from_config(config: Config) -> TempEmailProvider | None:
    """A real Gmail inbox (read over IMAP, see temp_email.py) when
    configured, so registration.py can open a verification link or type in
    a code instead of stalling on "email verification required". Takes
    priority over a plain GROWTHRADAR_EMAIL override when both are set --
    that address alone can't be read back, so there's nothing useful to do
    with it once a readable inbox is available."""
    if config.gmail_address and config.gmail_app_password:
        return GmailImapProvider(config.gmail_address, config.gmail_app_password)
    return None


def _sync_session_to_newest_page(session: BrowserSession) -> None:
    """If a click opened a new tab/window (a signup CTA with target="_blank"
    -- seen live on kamiapp.com's "Try for free"/"Create a free account"
    buttons, which open the actual app at a different domain), track that
    new tab as the session's active page instead of the stale original.
    Without this, run_registration's own internal page-switching (see
    registration.py's _switch_to_new_page) never reaches the caller, so a
    later phase (post-registration exploration) would keep exploring the
    abandoned original tab instead of the one registration actually
    completed on."""
    if session.page is None:
        return
    with suppress(PlaywrightError):
        pages = session.page.context.pages
        if pages and pages[-1] is not session.page:
            session.page = pages[-1]


def _attempt_registration(
    session: BrowserSession,
    store: EvidenceStore,
    run_logger: RunLogger,
    config: Config,
    landing_url: str,
    *,
    deadline: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> RegistrationResult | None:
    target_url = _find_registration_page_url(store, run_logger.run_id)

    if target_url is not None:
        if session.page is None or session.page.url != target_url:
            if not session.goto(target_url):
                run_logger.error(f"could not navigate back to registration page {target_url}")
                return None
    else:
        # No page the crawl visited was ever classified "registration" --
        # common on sites where signup is a client-side modal with no URL of
        # its own (e.g. allevents.in's "Sign in" button). Fall back to
        # opening that flow directly from the landing page rather than
        # giving up (GRO-30/31's open_registration_entry_point handles the
        # click-priority: signup control, else login, then drill into a
        # nested signup link).
        if session.page is None:
            run_logger.action("registration_skipped", reason="no registration page discovered")
            return None
        if session.page.url != landing_url and not session.goto(landing_url):
            run_logger.action("registration_skipped", reason="no registration page discovered")
            return None
        if not open_registration_entry_point(session.page, run_logger):
            run_logger.action("registration_skipped", reason="no registration page discovered")
            return None
        _sync_session_to_newest_page(session)

    page = session.page
    if page is None:
        return None
    result = run_registration(
        page,
        store,
        run_logger,
        country=config.registrant_country,
        company_name=config.registrant_company,
        email_override=config.registrant_email,
        password_override=config.registrant_password,
        first_name_override=config.registrant_first_name,
        last_name_override=config.registrant_last_name,
        config=config,
        temp_email_provider=_temp_email_provider_from_config(config),
        deadline=deadline,
        cancelled=cancelled,
    )
    # run_registration may have followed a new tab internally (see
    # registration.py's _switch_to_new_page) -- that only rebinds its own
    # local page variable, so the session must be re-synced here for
    # post-registration exploration to pick up the right tab.
    _sync_session_to_newest_page(session)
    return result
