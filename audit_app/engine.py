"""Audit engine: canonical working-paper rows, invoice-to-ledger matching and audit review checks.

`compute_rows` is the single source of truth used by the dashboard, the Excel working paper and
every report, so all outputs reconcile to the same underlying data."""
from __future__ import annotations

import re
import statistics
from datetime import date
from difflib import SequenceMatcher

from . import db
from . import config
from .findings_ar import DOC_TYPE_AR, FIELD_AR, PROC_AR, STATIC_AR, money
from .extraction import zatca_qr
from .extraction.normalize import (HIJRI_NOTE, business_days_between, clean_exact, has_arabic, levenshtein_ratio, norm_doc_no,
                                 norm_invoice_no, norm_party, to_date, valid_saudi_vat)

MATCH_STATUSES = ["Matched", "Not Found in Ledger", "Possible Match", "Duplicate Invoice", "Amount Mismatch",
                  "Customer Mismatch", "Ledger Not Uploaded"]
DIFF_STATUSES = ["No Difference", "Difference Identified", "Missing Ledger Amount", "Missing Invoice Data"]
MANUAL_COLS = {"L": "gdn_issued", "M": "gdn_date", "N": "price_list_matched", "O": "invoice_approved",
               "P": "explanation"}
REQUIRED = ["invoice_number", "invoice_date", "buyer_name", "taxable_amount", "currency"]


def fv(inv: dict, k: str):
    f = (inv.get("fields") or {}).get(k) or {}
    return f.get("value")


def load_invoices(eid: int) -> list[dict]:
    rows = db.q("SELECT i.*, f.original_name, f.sha256 FROM invoices i JOIN files f ON f.id=i.file_id "
                "WHERE i.engagement_id=? ORDER BY i.id", (eid,))
    for r in rows:
        r["zatca_qr"] = db.jload(r.get("zatca_qr"), None)
        for k in ("fields", "ai_fields", "manual", "match", "line_items", "extraction_notes"):
            r[k] = db.jload(r[k], {} if k not in ("line_items", "extraction_notes") else [])
    return rows


def load_ledger(eid: int) -> list[dict]:
    rows = db.q("SELECT * FROM ledger_entries WHERE engagement_id=? ORDER BY row_number", (eid,))
    for r in rows:
        r["raw"] = db.jload(r["raw"], {})
    return rows


def fx_rates(eid: int) -> dict:
    rates = {r["currency"]: r for r in db.q("SELECT * FROM fx_rates WHERE engagement_id=?", (eid,))}
    rates.setdefault("SAR", {"currency": "SAR", "rate": 1.0, "source": "Functional currency", "entered_by": "system"})
    return rates


def aliases(eid: int) -> set[tuple[str, str]]:
    return {(a["invoice_name"], a["ledger_name"]) for a in db.q("SELECT * FROM customer_aliases WHERE engagement_id=?", (eid,))}


def name_similarity(a: str | None, b: str | None, alias_set=frozenset()) -> float:
    na, nb = norm_party(a), norm_party(b)
    if not na or not nb:
        return 0.0
    if na == nb or (na, nb) in alias_set:
        return 1.0
    r = SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    j = len(ta & tb) / max(1, len(ta | tb))
    return round(max(r, j), 3)


# ------------------------------------------------------------------------------------------- matching
# 3-pass reconciliation:
#   Pass 1  exact reference: clean(invoice no.) == clean(ledger ref.) AND same customer
#   Pass 2  normalised reference: prefixes (INV-, TAX-, #), punctuation and leading zeros stripped
#   Pass 3  heuristic: customer Levenshtein similarity > 85 %, posting date within +/-5 business days and
#           amount within tolerance -> proposed only; the auditor must confirm (never linked automatically)
PASS3_NAME_SIM = 0.85
PASS3_BUSINESS_DAYS = 5
PASS_METHODS = {1: "pass1_exact", 2: "pass2_normalized", 3: "pass3_heuristic"}


def run_matching(eid: int, user: str) -> dict:
    eng = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    tol = float(eng["tolerance"])
    invs = [i for i in load_invoices(eid) if i["status"] != "superseded"]
    ledger = load_ledger(eid)
    rates = fx_rates(eid)
    al = aliases(eid)
    by_no: dict[str, list[dict]] = {}
    for le in ledger:
        k = norm_doc_no(le["invoice_number"])
        if k:
            by_no.setdefault(k, []).append(le)
    inv_no_count: dict[str, int] = {}
    for i in invs:
        k = norm_doc_no(fv(i, "invoice_number"))
        if k:
            inv_no_count[k] = inv_no_count.get(k, 0) + 1

    linked: set[int] = set()
    # auditor-confirmed links are preserved and reserved first
    for i in invs:
        m = i["match"] or {}
        if m.get("method") == "auditor_confirmed" and m.get("ledger_id"):
            linked.add(m["ledger_id"])

    results = {}
    for i in invs:
        prev = i["match"] or {}
        if prev.get("method") in ("auditor_confirmed", "auditor_rejected") and (
                prev.get("method") == "auditor_rejected" or any(le["id"] == prev.get("ledger_id") for le in ledger)):
            results[i["id"]] = _refresh_confirmed(i, prev, ledger, rates, tol, al)
            continue
        if not ledger:
            results[i["id"]] = {"status": "Ledger Not Uploaded", "ledger_id": None, "method": None, "evidence": [],
                                "candidates": [], "pass": None}
            continue
        no = fv(i, "invoice_number")
        k = norm_doc_no(no)
        amt_sar = _sar(i, rates)
        hits = by_no.get(k, []) if k else []
        if k and inv_no_count.get(k, 0) > 1:
            cands = [_cand(le, i, amt_sar, tol, al, "Same invoice number") for le in hits]
            results[i["id"]] = {"status": "Duplicate Invoice", "ledger_id": None, "method": None, "pass": None,
                                "evidence": [f"Invoice number {no} appears on {inv_no_count[k]} invoice documents"],
                                "candidates": cands}
            continue
        if len(hits) > 1:
            results[i["id"]] = {"status": "Duplicate Invoice", "ledger_id": None, "method": None, "pass": None,
                                "evidence": [f"Invoice number recorded {len(hits)} times in the ledger (rows "
                                             f"{', '.join(str(h['row_number']) for h in hits)})"],
                                "candidates": [_cand(le, i, amt_sar, tol, al, "Same invoice number") for le in hits]}
            continue
        if len(hits) == 1:
            le = hits[0]
            linked.add(le["id"])
            exact = clean_exact(no) == clean_exact(le["invoice_number"])
            same_cust = bool(le["customer"]) and (norm_party(fv(i, "buyer_name")) == norm_party(le["customer"])
                                                  or (norm_party(fv(i, "buyer_name")), norm_party(le["customer"])) in al)
            ps = 1 if exact and same_cust else 2
            res = _evaluate_link(i, le, amt_sar, tol, al, PASS_METHODS[ps])
            res["pass"] = ps
            res["evidence"].insert(0, "Pass 1 - exact invoice number and customer" if ps == 1 else
                                   f"Pass 2 - normalised invoice number ('{no}' / '{le['invoice_number']}' -> {k})"
                                   + ("" if same_cust else "; customer name differs"))
            results[i["id"]] = res
            continue
        results[i["id"]] = None  # Pass 3 after all number matches have reserved their ledger rows

    for i in invs:
        if results.get(i["id"]) is not None:
            continue
        amt_sar = _sar(i, rates)
        cands = []
        for le in ledger:
            if le["id"] in linked:
                continue
            c = _cand(le, i, amt_sar, tol, al, None)
            if c["score"] >= 0.6:
                cands.append(c)
        cands.sort(key=lambda c: (not c["pass3_criteria_met"], -c["score"]))
        if cands:
            strong = sum(1 for c in cands if c["pass3_criteria_met"])
            results[i["id"]] = {"status": "Possible Match", "ledger_id": None, "method": PASS_METHODS[3], "pass": 3,
                                "evidence": ["Pass 3 - no ledger entry with the same invoice number; candidates proposed on "
                                             f"customer (Levenshtein > {PASS3_NAME_SIM:.0%}), date (±{PASS3_BUSINESS_DAYS} "
                                             "business days) and amount (within tolerance) - auditor sign-off required",
                                             f"{strong} candidate(s) meet all Pass 3 criteria; {len(cands) - strong} partial"],
                                "candidates": cands[:5]}
        else:
            results[i["id"]] = {"status": "Not Found in Ledger", "ledger_id": None, "method": None, "pass": None,
                                "evidence": ["No ledger entry with this invoice number (Pass 1/2), and no entry matching "
                                             "customer, date and amount (Pass 3)"], "candidates": []}

    with db.tx() as c:
        for iid, m in results.items():
            old = next(x["match"] for x in invs if x["id"] == iid)
            if (old or {}).get("status") != m["status"] or (old or {}).get("ledger_id") != m.get("ledger_id"):
                db.log(user, eid, "invoice", iid, "match", "match_status", (old or {}).get("status"), m["status"], c=c)
            m["ledger_amount"] = next((le["amount"] for le in ledger if le["id"] == m.get("ledger_id")), None)
            c.execute("UPDATE invoices SET match=?, updated_at=? WHERE id=?", (db.jdump(m), db.now(), iid))
    return results


