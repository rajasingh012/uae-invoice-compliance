# Architecture

> **Audience:** anyone extending or modifying this codebase. If you only want to
> run it, see `../README.md`. If you want to know *why* a decision was made —
> especially one you'd reverse — see `progress.md`.

## What this is, in one sentence

A single-purpose web tool that takes a UAE invoice PDF, asks a vision LLM to
extract the FTA-mandated fields, runs those fields through a flat JSON rule
set, and renders a one-page report with cited findings.

## What this is not

- Not an agent platform. No supervisor, no DAG, no multi-step planning.
- Not an ERP. No inventory, no payments, no customer database.
- Not a regulatory submission system. We do not talk to FTA, an ASP, or
  Peppol. We check fields. Filing is downstream.
- Not a full PINT-AE Schematron validator. We check the SME-relevant subset
  of FTA e-invoicing field readiness. See `progress.md` for the scoping
  decision and its rationale.

## System shape

```
            ┌────────────────────────────────────────────────────────────┐
            │                       Browser (user)                       │
            └─────────────────────┬──────────────────────────────────────┘
                                  │   HTTP
                                  ▼
            ┌────────────────────────────────────────────────────────────┐
            │  FastAPI  (app/main.py)                                    │
            │  • GET  /                upload form                       │
            │  • POST /reports         PDF upload → run pipeline → 303   │
            │  • GET  /reports/{id}    render report                     │
            │  • GET  /health          liveness                          │
            └─────┬───────────────────┬───────────────────┬──────────────┘
                  │                   │                   │
        rule set  │       LLM call    │    envelope store │
                  │                   │                   │
                  ▼                   ▼                   ▼
         ┌────────────────┐   ┌────────────────┐   ┌──────────────────────┐
         │ rules/         │   │ app/           │   │ runtime_data/        │
         │ einvoicing_    │   │ extractor.py   │   │ envelopes/           │
         │ uae.json       │   │ (DeepSeek v4   │   │ {report_id}.json     │
         │                │   │  vision)       │   │                      │
         │ 9 rules        │   │                │   │ gitignored           │
         │ no code, just  │   │ 1 HTTP POST    │   │                      │
         │ data           │   │ per upload     │   │ 1 file per run       │
         └────────┬───────┘   └────────┬───────┘   └──────────────────────┘
                  │                    │
                  ▼                    ▼
         ┌────────────────┐   ┌────────────────┐
         │ app/           │   │ DeepSeek v4    │
         │ rules_engine   │   │ /v1/chat/      │
         │ .py            │   │ completions    │
         │                │   │                │
         │ pure Python    │   │ returns JSON   │
         │ no LLM, no I/O │   │ matching the   │
         │ ~120 LOC       │   │ extraction     │
         └────────────────┘   │ schema         │
                              └────────────────┘
```

### Why this shape

- **One LLM call per upload.** All extraction is one POST to DeepSeek v4 with
  the PDF as a base64 data URL and `response_format: json_object`. No
  multi-turn, no agent loop, no streaming. The cost per run is one request,
  predictable, < AED 0.05 on the demo fixture.
- **Rules as data, not code.** Every FTA check lives in `rules/einvoicing_uae.json`
  with `code`, `field`, `severity`, `applies_when`, `message`, `fix`,
  `citation`. Editing rules does not require a redeploy of the rule engine.
  This is the single most important architectural choice — it lets a tax
  advisor *argue with the rules* without arguing with the code.
- **Rule engine is a single function.** `app/rules_engine.py::evaluate`
  takes the extracted dict + the rules doc, returns findings. Pure Python,
  no LLM, no I/O. Trivially unit-testable. The `applies_when` expressions
  are a tiny eval-but-not-arbitrary subset (no builtins available beyond
  `extracted`).
- **Persistence is JSON blobs.** One file per run in `runtime_data/envelopes/`.
  Inspectable with `cat`. Replaceable. SQLite is overkill until we need to
  query historical runs by date, firm, or finding type — not a v0.1 problem.
- **No async in the agents themselves.** `app/main.py::upload_and_run` runs
  the synchronous pipeline in a threadpool via
  `starlette.concurrency.run_in_threadpool`. Same pattern as
  `uae-compliance-copilot`'s API layer. Lets us add async later without
  rewriting the agent.

## Extraction contract

The LLM is asked to return a JSON object matching this shape:

