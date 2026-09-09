"""DeepSeek v4 vision extraction — invoice PDF -> structured JSON.

The model is asked to return a single JSON object matching InvoiceFields.
Layout variance is absorbed by the LLM; the rules engine stays deterministic.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# Fields we want — keep this small and stable. Adding a field here means
# downstream rules can rely on it.
EXTRACTION_SCHEMA_HINT = """{
  "supplier_trn": "<15-digit UAE TRN (format: starts with 1, ends with 03), or null>",
  "supplier_name": "<string, or null>",
  "buyer_trn": "<15-digit UAE TRN, or null>",
  "tin": "<10-digit Peppol participant ID (first 10 digits of TRN), or null>",
  "buyer_name": "<string, or null>",
  "invoice_number": "<string, or null>",
  "invoice_date": "<ISO-8601 date YYYY-MM-DD, or null>",
  "currency": "<3-letter ISO 4217, or null>",
  "subtotal": <number, or null>,
  "vat_rate": <number 0-100, or null>,
  "vat_amount": <number, or null>",
  "total": <number, or null>,
  "line_items": [
    {"description": "<str>", "quantity": <num>, "unit_price": <num>, "vat_rate": <num>}
  ],
  "invoice_type": "B2B | B2C | intra_gcc | cross_border_services | unknown",
  "reverse_charge": <true | false>,
  "notes": "<any free-text caveats, or empty string>"
}"""

SYSTEM_PROMPT = (
    "You are a precise invoice-field extractor for UAE tax compliance. "
    "Read the PDF carefully, including Arabic fields and footer/header TRNs. "
    "Return ONLY a single JSON object with the schema described. "
    "Use null for missing fields — do not invent values. "
    "If a TRN is unreadable, set it to null and note it in 'notes'. "
    "Do not include prose outside the JSON."
)


def _pdf_to_data_url(pdf_path: Path) -> str:
    raw = pdf_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:application/pdf;base64,{b64}"


def extract_invoice_fields(pdf_path: Path) -> dict[str, Any]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Copy .env.example to .env and fill it."
        )

    data_url = _pdf_to_data_url(pdf_path)

    user_prompt = (
        "Extract the invoice fields from this PDF.\n"
        f"Return this JSON shape:\n{EXTRACTION_SCHEMA_HINT}"
    )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers=headers,
            json=payload,
        )
        resp.raise_for_status()
        body = resp.json()

    try:
        raw_text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise RuntimeError(f"Unexpected DeepSeek response: {body}") from exc

    try:
        extracted = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.error("DeepSeek returned non-JSON: %r", raw_text[:500])
        raise RuntimeError(f"DeepSeek returned non-JSON output: {exc}") from exc

    return extracted