def _sar(inv, rates):
    t = fv(inv, "taxable_amount")
    r = _rate_for(inv, rates)
    if t is None or r is None:
        return None
    sign = -1 if inv.get("doc_type") == "credit_note" and float(t) > 0 else 1
    return round(sign * float(t) * r, 2)


def _rate_for(inv, rates):
    if inv.get("fx_override"):
        return float(inv["fx_override"])
    cur = fv(inv, "currency")
    if cur and cur in rates:
        return float(rates[cur]["rate"])
    return None


def _cand(le, inv, amt_sar, tol, al, reason):
    reasons, score = [], 0.0
    if reason:
        reasons.append(reason)
    amt_ok = amt_sar is not None and le["amount"] is not None and abs(le["amount"] - amt_sar) <= tol
    if amt_ok:
        score += 0.5; reasons.append(f"Amount agrees within tolerance ({le['amount']:,.2f})")
    lev = levenshtein_ratio(fv(inv, "buyer_name"), le["customer"])
    alias = (norm_party(fv(inv, "buyer_name")), norm_party(le["customer"])) in al
    name_ok = alias or lev > PASS3_NAME_SIM
    if name_ok:
        score += 0.3; reasons.append("Customer is a confirmed alias" if alias and lev <= PASS3_NAME_SIM
                                     else f"Customer similar (Levenshtein {lev:.0%})")
    elif name_similarity(fv(inv, "buyer_name"), le["customer"], al) >= 0.8:
        score += 0.2; reasons.append(f"Customer partly similar (Levenshtein {lev:.0%})")
    d1, d2 = to_date(fv(inv, "invoice_date")), to_date(le["date"])
    bd = business_days_between(d1, d2) if d1 and d2 else None
    if bd is not None:
        if bd <= PASS3_BUSINESS_DAYS:
            score += 0.2; reasons.append(f"Posting date within {bd} business day(s)")
        elif bd <= 2 * PASS3_BUSINESS_DAYS:
            score += 0.1; reasons.append(f"Posting date {bd} business days apart")
    return {"ledger_id": le["id"], "row_number": le["row_number"], "invoice_number": le["invoice_number"],
            "customer": le["customer"], "date": le["date"], "amount": le["amount"], "score": round(score, 2),
            "reasons": reasons, "levenshtein": lev, "business_days": bd,
            "pass3_criteria_met": bool(amt_ok and name_ok and bd is not None and bd <= PASS3_BUSINESS_DAYS)}


def _evaluate_link(inv, le, amt_sar, tol, al, method):
    ev = [f"Ledger row {le['row_number']}: invoice number {le['invoice_number']} agrees to invoice"]
    status = "Matched"
    sim = name_similarity(fv(inv, "buyer_name"), le["customer"], al)
    if le["customer"] and fv(inv, "buyer_name") and sim < 0.6:
        status = "Customer Mismatch"
        script = has_arabic(fv(inv, "buyer_name")) != has_arabic(le["customer"])
        ev.append(f"Customer per invoice '{fv(inv, 'buyer_name')}' vs ledger '{le['customer']}' (similarity {sim:.0%})"
                  + (" - names are in different scripts; confirm alias" if script else ""))
    if amt_sar is not None and le["amount"] is not None and abs(le["amount"] - amt_sar) > tol:
        status = "Amount Mismatch" if status == "Matched" else status
        ev.append(f"Ledger amount {le['amount']:,.2f} vs invoice SAR amount {amt_sar:,.2f}")
    elif amt_sar is None:
        ev.append("Amount not compared - invoice taxable amount or exchange rate missing")
    else:
        ev.append(f"Amount agrees within tolerance ({le['amount']:,.2f})")
    return {"status": status, "ledger_id": le["id"], "method": method, "evidence": ev, "candidates": [],
            "customer_similarity": sim}


