# UAE Invoice Compliance

Upload a UAE invoice PDF. Get back a one-page report listing the e-invoicing
and VAT field issues that need fixing before you file, with the rule citation
and a fix recommendation per finding.

This is decision-support, not tax or legal advice. Always verify with a
qualified UAE tax advisor before filing.

## How it works

1. Upload a PDF invoice
2. DeepSeek v4 (vision) extracts the structured fields — supplier/buyer TRN,
   invoice number/date, line items, VAT, currency, reverse-charge flag
3. A flat JSON rule set runs deterministic checks against those fields
   (`rules/einvoicing_uae.json`)
4. The report page shows findings grouped by severity, each with:
   - **Message** — what is wrong
   - **Fix** — what to do about it
   - **Citation** — which FTA document the rule is anchored to

## Run it locally

```bash
cp .env.example .env       # fill DEEPSEEK_API_KEY
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000> and upload a PDF invoice.

## Editing the rules

All e-invoicing / VAT field checks live in `rules/einvoicing_uae.json`. Each
rule has a stable `code`, a `field` it inspects on the extracted JSON, a
`severity` (critical / warning / info), a `message`, a `fix`, and a `citation`
pointing at an FTA publication. Add or adjust rules without touching code —
the rule engine reads this file at startup.

For rules whose condition can't be expressed by `applies_when` + `field`, add a
new branch to `app/rules_engine.py::_rule_fires`.

## Project layout

```
app/
  main.py            FastAPI app + routes + JSON-blob report persistence
  extractor.py       DeepSeek v4 vision call -> structured invoice JSON
  rules_engine.py    Deterministic rule evaluator (no LLM, no I/O)
  templates/         Jinja2 templates (index, report)
  static/            CSS
rules/
  einvoicing_uae.json   FTA e-invoicing + VAT field checks
samples/                (placeholder for anonymized test invoices)
```

## Rule sources

The rules anchor against publicly available UAE FTA publications:

- FTA VAT Guide — tax invoice requirements, reverse charge, input VAT recovery
- FTA E-Invoicing Pilot — mandatory fields schema
- UAE Federal Decree-Law No. 47/2022 — Corporate Tax deductible expenses

Links are listed at the top of `rules/einvoicing_uae.json` under
`source_documents`.

## Scope of compliance (v0.1)

This tool checks the **subset of FTA e-invoicing and VAT field-readiness
rules most UAE SMEs encounter day-to-day** — supplier/buyer TRN presence
and format, invoice number and date, line-item structure, VAT math,
reverse-charge flag, currency. It is **not** a full PINT-AE Schematron
validator and does not submit invoices to FTA, an ASP, or Peppol. For full
PINT-AE structural conformance, see `cmendezs/mcp-einvoicing-ae`.

The rules live in `rules/einvoicing_uae.json`. Add or adjust them without
touching code.

## Limitations (v0.1)

- Text-layer PDFs and digitally-generated invoices only. Scanned/image-only
  PDFs may need an OCR layer (planned).
- Single-invoice run, no batch upload yet.
- No authentication; assumes private/internal use.
- Findings are static — there is no AI commentary on the report.

## Why this exists

Audit, tax, and advisory firms handling UAE SME clients spend hours per
invoice manually checking FTA e-invoicing field readiness. A non-compliant
invoice carries a flat AED 2,500 penalty; e-invoicing non-compliance is
AED 5,000/month. This tool is the first-pass check before a human reviews.