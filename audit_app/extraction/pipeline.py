"""Extraction orchestration: rule engine on the text layer, AI for scanned or low-confidence
documents, cross-validation where both ran. Produces fields with provenance for every value."""
from __future__ import annotations

import re

from .. import config
from . import llm, rules, zatca_qr
from .normalize import HIJRI_NOTE, norm_text, parse_amount, parse_date, to_date
from .pdf_text import DocText, read_pdf

FIELDS = ["seller_name", "seller_vat", "buyer_name", "buyer_vat", "invoice_number", "invoice_date", "currency",
          "description", "quantity", "unit_price", "discount", "taxable_amount", "vat_rate", "vat_amount",
          "gross_amount", "credit_note_ref", "purchase_order"]
NUMERIC = {"quantity", "unit_price", "discount", "taxable_amount", "vat_rate", "vat_amount", "gross_amount"}
KEY_FIELDS = {"invoice_number": 1.0, "invoice_date": 1.0, "buyer_name": 1.0, "taxable_amount": 1.5, "currency": 0.5}


def _empty(note=None):
    return {"value": None, "confidence": 0.0, "page": None, "bbox": None, "source_text": None, "method": None, "note": note}


def overall_confidence(fields: dict) -> float:
    tot = sum(KEY_FIELDS.values())
    return round(sum(fields.get(k, {}).get("confidence", 0) * w for k, w in KEY_FIELDS.items()) / tot, 3)