def _refresh_confirmed(inv, prev, ledger, rates, tol, al):
    if prev.get("method") == "auditor_rejected":
        return {**prev, "status": "Not Found in Ledger"}
    le = next(x for x in ledger if x["id"] == prev["ledger_id"])
    res = _evaluate_link(inv, le, _sar(inv, rates), tol, al, "auditor_confirmed")
    res.update(confirmed_by=prev.get("confirmed_by"), confirmed_at=prev.get("confirmed_at"), **{"pass": prev.get("pass")})
    res["evidence"].insert(0, f"Link confirmed by {prev.get('confirmed_by')} on {prev.get('confirmed_at')}")
    return res


# ---------------------------------------------------------------------------------- canonical rows
def compute_rows(eid: int) -> list[dict]:
    eng = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    tol = float(eng["tolerance"])
    rates = fx_rates(eid)
    ledger = {le["id"]: le for le in load_ledger(eid)}
    rows = []
    for n, inv in enumerate([i for i in load_invoices(eid) if i["status"] != "superseded"], start=1):
        m = inv["match"] or {}
        le = ledger.get(m.get("ledger_id")) if m.get("ledger_id") else None
        E = fv(inv, "taxable_amount")
        # Credit notes reduce revenue: shown as negative so they reconcile to the (negative) ledger posting
        if E is not None and inv["doc_type"] == "credit_note" and float(E) > 0:
            E = -float(E)
        I = _rate_for(inv, rates)
        J = round(float(E) * I, 2) if E is not None and I is not None else None
        D = le["amount"] if le else None
        K = round(D - J, 2) if D is not None and J is not None else None
        missing = [k for k in REQUIRED if fv(inv, k) in (None, "")]
        if I is None and fv(inv, "currency"):
            missing.append("exchange_rate")
        if missing:
            dstat = "Missing Invoice Data"
        elif D is None:
            dstat = "Missing Ledger Amount"
        elif abs(K) <= tol:
            dstat = "No Difference"
        else:
            dstat = "Difference Identified"
        manual = inv["manual"] or {}
        d = to_date(fv(inv, "invoice_date"))
        recon = recon_category(m.get("status"), D, J, K, tol)
        rows.append({
            "invoice_id": inv["id"], "sno": n, "file": inv["original_name"], "status": inv["status"],
            "customer": fv(inv, "buyer_name"), "product_type": fv(inv, "description"), "ledger_amount": D,
            "invoice_amount": E, "currency": fv(inv, "currency"), "invoice_date": d.isoformat() if d else None,
            "invoice_number": fv(inv, "invoice_number"), "exchange_rate": I, "amount_sar": J, "difference": K,
            "vat_amount": fv(inv, "vat_amount"), "gross_amount": fv(inv, "gross_amount"),
            "vat_rate": fv(inv, "vat_rate"), "doc_type": inv["doc_type"],
            "manual": manual, "match_status": m.get("status") or "Ledger Not Uploaded", "recon_category": recon,
            "match_pass": m.get("pass"), "zatca_qr": _qr_summary(inv), "qr_status": _qr_status(inv),
            "match_method": m.get("method"), "diff_status": dstat, "missing": missing,
            "review_status": inv["review_status"], "reviewer": inv["reviewer"],
            "confidence": inv["overall_confidence"], "method": inv["extraction_method"],
            "month": d.strftime("%Y-%m") if d else None, "ledger_row": le["row_number"] if le else None,
            "ledger_customer": le["customer"] if le else None,
            "auditor_confirmed": all(((inv["fields"] or {}).get(k) or {}).get("confirmed") for k in REQUIRED),
            "suggestion": suggest_explanation(inv, D, J, K, tol),
            "suggestion_ar": suggest_explanation(inv, D, J, K, tol, "ar"),
        })
    return rows


RECON_CATEGORIES = ["MATCHED_CLEAN", "WITHIN_TOLERANCE", "MATERIAL_DIFFERENCE", "NOT_IN_LEDGER", "POSSIBLE_MATCH",
                    "DUPLICATE_INVOICE", "INCOMPLETE_DATA", "LEDGER_NOT_UPLOADED", "UNMATCHED_LEDGER_ROW"]


def recon_category(match_status, D, J, K, tol) -> str:
    """Discrepancy category (Section 4 of the specification). UNMATCHED_LEDGER_ROW applies to ledger rows only."""
    if match_status == "Duplicate Invoice":
        return "DUPLICATE_INVOICE"
    if match_status == "Not Found in Ledger":
        return "NOT_IN_LEDGER"
    if match_status == "Possible Match":
        return "POSSIBLE_MATCH"
    if match_status in (None, "Ledger Not Uploaded"):
        return "LEDGER_NOT_UPLOADED"
    if D is None or J is None or K is None:
        return "INCOMPLETE_DATA"
    if K == 0:
        return "MATCHED_CLEAN"
    return "WITHIN_TOLERANCE" if abs(K) <= tol else "MATERIAL_DIFFERENCE"


def _qr_summary(inv) -> dict | None:
    q = inv.get("zatca_qr")
    if q is None:
        return None
    if not q.get("payload"):
        return {"found": False}
    d = q.get("decoded") or {}
    return {"found": True, "page": q.get("page"), "bbox": q.get("bbox"), "tlv_valid": d.get("tlv_valid"),
            "phase": d.get("phase"), "seller_name": d.get("seller_name"), "vat_registration": d.get("vat_registration"),
            "timestamp": d.get("timestamp"), "invoice_total": d.get("invoice_total"), "vat_total": d.get("vat_total"),
            "signature_verified": False, "errors": d.get("errors", []), "warnings": d.get("warnings", [])}


def _qr_status(inv) -> str:
    q = inv.get("zatca_qr")
    if q is None:
        return "not_checked"
    if not q.get("payload"):
        return "not_found"
    d = q.get("decoded") or {}
    return f"valid_phase{d.get('phase')}" if d.get("tlv_valid") else "invalid"


RISK_ORDER = {"High": 0, "Medium": 1, "Low": 2}


def attach_risk(eid: int, rows: list[dict]) -> list[dict]:
    """Adds `risk` (highest risk among the invoice's open findings, or None) and `open_findings` to each row,
    so the dashboard can filter and chart by risk from the same rows that feed the KPIs."""
    by_inv: dict[int, list[str]] = {}
    for f in db.q("SELECT invoice_id, risk FROM findings WHERE engagement_id=? AND active=1 AND review_status != 'cleared' "
                  "AND invoice_id IS NOT NULL", (eid,)):
        by_inv.setdefault(f["invoice_id"], []).append(f["risk"])
    for r in rows:
        risks = by_inv.get(r["invoice_id"], [])
        r["open_findings"] = len(risks)
        r["risk"] = min(risks, key=lambda x: RISK_ORDER.get(x, 9)) if risks else None
    return rows


