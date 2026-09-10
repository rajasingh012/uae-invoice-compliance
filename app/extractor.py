"""DeepSeek v4 vision extraction — invoice PDF -> structured JSON.

The model is asked to return a single JSON object matching InvoiceFields.
Layout variance is absorbed by the LLM; the rules engine stays deterministic.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
import fitz  # pymupdf — used to render PDF first page to PNG for vision input

logger = logging.getLogger(__name__)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash-vision-exp")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# DeepSeek's vision models accept JPEG/PNG/GIF/WebP. PDF is not supported
# as direct input, so we render the first page via pymupdf.
_VISION_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VISION_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

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


def _load_image_data_url(path: Path) -> tuple[str, str]:
    """Return (data_url, kind) for either a PDF or a vision-supported image.

    PDFs: render first page to PNG via pymupdf, return as image/png data URL.
    Images (png/jpg/jpeg/gif/webp): base64-encode directly.
    Other extensions raise ValueError so the caller surfaces a clear error.

    Returns a tuple so the caller can log what happened ("rendered from PDF"
    vs "passed through PNG") — useful for debugging.
    """
    ext = path.suffix.lower()

    if ext in _VISION_IMAGE_EXTS:
        mime = _VISION_MIME[ext]
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}", "image"

    if ext == ".pdf":
        with fitz.open(path) as doc:
            if not doc.page_count:
                raise RuntimeError(f"PDF has no pages: {path}")
            page = doc.load_page(0)
            pix = page.get_pixmap(dpi=200)  # 200 dpi is plenty for invoice OCR
            png_bytes = pix.tobytes("png")
        b64 = base64.b64encode(png_bytes).decode("ascii")
        return f"data:image/png;base64,{b64}", "pdf→png"

    raise ValueError(
        f"Unsupported input format {ext!r}. Accepted: PDF, PNG, JPEG, GIF, WebP."
    )


def extract_invoice_fields(pdf_path: Path) -> dict[str, Any]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is not set. Copy .env.example to .env and fill it."
        )

    data_url, kind = _load_image_data_url(pdf_path)
    logger.info("Extracting from %s (kind=%s)", pdf_path.name, kind)

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