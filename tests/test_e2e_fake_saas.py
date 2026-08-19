"""End-to-end pipeline test against a local, fully offline "fake SaaS site".

No real network is touched: every page is a `data:` URL, and the one
"third-party script" reference uses the RFC 2606 `.invalid` TLD, which is
guaranteed to never resolve -- so DNS fails fast and deterministically in any
environment (online or offline), while its `src` text still contains the
substring GRO-13's tool signatures match on (script-tag detection doesn't
require the resource to actually load).
"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from growthradar.config import Config
from growthradar.event_log import read_log
from growthradar.orchestrator import run_growthradar_session
from growthradar.report import to_json, to_markdown

# Present in every page's <head>, like a real site-wide layout script include.
_FAKE_COMPETITOR_SCRIPT = "<script src='https://pendo.io.invalid/agent.js'></script>"


def _data_url(html: str) -> str:
    return "data:text/html," + quote(html)


def _page(title: str, body: str) -> str:
    head = f"<title>{title}</title>{_FAKE_COMPETITOR_SCRIPT}"
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _build_fake_saas_site() -> dict[str, str]:
    dashboard_url = _data_url(
        _page(
            "Dashboard",
            "<h1>Dashboard</h1>"
            "<div class='onboarding-checklist'>Welcome! Here is your onboarding checklist:"
            "<ul><li>Step 1 of 4: Connect your data</li></ul></div>"
            "<div class='tooltip'>Need help? Hover for a tooltip.</div>",
        )
    )
    help_url = _data_url(
        _page(
            "Help Center",
            "<h1>Help Center</h1><p>Browse our knowledge base and resource center.</p>",
        )
    )
    changelog_url = _data_url(
        _page("Changelog", "<h1>Product Updates</h1><p>See what's new this month.</p>")
    )
    signup_url = _data_url(
        _page(
            "Sign Up",
            "<div id='signup-form'>"
            "<input name='email' type='email' placeholder='Email' />"
            "<input name='password' type='password' placeholder='Password' />"
            "<input name='company' placeholder='Company name' />"
            "<button onclick=\"document.getElementById('signup-form').style.display='none';"
            "document.body.insertAdjacentHTML("
            "'beforeend', '<div id=welcome>Welcome aboard!</div>');"
            '">Sign up</button>'
            "</div>",
        )
    )
    home_url = _data_url(
        _page(
            "Acme",
            "<h1>Acme -- Project Management for Teams</h1>"
            "<nav>"
            f"<a href='{signup_url}'>Sign up</a>"
            f"<a href='{dashboard_url}'>Dashboard</a>"
            f"<a href='{help_url}'>Help Center</a>"
            f"<a href='{changelog_url}'>Product Updates</a>"
            "</nav>"
            "<p>Start your free trial today, no credit card required.</p>",
        )
    )

    return {
        "home": home_url,
        "signup": signup_url,
        "dashboard": dashboard_url,
        "help": help_url,
        "changelog": changelog_url,
    }


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("GROWTHRADAR_DB_PATH", str(tmp_path / "growthradar.db"))
    monkeypatch.setenv("GROWTHRADAR_MAX_PAGES", "10")
    monkeypatch.setenv("GROWTHRADAR_CRAWL_DELAY", "0")
    # Bounds how long each page load waits on the doomed-to-fail .invalid request.
    monkeypatch.setenv("GROWTHRADAR_REQUEST_TIMEOUT", "5")
    return Config.from_env(env_path="/nonexistent/.env")


def test_full_pipeline_against_fake_saas_site(tmp_path: Path, config: Config) -> None:
    site = _build_fake_saas_site()

    outcome = run_growthradar_session(
        site["home"], config=config, run_id="e2e-run", log_dir=tmp_path
    )

    # channel="chrome" (see browser.py) needs a real, separately-installed
    # Google Chrome (`patchright install chrome`), not the bundled Chromium.
    # Skip rather than fail where it isn't present, instead of every other
    # assertion below failing for the same unrelated reason.
    if any("chrome" in e.lower() and "not found" in e.lower() for e in outcome.errors):
        pytest.skip(f"Google Chrome not installed: {outcome.errors}")

    # -- Exploration: every page was discovered and visited --
    assert outcome.exploration is not None
    visited_urls = {v.url for v in outcome.exploration.visited if v.success}
    assert visited_urls == set(site.values())

    # -- Registration: the signup form was found and completed --
    assert outcome.registration is not None
    assert outcome.registration.submitted is True
    assert outcome.registration.steps_completed == 1

    # -- Report: every Linear.md "Final Report" field reflects real evidence --
    report = outcome.report
    assert report.registration_completed is True
    assert report.trial_available is True
    assert report.onboarding_detected is True
    assert "Pendo" in report.technologies_detected
    assert report.help_center_url == site["help"]
    assert site["changelog"] in report.product_update_pages
    assert len(report.explored_pages) == 5
    assert report.evidence_collected > 10
    assert report.verdict in ("warm", "hot")
    assert 0.0 <= report.confidence_score <= 100.0

    # -- Rendering both output formats works on real pipeline output --
    markdown = to_markdown(report)
    assert "GrowthRadar Report" in markdown
    assert "Pendo" in markdown

    payload = json.loads(to_json(report))
    assert payload["technologies_detected"] == list(report.technologies_detected)

    assert outcome.errors == ()


class _FakeGroqResponse:
    """Minimal stand-in for `http.client.HTTPResponse`, matching how
    vision_fallback.py/llm_summary.py read a `urllib.request.urlopen` result
    (context manager + `.read()`), used to fake every Groq call below without
    touching the network."""

    def __init__(self, content: str) -> None:
        self._data = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")

    def __enter__(self) -> "_FakeGroqResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def test_vision_fallback_fills_field_unrecognized_by_dom_heuristics(
    tmp_path: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A field with no keyword match anywhere in registration.py's
    _FIELD_PATTERNS (or the generic name-field fallback) -- e.g. a custom
    "team size" question -- must still get filled via the vision fallback
    (see registration.py's _fill_via_vision_fallback) instead of being left
    empty while the DOM-based heuristics silently move on to something else.
    """
    vision_config = replace(
        config, groq_api_key="gsk-test-123", groq_vision_model="qwen/qwen3.6-27b"
    )

    signup_url = _data_url(
        _page(
            "Sign Up",
            "<div id='signup-form'>"
            "<input name='email' type='email' placeholder='Email' />"
            "<input name='team_size' placeholder='How many people are on your team?' />"
            "<button onclick=\"document.getElementById('signup-form').style.display='none';"
            "document.body.insertAdjacentHTML("
            "'beforeend', '<div id=welcome>Welcome aboard!</div>');"
            '">Sign up</button>'
            "</div>",
        )
    )
    home_url = _data_url(
        _page(
            "Acme",
            f"<h1>Acme -- Project Management for Teams</h1>"
            f"<nav><a href='{signup_url}'>Sign up</a></nav>"
            "<p>Start your free trial today, no credit card required.</p>",
        )
    )

    def fake_urlopen(request: Any, timeout: float = 30) -> _FakeGroqResponse:
        # Every Groq caller in the pipeline (this fallback, llm_summary.py's
        # narrative) shares the same choices[0].message.content shape and
        # degrades gracefully on unexpected content (see each module's own
        # "never raises" contract) -- one canned field-suggestion answer is
        # enough to exercise the path under test without the others erroring.
        return _FakeGroqResponse('{"1": "12-20 people"}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    outcome = run_growthradar_session(
        home_url, config=vision_config, run_id="e2e-vision-run", log_dir=tmp_path
    )

    if any("chrome" in e.lower() and "not found" in e.lower() for e in outcome.errors):
        pytest.skip(f"Google Chrome not installed: {outcome.errors}")

    assert outcome.registration is not None
    assert outcome.registration.submitted is True

    entries = list(read_log(tmp_path / "e2e-vision-run.jsonl"))
    fill_events = [e for e in entries if e.message == "vision_fallback_filled"]
    assert fill_events, "expected the vision fallback to fire and fill the unrecognized field"
    assert fill_events[0].data["fields"] == 1


