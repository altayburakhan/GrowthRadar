import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.hidden_nav import discover_hidden_navigation


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


@pytest.fixture
def page(config: Config):
    with BrowserSession(config) as session:
        yield session.start()


def test_discovers_link_revealed_by_hamburger_button(page: Page) -> None:
    page.set_content(
        "<html><body>"
        "<button class='hamburger-menu' "
        "onclick=\"document.getElementById('m').style.display='block'\">"
        "☰</button>"
        "<nav id='m' style='display:none'><a href='/help'>Help Center</a></nav>"
        "</body></html>"
    )

    discovered = discover_hidden_navigation(page)

    hrefs = {item.href for item in discovered}
    assert "/help" in hrefs


def test_discovers_link_revealed_by_aria_haspopup_trigger(page: Page) -> None:
    page.set_content(
        "<html><body>"
        "<button aria-haspopup='true' aria-expanded='false' "
        "onclick=\"document.getElementById('profile-menu').style.display='block'\">"
        "Account</button>"
        "<div id='profile-menu' style='display:none'>"
        "<a href='/settings'>Settings</a><a href='/billing'>Billing</a>"
        "</div>"
        "</body></html>"
    )

    discovered = discover_hidden_navigation(page)

    hrefs = {item.href for item in discovered}
    assert "/settings" in hrefs
    assert "/billing" in hrefs


def test_discovers_link_revealed_by_labeled_menu_button(page: Page) -> None:
    page.set_content(
        "<html><body>"
        "<button onclick=\"document.getElementById('more').style.display='block'\">More</button>"
        "<div id='more' style='display:none'><a href='/docs'>Documentation</a></div>"
        "</body></html>"
    )

    discovered = discover_hidden_navigation(page)

    hrefs = {item.href for item in discovered}
    assert "/docs" in hrefs


def test_returns_empty_when_no_triggers_present(page: Page) -> None:
    page.set_content("<html><body><p>Nothing to click here</p></body></html>")

    assert discover_hidden_navigation(page) == []


def test_ignores_links_already_visible_before_interaction(page: Page) -> None:
    page.set_content(
        "<html><body>"
        "<a href='/already-visible'>Already visible</a>"
        "<button class='menu-toggle' "
        "onclick=\"document.getElementById('m').style.display='block'\">"
        "Menu</button>"
        "<nav id='m' style='display:none'><a href='/newly-revealed'>New</a></nav>"
        "</body></html>"
    )

    discovered = discover_hidden_navigation(page)

    hrefs = {item.href for item in discovered}
    assert "/already-visible" not in hrefs
    assert "/newly-revealed" in hrefs


class _FakeLocator:
    """Real Playwright Locators are lazy -- .locator()/.get_by_role() never
    raise; failures happen on an action like .count()/.click(). This fake
    mirrors that so the test exercises a realistic failure mode."""

    def count(self) -> int:
        raise PlaywrightError("target closed")


class _FailingPage:
    url = "https://broken.example.com"

    def evaluate(self, script: str, *args: object) -> None:
        raise PlaywrightError("evaluation context destroyed")

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator()

    def get_by_role(self, role: str, **kwargs: object) -> _FakeLocator:
        return _FakeLocator()


def test_never_raises_when_page_interaction_fails() -> None:
    discovered = discover_hidden_navigation(_FailingPage())  # type: ignore[arg-type]
    assert discovered == []