def unmatched_ledger_rows(eid: int, invs: list[dict] | None = None, ledger: list[dict] | None = None) -> list[dict]:
    """Ledger entries not linked to, nor proposed for, any invoice (fictitious revenue / occurrence risk)."""
    invs = invs if invs is not None else [i for i in load_invoices(eid) if i["status"] != "superseded"]
    ledger = ledger if ledger is not None else load_ledger(eid)
    linked = {(i["match"] or {}).get("ledger_id") for i in invs}
    cand = {c["ledger_id"] for i in invs for c in (i["match"] or {}).get("candidates", [])}
    return [le for le in ledger if le["id"] not in linked and le["id"] not in cand]


def suggest_explanation(inv, D, J, K, tol, lang: str = "en") -> str | None:
    """AI-assisted (rule-based) hypotheses for a difference, in English or Arabic. Requires auditor confirmation
    before it is written to the working paper (column P)."""
    if K is None or abs(K) <= tol:
        return None
    hints: list[tuple[str, str]] = []
    vat, gross, E, disc = (fv(inv, k) for k in ("vat_amount", "gross_amount", "taxable_amount", "discount"))
    rate = None
    if J and E:
        rate = J / float(E)
    if vat is not None and rate and abs(K - float(vat) * rate) <= tol:
        hints.append(("Ledger amount appears to include VAT (difference equals the VAT amount); sales should be recorded net of VAT",
                      "يبدو أن مبلغ الدفتر يشمل ضريبة القيمة المضافة (الفرق يساوي مبلغ الضريبة)؛ والمبيعات تُسجَّل صافية من الضريبة"))
    if gross is not None and rate and abs(D - float(gross) * rate) <= tol and not hints:
        hints.append(("Ledger amount equals the VAT-inclusive invoice total", "مبلغ الدفتر يساوي إجمالي الفاتورة شاملًا الضريبة"))
    if disc and rate and abs(abs(K) - float(disc) * rate) <= tol:
        hints.append(("Difference equals the invoice discount - discount may be recorded separately or not recorded",
                      "الفرق يساوي خصم الفاتورة - قد يكون الخصم مسجلًا بشكل منفصل أو غير مسجل"))
    if E is not None and D is not None and abs(D - float(E)) <= tol and rate and rate != 1:
        hints.append(("Ledger appears to record the foreign-currency amount without translation to SAR",
                      "يبدو أن الدفتر سجّل المبلغ بالعملة الأجنبية دون تحويله إلى الريال"))
    if rate and rate != 1 and not hints:
        if E:
            hints.append(("Possible exchange-rate difference (ledger may use the transaction-date rate); "
                          f"implied ledger rate {D / float(E):.4f}",
                          f"فرق محتمل في سعر الصرف (قد يستخدم الدفتر سعر تاريخ العملية)؛ السعر الضمني في الدفتر {D / float(E):.4f}"))
        else:
            hints.append(("Possible exchange-rate difference", "فرق محتمل في سعر الصرف"))
    if not hints:
        hints.append((f"Unexplained difference of SAR {K:,.2f} - obtain explanation and supporting adjustment from client",
                      f"فرق غير مفسَّر قدره {K:,.2f} ريال - احصل من العميل على التفسير والتسوية المؤيدة"))
    return ("؛ " if lang == "ar" else "; ").join(h[1] if lang == "ar" else h[0] for h in hints)


# ------------------------------------------------------------------------------------- audit checks
PROC = {
    "INV_NO_MISSING": "Inspect the original invoice; obtain the invoice number from the client's billing system and agree to the sequence register.",
    "CUST_MISSING": "Agree customer to the sales order / contract and customer master file.",
    "DATE_MISSING": "Determine the invoice date from the original document or the e-invoicing (FATOORA) record.",
    "TAXABLE_MISSING": "Recompute the taxable amount from line items; obtain a legible copy of the invoice.",
    "CURRENCY_MISSING": "Confirm the invoice currency with the client and the sales contract.",
    "FX_MISSING": "Enter the authorised exchange rate for the currency (source: SAMA / approved policy rate) in engagement settings.",
    "VAT_RECALC": "Recalculate VAT at the applicable rate; investigate rounding or rate error; consider VAT return impact.",
    "VAT_ARITHMETIC_ERROR": "Re-perform invoice arithmetic (taxable + VAT = total, tolerance SAR 0.05) and discuss errors with the client.",
    "HIJRI_DATE": "Agree the Hijri date to the original invoice and confirm the Gregorian equivalent (Umm al-Qura) before using it for period / cut-off testing.",
    "ZATCA_QR_MISSING": "Confirm whether the invoice should carry a ZATCA QR code under the e-invoicing phase applicable to the seller and invoice type; obtain the FATOORA-generated invoice.",
    "ZATCA_QR_INVALID": "Obtain the original e-invoice (XML / FATOORA copy); a malformed QR may indicate a non-compliant or altered document.",
    "ZATCA_QR_MISMATCH": "Compare the printed invoice with the QR data and the original e-invoice XML; a difference may indicate an altered PDF - escalate under ISA 240.",
    "LINE_SUM": "Re-add the invoice line items and agree to the invoice net total.",
    "VAT_RATE": "Confirm the VAT treatment (standard 15%, zero-rated or exempt) and supporting conditions.",
    "SELLER_VAT": "Verify the seller VAT registration number against the entity's ZATCA registration certificate.",
    "BUYER_VAT": "Verify the buyer VAT number format; for B2B standard tax invoices confirm the buyer's VAT registration.",
    "DOC_TYPE": "Obtain the valid tax invoice; a proforma / quotation is not evidence of a sale.",
    "CREDIT_NOTE": "Agree the credit note to the original invoice, approval and reason; confirm recording as a reduction of revenue in the correct period.",
    "DUP_INV_NO": "Inspect both documents; determine whether the sale has been recorded twice or the numbering is erroneous; review the invoice sequence.",
    "DUP_FILE": "Remove the duplicate document from the sample or confirm it represents a separate transaction.",
    "DUP_SUSPECT": "Compare both invoices (customer, date, amount) and confirm they are separate transactions.",
    "NOT_IN_LEDGER": "Search the ledger and suspense accounts for the transaction; consider completeness of revenue and cut-off.",
    "POSSIBLE_MATCH": "Review the proposed ledger entry and confirm or reject the match in the review screen.",
    "AMOUNT_DIFF": "Obtain explanation for the difference, agree to supporting adjustments, and assess for misstatement.",
    "CUSTOMER_MISMATCH": "Agree customer identity (CR / VAT number); if the names refer to the same party, confirm a customer alias.",
    "LEDGER_DUP": "Inspect ledger postings for double recording of the same invoice; propose adjustment if duplicated.",
    "LEDGER_NO_INVOICE": "Request the supporting invoice for this ledger entry (occurrence assertion).",
    "LOW_CONFIDENCE": "Compare the extracted values with the PDF and confirm or correct each low-confidence field.",
    "NEEDS_MANUAL": "Enter the invoice data manually from the source document (no reliable text could be read).",
    "UNUSUAL_AMOUNT": "Inspect the contract / order for the unusually large invoice; confirm delivery and approval.",
    "DATE_OUTSIDE_FY": "Confirm the period of recognition; invoices outside the financial year should not be in current-year sales.",
    "CUTOFF_RISK": "Obtain the goods delivery note / service completion evidence to confirm recognition in the correct period.",
    "CUTOFF_ERROR": "Delivery and invoice are in different periods - assess cut-off misstatement and propose adjustment.",
    "NO_GDN": "Obtain evidence of delivery; absence of a GDN indicates possible occurrence risk.",
    "NOT_APPROVED": "Obtain evidence of invoice approval per the client's authority matrix.",
    "PRICE_NOT_MATCHED": "Compare selling price with the approved price list and obtain approval for deviations.",
}


