# Progress — Decision Log

> This file is the record of *why* we made each decision, not just *what* we
> shipped. Newest at the top. Each entry should answer three things:
> **what was decided, what we considered instead, and what triggered it.**
> If you find yourself wanting to reverse one of these, the entry should give
> you enough context to either (a) defend the original choice or (b) make a
> better-informed reversal.

---

## 2026-09-09 — Initial scope decisions for v0.1 demo

**Trigger:** Pragadeesh (Brand Marketing, Young Global — UAE audit/tax firm)
sent a WhatsApp message asking if we could "do anything" with their firm's
compliance work. He mentioned "Audit n Tax" as the domain. No problem
statement, no user count, no budget. Soft commitment: "i wil put our team on
the demo."

**Decisions made in this conversation:**

### D1 — Fork `imranbaloch/uae-compliance-copilot` vs build new

- **Considered:** Forking the existing repo on `~/uae-compliance-copilot`.
- **Decided:** Build new. Private repo at
  `github.com/rajasingh012/uae-invoice-compliance`.
- **Reason:** The existing repo's multi-agent supervisor, DAG engine, JSON
  input shape, and sanctions/PEP screening are dead weight for a PDF
  invoice check tool. Carrying them forward would mean ripping out 60% of
  the codebase to ship 40% of it. Reused patterns: severity-pill CSS,
  Jinja2 + FastAPI shape, JSON-blob envelope store. Reused *no* code.
- **Reversal cost:** Low. The patterns copied are < 100 lines.

### D2 — Repo name

- **Considered:** `young-global-invoice-check` (brand-named) vs
  `uae-invoice-compliance` (generic).
- **Decided:** `uae-invoice-compliance`.
- **Reason:** Brand-naming a tool we built solo, before the firm has
  endorsed it, creates an implicit commitment they may not want. Generic
  name reads as "industry tool, could be anyone's" — easier sell, no
  awkward conversation if the firm says no later.
- **Reversal cost:** Trivial. A repo can be renamed with `gh repo rename`.

### D3 — Visibility: private

- **Considered:** Public (easier for Pragadeesh to share the link).
- **Decided:** Private.
- **Reason:** No content worth showing publicly yet. Promote to public
  after the first real demo produces screenshots. Easier to flip from
  private → public than the reverse.

### D4 — FastAPI + Jinja2 HTML vs Streamlit

- **Considered:** Streamlit (faster to ship, but re-renders whole page,
  brand-feel is developer-tool).
- **Decided:** FastAPI + server-rendered Jinja templates.
- **Reason:** Audience is Brand Marketing + tax professionals, not
  engineers. Server-rendered HTML lets us control the brand surface and
  ship a landing page that looks like a product. No JS build step, no
  Node, no React.

### D5 — PDF-only input (not JSON, not XML)

- **Considered:** JSON (what the forked repo did), XML, CSV.
- **Decided:** PDF only.
- **Reason:** Non-technical user = the marketing person at Young Global
  who has to demo this to the tax team without hand-converting inputs.
  PDF is the artifact that actually exists in the firm's inbox.

### D6 — No "AI agent" language on the UI

- **Considered:** Leading with "AI agents," "autonomous," "supervisor,"
  "DAG" in marketing copy (Pragadeesh is Brand Marketing, would love it).
- **Decided:** Use "AI" only on the user-facing surface. All agent/DAG
  language stays in the codebase.
- **Reason:** The tax team that Pragadeesh forwards this to reads "agent"
  as "untested, autonomous, hard to audit." That audience is allergic to
  the word. Brand appeal to Pragadeesh = rejection by his team. We chose
  the audience that decides.

### D7 — DeepSeek v4 single model, no provider abstraction

- **Considered:** `LLMProvider` interface supporting OpenAI / Anthropic /
  Ollama / custom (what uae-compliance-copilot does).
- **Decided:** Direct `httpx` POST to DeepSeek v4. No abstraction.
- **Reason:** Demo runs against one provider. A 20-line swap is cheaper
  than the abstraction. Add the abstraction when a second provider is
  actually needed.

