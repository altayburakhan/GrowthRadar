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

import csv
import io
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from growthradar.config import Config
from growthradar.evidence import EvidenceStore
from growthradar.history import RunHistoryStore
from growthradar.orchestrator import RunOutcome, run_growthradar_session
from growthradar.report import generate_report, to_dict
from growthradar.scoring import score_run

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


@dataclass
class BatchRow:
    input: str
    url: str | None
    status: str  # "queued" | "invalid" | "running" | "done" | "error"
    run_id: str | None = None
    error: str | None = None


@dataclass
class BatchState:
    batch_id: str
    rows: list[BatchRow]
    status: str  # "running" | "done"


_batches: dict[str, BatchState] = {}
_batches_lock = threading.Lock()


def _parse_batch_csv(content: str) -> list[BatchRow]:
    """One target URL per row, first column.

    Blank lines are skipped silently -- trailing blank lines are routine in
    exported/hand-edited CSVs, not a user error worth flagging. A bare word
    like "url" or "example" is queued as-is (https:// prefixed, same as the
    single-URL dashboard input) rather than guessed at as a stray header --
    there's no reliable way to tell a header cell apart from a genuine
    one-word intranet hostname without a network call, and a bad guess just
    fails gracefully as a normal run error like any other unreachable
    target. Only rows with no possible host at all (e.g. a bare scheme with
    nothing after it) or an explicitly non-http(s) scheme (e.g. "ftp://",
    "mailto:") are flagged "invalid" up front, so the uploader sees exactly
    which rows never ran instead of a silently-dropped row looking like it
    ran and scored nothing (Linear.md: "never make unsupported claims").
    """
    rows: list[BatchRow] = []
    for raw_row in csv.reader(io.StringIO(content)):
        if not raw_row or not raw_row[0].strip():
            continue
        raw = raw_row[0].strip()
        candidate = raw if "://" in raw else f"https://{raw}"
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            rows.append(BatchRow(input=raw, url=None, status="invalid", error="Gecersiz URL"))
            continue
        rows.append(BatchRow(input=raw, url=candidate, status="queued"))
    return rows


def _run_batch(batch: BatchState) -> None:
    # Deliberately sequential, one Playwright session at a time -- matches
    # every other phase's per-run isolation and avoids several real browser
    # sessions competing for the same machine's resources at once.
    config = Config.from_env()
    for row in batch.rows:
        if row.status == "invalid":
            continue
        row.status = "running"
        row.run_id = f"web-{uuid.uuid4().hex[:8]}"
        assert row.url is not None
        job = JobState(
            run_id=row.run_id,
            url=row.url,
            status="running",
            started_at=datetime.now(UTC).isoformat(),
            config=config,
        )
        with _jobs_lock:
            _jobs[row.run_id] = job
        _execute(job, headless=True, attempt_registration=True)
        row.status = job.status
        row.error = job.error
    batch.status = "done"


def _screenshots_for_run(config: Config, run_id: str) -> list[dict[str, Any]]:
    # Matches on visible_ui.screenshot_kind (set by every capture_and_record
    # call, see screenshot.py), not the label text -- registration.py's own
    # captures ("registration form", "registration form after N step(s)",
    # "registration blocked by anti-bot challenge (captcha)") never started
    # with "screenshot:" and were previously invisible in this dashboard
    # entirely, exploration-phase page screenshots being the only ones that
    # ever showed up.
    shots: list[dict[str, Any]] = []
    with EvidenceStore.from_config(config) as store:
        for e in store.for_run(run_id):
            if not e.screenshot:
                continue
            kind = e.visible_ui.get("screenshot_kind") if isinstance(e.visible_ui, dict) else None
            if kind is None:
                continue
            shots.append(
                {
                    "url": "/" + e.screenshot.replace("\\", "/"),
                    "kind": kind,
                    "page_url": e.url,
                }
            )
    return shots