def _f(code, risk, obs, reason, doc, ar=None, **kw):
    """A finding in English (record of reference) with its Arabic wording stored alongside for the Arabic UI."""
    obs_ar, reason_ar = ar or STATIC_AR.get(code, (None, None))
    return {"code": code, "risk": risk, "observation": obs, "reason": reason, "document": doc, "procedure": PROC[code],
            "observation_ar": obs_ar, "reason_ar": reason_ar, "procedure_ar": PROC_AR.get(code), **kw}


def run_checks(eid: int, low_conf: float | None = None) -> list[dict]:
    low_conf = config.LOW_CONFIDENCE if low_conf is None else low_conf
    eng = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    tol = float(eng["tolerance"])
    mat = float(eng["materiality"]) if eng["materiality"] else None
    fy_start, fy_end = date.fromisoformat(eng["fy_start"]), date.fromisoformat(eng["fy_end"])
    invs = {i["id"]: i for i in load_invoices(eid) if i["status"] != "superseded"}
    rows = compute_rows(eid)
    ledger = load_ledger(eid)
    out: list[dict] = []
    sar_vals = [r["amount_sar"] for r in rows if r["amount_sar"] is not None and r["amount_sar"] > 0]
    q3 = iqr = med = None
    if len(sar_vals) >= 8:
        qs = statistics.quantiles(sar_vals, n=4)
        q3, iqr, med = qs[2], qs[2] - qs[0], statistics.median(sar_vals)
    by_hash: dict[str, list[int]] = {}
    for i in invs.values():
        by_hash.setdefault(i["sha256"], []).append(i["id"])
    sig: dict[tuple, list[int]] = {}
    for r in rows:
        if r["customer"] and r["invoice_date"] and r["invoice_amount"] is not None:
            sig.setdefault((norm_party(r["customer"]), r["invoice_date"], round(float(r["invoice_amount"]), 2)), []).append(r["invoice_id"])

    for r in rows:
        inv = invs[r["invoice_id"]]
        F = inv["fields"] or {}
        doc = f"{r['file']}"
        def p(k):
            pg = (F.get(k) or {}).get("page")
            return f"{doc} p.{pg}" if pg else doc
        add = lambda f: out.append({**f, "invoice_id": inv["id"]})
        if inv["status"] == "needs_manual":
            add(_f("NEEDS_MANUAL", "High", "Invoice data could not be extracted from the document",
                   "No machine-readable text and AI extraction unavailable or unsuccessful", doc))
        if not r["invoice_number"]:
            add(_f("INV_NO_MISSING", "High", "Invoice number not identified", "Invoice number is the primary vouching key", doc))
        if not r["customer"]:
            add(_f("CUST_MISSING", "High", "Customer name not identified", "Customer is required to vouch the sale", doc))
        if not r["invoice_date"]:
            add(_f("DATE_MISSING", "High", "Invoice date not identified", "Date is required for period / cut-off testing", doc))
        if r["invoice_amount"] is None:
            add(_f("TAXABLE_MISSING", "High", "Taxable amount (excluding VAT) not identified",
                   "Working paper requires the amount excluding VAT", doc))
        if not r["currency"]:
            add(_f("CURRENCY_MISSING", "Medium", "Invoice currency not stated / not identified",
                   "SAR translation cannot be performed without the currency", doc))
        elif r["exchange_rate"] is None:
            add(_f("FX_MISSING", "Medium", f"No authorised exchange rate entered for {r['currency']}",
                   "Amount in SAR and difference cannot be calculated", doc,
                   ar=(f"لم يُدخل سعر صرف معتمد للعملة {r['currency']}", "لا يمكن حساب المبلغ بالريال ولا الفرق")))
        T, V, G, rate = (fv(inv, k) for k in ("taxable_amount", "vat_amount", "gross_amount", "vat_rate"))
        if T is not None and V is not None and rate is not None:
            exp = round(float(T) * float(rate) / 100, 2)
            if abs(exp - float(V)) > 0.05:
                add(_f("VAT_RECALC", "Medium", f"VAT recalculation difference: invoice {float(V):,.2f} vs recalculated {exp:,.2f}",
                       f"VAT at {rate}% on {float(T):,.2f}", p("vat_amount"),
                       ar=(f"فرق في إعادة احتساب الضريبة: في الفاتورة {money(V)} مقابل {money(exp)} بعد إعادة الاحتساب",
                           f"ضريبة بنسبة {rate}% على {money(T)}")))
        if T is not None and V is not None and G is not None and abs(float(T) + float(V) - float(G)) > 0.05:
            add(_f("VAT_ARITHMETIC_ERROR", "Medium", f"Invoice arithmetic error: {float(T):,.2f} + {float(V):,.2f} ≠ {float(G):,.2f}",
                   "Taxable amount plus VAT does not equal invoice total", p("gross_amount"),
                   ar=(f"خطأ حسابي في الفاتورة: {money(T)} + {money(V)} ≠ {money(G)}",
                       "المبلغ الخاضع للضريبة مضافًا إليه الضريبة لا يساوي إجمالي الفاتورة")))
        items = inv["line_items"] or []
        if T is not None and items and all(it.get("amount") is not None for it in items):
            s = round(sum(float(it["amount"]) for it in items) - float(fv(inv, "discount") or 0), 2)
            if abs(s - float(T)) > 0.05:
                add(_f("LINE_SUM", "Low", f"Line items total {s:,.2f} differs from taxable amount {float(T):,.2f}",
                       "Line-item extraction may be incomplete or invoice arithmetic is incorrect", p("taxable_amount"),
                       ar=(f"مجموع بنود الفاتورة {money(s)} يختلف عن المبلغ الخاضع للضريبة {money(T)}",
                           "قد يكون استخراج البنود ناقصًا، أو في حساب الفاتورة خطأ")))
        if rate is not None and float(rate) not in (0.0, 15.0) and r["currency"] in ("SAR", None):
            add(_f("VAT_RATE", "Medium", f"VAT rate of {rate}% is not the KSA standard (15%) or zero rate",
                   "Unusual rate for a Saudi supply", p("vat_rate"),
                   ar=(f"نسبة الضريبة {rate}% ليست النسبة الأساسية في المملكة (15%) ولا نسبة الصفر", "نسبة غير معتادة لتوريد داخل المملكة")))
        sv, bv = fv(inv, "seller_vat"), fv(inv, "buyer_vat")
        if inv["doc_type"] in ("tax_invoice", "simplified_tax_invoice") and not sv:
            add(_f("SELLER_VAT", "Medium", "Seller VAT number not shown on tax invoice", "A tax invoice must show the supplier VAT number", doc,
                   ar=("الرقم الضريبي للبائع غير ظاهر في الفاتورة الضريبية", "يجب أن تُظهر الفاتورة الضريبية الرقم الضريبي للمورد")))
        elif sv and not valid_saudi_vat(sv):
            add(_f("SELLER_VAT", "Low", f"Seller VAT number {sv} does not follow the 15-digit format (3…3)", "Format check", p("seller_vat"),
                   ar=(f"الرقم الضريبي للبائع {sv} لا يطابق صيغة الخمسة عشر رقمًا (يبدأ وينتهي بالرقم 3)", "فحص الصيغة")))
        if bv and not valid_saudi_vat(bv):
            add(_f("BUYER_VAT", "Low", f"Buyer VAT number {bv} does not follow the 15-digit format (3…3)", "Format check", p("buyer_vat"),
                   ar=(f"الرقم الضريبي للمشتري {bv} لا يطابق صيغة الخمسة عشر رقمًا (يبدأ وينتهي بالرقم 3)", "فحص الصيغة")))
        dn = (F.get("invoice_date") or {}).get("note") or ""
        if dn.startswith(HIJRI_NOTE) and not (F.get("invoice_date") or {}).get("confirmed") \
                and (F.get("invoice_date") or {}).get("method") != "auditor":
            hm = re.search(r"(\d{2}/\d{2}/\d{4}) AH converted to (\S+)", dn)
            add(_f("HIJRI_DATE", "Low", dn, "Invoice dated in the Hijri calendar; Gregorian date derived by conversion", p("invoice_date"),
                   ar=((f"التاريخ الهجري {hm.group(1)} هـ حُوِّل إلى {hm.group(2)} (تقويم أم القرى) - يتطلب تأكيد المراجع" if hm else None),
                       "الفاتورة مؤرخة بالتقويم الهجري، والتاريخ الميلادي مستنتج بالتحويل")))
        for qf in _qr_checks(inv, r, doc):
            add(qf)
        if inv["doc_type"] in ("proforma", "quotation"):
            add(_f("DOC_TYPE", "High", f"Document is a {inv['doc_type']}, not a tax invoice", "Not valid evidence of a sale", doc,
                   ar=(f"المستند {DOC_TYPE_AR.get(inv['doc_type'], inv['doc_type'])} وليس فاتورة ضريبية", "ليس دليلًا صالحًا على حدوث البيع")))
        if inv["doc_type"] == "credit_note":
            add(_f("CREDIT_NOTE", "Medium", "Credit note included in the sales population"
                   + (f" (original invoice {fv(inv, 'credit_note_ref')})" if fv(inv, "credit_note_ref") else ""),
                   "Credit notes reduce revenue and require separate testing", doc,
                   ar=("إشعار دائن ضمن مجتمع المبيعات"
                       + (f" (الفاتورة الأصلية {fv(inv, 'credit_note_ref')})" if fv(inv, "credit_note_ref") else ""),
                       "الإشعارات الدائنة تخفّض الإيراد وتتطلب اختبارًا مستقلًا")))
        if r["match_status"] == "Duplicate Invoice" and r["invoice_number"]:
            dupe_docs = [x for x in rows if x["invoice_number"] and norm_invoice_no(x["invoice_number"]) == norm_invoice_no(r["invoice_number"])]
            if len(dupe_docs) > 1:
                add(_f("DUP_INV_NO", "High", f"Invoice number {r['invoice_number']} appears on {len(dupe_docs)} documents: "
                       + ", ".join(x["file"] for x in dupe_docs), "Duplicate invoice numbers indicate double billing or sequence error", doc,
                       ar=(f"رقم الفاتورة {r['invoice_number']} ظاهر في {len(dupe_docs)} مستندات: " + "، ".join(x["file"] for x in dupe_docs),
                           "تكرار رقم الفاتورة يشير إلى فوترة مزدوجة أو خطأ في التسلسل")))
            else:
                add(_f("LEDGER_DUP", "High", f"Invoice {r['invoice_number']} recorded more than once in the sales ledger",
                       "Possible duplicate revenue recording", doc,
                       ar=(f"الفاتورة {r['invoice_number']} مسجلة أكثر من مرة في دفتر المبيعات", "احتمال تسجيل الإيراد مرتين")))
        if len(by_hash.get(inv["sha256"], [])) > 1:
            add(_f("DUP_FILE", "High", "Identical PDF file uploaded more than once", "Same file hash", doc))
        if r["customer"] and r["invoice_date"] and r["invoice_amount"] is not None:
            same = sig.get((norm_party(r["customer"]), r["invoice_date"], round(float(r["invoice_amount"]), 2)), [])
            if len(same) > 1 and len({norm_invoice_no(fv(invs[x], 'invoice_number')) for x in same}) > 1:
                add(_f("DUP_SUSPECT", "Medium", "Another invoice has the same customer, date and amount",
                       "Possible duplicate invoice under a different number", doc))
        ms = r["match_status"]
        if ms == "Not Found in Ledger":
            add(_f("NOT_IN_LEDGER", "High", "Invoice not found in the sales ledger", "No ledger entry by number, or by customer/date/amount", doc))
        elif ms == "Possible Match":
            add(_f("POSSIBLE_MATCH", "Medium", "Ledger entry proposed on customer/date/amount - not matched by invoice number",
                   "Ambiguous match requires auditor confirmation", doc))
        elif ms == "Customer Mismatch":
            add(_f("CUSTOMER_MISMATCH", "Medium", f"Customer per invoice '{r['customer']}' differs from ledger '{r['ledger_customer']}'",
                   "Revenue may be recorded against the wrong customer", doc,
                   ar=(f"العميل في الفاتورة «{r['customer']}» يختلف عن العميل في الدفتر «{r['ledger_customer']}»",
                       "قد يكون الإيراد مسجلًا على عميل خاطئ")))
        if r["diff_status"] == "Difference Identified":
            risk = "High" if (mat is not None and abs(r["difference"]) >= mat) or mat is None and abs(r["difference"]) > 1000 else "Medium"
            add(_f("AMOUNT_DIFF", risk, f"Difference of SAR {r['difference']:,.2f} between ledger ({r['ledger_amount']:,.2f}) and invoice ({r['amount_sar']:,.2f})",
                   f"Absolute difference exceeds tolerance of SAR {tol:,.2f}", doc, suggestion=r["suggestion"],
                   ar=(f"فرق قدره {money(r['difference'])} ريال بين الدفتر ({money(r['ledger_amount'])}) والفاتورة ({money(r['amount_sar'])})",
                       f"الفرق المطلق يتجاوز حد التسامح البالغ {money(tol)} ريال")))
        low = [k for k in REQUIRED + ["vat_amount", "gross_amount"]
               if (F.get(k) or {}).get("value") is not None and (F.get(k) or {}).get("confidence", 0) < low_conf
               and not (F.get(k) or {}).get("confirmed")]
        if low and inv["status"] != "needs_manual":
            add(_f("LOW_CONFIDENCE", "Medium", "Low-confidence extraction for: " + ", ".join(low),
                   "Values may be misread; auditor confirmation required", doc,
                   ar=("درجة ثقة منخفضة في استخراج: " + "، ".join(FIELD_AR.get(k, k) for k in low),
                       "قد تكون القيم مقروءة خطأً؛ يلزم تأكيد المراجع")))
        if q3 is not None and r["amount_sar"] is not None and r["amount_sar"] > q3 + 3 * iqr and r["amount_sar"] > 3 * med:
            add(_f("UNUSUAL_AMOUNT", "Low", f"Invoice amount SAR {r['amount_sar']:,.2f} is unusually large (median SAR {med:,.2f})",
                   "Statistical outlier (> Q3 + 3×IQR)", doc,
                   ar=(f"مبلغ الفاتورة {money(r['amount_sar'])} ريال كبير على نحو غير معتاد (الوسيط {money(med)} ريال)",
                       "قيمة شاذة إحصائيًا (أكبر من الربيع الثالث + 3 × المدى الربيعي)")))
        d = to_date(r["invoice_date"])
        man = r["manual"]
        gdn = to_date(man.get("gdn_date"))
        if d and not (fy_start <= d <= fy_end):
            add(_f("DATE_OUTSIDE_FY", "High", f"Invoice dated {d.isoformat()} is outside FY {eng['financial_year']} ({fy_start} to {fy_end})",
                   "Period / cut-off", p("invoice_date"),
                   ar=(f"تاريخ الفاتورة {d.isoformat()} خارج السنة المالية {eng['financial_year']} (من {fy_start} إلى {fy_end})",
                       "الفترة والقطع")))
        if d:
            near = abs((d - fy_end).days) <= int(eng["cutoff_days"])
            if gdn and ((d <= fy_end) != (gdn <= fy_end)):
                add(_f("CUTOFF_ERROR", "High", f"Invoice dated {d} but goods delivered {gdn} - different financial periods",
                       "Revenue recognised in a different period from delivery", doc,
                       ar=(f"الفاتورة بتاريخ {d} والبضاعة سُلِّمت في {gdn} - فترتان ماليتان مختلفتان",
                           "أُثبت الإيراد في فترة غير فترة التسليم")))
            elif near and not gdn:
                add(_f("CUTOFF_RISK", "Medium", f"Invoice dated {d} is within {eng['cutoff_days']} days of year end; delivery date not yet evidenced",
                       "Cut-off cannot be concluded without delivery evidence", doc,
                       ar=(f"الفاتورة بتاريخ {d} تقع ضمن {eng['cutoff_days']} يومًا من نهاية السنة، ولم يُوثَّق تاريخ التسليم بعد",
                           "لا يمكن الحكم على القطع دون دليل على التسليم")))
        if str(man.get("gdn_issued", "")).lower() in ("no", "n", "false"):
            add(_f("NO_GDN", "High", "Auditor recorded that no goods delivery note was issued", "Missing supporting document", doc))
        if str(man.get("invoice_approved", "")).lower() in ("no", "n", "false"):
            add(_f("NOT_APPROVED", "High", "Auditor recorded that the invoice was not approved", "Authorisation control failure", doc))
        if str(man.get("price_list_matched", "")).lower() in ("no", "n", "false"):
            add(_f("PRICE_NOT_MATCHED", "Medium", "Selling price does not agree with the approved price list", "Pricing control exception", doc))

    # ledger-side checks
    for le in unmatched_ledger_rows(eid, list(invs.values()), ledger):
        out.append({**_f("LEDGER_NO_INVOICE", "High" if (le["amount"] or 0) >= (mat or 0) and mat else "Medium",
                         f"Ledger row {le['row_number']} ({le['invoice_number'] or 'no invoice no.'}, {le['customer'] or '-'}, "
                         f"{(le['amount'] or 0):,.2f}) has no supporting invoice in the uploaded population",
                         "Recorded sale not vouched to an invoice", f"Sales ledger row {le['row_number']}",
                         ar=(f"القيد رقم {le['row_number']} في الدفتر ({le['invoice_number'] or 'بلا رقم فاتورة'}، {le['customer'] or '-'}، "
                             f"{money(le['amount'] or 0)}) ليس له فاتورة مؤيدة ضمن الفواتير المرفوعة",
                             "مبيعات مسجلة غير مؤيدة بفاتورة")), "ledger_id": le["id"]})
    _store_findings(eid, out)
    return out


