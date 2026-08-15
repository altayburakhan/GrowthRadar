import time
from dataclasses import replace
from pathlib import Path

import pytest
from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import Page
from patchright.sync_api import TimeoutError as PlaywrightTimeoutError

from growthradar.browser import BrowserSession, dismiss_overlays, retry
from growthradar.config import Config


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


def _skip_if_chrome_unavailable(session: BrowserSession) -> None:
    # channel="chrome" needs a real, separately-installed Google Chrome
    # (`playwright install chrome`) -- not the bundled Chromium every other
    # test in this file uses. Skip rather than fail where it isn't present
    # (e.g. this sandbox), consistent with testing the real browser instead
    # of mocking Playwright's launch API.
    try:
        session.start()
    except PlaywrightError as exc:
        # start() can raise after already creating self._playwright (the
        # channel="chrome" launch itself is what fails) -- close() must run
        # before skip()/raise or that driver process leaks and silently
        # breaks every later test's sync_playwright() call in this session
        # ("Playwright Sync API inside the asyncio loop").
        session.close()
        if "not found" in str(exc):
            pytest.skip(f"Google Chrome not installed: {exc}")
        raise


def test_start_is_idempotent(config: Config) -> None:
    session = BrowserSession(config)
    try:
        page1 = session.start()
        page2 = session.start()
        assert page1 is page2
    finally:
        session.close()


