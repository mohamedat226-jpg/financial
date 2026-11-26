"""Invoice extraction in the specification's JSON contract (Section 3.1), with provenance for every value.

Values the extraction did not find (or that the engine does not extract, e.g. CR number, buyer address,
supply date) are null - never inferred. `provenance` records, per field, the page, bounding box, raw source
text, method, confidence and whether an auditor confirmed or entered it."""
from __future__ import annotations

import uuid

from . import config, db, engine

_NS = uuid.UUID("6f1c2a52-5a1e-4c6b-9d1e-2f0b7c1d8e01")   # fixed namespace -> stable document_id per file hash

# contract path -> internal field name
FIELD_MAP = {
    "seller.name": "seller_name", "seller.vat_number": "seller_vat",
    "buyer.name": "buyer_name", "buyer.vat_number": "buyer_vat",
    "invoice_metadata.invoice_number": "invoice_number", "invoice_metadata.invoice_date": "invoice_date",
    "invoice_metadata.currency": "currency", "invoice_metadata.purchase_order": "purchase_order",
    "invoice_metadata.credit_note_ref": "credit_note_ref",
    "financials.line_items_summary": "description", "financials.total_quantity": "quantity",
    "financials.gross_discount": "discount", "financials.taxable_amount_excl_vat": "taxable_amount",
    "financials.vat_rate": "vat_rate", "financials.vat_amount": "vat_amount",
    "financials.gross_amount_incl_vat": "gross_amount",
}


def _arith(T, V, G):
    if T is None or V is None or G is None:
        return None          # cannot be tested - not "passed"
    return abs(float(T) + float(V) - float(G)) <= 0.05


def invoice_contract(inv: dict, row: dict | None, findings: list[dict]) -> dict:
    F = inv.get("fields") or {}
    v = lambda k: (F.get(k) or {}).get("value")
    rate = v("vat_rate")
    T, V, G = v("taxable_amount"), v("vat_amount"), v("gross_amount")
    q = inv.get("zatca_qr")
    qd = (q or {}).get("decoded") if q and q.get("payload") else None
    flags = sorted({f["code"] for f in findings})
    low = sorted(k for k, f in F.items() if f and f.get("value") is not None and not f.get("confirmed")
                 and f.get("method") != "auditor" and (f.get("confidence") or 0) < config.LOW_CONFIDENCE)
    prov = {}
    for path, k in FIELD_MAP.items():
        f = F.get(k) or {}
        if f.get("value") is None:
            continue
        prov[path] = {"source_filename": inv["original_name"], "page": f.get("page"), "bbox": f.get("bbox"),
                      "raw_text": f.get("source_text"), "method": f.get("method"), "confidence": f.get("confidence"),
                      "auditor_confirmed": bool(f.get("confirmed") or f.get("method") == "auditor"),
                      "low_confidence": k in low, "note": f.get("note")}
    return {
        "document_id": str(uuid.uuid5(_NS, inv["sha256"])),
        "source_filename": inv["original_name"],
        "sha256": inv["sha256"],
        "page_count": inv.get("page_count"),
        "document_type": inv.get("doc_type"),
        "extraction_method": inv.get("extraction_method"),
        "extraction_confidence": inv.get("overall_confidence"),
        "seller": {"name": v("seller_name"), "vat_number": v("seller_vat"), "cr_number": None},
        "buyer": {"name": v("buyer_name"), "vat_number": v("buyer_vat"), "address": None},
        "invoice_metadata": {
            "invoice_number": v("invoice_number"), "invoice_date": v("invoice_date"), "supply_date": None,
            "currency": v("currency"), "exchange_rate": (row or {}).get("exchange_rate"),
            "purchase_order": v("purchase_order"), "credit_note_ref": v("credit_note_ref")},
        "financials": {
            "line_items_summary": v("description"), "total_quantity": v("quantity"), "gross_discount": v("discount"),
            "taxable_amount_excl_vat": T,
            "vat_rate": None if rate is None else round(float(rate) / 100, 4),   # contract expresses the rate as a fraction
            "vat_amount": V, "gross_amount_incl_vat": G, "arithmetic_check_passed": _arith(T, V, G)},
        "zatca_qr_data": None if not qd else {
            "seller_name": qd.get("seller_name"), "vat_registration": qd.get("vat_registration"),
            "timestamp": qd.get("timestamp"), "invoice_total": qd.get("invoice_total"), "vat_total": qd.get("vat_total"),
            "tlv_valid": qd.get("tlv_valid"), "phase": qd.get("phase"), "signature_verified": False,
            "page": q.get("page"), "bbox": q.get("bbox")},
        "zatca_qr_status": (row or {}).get("qr_status"),
        "reconciliation": None if not row else {
            "match_status": row.get("match_status"), "match_pass": row.get("match_pass"),
            "category": row.get("recon_category"), "ledger_row": row.get("ledger_row"),
            "ledger_amount": row.get("ledger_amount"), "amount_sar": row.get("amount_sar"), "difference": row.get("difference")},
        "audit_flags": sorted(set(flags) | ({"LOW_CONFIDENCE"} if low else set())),
        "low_confidence_fields": low,
        "review_status": inv.get("review_status"),
        "provenance": prov,
    }


def engagement_contract(eid: int) -> dict:
    eng = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    invs = [i for i in engine.load_invoices(eid) if i["status"] != "superseded"]
    rows = {r["invoice_id"]: r for r in engine.compute_rows(eid)}
    fnd: dict[int, list] = {}
    for f in db.q("SELECT * FROM findings WHERE engagement_id=? AND active=1", (eid,)):
        fnd.setdefault(f["invoice_id"], []).append(f)
    return {"schema": "sales-vouching-extraction/1.0", "client": eng["client_name"],
            "financial_year": eng["financial_year"], "engagement_status": eng["status"],
            "low_confidence_threshold": config.LOW_CONFIDENCE,
            "invoices": [invoice_contract(i, rows.get(i["id"]), fnd.get(i["id"], [])) for i in invs]}