def _qr_checks(inv, r, doc) -> list[dict]:
    """Cross-check the ZATCA QR (seller VAT, timestamp date, total incl. VAT, VAT total) against the invoice."""
    q = inv.get("zatca_qr")
    if q is None or inv["status"] == "needs_manual" and not q.get("payload"):
        return []   # detector unavailable, or nothing readable at all (already flagged NEEDS_MANUAL)
    out = []
    if not q.get("payload"):
        if inv["doc_type"] in ("tax_invoice", "simplified_tax_invoice", "credit_note"):
            out.append(_f("ZATCA_QR_MISSING", "Low", "No ZATCA QR code detected on the tax invoice",
                          "QR code is mandatory for simplified tax invoices (Phase 1) and for all e-invoices from Phase 2", doc))
        return out
    d = q.get("decoded") or {}
    loc = f"{doc} p.{q.get('page')} (QR)"
    if not d.get("tlv_valid"):
        out.append(_f("ZATCA_QR_INVALID", "Medium", "QR code is not a valid ZATCA TLV: " + "; ".join(d.get("errors", [])),
                      "Structure / mandatory tags 1-5 check", loc,
                      ar=("رمز QR ليس بصيغة TLV صالحة حسب متطلبات ZATCA: " + "؛ ".join(d.get("errors", [])),
                          "فحص البنية والوسوم الإلزامية من 1 إلى 5")))
        return out
    diffs, diffs_ar = [], []
    sv = fv(inv, "seller_vat")
    if sv and d.get("vat_registration") and str(sv) != d["vat_registration"]:
        diffs.append(f"seller VAT {sv} vs QR {d['vat_registration']}")
        diffs_ar.append(f"الرقم الضريبي للبائع {sv} مقابل {d['vat_registration']} في الرمز")
    G, V = fv(inv, "gross_amount"), fv(inv, "vat_amount")
    if G is not None and d.get("invoice_total") is not None and abs(abs(float(G)) - abs(d["invoice_total"])) > 0.05:
        diffs.append(f"total incl. VAT {float(G):,.2f} vs QR {d['invoice_total']:,.2f}")
        diffs_ar.append(f"الإجمالي شامل الضريبة {money(G)} مقابل {money(d['invoice_total'])} في الرمز")
    if V is not None and d.get("vat_total") is not None and abs(abs(float(V)) - abs(d["vat_total"])) > 0.05:
        diffs.append(f"VAT {float(V):,.2f} vs QR {d['vat_total']:,.2f}")
        diffs_ar.append(f"الضريبة {money(V)} مقابل {money(d['vat_total'])} في الرمز")
    qd, idt = zatca_qr.qr_date(d), to_date(r["invoice_date"])
    if qd and idt and qd != idt:
        diffs.append(f"invoice date {idt} vs QR timestamp {d.get('timestamp')}")
        diffs_ar.append(f"تاريخ الفاتورة {idt} مقابل الطابع الزمني {d.get('timestamp')} في الرمز")
    if diffs:
        out.append(_f("ZATCA_QR_MISMATCH", "High", "Printed invoice disagrees with its ZATCA QR: " + "; ".join(diffs),
                      "QR data is generated by the e-invoicing solution; differences may indicate an altered document", loc,
                      ar=("الفاتورة المطبوعة تخالف بيانات رمز QR: " + "؛ ".join(diffs_ar),
                          "بيانات رمز QR يولّدها نظام الفوترة الإلكترونية؛ والاختلاف قد يشير إلى مستند معدَّل")))
    return out


