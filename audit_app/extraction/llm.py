"""AI document understanding via Claude (reads the PDF natively, including scanned pages).
Used for scanned documents, low-confidence rule extraction, or when configured for every file.
The model is instructed never to infer values that are not printed on the document."""
from __future__ import annotations

import base64
from typing import Optional

from pydantic import BaseModel

from .. import config


class Evidence(BaseModel):
    value: Optional[str]
    page: Optional[int]
    source_text: Optional[str]
    confidence: float


class LineItem(BaseModel):
    description: Optional[str]
    quantity: Optional[str]
    unit_price: Optional[str]
    amount_excluding_vat: Optional[str]
    page: Optional[int]


class InvoiceExtraction(BaseModel):
    document_type: str
    language: str
    seller_name: Evidence
    seller_vat: Evidence
    buyer_name: Evidence
    buyer_vat: Evidence
    invoice_number: Evidence
    invoice_date: Evidence
    currency: Evidence
    description: Evidence
    quantity: Evidence
    unit_price: Evidence
    discount: Evidence
    taxable_amount: Evidence
    vat_rate: Evidence
    vat_amount: Evidence
    gross_amount: Evidence
    credit_note_ref: Evidence
    purchase_order: Evidence
    line_items: list[LineItem]
    notes: list[str]


SYSTEM = """You extract data from sales invoices for an external audit (sales vouching). Accuracy and \
traceability matter more than completeness.

Rules:
- Report only what is printed on the document. If a field is absent or illegible, return value null and \
confidence 0. Never infer, estimate or fill a value from general knowledge.
- For every field give the 1-based page number and source_text: the exact characters printed on the \
document from which you read the value (label and value), copied verbatim in their original language.
- taxable_amount is the total amount EXCLUDING VAT (net of any invoice discount). Identify it from its \
label (e.g. "Total excluding VAT", "Taxable amount", "الإجمالي غير شامل ضريبة القيمة المضافة", \
"المبلغ الخاضع للضريبة") and confirm it with the arithmetic taxable + VAT = total. Never take the first or \
last number on the page by position, and never report the VAT-inclusive total as taxable_amount.
- Amounts: plain numbers without thousands separators or currency symbols, using "." as the decimal \
separator (convert Arabic-Indic digits). vat_rate as a number of percent (e.g. "15").
- invoice_date in ISO format YYYY-MM-DD. If the date is Hijri, return the printed text as value and \
explain in notes; do not convert it. If day/month order is ambiguous, say so in notes.
- currency as an ISO 4217 code (ريال / ر.س / SR = SAR) only if the document states or clearly shows it.
- seller = the issuing company; buyer = the customer. VAT numbers are the 15-digit registration numbers.
- document_type: one of tax_invoice, simplified_tax_invoice, credit_note, debit_note, proforma, \
quotation, commercial_invoice, other. language: en, ar, bilingual.
- confidence between 0 and 1 reflecting legibility and label clarity. Put any inconsistency \
(arithmetic not reconciling, unreadable areas, multiple totals) in notes."""


class AIUnavailable(Exception):
    pass


def extract_with_ai(pdf_path: str) -> InvoiceExtraction:
    if not config.ai_available():
        raise AIUnavailable("AI extraction is not configured (set ANTHROPIC_API_KEY)")
    import anthropic

    client = anthropic.Anthropic()
    with open(pdf_path, "rb") as fh:
        data = base64.standard_b64encode(fh.read()).decode()
    try:
        resp = client.beta.messages.parse(
            model=config.AI_MODEL,
            max_tokens=16000,
            system=SYSTEM,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": config.AI_EFFORT},
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
                {"type": "text", "text": "Extract the invoice fields from this document."},
            ]}],
            output_format=InvoiceExtraction,
        )
    except anthropic.RateLimitError as e:
        raise AIUnavailable(f"AI rate limited: {e.message}") from e
    except anthropic.APIStatusError as e:
        raise AIUnavailable(f"AI service error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIUnavailable(f"AI service unreachable: {e}") from e
    if resp.stop_reason == "refusal":
        raise AIUnavailable("AI declined to process this document")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise AIUnavailable("AI response incomplete")
    return resp.parsed_output