def _locate(doc: DocText | None, text: str | None, page: int | None):
    """Find bbox of AI-quoted source text in the text layer, for traceability."""
    if not doc or not text or not doc.has_text_layer:
        return None, page
    target = norm_text(text)
    best = None
    for ln in doc.lines:
        if page and ln.page != page:
            continue
        if target and (target in ln.norm or target in ln.norm_rev or ln.norm in target and len(ln.norm) > 6):
            best = ln
            break
    if best is None:
        toks = [t for t in re.split(r"\s+", target) if len(t) > 2]
        scored = [(sum(1 for t in toks if t in ln.norm or t in ln.norm_rev), ln) for ln in doc.lines
                  if not page or ln.page == page]
        scored = [s for s in scored if s[0] >= max(1, len(toks) // 2)]
        if scored:
            best = max(scored, key=lambda s: s[0])[1]
    return (best.bbox, best.page) if best else (None, page)


def _ai_to_fields(ai: llm.InvoiceExtraction, doc: DocText | None) -> dict:
    out = {}
    for k in FIELDS:
        ev: llm.Evidence = getattr(ai, k)
        val, dnote = ev.value, None
        if val is not None:
            if k in NUMERIC:
                val = parse_amount(val)
            elif k == "invoice_date":
                d, _, dnote = parse_date(str(val))
                if d is None:
                    d = to_date(val)
                val = d.isoformat() if d else None
            elif k == "currency":
                val = val.strip().upper()[:3]
        bbox, page = _locate(doc, ev.source_text, ev.page)
        out[k] = {"value": val, "confidence": round(float(ev.confidence) if val is not None else 0.0, 3), "page": page,
                  "bbox": bbox, "source_text": ev.source_text, "method": "ai", "note": None}
        if k == "invoice_date" and val is not None and dnote and dnote.startswith(HIJRI_NOTE):
            out[k].update(confidence=min(out[k]["confidence"], 0.6), note=dnote)
    return out


def _same(k, a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if k in NUMERIC:
        return abs(float(a) - float(b)) <= 0.011
    if k == "invoice_number":
        return re.sub(r"\W", "", str(a)).upper() == re.sub(r"\W", "", str(b)).upper()
    return norm_text(str(a)) == norm_text(str(b))


def run(pdf_path: str) -> dict:
    """Returns dict(fields, doc_type, language, line_items, notes, method, has_text_layer, page_count, status)."""
    notes: list[str] = []
    doc = read_pdf(pdf_path)
    rule_res = None
    if doc.lines:
        rule_res = rules.extract(doc)
        notes += rule_res["notes"]
    rule_fields = rule_res["fields"] if rule_res else {k: _empty() for k in FIELDS}
    rule_conf = overall_confidence(rule_fields)

    want_ai = (config.EXTRACTION_MODE == "ai_always" or not doc.has_text_layer
               or rule_conf < config.AI_ESCALATION_CONFIDENCE)
    ai_fields = None
    if want_ai and config.ai_available():
        try:
            ai = llm.extract_with_ai(pdf_path)
            ai_fields = _ai_to_fields(ai, doc)
            notes += [f"AI: {n}" for n in ai.notes]
            if rule_res is None or not rule_res.get("doc_type"):
                rule_res = rule_res or {"line_items": [], "language": ai.language}
                rule_res["doc_type"] = ai.document_type
            if not (rule_res.get("line_items")):
                rule_res["line_items"] = [{"description": li.description, "quantity": parse_amount(li.quantity),
                                           "unit_price": parse_amount(li.unit_price),
                                           "amount": parse_amount(li.amount_excluding_vat), "page": li.page,
                                           "bbox": None, "source_text": None} for li in ai.line_items]
        except llm.AIUnavailable as e:
            notes.append(f"AI extraction not performed: {e}")
    elif want_ai:
        if not doc.has_text_layer:
            notes.append("Scanned document without text layer and AI/OCR not configured - manual data entry required")
        else:
            notes.append("Low rule-engine confidence; AI cross-check not configured")

    # merge
    fields = {}
    method = "rules" if rule_res and doc.has_text_layer else "ocr" if doc.ocr_used else "none"
    if ai_fields:
        method = "ai" if not doc.has_text_layer or rule_conf == 0 else "rules+ai"
    for k in FIELDS:
        r = rule_fields.get(k) or _empty()
        a = (ai_fields or {}).get(k)
        if a is None:
            fields[k] = r
            continue
        if r.get("value") is None:
            fields[k] = a
        elif a.get("value") is None:
            fields[k] = r
        elif _same(k, r["value"], a["value"]):
            fields[k] = {**r, "confidence": round(min(0.99, max(r["confidence"], a["confidence"]) + 0.05), 3),
                         "method": "rules+ai", "note": r.get("note")}
        else:
            # Disagreement: keep the rule value (it carries exact coordinates) but flag for review
            pick = r if r["confidence"] >= a["confidence"] else a
            fields[k] = {**pick, "confidence": min(0.5, pick["confidence"]),
                         "note": f"Rule engine read '{r['value']}', AI read '{a['value']}' - auditor to resolve",
                         "alternatives": [{"method": "rule", "value": r["value"]}, {"method": "ai", "value": a["value"]}]}
    for k in FIELDS:
        fields.setdefault(k, _empty())

    qr = None
    try:
        qr = zatca_qr.find_in_pdf(pdf_path)
    except Exception as e:  # noqa: BLE001 - QR evidence is supplementary; never fail extraction on it
        notes.append(f"ZATCA QR detection failed: {type(e).__name__}: {e}")
    if qr is None:
        notes.append("ZATCA QR not checked (QR detector not installed)")
    elif qr.get("payload") is None:
        notes.append("No QR code detected on the invoice")
    else:
        d = qr["decoded"]
        notes.append(f"ZATCA QR found on page {qr['page']} (phase {d['phase']}, "
                     f"{'valid' if d['tlv_valid'] else 'INVALID'} TLV)")

    conf = overall_confidence(fields)
    has_core = any(fields[k]["value"] is not None for k in ("invoice_number", "taxable_amount", "gross_amount"))
    status = "extracted" if has_core else "needs_manual"
    return {"fields": fields, "doc_type": (rule_res or {}).get("doc_type"), "language": (rule_res or {}).get("language"),
            "line_items": (rule_res or {}).get("line_items", []), "notes": notes, "method": method,
            "has_text_layer": doc.has_text_layer, "page_count": doc.pages, "status": status, "confidence": conf,
            "zatca_qr": qr}