def _store_findings(eid: int, found: list[dict]) -> None:
    existing = {(f["invoice_id"], f["ledger_id"], f["code"]): f for f in db.q("SELECT * FROM findings WHERE engagement_id=?", (eid,))}
    seen = set()
    with db.tx() as c:
        for f in found:
            key = (f.get("invoice_id"), f.get("ledger_id"), f["code"])
            seen.add(key)
            if key in existing:
                c.execute("UPDATE findings SET observation=?, risk=?, reason=?, document=?, procedure=?, observation_ar=?, reason_ar=?, "
                          "procedure_ar=?, active=1 WHERE id=?",
                          (f["observation"], f["risk"], f["reason"], f["document"], f["procedure"], f.get("observation_ar"),
                           f.get("reason_ar"), f.get("procedure_ar"), existing[key]["id"]))
            else:
                c.execute("INSERT INTO findings(engagement_id,invoice_id,ledger_id,code,observation,risk,reason,document,procedure,"
                          "observation_ar,reason_ar,procedure_ar,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (eid, f.get("invoice_id"), f.get("ledger_id"), f["code"], f["observation"], f["risk"], f["reason"],
                           f["document"], f["procedure"], f.get("observation_ar"), f.get("reason_ar"), f.get("procedure_ar"), db.now()))
        for key, f in existing.items():
            if key not in seen and f["active"]:
                c.execute("UPDATE findings SET active=0 WHERE id=?", (f["id"],))


