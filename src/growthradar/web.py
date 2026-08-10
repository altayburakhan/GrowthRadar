"""Local web dashboard: a clickable alternative to the CLI.

Wraps `orchestrator.run_growthradar_session` behind a small FastAPI app so a
user can paste a URL, click a button, and watch the run progress in a
browser instead of a terminal. Each run executes in a background thread
(Playwright's sync API isn't asyncio-compatible) via a small in-memory job
registry -- state lives only for the lifetime of the server process, which
is fine for a single-user local tool. Screenshots and history are read back
from the same SQLite evidence/history stores every other module writes to,
so the dashboard never duplicates data collection.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from growthradar.config import Config
from growthradar.evidence import EvidenceStore
from growthradar.history import RunHistoryStore
from growthradar.orchestrator import RunOutcome, run_growthradar_session
from growthradar.report import to_dict

logger = logging.getLogger(__name__)

_SCREENSHOT_DIR = Path("screenshots")
_SCREENSHOT_DIR.mkdir(exist_ok=True)


@dataclass
class JobState:
    run_id: str
    url: str
    status: str  # "running" | "done" | "error"
    started_at: str
    config: Config
    outcome: RunOutcome | None = None
    error: str | None = None


_jobs: dict[str, JobState] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="growthradar-run")


def _execute(job: JobState, *, headless: bool, attempt_registration: bool) -> None:
    try:
        outcome = run_growthradar_session(
            job.url,
            config=job.config,
            run_id=job.run_id,
            headless=headless,
            attempt_registration=attempt_registration,
        )
        with _jobs_lock:
            job.outcome = outcome
            job.status = "done"
    except Exception as exc:  # last-resort safety net; run_growthradar_session shouldn't raise
        logger.exception("dashboard run %s failed unexpectedly", job.run_id)
        with _jobs_lock:
            job.error = str(exc)
            job.status = "error"


def _screenshots_for_run(config: Config, run_id: str) -> list[dict[str, Any]]:
    shots: list[dict[str, Any]] = []
    with EvidenceStore.from_config(config) as store:
        for e in store.for_run(run_id):
            if not e.label.startswith("screenshot:") or not e.screenshot:
                continue
            kind = e.visible_ui.get("screenshot_kind") if isinstance(e.visible_ui, dict) else None
            shots.append(
                {
                    "url": "/" + e.screenshot.replace("\\", "/"),
                    "kind": kind,
                    "page_url": e.url,
                }
            )
    return shots


class StartRunRequest(BaseModel):
    url: str
    max_pages: int | None = None
    attempt_registration: bool = True
    country: str | None = None
    company: str | None = None
    email: str | None = None


def create_app() -> FastAPI:
    app = FastAPI(title="GrowthRadar Dashboard")
    app.mount("/screenshots", StaticFiles(directory=str(_SCREENSHOT_DIR)), name="screenshots")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    @app.post("/api/runs")
    def start_run(req: StartRunRequest) -> dict[str, str]:
        if not req.url.strip():
            raise HTTPException(status_code=400, detail="url is required")

        config = Config.from_env()
        overrides: dict[str, Any] = {}
        if req.max_pages is not None:
            overrides["max_pages"] = req.max_pages
        if req.country:
            overrides["registrant_country"] = req.country
        if req.company:
            overrides["registrant_company"] = req.company
        if req.email:
            overrides["registrant_email"] = req.email
        if overrides:
            config = replace(config, **overrides)

        run_id = f"web-{uuid.uuid4().hex[:8]}"
        job = JobState(
            run_id=run_id,
            url=req.url.strip(),
            status="running",
            started_at=datetime.now(UTC).isoformat(),
            config=config,
        )
        with _jobs_lock:
            _jobs[run_id] = job
        _executor.submit(
            _execute, job, headless=True, attempt_registration=req.attempt_registration
        )
        return {"run_id": run_id, "status": "running"}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        with _jobs_lock:
            job = _jobs.get(run_id)
        if job is None:
            raise HTTPException(status_code=404, detail="run not found")

        payload: dict[str, Any] = {
            "run_id": job.run_id,
            "url": job.url,
            "status": job.status,
            "started_at": job.started_at,
        }
        if job.status == "done" and job.outcome is not None:
            payload["report"] = to_dict(job.outcome.report)
            payload["errors"] = list(job.outcome.errors)
            payload["screenshots"] = _screenshots_for_run(job.config, run_id)
        elif job.status == "error":
            payload["error"] = job.error
        return payload

    @app.get("/api/history")
    def history(limit: int = 20) -> list[dict[str, Any]]:
        config = Config.from_env()
        with RunHistoryStore.from_config(config) as store:
            rows = store.recent(limit=limit)
        return [asdict(r) for r in rows]

    return app


app = create_app()


_INDEX_HTML = """<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>GrowthRadar Dashboard</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #0b0d12; --panel: #151922; --border: #262c38; --text: #e6e9ef;
    --muted: #8b93a3; --accent: #5b8cff; --hot: #ff5470; --warm: #ffb020; --cold: #5b8cff;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg: #f5f6f8; --panel: #ffffff; --border: #e2e5ea; --text: #1a1d24; --muted: #656d7d; }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.5;
  }
  .wrap { max-width: 960px; margin: 0 auto; padding: 32px 20px 80px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .subtitle { color: var(--muted); margin: 0 0 28px; font-size: 14px; }
  .panel {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 20px; margin-bottom: 20px;
  }
  .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
  input[type=text], input[type=number] {
    background: var(--bg); border: 1px solid var(--border); color: var(--text);
    border-radius: 8px; padding: 10px 12px; font-size: 14px;
  }
  #url { flex: 1; min-width: 240px; }
  #maxPages { width: 90px; }
  label.check { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); }
  button {
    background: var(--accent); color: white; border: none; border-radius: 8px;
    padding: 10px 18px; font-size: 14px; cursor: pointer; font-weight: 600;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  .status-line { margin-top: 14px; font-size: 13px; color: var(--muted); display: none; }
  .status-line.active { display: block; }
  .spinner {
    display: inline-block; width: 12px; height: 12px; border: 2px solid var(--border);
    border-top-color: var(--accent); border-radius: 50%; animation: spin 0.8s linear infinite;
    vertical-align: middle; margin-right: 6px;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .badge {
    display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px;
    font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em;
  }
  .badge.hot { background: color-mix(in srgb, var(--hot) 20%, transparent); color: var(--hot); }
  .badge.warm { background: color-mix(in srgb, var(--warm) 20%, transparent); color: var(--warm); }
  .badge.cold { background: color-mix(in srgb, var(--cold) 20%, transparent); color: var(--cold); }
  .report-head { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .report-head h2 { margin: 0; font-size: 18px; }
  .fields { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 20px; margin: 16px 0; font-size: 13px; }
  .fields dt { color: var(--muted); }
  .fields dd { margin: 0; }
  .bars { display: flex; flex-direction: column; gap: 10px; margin: 16px 0; }
  .bar-label { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  .bar-track { background: var(--bg); border-radius: 6px; height: 8px; overflow: hidden; }
  .bar-fill { height: 100%; background: var(--accent); }
  .recommendation { padding: 12px 14px; background: var(--bg); border-radius: 8px; font-size: 14px; margin: 16px 0; }
  .recommendation.llm { border: 1px solid var(--accent); }
  .shots { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; margin-top: 14px; }
  .shots a { display: block; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; text-decoration: none; }
  .shots img { width: 100%; height: 110px; object-fit: cover; display: block; background: var(--bg); }
  .shots .cap { font-size: 11px; color: var(--muted); padding: 6px 8px; }
  .pages { font-size: 13px; }
  .pages li { word-break: break-all; }
  .errors { color: var(--hot); font-size: 13px; margin-top: 10px; }
  .history-item {
    display: flex; justify-content: space-between; gap: 10px; padding: 8px 0;
    border-bottom: 1px solid var(--border); font-size: 13px;
  }
  .history-item:last-child { border-bottom: none; }
  .history-item .company { font-weight: 600; }
  .history-item .url { color: var(--muted); }
  .empty { color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>GrowthRadar Dashboard</h1>
  <p class="subtitle">Bir URL gir, Başlat'a bas -- ajan siteyi kesfeder, kayit dener, skorlar.</p>

  <div class="panel">
    <div class="row">
      <input id="url" type="text" placeholder="https://example.com" />
      <input id="maxPages" type="number" placeholder="8" min="1" />
      <label class="check"><input id="attemptRegistration" type="checkbox" checked /> Kayit dene</label>
      <button id="startBtn">Baslat</button>
    </div>
    <div class="row" style="margin-top:10px;">
      <input id="country" type="text" placeholder="Ulke (varsayilan: United States)" />
      <input id="company" type="text" placeholder="Sirket adi (bos = rastgele)" />
      <input id="email" type="text" placeholder="E-posta (bos = rastgele @example.com)" />
    </div>
    <div id="statusLine" class="status-line"></div>
  </div>

  <div id="reportPanel"></div>

  <div class="panel">
    <h2 style="margin-top:0; font-size:15px;">Gecmis calismalar</h2>
    <div id="historyList" class="empty">Yukleniyor...</div>
  </div>
</div>

<script>
const startBtn = document.getElementById('startBtn');
const statusLine = document.getElementById('statusLine');
const reportPanel = document.getElementById('reportPanel');
const historyList = document.getElementById('historyList');
let pollTimer = null;

function verdictBadge(verdict) {
  return `<span class="badge ${verdict}">${verdict}</span>`;
}

function renderBar(label, dim) {
  const pct = Math.max(0, Math.min(100, dim.score));
  return `<div>
    <div class="bar-label"><span>${label}</span><span>${dim.score.toFixed(1)} (${dim.signal_count}/${dim.max_signals})</span></div>
    <div class="bar-track"><div class="bar-fill" style="width:${pct}%"></div></div>
  </div>`;
}

function renderReport(data) {
  const r = data.report;
  const shots = data.screenshots || [];
  const shotsHtml = shots.length
    ? `<div class="shots">${shots.map(s => `
        <a href="${s.url}" target="_blank" rel="noopener">
          <img src="${s.url}" loading="lazy" />
          <div class="cap">${s.kind || 'page'}</div>
        </a>`).join('')}</div>`
    : '<p class="empty">Ekran goruntusu yok.</p>';

  const pagesHtml = r.explored_pages.length
    ? `<ul class="pages">${r.explored_pages.map(u => `<li>${u}</li>`).join('')}</ul>`
    : '<p class="empty">Sayfa kesfedilmedi.</p>';

  const errorsHtml = (data.errors && data.errors.length)
    ? `<div class="errors">Uyarilar:<ul>${data.errors.map(e => `<li>${e}</li>`).join('')}</ul></div>`
    : '';

  reportPanel.innerHTML = `
    <div class="panel">
      <div class="report-head">
        <h2>${r.company}</h2>
        ${verdictBadge(r.verdict)}
      </div>
      <dl class="fields">
        <dt>Urun</dt><dd>${r.product_url || '-'}</dd>
        <dt>Kayit tamamlandi</dt><dd>${r.registration_completed ? 'Evet' : 'Hayir'}</dd>
        <dt>Deneme surumu</dt><dd>${r.trial_available ? 'Var' : 'Yok'}</dd>
        <dt>Onboarding tespit edildi</dt><dd>${r.onboarding_detected ? 'Evet' : 'Hayir'}</dd>
        <dt>Toplanan kanit</dt><dd>${r.evidence_collected}</dd>
        <dt>Teknolojiler</dt><dd>${r.technologies_detected.join(', ') || 'Yok'}</dd>
        <dt>Yardim merkezi</dt><dd>${r.help_center_url || 'Bulunamadi'}</dd>
        <dt>Guven skoru</dt><dd>${r.confidence_score.toFixed(1)}/100</dd>
      </dl>
      <div class="recommendation">${r.final_recommendation}</div>
      ${r.llm_summary ? `<div class="recommendation llm"><strong>AI ozeti:</strong> ${r.llm_summary}</div>` : ''}
      <div class="bars">
        ${renderBar('ICP fit', r.score.icp_fit)}
        ${renderBar('Onboarding firsati', r.score.onboarding_opportunity)}
        ${renderBar('Urun deneyimi', r.score.product_experience)}
      </div>
      ${errorsHtml}
      <h3 style="font-size:14px;">Ekran goruntuleri</h3>
      ${shotsHtml}
      <h3 style="font-size:14px;">Kesfedilen sayfalar (${r.explored_pages.length})</h3>
      ${pagesHtml}
    </div>`;
}

async function poll(runId) {
  const res = await fetch(`/api/runs/${runId}`);
  const data = await res.json();

  if (data.status === 'running') {
    statusLine.innerHTML = `<span class="spinner"></span>Calisiyor...`;
    return;
  }
  clearInterval(pollTimer);
  startBtn.disabled = false;

  if (data.status === 'error') {
    statusLine.innerHTML = `Basarisiz: ${data.error || 'bilinmeyen hata'}`;
    return;
  }
  statusLine.innerHTML = 'Tamamlandi.';
  renderReport(data);
  loadHistory();
}

async function startRun() {
  const url = document.getElementById('url').value.trim();
  if (!url) { return; }
  const maxPagesVal = document.getElementById('maxPages').value;
  const attemptRegistration = document.getElementById('attemptRegistration').checked;
  const country = document.getElementById('country').value.trim();
  const company = document.getElementById('company').value.trim();
  const email = document.getElementById('email').value.trim();

  startBtn.disabled = true;
  reportPanel.innerHTML = '';
  statusLine.classList.add('active');
  statusLine.innerHTML = `<span class="spinner"></span>Baslatiliyor...`;

  const res = await fetch('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      url,
      max_pages: maxPagesVal ? parseInt(maxPagesVal, 10) : null,
      attempt_registration: attemptRegistration,
      country: country || null,
      company: company || null,
      email: email || null,
    }),
  });
  if (!res.ok) {
    startBtn.disabled = false;
    statusLine.innerHTML = 'Baslatilamadi.';
    return;
  }
  const data = await res.json();
  pollTimer = setInterval(() => poll(data.run_id), 2000);
  poll(data.run_id);
}

async function loadHistory() {
  const res = await fetch('/api/history');
  const rows = await res.json();
  if (!rows.length) {
    historyList.innerHTML = '<p class="empty">Henuz calisma yok.</p>';
    return;
  }
  historyList.innerHTML = rows.map(r => `
    <div class="history-item">
      <div><span class="company">${r.company}</span> <span class="url">${r.product_url}</span></div>
      <div>${verdictBadge(r.verdict)} ${r.overall_score.toFixed(1)}</div>
    </div>`).join('');
}

startBtn.addEventListener('click', startRun);
document.getElementById('url').addEventListener('keydown', e => { if (e.key === 'Enter') startRun(); });
loadHistory();
</script>
</body>
</html>
"""
