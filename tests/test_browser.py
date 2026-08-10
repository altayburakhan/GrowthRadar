import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from growthradar.browser import BrowserSession, dismiss_overlays, retry
from growthradar.config import Config


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


def test_start_is_idempotent(config: Config) -> None:
    session = BrowserSession(config)
    try:
        page1 = session.start()
        page2 = session.start()
        assert page1 is page2
    finally:
        session.close()


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


def test_retry_succeeds_after_transient_failure() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise PlaywrightError("boom")
        return "ok"

    assert retry(flaky, retries=3, delay=0) == "ok"
    assert calls["n"] == 2