def test_radio_group_choice_gates_disabled_submit_button(
    tmp_path: Path, config: Config
) -> None:
    """A native `<input type="radio">` picker gating an otherwise-disabled
    submit button (seen live on doxy.me: MUI `role="radiogroup"` cards,
    "I'm a provider" / "I'm a patient") must get selected -- see
    registration.py's _check_unclaimed_radio_option. Before that function
    existed, none of the fill/check/click-chooser heuristics recognized a
    bare radio input, so this screen stalled at steps_completed=0 forever.
    """
    signup_url = _data_url(
        _page(
            "Sign Up",
            "<div role='radiogroup'>"
            "<label><input type='radio' name='user-role' value='provider' "
            "onclick=\"document.getElementById('continue-btn').disabled=false\">"
            "<h2>I'm a provider</h2></label>"
            "<label><input type='radio' name='user-role' value='patient' "
            "onclick=\"document.getElementById('continue-btn').disabled=false\">"
            "<h2>I'm a patient</h2></label>"
            "</div>"
            "<button id='continue-btn' disabled "
            "onclick=\"document.body.insertAdjacentHTML("
            "'beforeend', '<div id=welcome>Welcome aboard!</div>');\">"
            "Continue</button>",
        )
    )
    home_url = _data_url(
        _page(
            "Acme",
            f"<h1>Acme -- Project Management for Teams</h1>"
            f"<nav><a href='{signup_url}'>Sign up</a></nav>"
            "<p>Start your free trial today, no credit card required.</p>",
        )
    )

    outcome = run_growthradar_session(
        home_url, config=config, run_id="e2e-radio-run", log_dir=tmp_path
    )

    if any("chrome" in e.lower() and "not found" in e.lower() for e in outcome.errors):
        pytest.skip(f"Google Chrome not installed: {outcome.errors}")

    assert outcome.registration is not None
    assert outcome.registration.submitted is True