def _run_from_evidence(config: Config, run_id: str) -> dict[str, Any] | None:
    # Fallback for history entries: `_jobs` is an in-memory, per-process
    # registry (see module docstring), so it never has anything for a run
    # started before the current server process -- a previous dashboard
    # session, or any run kicked off via the CLI. `score_run`/`generate_report`
    # are pure functions over an evidence list (report.py), so a past run's
    # report is a straight recompute from what's already persisted, not new
    # data collection. None (not a 404 here) if there's truly no evidence,
    # so the caller can 404.
    with EvidenceStore.from_config(config) as store:
        evidence = store.for_run(run_id)
    if not evidence:
        return None
    score = score_run(run_id, evidence, config)
    report = generate_report(run_id, evidence, score)
    return {
        "run_id": run_id,
        "url": report.product_url,
        "status": "done",
        "started_at": evidence[0].timestamp,
        "report": to_dict(report),
        "errors": [],
        "screenshots": _screenshots_for_run(config, run_id),
    }


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
            config = Config.from_env()
            fallback = _run_from_evidence(config, run_id)
            if fallback is None:
                raise HTTPException(status_code=404, detail="run not found")
            return fallback

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

    @app.post("/api/batch")
    async def start_batch(file: UploadFile = File(...)) -> dict[str, Any]:
        raw = await file.read()
        rows = _parse_batch_csv(raw.decode("utf-8", errors="replace"))
        if not rows:
            raise HTTPException(status_code=400, detail="CSV bos ya da okunamadi")

        batch_id = f"batch-{uuid.uuid4().hex[:8]}"
        batch = BatchState(batch_id=batch_id, rows=rows, status="running")
        with _batches_lock:
            _batches[batch_id] = batch
        _executor.submit(_run_batch, batch)
        return {"batch_id": batch_id, "status": "running", "row_count": len(rows)}

    @app.get("/api/batch/{batch_id}")
    def get_batch(batch_id: str) -> dict[str, Any]:
        with _batches_lock:
            batch = _batches.get(batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="batch not found")
        return {
            "batch_id": batch.batch_id,
            "status": batch.status,
            "rows": [asdict(r) for r in batch.rows],
        }

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
  .recommendation.competitor { border: 1px solid var(--warm); }
  .recommendation.existing-customer { border: 1px solid var(--cold); }
  .shots { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; margin-top: 14px; }
  .shots a { display: block; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; text-decoration: none; }
  .shots img { width: 100%; height: 110px; object-fit: cover; display: block; background: var(--bg); }
  .shots .cap { font-size: 11px; color: var(--muted); padding: 6px 8px; }
  .pages { font-size: 13px; }
  .pages li { word-break: break-all; }
  .muted { color: var(--muted); font-size: 12px; }
  .errors { color: var(--hot); font-size: 13px; margin-top: 10px; }
  .history-item {
    display: flex; justify-content: space-between; gap: 10px; padding: 8px 0;
    border-bottom: 1px solid var(--border); font-size: 13px;
    cursor: pointer;
  }
  .history-item:last-child { border-bottom: none; }
  .history-item:hover { background: var(--bg); }
  .history-item .company { font-weight: 600; }
  .history-item .url { color: var(--muted); }
  .batch-row {
    display: flex; justify-content: space-between; gap: 10px; padding: 6px 0;
    border-bottom: 1px solid var(--border); font-size: 13px;
  }
  .batch-row:last-child { border-bottom: none; }
  .batch-row.clickable { cursor: pointer; }
  .batch-row.clickable:hover { background: var(--bg); }
  .batch-row .url { color: var(--muted); }
  .batch-status { font-size: 11px; padding: 2px 8px; border-radius: 10px; color: var(--muted); }
  .batch-status.done { color: var(--cold); }
  .batch-status.error, .batch-status.invalid { color: var(--hot); }
  .batch-status.running { color: var(--warm); }
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

  <div class="panel">
    <h2 style="margin-top:0; font-size:15px;">Toplu tarama (CSV)</h2>
    <div class="row">
      <input id="csvFile" type="file" accept=".csv,text/csv" />
      <button id="batchBtn">CSV Yukle</button>
    </div>
    <div id="batchStatusLine" class="status-line"></div>
    <div id="batchList"></div>
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
const batchBtn = document.getElementById('batchBtn');
const batchStatusLine = document.getElementById('batchStatusLine');
const batchList = document.getElementById('batchList');
let pollTimer = null;
let batchPollTimer = null;

