"""Pure-Python rule evaluator — no LLM, no I/O.

`evaluate(extracted, rules) -> list[Finding]` is deterministic and trivially
unit-testable. Each rule in rules/einvoicing_uae.json declares:
  - code: stable string ID
  - field: which extracted key to inspect
  - severity: critical | warning | info
  - applies_when: simple Python expression evaluated against extracted (use with care)
  - message / fix / citation: shown verbatim on the report

The `applies_when` field is intentionally a tiny expression language. If a rule
grows complex, promote it to a dedicated function in this file.
"""
from __future__ import annotations

import re
from typing import Any


# PINT-AE TRN format: 15 digits, starts with '1', ends with '03'.
# Layout: '1' + 12 middle digits + '03' = 15 characters total.
# Example valid TRN: 100000000000003 (1 + 12 zeros + 03)
# See https://docs.peppol.eu/poac/pint-ae/ and FTA TRN issuance rules.
_TRN_RE = re.compile(r"^1\d{12}03$")

# TIN (Peppol participant ID) is the first 10 digits of the TRN.
_TIN_RE = re.compile(r"^1\d{9}$")


def _is_valid_trn(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return bool(_TRN_RE.match(value.strip()))


def _line_vat_total(line_items: list[dict[str, Any]]) -> float | None:
    total = 0.0
    for li in line_items or []:
        qty = li.get("quantity") or 0
        price = li.get("unit_price") or 0
        rate = li.get("vat_rate")
        if rate is None:
            return None
        total += qty * price * (rate / 100.0)
    return round(total, 2)


def _line_subtotal_sum(line_items: list[dict[str, Any]]) -> float | None:
    """Sum of (qty * unit_price) across all line items. None if any line lacks qty/price."""
    total = 0.0
    for li in line_items or []:
        qty = li.get("quantity")
        price = li.get("unit_price")
        if qty is None or price is None:
            return None
        total += float(qty) * float(price)
    return round(total, 2)


def _line_vat_individually_consistent(
    line_items: list[dict[str, Any]],
    vat_amount: Any,
) -> bool:
    """True iff every line's stated VAT matches (qty * price * rate / 100).

    Used to catch the hallucination pattern where the invoice total is right
    but individual line VATs are wrong.
    """
    if vat_amount is None or not line_items:
        return True
    stated_total = float(vat_amount)
    computed_total = 0.0
    for li in line_items:
        qty = li.get("quantity") or 0
        price = li.get("unit_price") or 0
        rate = li.get("vat_rate") or 0
        computed_total += qty * price * (rate / 100.0)
    # 1% tolerance on the rolled-up total — accommodates single-line rounding
    # while still flagging gross errors like "1 AED instead of 50 AED".
    if stated_total == 0:
        return computed_total < 0.01
    return abs(stated_total - computed_total) / abs(stated_total) <= 0.01


def evaluate(
    extracted: dict[str, Any], rules_doc: dict[str, Any]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for rule in rules_doc.get("rules", []):
        if _rule_fires(rule, extracted):
            findings.append(
                {
                    "code": rule["code"],
                    "severity": rule["severity"],
                    "message": rule["message"],
                    "fix": rule["fix"],
                    "citation": rule["citation"],
                }
            )
    return findings


def _rule_fires(rule: dict[str, Any], extracted: dict[str, Any]) -> bool:
    field = rule.get("field")
    applies = rule.get("applies_when", "always")

    if applies != "always":
        try:
            result = eval(applies, {"extracted": extracted}, {})  # noqa: S307
            if not bool(result):
                return False
        except Exception:
            return False

    if field == "supplier_trn":
        return not _is_valid_trn(extracted.get("supplier_trn"))

    if field == "buyer_trn":
        buyer = extracted.get("buyer_trn")
        return buyer is None or not _is_valid_trn(buyer)

    if field == "invoice_date":
        date = extracted.get("invoice_date")
        if not date:
            return True
        # YYYY-MM-DD; future-dated
        try:
            from datetime import date as _date
            y, m, d = (int(x) for x in date.split("-"))
            return _date(y, m, d) > _date.today()
        except Exception:
            return False

    if field == "invoice_number":
        return not (extracted.get("invoice_number") or "").strip()

    if field == "line_items":
        return not (extracted.get("line_items") or [])

    if field == "vat_amount":
        stated = extracted.get("vat_amount")
        computed = _line_vat_total(extracted.get("line_items") or [])
        if stated is None or computed is None:
            return False
        return round(float(stated), 2) != computed

    if field == "line_subtotal_sum":
        # Sum of (qty * unit_price) per line must match the stated subtotal.
        # Catches: LLM reads line qty as "10" when it was "100", or hallucinated subtotal.
        stated = extracted.get("subtotal")
        computed = _line_subtotal_sum(extracted.get("line_items") or [])
        if stated is None or computed is None:
            return False
        # Tolerance: 1% of stated subtotal, min AED 0.50 (catches real errors,
        # ignores small rounding noise).
        tolerance = max(0.50, abs(float(stated)) * 0.01)
        return abs(float(stated) - computed) > tolerance

    if field == "line_vat_individual":
        # Stated vat_amount must equal sum of per-line VAT.
        # Sibling of vat_amount rule — catches hallucination where total VAT is right
        # but individual lines are wrong.
        return not _line_vat_individually_consistent(
            extracted.get("line_items") or [], extracted.get("vat_amount")
        )

    if field == "total_reconciliation":
        # Stated total must equal stated subtotal + stated vat_amount.
        # Catches: LLM misread the printed total, or one of subtotal/vat
        # was hallucinated while the other was correct.
        subtotal = extracted.get("subtotal")
        vat = extracted.get("vat_amount")
        total = extracted.get("total")
        if subtotal is None or vat is None or total is None:
            return False
        expected = float(subtotal) + float(vat)
        tolerance = max(0.50, abs(expected) * 0.01)
        return abs(float(total) - expected) > tolerance

    if field == "reverse_charge":
        inv_type = extracted.get("invoice_type")
        rc = extracted.get("reverse_charge")
        if inv_type in ("cross_border_services", "intra_gcc_import"):
            return not rc
        return False

    if field == "currency":
        cur = (extracted.get("currency") or "").upper()
        return cur and cur != "AED"

    if field == "corporate_tax_treatment":
        return False  # info-only, never fires as a finding

    return False