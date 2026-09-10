"""UAE Invoice Compliance — FastAPI app.

Single-purpose web tool:
  - Upload a PDF invoice
  - DeepSeek v4 (vision) extracts the structured fields
  - A flat rules.json runs deterministic e-invoicing / VAT checks against those fields
  - Render a one-page report with cited findings and a per-finding fix

Run:
    cp .env.example .env  # fill DEEPSEEK_API_KEY
    pip install -r requirements.txt
    uvicorn app.main:app --reload
    open http://127.0.0.1:8000
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.extractor import extract_invoice_fields
from app.rules_engine import evaluate

load_dotenv()  # noqa: E402  (must run before extractor reads DEEPSEEK_API_KEY)

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
RULES_PATH = BASE_DIR.parent / "rules" / "einvoicing_uae.json"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "./uploads")).resolve()

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("uae-invoice-compliance")

app = FastAPI(title="UAE Invoice Compliance", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --- helpers ----------------------------------------------------------------

def _rules_payload() -> dict[str, Any]:
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    _validate_rules(rules)
    return rules


def _validate_rules(rules: dict[str, Any]) -> None:
    """Fail loudly at startup if any rule has a bad applies_when expression.

    Without this, a SyntaxError in applies_when is swallowed by the
    eval/except in _rule_fires and the rule silently never fires. We
    hit this exact bug on 2026-09-09 (5 rules had unparseable
    `field present` expressions).
    """
    for rule in rules.get("rules", []):
        expr = rule.get("applies_when", "always")
        if expr == "always":
            continue
        try:
            compile(expr, f"<rule {rule['code']}>", "eval")
        except SyntaxError as exc:
            raise RuntimeError(
                f"Rule {rule['code']!r} has invalid applies_when expression "
                f"{expr!r}: {exc.msg}"
            ) from exc


def _risk_level(score: int) -> str:
    if score < 20:
        return "low"
    if score < 50:
        return "moderate"
    return "high"


def _score_findings(findings: list[dict[str, Any]]) -> int:
    """Risk score 0-100 — weighted by severity. Capped at 100."""
    weights = {"critical": 30, "warning": 12, "info": 3}
    raw = sum(weights.get(f["severity"], 5) for f in findings)
    return min(raw, 100)


# --- routes -----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"recent": _recent_runs(), "samples": _sample_cards(), "error": None},
    )


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/reports", response_class=HTMLResponse)
async def upload_and_run(request: Request, file: UploadFile = File(...)) -> Response:
    # Persist upload so we can preview it on the report page
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = Path(file.filename or "invoice.pdf").name
    pdf_id = uuid.uuid4().hex[:12]
    saved_path = UPLOAD_DIR / f"{pdf_id}_{safe_name}"
    raw = await file.read()
    saved_path.write_bytes(raw)

    try:
        extracted = extract_invoice_fields(saved_path)
    except Exception as exc:  # extractor is the LLM boundary, surface any failure clearly
        logger.exception("Extraction failed for %s", saved_path)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "recent": _recent_runs(),
                "error": f"Could not extract fields from this file: {exc}",
            },
            status_code=400,
        )

    rules = _rules_payload()
    findings = evaluate(extracted, rules)
    score = _score_findings(findings)

    envelope = {
        "id": pdf_id,
        "filename": safe_name,
        "extracted": extracted,
        "findings": findings,
        "risk_score": score,
        "risk_level": _risk_level(score),
        "summary": _summary(findings, extracted),
        "rules_source": rules["title"],
    }
    _save_envelope(envelope)
    return RedirectResponse(url=f"/reports/{pdf_id}", status_code=303)


@app.get("/reports/{report_id}", response_class=HTMLResponse)
async def view_report(request: Request, report_id: str) -> HTMLResponse:
    envelope = _load_envelope(report_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")
    return templates.TemplateResponse(request, "report.html", {"envelope": envelope})


@app.get("/reports/{report_id}/source")
async def report_source(report_id: str) -> Response:
    """Serve the original uploaded file (PDF or image) for preview on the report page."""
    envelope = _load_envelope(report_id)
    if envelope is None:
        raise HTTPException(status_code=404, detail=f"Report '{report_id}' not found")

    # Sample reports live in static/samples/, not in uploads/
    if report_id.startswith("sample-"):
        fixture_path = STATIC_DIR / "samples" / envelope.get("filename", "")
        if not fixture_path.exists():
            raise HTTPException(
                status_code=410,
                detail=f"Sample fixture no longer on disk: {fixture_path.name}",
            )
        safe_name = fixture_path.name
        media_type = (
            "application/pdf"
            if safe_name.lower().endswith(".pdf")
            else f"image/{_img_ext(safe_name)}"
        )
        return Response(
            content=fixture_path.read_bytes(),
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
        )

    safe_name = Path(envelope.get("filename", "invoice.pdf")).name  # strip any path traversal
    source_path = UPLOAD_DIR / f"{report_id}_{safe_name}"
    if not source_path.exists():
        raise HTTPException(
            status_code=410,
            detail=f"Original file no longer on disk: {source_path.name}",
        )

    media_type = "application/pdf" if safe_name.lower().endswith(".pdf") else f"image/{_img_ext(safe_name)}"
    return Response(
        content=source_path.read_bytes(),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{safe_name}"'},
    )


_IMG_MIME = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "gif": "gif", "webp": "webp"}


def _img_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    return _IMG_MIME.get(ext, "png")


# --- on-disk envelope store (JSON blob, same pattern as uae-compliance-copilot)
# SQLite would be cleaner at scale; for a private demo, a single JSON file is fine
# and easy to inspect.

ENVELOPES_DIR = Path("./runtime_data/envelopes").resolve()
SAMPLES_DIR = (BASE_DIR.parent / "samples" / "reports").resolve()


def _envelope_path(report_id: str) -> Path:
    ENVELOPES_DIR.mkdir(parents=True, exist_ok=True)
    return ENVELOPES_DIR / f"{report_id}.json"


def _save_envelope(envelope: dict[str, Any]) -> None:
    _envelope_path(envelope["id"]).write_text(
        json.dumps(envelope, indent=2, default=str), encoding="utf-8"
    )


def _load_envelope(report_id: str) -> dict[str, Any] | None:
    """Resolve a report by id. Sample-* ids fall back to committed JSON in
    samples/reports/ so the demo cards work even after a fresh deploy
    (sample reports survive restarts; user uploads do not)."""
    path = _envelope_path(report_id)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    if report_id.startswith("sample-"):
        sample_path = SAMPLES_DIR / f"{report_id}.json"
        if sample_path.exists():
            return json.loads(sample_path.read_text(encoding="utf-8"))
    return None


def _sample_cards() -> list[dict[str, Any]]:
    """Metadata for the 3 pre-baked sample reports shown on the index page."""
    out: list[dict[str, Any]] = []
    if not SAMPLES_DIR.exists():
        return out
    for path in sorted(SAMPLES_DIR.glob("sample-*.json")):
        env = json.loads(path.read_text(encoding="utf-8"))
        out.append(
            {
                "id": env["id"],
                "filename": env.get("filename", "sample"),
                "tagline": env.get("tagline", ""),
                "thumbnail_url": env.get("thumbnail_url"),
                "fixture_url": env.get("fixture_url"),
                "risk_score": env["risk_score"],
                "risk_level": env["risk_level"],
                "summary": env.get("summary", ""),
                "findings_count": len(env.get("findings", [])),
            }
        )
    return out


def _recent_runs(limit: int = 5) -> list[dict[str, Any]]:
    ENVELOPES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(ENVELOPES_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for p in files[:limit]:
        env = json.loads(p.read_text(encoding="utf-8"))
        out.append(
            {
                "id": env["id"],
                "filename": env.get("filename", "invoice.pdf"),
                "risk_score": env["risk_score"],
                "risk_level": env["risk_level"],
                "summary": env.get("summary", "")[:60],
            }
        )
    return out


def _summary(findings: list[dict[str, Any]], extracted: dict[str, Any]) -> str:
    crit = sum(1 for f in findings if f["severity"] == "critical")
    warn = sum(1 for f in findings if f["severity"] == "warning")
    info = sum(1 for f in findings if f["severity"] == "info")
    inv_no = extracted.get("invoice_number") or "unknown invoice"
    parts = []
    if crit:
        parts.append(f"{crit} critical issue{'s' if crit != 1 else ''}")
    if warn:
        parts.append(f"{warn} warning{'s' if warn != 1 else ''}")
    if info:
        parts.append(f"{info} informational note{'s' if info != 1 else ''}")
    if not parts:
        return f"{inv_no}: all e-invoicing field checks passed."
    return f"{inv_no}: {', '.join(parts)}."