```json
{
  "supplier_trn":      "string | null",
  "supplier_name":     "string | null",
  "buyer_trn":         "string | null",
  "buyer_name":        "string | null",
  "tin":               "string | null",
  "invoice_number":    "string | null",
  "invoice_date":      "YYYY-MM-DD | null",
  "currency":          "ISO 4217 | null",
  "subtotal":          "number | null",
  "vat_rate":          "number 0-100 | null",
  "vat_amount":        "number | null",
  "total":             "number | null",
  "line_items":        [{ "description", "quantity", "unit_price", "vat_rate" }],
  "invoice_type":      "B2B | B2C | intra_gcc | cross_border_services | unknown",
  "reverse_charge":    "boolean",
  "notes":             "string"
}
```

`null` for missing fields — the LLM is told never to invent. This is the
*only* contract the rule engine depends on. Adding a field to this shape is a
two-line change: update the extraction prompt, add a rule that reads it.

## Rule schema

```json
{
  "code":         "TRN-SUPPLIER-PRESENT",
  "field":        "supplier_trn",
  "severity":     "critical | warning | info",
  "applies_when": "Python expression evaluated against `extracted`, or 'always'",
  "message":      "Human-readable problem statement",
  "fix":          "Recommended action for the tax team",
  "citation":     "Which FTA document / clause triggered it"
}
```

The `applies_when` expression has access only to the `extracted` dict — no
imports, no builtins, no attribute access. If a rule grows beyond what an
expression can express, promote it to a dedicated branch in
`app/rules_engine.py::_rule_fires`.

## Threat model (v0.1)

This is a **private, internal demo**, not a public service. Threat model:

- **Authentication:** none. Anyone with the URL can upload a PDF and read
  results. Acceptable for a localhost demo and a private repo. **MUST be
  added before any non-demo deployment.**
- **Rate limiting:** none. LLM costs are bounded by total demo runs (~50
  invoices). Acceptable at demo scale. **MUST be added before public
  exposure.**
- **Data residency:** PDFs and envelope JSON live on the demo host's disk.
  No data is sent anywhere except DeepSeek v4 (one POST per upload). For
  production use, a UAE-hosted LLM endpoint should be evaluated.
- **Prompt injection:** the LLM receives raw PDF bytes. A malicious PDF
  could embed text that manipulates the extraction prompt. Mitigation: the
  extraction prompt requests a strict JSON schema with `null` for missing
  fields and `response_format: json_object` is set on the API call.
  Not bulletproof — explicit untrusted-content treatment is a v0.2 task.

## What we explicitly rejected, and why

| Option | Why we did not pick it |
|---|---|
| Fork `uae-compliance-copilot` | Brings multi-agent baggage (supervisor, DAG, sanctions screening) that is dead weight for a PDF check tool |
| Streamlit instead of FastAPI + Jinja | Streamlit re-runs the whole page on every interaction and the brand-feel for the audit/tax audience is worse |
| Multi-agent supervisor | One extraction call is sufficient; supervisor complexity adds failure modes without adding capability |
| Provider-agnostic LLM layer | Demo runs against DeepSeek v4 only. A 20-line swap to OpenAI/Anthropic is cheaper than a provider abstraction |
| SQLite + ORM | JSON blobs are inspectable, debuggable, and replaceable. Promote when we actually need date/firm queries |
| Full PINT-AE Schematron | ~40 fields, very precise UBL structure. Out of scope for v0.1 SME-targeted demo. See `progress.md` |
| ZATCA / multi-jurisdiction support | Different country, different rules (XML + QR + clearance). Adding it now is feature-creep, not scope |
| Authentication / rate limiting | Not needed for private demo; documented as a v0.2 blocker before public exposure |

## Future direction (when triggered, not speculatively)

- **PINT-AE conformance layer.** Swap the flat rules for Schematron rules
  once the team actually needs full structural conformance.
- **Scanned-PDF support.** Add `pymupdf` + OCR (system tesseract with Arabic
  pack, or DeepSeek vision on rendered pages).
- **Audit-trail log.** Per-run structured log of which rules fired, the
  extracted values they fired on, and the citation source. Tax-grade
  defensibility.
- **Multi-invoice batch upload.** Single PDF with 50 invoices → one report.
- **Auth + rate limiting + UAE-hosted LLM endpoint.** Required before any
  client-facing deployment.

None of these are pre-scheduled. Each is triggered by a real ask from a real
user, not a roadmap item.