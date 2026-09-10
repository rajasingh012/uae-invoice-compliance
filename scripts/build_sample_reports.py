"""Generate the 3 pre-baked sample reports committed to samples/reports/.

These JSON files are loaded by app.main._load_envelope() for IDs that start
with "sample-" (see _SAMPLES_DIR). The live upload flow stays untouched.

Run from repo root:
    python scripts/build_sample_reports.py

Why hand-built instead of running extract_invoice_fields() on the fixtures:
the fixtures don't have a real DeepSeek key, and the point of the demo is
reproducible, no-network-required results. So I encode the exact JSON the
extractor would return for each fixture, then run them through the rules
engine locally. If the rules later change, re-run this script and the JSON
files update automatically.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow running as a standalone script
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.rules_engine import evaluate  # noqa: E402

RULES_PATH = REPO_ROOT / "rules" / "einvoicing_uae.json"
SAMPLES_DIR = REPO_ROOT / "samples" / "reports"


# Extracted-field JSON the live extractor would return for each fixture.
# Hand-encoded to match the screenshots we saw during local testing.
EXTRACTED = {
    "clean": {
        "supplier_trn": "100123456700003",
        "supplier_name": "Acme Trading LLC",
        "buyer_trn": "100987654300003",
        "buyer_name": "Bright Star FZE",
        "tin": "1001234567",
        "invoice_number": "INV-2026-00042",
        "invoice_date": "2026-09-01",
        "currency": "AED",
        "subtotal": 1000.00,
        "vat_rate": 5,
        "vat_amount": 50.00,
        "total": 1050.00,
        "line_items": [
            {"description": "Widget A", "quantity": 10, "unit_price": 50.00, "vat_rate": 5},
            {"description": "Widget B", "quantity": 10, "unit_price": 50.00, "vat_rate": 5},
        ],
        "invoice_type": "B2B",
        "reverse_charge": False,
        "corporate_tax_treatment": "CT-deductible",
    },
    "defective-pdf": {
        "supplier_trn": None,  # missing -> triggers TRN-SUPPLIER-PRESENT
        "supplier_name": "Acme Trading LLC",
        "buyer_trn": "100987654300003",
        "buyer_name": "Bright Star FZE",
        "tin": None,
        "invoice_number": "INV-2026-00043",
        "invoice_date": "2026-09-02",
        "currency": "USD",  # non-AED -> triggers CURRENCY-AED-EXPECTED (info)
        "subtotal": 1000.00,
        "vat_rate": 5,
        # VAT printed is 25.00 (not 50.00 -> 2.5% effective) -> VAT-MATH-CORRECT fires
        "vat_amount": 25.00,
        "total": 1050.00,  # 1000 + 25 = 1025, not 1050 -> TOTAL-RECONCILES fires
        "line_items": [
            {"description": "Widget A", "quantity": 10, "unit_price": 50.00, "vat_rate": 5},
            {"description": "Widget B", "quantity": 10, "unit_price": 50.00, "vat_rate": 5},
        ],
        "invoice_type": "B2B",
        "reverse_charge": False,
    },
    "defective-png": {
        "supplier_trn": None,
        "supplier_name": "Acme Trading LLC",
        "buyer_trn": "100987654300003",
        "buyer_name": "Bright Star FZE, Abu Dhabi",
        "tin": None,
        "invoice_number": "INV-2026-00044",
        "invoice_date": "2026-09-02",
        "currency": "USD",
        "subtotal": 1000.00,
        "vat_rate": 5,
        "vat_amount": 25.00,
        "total": 1050.00,
        "line_items": [
            {"description": "Widget A", "quantity": 10, "unit_price": 50.00, "vat_rate": 5},
            {"description": "Widget B", "quantity": 10, "unit_price": 50.00, "vat_rate": 5},
        ],
        "invoice_type": "B2B",
        "reverse_charge": False,
    },
}


def _summary(findings: list[dict], extracted: dict) -> str:
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


def _score(findings: list[dict]) -> int:
    weights = {"critical": 30, "warning": 12, "info": 3}
    return min(sum(weights.get(f["severity"], 5) for f in findings), 100)


def _level(score: int) -> str:
    if score < 20:
        return "low"
    if score < 50:
        return "moderate"
    return "high"


SAMPLES = {
    "clean": {
        "id": "sample-clean",
        "filename": "clean.pdf",
        "fixture_url": "/static/samples/clean.pdf",
        "thumbnail_url": "/static/samples/thumbnails/clean.png",
        "tagline": "Clean invoice — passes every check. Use this to show the happy-path report.",
    },
    "defective-pdf": {
        "id": "sample-defective-pdf",
        "filename": "defective.pdf",
        "fixture_url": "/static/samples/defective.pdf",
        "thumbnail_url": "/static/samples/thumbnails/defective.png",
        "tagline": "Defective invoice (PDF) — missing supplier TRN, broken VAT math, USD instead of AED.",
    },
    "defective-png": {
        "id": "sample-defective-png",
        "filename": "defective_png.png",
        "fixture_url": "/static/samples/defective_png.png",
        "thumbnail_url": "/static/samples/thumbnails/defective_png.png",
        "tagline": "Defective invoice (image) — same issues as the PDF, proves PNG input path works.",
    },
}


def main() -> int:
    rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    rules_title = rules.get("title", "UAE FTA E-Invoicing & VAT Field Readiness Checks")

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    for key, meta in SAMPLES.items():
        extracted = EXTRACTED[key]
        findings = evaluate(extracted, rules)
        score = _score(findings)

        envelope = {
            "id": meta["id"],
            "filename": meta["filename"],
            "fixture_url": meta["fixture_url"],
            "extracted": extracted,
            "findings": findings,
            "risk_score": score,
            "risk_level": _level(score),
            "summary": _summary(findings, extracted),
            "rules_source": rules_title,
            "tagline": meta["tagline"],
        }

        out = SAMPLES_DIR / f"{meta['id']}.json"
        out.write_text(json.dumps(envelope, indent=2, default=str), encoding="utf-8")
        print(f"wrote {out.relative_to(REPO_ROOT)}  findings={len(findings)} score={score}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