def test_start_failure_after_driver_launch_does_not_leak_the_driver(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a failure anywhere in _start_after_driver (e.g. a real
    # launch_persistent_context SingletonLock error, seen live) used to leak
    # the playwright driver's event loop -- `with BrowserSession(...)` never
    # calls __exit__/close() when __enter__ (start()) itself raises, so
    # nothing ever stopped it. On the dashboard, whose ThreadPoolExecutor
    # reuses worker threads across scans, that leaked, still-"running" event
    # loop then made every later scan that landed on the same thread fail
    # immediately with "Please use the Async API instead" -- a completely
    # unrelated asyncio error on a run that never touched asyncio itself.
    session = BrowserSession(config)
    monkeypatch.setattr(
        session,
        "_start_after_driver",
        lambda playwright: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(RuntimeError):
        session.start()

    assert session._playwright is None
    assert session.page is None

    # The real symptom: a second, unrelated session must still be able to
    # start cleanly afterward -- it couldn't when the first one's driver was
    # left running.
    second = BrowserSession(config)
    try:
        second.start()
        assert second.page is not None
    finally:
        second.close()


def test_close_is_idempotent(config: Config) -> None:
    session = BrowserSession(config)
    session.start()
    session.close()
    session.close()  # must not raise


def test_goto_loads_page_and_returns_true(config: Config) -> None:
    with BrowserSession(config) as session:
        ok = session.goto("data:text/html,<html><body><h1>Hello</h1></body></html>")
        assert ok is True
        assert session.page is not None
        assert "Hello" in session.page.content()


def test_goto_before_start_raises() -> None:
    session = BrowserSession(Config.from_env(env_path="/nonexistent/.env"))
    with pytest.raises(RuntimeError):
        session.goto("data:text/html,<html></html>")


def test_native_dialog_is_dismissed_and_logged(config: Config) -> None:
    with BrowserSession(config) as session:
        page = session.start()
        page.set_content("<button onclick='alert(1)'>Click</button>")
        page.click("text=Click")
        page.wait_for_timeout(200)
        assert len(session.dialog_log) == 1
        assert session.dialog_log[0].dialog_type == "alert"


def test_dismiss_overlays_returns_false_when_no_buttons(config: Config) -> None:
    with BrowserSession(config) as session:
        page = session.start()
        page.set_content("<html><body><p>No overlay here</p></body></html>")
        assert dismiss_overlays(page, timeout=0.3) is False


def test_dismiss_overlays_clicks_matching_button(config: Config) -> None:
    with BrowserSession(config) as session:
        page = session.start()
        page.set_content(
            "<html><body>"
            "<div id='banner'><button onclick=\"document.getElementById('banner').remove()\">"
            "Accept all</button></div>"
            "</body></html>"
        )
        assert dismiss_overlays(page, timeout=1.0) is True
        assert page.query_selector("#banner") is None


def test_dismiss_overlays_clicks_button_inside_a_shadow_root(config: Config) -> None:
    # Regression (influencity.com): a consent-management widget rendered its
    # "Privacy Center" modal inside an open shadow root. get_by_role("button",
    # name=...) found nothing there -- apparently unable to compute an
    # accessible name for the shadow-hosted button -- even though the button
    # was visible and clickable via a plain CSS locator.
    with BrowserSession(config) as session:
        page = session.start()
        page.set_content("<html><body><div id='host'></div></body></html>")
        page.evaluate("""() => {
                const host = document.getElementById('host');
                const root = host.attachShadow({mode: 'open'});
                // this.parentElement, not document.getElementById -- the
                // shadow root has its own id scope the light-DOM document
                // can't see into.
                root.innerHTML = `<div id="modal">
                    <button onclick="this.parentElement.remove()">Accept</button>
                </div>`;
            }""")
        assert dismiss_overlays(page, timeout=1.0) is True
        assert page.evaluate(
            "() => !document.getElementById('host').shadowRoot.getElementById('modal')"
        )


def test_dismiss_overlays_clicks_an_icon_only_button_via_aria_label(config: Config) -> None:
    # Regression (mirro.io): a HubSpot marketing popup's close control has
    # no visible text at all -- just an SVG icon behind
    # role="button" aria-label="Close" -- so the existing text-only
    # matching never found it, leaving the popup up and blocking every
    # click on the page beneath it (including the site's own "Login" link).
    with BrowserSession(config) as session:
        page = session.start()
        page.set_content(
            "<html><body>"
            '<div id="popup" role="button" aria-label="Close" '
            "onclick=\"document.getElementById('popup').remove()\">"
            "<svg></svg></div>"
            "</body></html>"
        )
        assert dismiss_overlays(page, timeout=1.0) is True
        assert page.locator("#popup").count() == 0


def test_dismiss_overlays_finds_overlay_inside_an_iframe(config: Config) -> None:
    # Regression (mirro.io): the popup's actual content, including its
    # close button, rendered inside a same-origin-restricted iframe served
    # from a third-party (HubSpot) domain -- a plain page.locator() never
    # searches inside an iframe's document at all, so the popup was
    # permanently undismissable no matter what button-matching it used.
    with BrowserSession(config) as session:
        page = session.start()
        inner_html = '<button onclick="this.remove()">Got it</button>'
        page.set_content(f"<html><body><iframe srcdoc='{inner_html}'></iframe></body></html>")
        page.wait_for_timeout(300)
        assert dismiss_overlays(page, timeout=1.0) is True
        frame = next(f for f in page.frames if f.url == "about:srcdoc")
        assert frame.locator("button").count() == 0


def test_dismiss_overlays_does_not_click_an_unrelated_link_containing_agree(
    config: Config,
) -> None:
    # Regression (onlypult.com): a plain-string has_text="Agree" filter is a
    # substring match, so it also matched an unrelated "License Agreement"
    # link sitting right under the signup form's own submit button --
    # dismiss_overlays (called every registration loop iteration) clicked it
    # repeatedly, each click opening a new tab and abandoning the actual,
    # still-unsubmitted form.
    with BrowserSession(config) as session:
        page = session.start()
        page.set_content(
            "<html><body>"
            "<a href='https://example.test/license_agreement' target='_blank'>"
            "License Agreement</a>"
            "</body></html>"
        )
        assert dismiss_overlays(page, timeout=0.3) is False
        assert len(page.context.pages) == 1


def test_retry_reraises_after_exhausting_attempts() -> None:
    calls = {"n": 0}

    def always_fail() -> str:
        calls["n"] += 1
        raise PlaywrightTimeoutError("boom")

    with pytest.raises(PlaywrightTimeoutError):
        retry(always_fail, retries=2, delay=0)
    assert calls["n"] == 2


def _route_fake_site(page: Page) -> None:
    def handler(route):  # noqa: ANN001
        if route.request.url.endswith(".gif"):
            route.fulfill(status=200, content_type="image/gif", body=b"GIF89a")
        else:
            route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<html><body>"
                    "<img src='https://fake.growthradar.test/pixel.gif' />"
                    "</body></html>"
                ),
            )

    page.route("https://fake.growthradar.test/**", handler)


def test_goto_records_requests_made_during_navigation(config: Config) -> None:
    with BrowserSession(config) as session:
        page = session.start()
        _route_fake_site(page)

        ok = session.goto("https://fake.growthradar.test/")

        assert ok is True
        assert len(session.requests) >= 2
        assert any(r.resource_type == "document" for r in session.requests)
        assert any(r.resource_type == "image" for r in session.requests)


def test_goto_clears_requests_from_previous_navigation(config: Config) -> None:
    with BrowserSession(config) as session:
        page = session.start()
        _route_fake_site(page)
        session.goto("https://fake.growthradar.test/")
        assert any(r.resource_type == "image" for r in session.requests)

        session.goto("data:text/html,<html><body>No images here</body></html>")
        assert all(r.resource_type != "image" for r in session.requests)


def test_google_profile_dir_reuses_a_persistent_session_across_runs(
    tmp_path: Path, config: Config
) -> None:
    # The whole point of google_profile_dir: state written by one run (here,
    # a cookie standing in for a signed-in Google session) must still be
    # there the next time a BrowserSession points at the same directory --
    # otherwise every run would need to sign in from scratch, defeating the
    # purpose of a persistent profile.
    profile_dir = str(tmp_path / "chrome-profile")
    profile_config = replace(config, google_profile_dir=profile_dir)

    first = BrowserSession(profile_config)
    _skip_if_chrome_unavailable(first)
    try:
        page = first.page
        assert page is not None
        page.goto("data:text/html,<html><body>first run</body></html>")
        page.context.add_cookies(
            [
                {
                    "name": "growthradar_test_session",
                    "value": "persisted",
                    "url": "https://accounts.google.com",
                    # A real signed-in-session cookie is persistent, not
                    # session-only -- without an explicit expiry, Playwright
                    # defaults to a session cookie, which browsers correctly
                    # discard on restart by design (not a persistence bug,
                    # just the wrong kind of cookie for this test).
                    "expires": time.time() + 3600,
                }
            ]
        )
    finally:
        first.close()

    second = BrowserSession(profile_config)
    try:
        second.start()
        cookies = second.page.context.cookies("https://accounts.google.com")  # type: ignore[union-attr]
        assert any(
            c["name"] == "growthradar_test_session" and c["value"] == "persisted" for c in cookies
        )
    finally:
        second.close()


def test_retry_succeeds_after_transient_failure() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise PlaywrightError("boom")
        return "ok"

    assert retry(flaky, retries=3, delay=0) == "ok"
    assert calls["n"] == 2