const _BATCH_STATUS_LABELS = {
  queued: 'sirada', running: 'calisiyor', done: 'tamamlandi',
  error: 'hata', invalid: 'gecersiz url',
};

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

  const competitorHtml = r.already_userguiding_customer
    ? `<div class="recommendation existing-customer">🟢 <strong>Zaten UserGuiding musterisi</strong> -- rekabetci bir mesaj degil, hesap yonetimi/upsell konusmasi.</div>`
    : (r.competitor_tools_detected && r.competitor_tools_detected.length
        ? `<div class="recommendation competitor">⚠️ <strong>Rakip arac tespit edildi: ${r.competitor_tools_detected.join(', ')}</strong> -- bu sirketin zaten onboarding arac butcesi var. Mesaji "ihtiyacin var mi"ya degil, degistirme/gecise gore kur.</div>`
        : '');

  const evidenceRows = (r.competitor_tool_evidence || []).map(s => {
    const signals = [];
    if (s.matched_scripts && s.matched_scripts.length) signals.push(`script: ${s.matched_scripts[0]}`);
    if (s.matched_globals && s.matched_globals.length) signals.push(`global: ${s.matched_globals[0]}`);
    if (s.matched_network && s.matched_network.length) signals.push(`network: ${s.matched_network[0]}`);
    return `<li><strong>${s.name}</strong> -- ${s.page_url}<br><span class="muted">${signals.join(' · ') || 'sinyal detayi yok'}</span></li>`;
  }).join('');
  const competitorEvidenceHtml = evidenceRows
    ? `<h3 style="font-size:14px;">Rakip arac nereden bulundu?</h3><ul class="pages">${evidenceRows}</ul>`
    : '';

  const emailVerificationHtml = r.email_verification_required
    ? `<div class="recommendation competitor">✉️ <strong>Bu site email dogrulamasi istiyor</strong> -- kayit formu dolduruldu ama hesap, gercek bir gelen kutusundaki linke tiklanmadan aktif olmuyor; otomatik akis burada duruyor.</div>`
    : '';

  reportPanel.innerHTML = `
    <div class="panel">
      <div class="report-head">
        <h2>${r.company}</h2>
        ${verdictBadge(r.verdict)}
      </div>
      ${competitorHtml}
      ${emailVerificationHtml}
      <dl class="fields">
        <dt>Urun</dt><dd>${r.product_url || '-'}</dd>
        <dt>Kayit tamamlandi</dt><dd>${r.registration_completed ? 'Evet' : 'Hayir'}</dd>
        <dt>Email dogrulama gerekiyor</dt><dd>${r.email_verification_required ? 'Evet' : 'Hayir'}</dd>
        <dt>Deneme surumu</dt><dd>${r.trial_available ? 'Var' : 'Yok'}</dd>
        <dt>Onboarding tespit edildi</dt><dd>${r.onboarding_detected ? 'Evet' : 'Hayir'}</dd>
        <dt>Toplanan kanit</dt><dd>${r.evidence_collected}</dd>
        <dt>Teknolojiler</dt><dd>${r.technologies_detected.join(', ') || 'Yok'}</dd>
        <dt>Rakip arac</dt><dd>${r.competitor_tools_detected.join(', ') || 'Yok'}</dd>
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
      ${competitorEvidenceHtml}
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

async function viewRun(runId) {
  if (pollTimer) { clearInterval(pollTimer); }
  startBtn.disabled = false;
  reportPanel.innerHTML = '';
  statusLine.classList.add('active');
  statusLine.innerHTML = `<span class="spinner"></span>Yukleniyor...`;

  const res = await fetch(`/api/runs/${runId}`);
  if (!res.ok) {
    statusLine.innerHTML = 'Bu calisma bulunamadi.';
    return;
  }
  const data = await res.json();
  if (data.status === 'error') {
    statusLine.innerHTML = `Basarisiz: ${data.error || 'bilinmeyen hata'}`;
    return;
  }
  statusLine.innerHTML = 'Tamamlandi.';
  renderReport(data);
}

function renderBatchRows(rows) {
  batchList.innerHTML = rows.map(r => {
    const clickable = r.status === 'done' || r.status === 'error';
    const label = _BATCH_STATUS_LABELS[r.status] || r.status;
    return `
    <div class="batch-row ${clickable ? 'clickable' : ''}" data-run-id="${r.run_id || ''}">
      <span class="url">${r.input}</span>
      <span class="batch-status ${r.status}">${r.error ? `${label}: ${r.error}` : label}</span>
    </div>`;
  }).join('');
  batchList.querySelectorAll('.batch-row.clickable').forEach(el => {
    if (el.dataset.runId) {
      el.addEventListener('click', () => viewRun(el.dataset.runId));
    }
  });
}

async function pollBatch(batchId) {
  const res = await fetch(`/api/batch/${batchId}`);
  const data = await res.json();
  renderBatchRows(data.rows);

  if (data.status === 'running') {
    const done = data.rows.filter(r => r.status === 'done' || r.status === 'error' || r.status === 'invalid').length;
    batchStatusLine.innerHTML = `<span class="spinner"></span>Isleniyor (${done}/${data.rows.length})...`;
    return;
  }
  clearInterval(batchPollTimer);
  batchBtn.disabled = false;
  batchStatusLine.innerHTML = 'Toplu tarama tamamlandi.';
  loadHistory();
}

async function startBatch() {
  const fileInput = document.getElementById('csvFile');
  const file = fileInput.files[0];
  if (!file) { return; }

  batchBtn.disabled = true;
  batchList.innerHTML = '';
  batchStatusLine.classList.add('active');
  batchStatusLine.innerHTML = `<span class="spinner"></span>Yukleniyor...`;

  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch('/api/batch', { method: 'POST', body: formData });
  if (!res.ok) {
    batchBtn.disabled = false;
    const detail = (await res.json().catch(() => ({}))).detail;
    batchStatusLine.innerHTML = detail || 'Yuklenemedi.';
    return;
  }
  const data = await res.json();
  batchPollTimer = setInterval(() => pollBatch(data.batch_id), 2000);
  pollBatch(data.batch_id);
}

async function loadHistory() {
  const res = await fetch('/api/history');
  const rows = await res.json();
  if (!rows.length) {
    historyList.innerHTML = '<p class="empty">Henuz calisma yok.</p>';
    return;
  }
  historyList.innerHTML = rows.map(r => `
    <div class="history-item" data-run-id="${r.run_id}">
      <div><span class="company">${r.company}</span> <span class="url">${r.product_url}</span></div>
      <div>${verdictBadge(r.verdict)} ${r.overall_score.toFixed(1)}</div>
    </div>`).join('');
  historyList.querySelectorAll('.history-item').forEach(el => {
    el.addEventListener('click', () => viewRun(el.dataset.runId));
  });
}

startBtn.addEventListener('click', startRun);
document.getElementById('url').addEventListener('keydown', e => { if (e.key === 'Enter') startRun(); });
batchBtn.addEventListener('click', startBatch);
loadHistory();
</script>
</body>
</html>
"""