def recompute(eid: int, user: str) -> None:
    run_matching(eid, user)
    run_checks(eid)


def summary(eid: int) -> dict:
    rows = compute_rows(eid)
    fnd = db.q("SELECT * FROM findings WHERE engagement_id=? AND active=1", (eid,))
    ledger = load_ledger(eid)
    s = lambda xs: round(sum(x for x in xs if x is not None), 2)
    outstanding = []
    for r in rows:
        man = r["manual"]
        miss = [lbl for k, lbl in (("gdn_issued", "GDN issued (L)"), ("gdn_date", "GDN date (M)"),
                                    ("price_list_matched", "Price list (N)"), ("invoice_approved", "Invoice approved (O)"))
                if not man.get(k)]
        if r["diff_status"] == "Difference Identified" and not man.get("explanation"):
            miss.append("Explanation of difference (P)")
        if r["review_status"] not in ("approved",):
            miss.append("Auditor approval")
        if miss:
            outstanding.append({"invoice_id": r["invoice_id"], "invoice_number": r["invoice_number"], "file": r["file"], "items": miss})
    return {
        "invoices_tested": len(rows),
        "total_value_tested_sar": s(r["amount_sar"] for r in rows),
        "total_invoice_excl_vat": s(r["invoice_amount"] for r in rows if r["currency"] == "SAR"),
        "total_ledger_amount": s(r["ledger_amount"] for r in rows),
        "ledger_rows": len(ledger), "ledger_total": s(le["amount"] for le in ledger),
        "matched": sum(1 for r in rows if r["match_status"] == "Matched"),
        "unmatched": sum(1 for r in rows if r["match_status"] in ("Not Found in Ledger", "Possible Match")),
        "exceptions": sum(1 for f in fnd if f["review_status"] != "cleared"),
        "findings_by_risk": {k: sum(1 for f in fnd if f["risk"] == k) for k in ("High", "Medium", "Low")},
        "total_differences": s(r["difference"] for r in rows if r["diff_status"] == "Difference Identified"),
        "net_differences": s(r["difference"] for r in rows),
        "approved": sum(1 for r in rows if r["review_status"] == "approved"),
        "recon_categories": {**{k: sum(1 for r in rows if r["recon_category"] == k) for k in RECON_CATEGORIES},
                             "UNMATCHED_LEDGER_ROW": len(unmatched_ledger_rows(eid, ledger=ledger))},
        "match_passes": {f"pass{p}": sum(1 for r in rows if r["match_pass"] == p) for p in (1, 2, 3)},
        "qr": {"checked": sum(1 for r in rows if r["zatca_qr"] is not None),
               "found": sum(1 for r in rows if (r["zatca_qr"] or {}).get("found")),
               "valid": sum(1 for r in rows if (r["zatca_qr"] or {}).get("tlv_valid"))},
        "low_confidence_threshold": config.LOW_CONFIDENCE,
        "outstanding_procedures": outstanding,
    }
