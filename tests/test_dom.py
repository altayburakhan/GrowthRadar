from pathlib import Path

import pytest
from patchright.sync_api import Error as PlaywrightError
from patchright.sync_api import Page

from growthradar.browser import BrowserSession
from growthradar.config import Config
from growthradar.dom import collect_and_record, collect_dom_snapshot
from growthradar.evidence import EvidenceStore


@pytest.fixture
def config() -> Config:
    return Config.from_env(env_path="/nonexistent/.env")


@pytest.fixture
def page(config: Config):
    with BrowserSession(config) as session:
        yield session.start()


def test_collects_title_and_url(page: Page) -> None:
    page.goto("data:text/html,<html><head><title>My Page</title></head><body></body></html>")

    snapshot = collect_dom_snapshot(page)

    assert snapshot.title == "My Page"
    assert snapshot.url.startswith("data:")


def test_visible_text_excludes_hidden_elements(page: Page) -> None:
    page.set_content(
        "<html><body>"
        "<p>Visible paragraph</p>"
        "<p style='display:none'>Hidden paragraph</p>"
        "</body></html>"
    )

    snapshot = collect_dom_snapshot(page)

    assert "Visible paragraph" in snapshot.visible_text
    assert "Hidden paragraph" not in snapshot.visible_text


def test_navigation_links_collected_from_nav_and_footer(page: Page) -> None:
    page.set_content(
        "<html><body>"
        "<nav><a href='/pricing'>Pricing</a></nav>"
        "<footer><a href='/help'>Help Center</a></footer>"
        "<p><a href='/ignored-in-body'>Body link</a></p>"
        "</body></html>"
    )

    snapshot = collect_dom_snapshot(page)

    hrefs = {item.href for item in snapshot.navigation}
    assert "/pricing" in hrefs
    assert "/help" in hrefs
    assert "/ignored-in-body" not in hrefs


def test_interactive_elements_include_buttons_links_and_inputs(page: Page) -> None:
    page.set_content(
        "<html><body>"
        "<button>Sign up</button>"
        "<a href='/login'>Log in</a>"
        "<input type='email' name='email' />"
        "<button style='display:none'>Hidden button</button>"
        "</body></html>"
    )

    snapshot = collect_dom_snapshot(page)

    texts = {el.text for el in snapshot.interactive_elements}
    tags = {el.tag for el in snapshot.interactive_elements}
    assert "Sign up" in texts
    assert "Log in" in texts
    assert "Hidden button" not in texts
    assert {"button", "a", "input"} <= tags


def test_html_is_cleaned_of_scripts_and_styles(page: Page) -> None:
    page.set_content(
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>window.x=1;</script><p>content</p></body></html>"
    )

    snapshot = collect_dom_snapshot(page)

    assert "<script" not in snapshot.html
    assert "<style" not in snapshot.html
    assert "<p>content</p>" in snapshot.html


def test_large_html_is_truncated_and_flagged(page: Page) -> None:
    big_text = "a" * 250_000
    page.set_content(f"<html><body><p>{big_text}</p></body></html>")

    snapshot = collect_dom_snapshot(page)

    assert len(snapshot.html) <= 200_000
    assert snapshot.truncated is True


class _FailingPage:
    """Duck-typed stand-in for a Page whose evaluate/title calls fail."""

    url = "https://broken.example.com"

    def evaluate(self, script: str) -> None:
        raise PlaywrightError("evaluation context destroyed")

    def title(self) -> str:
        raise PlaywrightError("target closed")


def test_collect_dom_snapshot_never_raises_on_failure() -> None:
    snapshot = collect_dom_snapshot(_FailingPage())  # type: ignore[arg-type]

    assert snapshot.url == "https://broken.example.com"
    assert snapshot.html == ""
    assert snapshot.interactive_elements == []


def test_collect_and_record_writes_dom_and_visible_ui_evidence(tmp_path: Path, page: Page) -> None:
    page.set_content(
        "<html><head><title>Dash</title></head>"
        "<body><nav><a href='/settings'>Settings</a></nav><p>Welcome back</p></body></html>"
    )

    with EvidenceStore(db_path=tmp_path / "e.db") as store:
        evidence = collect_and_record(page, store, "run-1", "dashboard DOM", confidence=0.4)

        assert evidence.visible_ui is not None
        assert "Welcome back" in evidence.visible_ui
        assert evidence.dom["title"] == "Dash"
        assert any(item["href"] == "/settings" for item in evidence.dom["navigation"])

        [stored] = store.for_run("run-1")
        assert stored.id == evidence.id