### D8 — Rules as data (JSON), not code

- **Considered:** Hard-coded rule list in `app/rules_engine.py`.
- **Decided:** External `rules/einvoicing_uae.json`. Rule engine reads
  it at request time.
- **Reason:** This is the single most important architectural choice.
  It lets a tax advisor *argue with the rules* without arguing with the
  code. When Pragadeesh's team says "why does it flag this?", the answer
  is "look at the JSON," not "read the Python."

### D9 — PINT-AE conformance: subset only, not full

- **Considered:** Adopting full PINT-AE Schematron (~40 fields, UBL 2.1
  structure, `mcp-einvoicing-ae` does this).
- **Decided:** Implement the SME-relevant subset only — TRN, buyer TRN,
  invoice number, line items, VAT math, reverse-charge flag, currency,
  future-dated check. 9 rules total.
- **Reason:** PINT-AE is the framework for ASPs that submit to FTA
  clearance. This tool is upstream of that — it catches field issues
  before filing, for SMEs who don't have ASPs. Wrong layer for full
  PINT-AE conformance. Demoting to a README disclaimer:
  *"Checks the subset of FTA e-invoicing fields most SMEs encounter — not
  full PINT-AE Schematron conformance."*
- **Reversal cost:** Medium. Swap flat rules for Schematron engine.

### D10 — TRN regex tightened to PINT-AE format

- **Considered:** `^\d{15}$` (matches any 15 digits).
- **Decided:** `^1\d{12}03$` (matches PINT-AE format: 15 digits starting
  with `1`, ending with `03`; example `100000000000003`).
- **Reason:** TRN format is documented in the PINT-AE spec. A senior
  auditor will spot a loose regex instantly. Tighten cost ~1 line.
- **Reversal cost:** Trivial.
- **Implementation note:** Off-by-one in quantifier. First attempt
  `^1\d{13}03$` = 16 chars; second attempt `^1\d{11}03$` = 14 chars.
  Correct: `^1\d{12}03$` = 15 chars (`1` + 12 digits + `03`). Caught
  by running test fixtures, not just reading the pattern. **Rule:** when
  writing a literal-plus-quantifier pattern, count the literal length
  explicitly before picking the quantifier.

### D11 — Add `tin` (10-digit) to extraction schema

- **Considered:** TRN-only schema.
- **Decided:** Add `tin` (10-digit Peppol participant ID) alongside `trn`.
- **Reason:** TRN (15 digits) is VAT registration. TIN (10 digits) is the
  Peppol participant ID, derived as first 10 digits of TRN. Tax teams
  will notice if only TRN is captured. Cross-border invoices use TIN.
- **Reversal cost:** Trivial.

### D12 — No auth, no rate limiting, no DB

- **Considered:** SQLite + auth + per-user quotas.
- **Decided:** None of these for v0.1.
- **Reason:** Private demo, ~50 invoices total expected. Adding
  infrastructure before the use case is proven is over-engineering.
  Documented in `architecture.md` as v0.2 blockers before any
  client-facing deployment.

---

## Open questions (not yet decided)

- **What does Pragadeesh's team actually run on?** Excel, FTA e-tax portal,
  Zoho Books, QuickBooks, an in-house tool? Determines whether we need to
  generate FTA Audit File (FAF) outputs alongside the report.
- **Do they need English + Arabic output?** Bilingual UAE firms often do.
  Adds a translation pass per finding.
- **Multi-client or single-firm view?** A firm with 200 SME clients may
  want to track compliance per-client over time. Adds DB requirement.

---

## Out of scope (explicitly rejected)

- ZATCA / Saudi e-invoicing support — different country, different rules
  (XML + QR + clearance API). Multi-jurisdiction is feature-creep.
- HS Code / SAC Code validation — that's an ERP-extensions concern, not a
  field-readiness tool concern.
- Production multi-tenant hosting — no use case proven yet.
- WCAG / accessibility audit — internal demo, not public.
- Streaming response (SSE/WebSocket for per-finding progress) — one
  request per upload, ~3-8 seconds total, no streaming value.