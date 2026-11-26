"""Export reports: PDF audit findings, CSV extraction report, Excel exception report, extraction log."""
from __future__ import annotations

import csv
import io
from datetime import datetime

import arabic_reshaper
from bidi.algorithm import get_display
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import db, engine
from .extraction.normalize import has_arabic

_FONT = "Helvetica"
for path, name in (("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVu"),):
    try:
        pdfmetrics.registerFont(TTFont(name, path))
        pdfmetrics.registerFont(TTFont(name + "-B", path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")))
        _FONT = name
    except Exception:
        pass


def _t(s) -> str:
    s = "" if s is None else str(s)
    if has_arabic(s):
        s = get_display(arabic_reshaper.reshape(s))
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _m(v):
    return "" if v is None else f"{v:,.2f}"


def findings_pdf(eid: int, user: str) -> bytes:
    eng = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    s = engine.summary(eid)
    rows = engine.compute_rows(eid)
    by_inv = {r["invoice_id"]: r for r in rows}
    fnd = db.q("SELECT * FROM findings WHERE engagement_id=? AND active=1 ORDER BY CASE risk WHEN 'High' THEN 0 "
               "WHEN 'Medium' THEN 1 ELSE 2 END, invoice_id", (eid,))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm,
                            bottomMargin=14 * mm, title=f"Sales Vouching Findings - {eng['client_name']}")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName=_FONT, fontSize=7.5, leading=9.5)
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontName=_FONT + ("-B" if _FONT != "Helvetica" else ""), fontSize=15)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName=_FONT + ("-B" if _FONT != "Helvetica" else ""), fontSize=11)
    status = "FINAL" if eng["status"] == "finalized" else "DRAFT - subject to auditor review"
    el = [Paragraph(f"Sales Vouching - Audit Findings Report", h1),
          Paragraph(_t(f"Client: {eng['client_name']} | Financial year: {eng['financial_year']} "
                       f"({eng['fy_start']} to {eng['fy_end']}) | Status: {status}"), body),
          Paragraph(f"Prepared by {_t(user)} on {datetime.now():%d %b %Y %H:%M}. Tolerance SAR {eng['tolerance']:,.2f}"
                    + (f"; materiality threshold SAR {eng['materiality']:,.2f}" if eng["materiality"] else ""), body),
          Spacer(1, 6), Paragraph("Summary", h2)]
    summ = [["Invoices tested", s["invoices_tested"], "Total value tested (SAR, excl. VAT)", _m(s["total_value_tested_sar"])],
            ["Matched", s["matched"], "Total ledger amount (matched)", _m(s["total_ledger_amount"])],
            ["Unmatched", s["unmatched"], "Total identified differences (SAR)", _m(s["total_differences"])],
            ["Open exceptions", s["exceptions"], "High / Medium / Low", "{High} / {Medium} / {Low}".format(**s["findings_by_risk"])],
            ["Approved by auditor", s["approved"], "Invoices with outstanding procedures", len(s["outstanding_procedures"])]]
    t = Table(summ, colWidths=[45 * mm, 25 * mm, 70 * mm, 40 * mm])
    t.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 8), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF2F7")),
                           ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#EEF2F7"))]))
    el += [t, Spacer(1, 8), Paragraph("Findings", h2)]
    data = [["#", "Risk", "Invoice / Document", "Audit observation", "Reason for flagging", "Recommended procedure", "Review"]]
    for n, f in enumerate(fnd, start=1):
        r = by_inv.get(f["invoice_id"]) or {}
        ref = (r.get("invoice_number") or "") + ("\n" if r else "") + (f["document"] or "")
        data.append([n, f["risk"], Paragraph(_t(ref), body), Paragraph(_t(f["observation"]), body), Paragraph(_t(f["reason"]), body),
                     Paragraph(_t(f["procedure"]), body),
                     Paragraph(_t(f["review_status"] + (f": {f['reviewer_note']}" if f["reviewer_note"] else "")), body)])
    if len(data) == 1:
        data.append(["", "", "", "No findings", "", "", ""])
    ft = Table(data, colWidths=[8 * mm, 14 * mm, 45 * mm, 68 * mm, 45 * mm, 70 * mm, 22 * mm], repeatRows=1)
    style = [("FONT", (0, 0), (-1, -1), _FONT, 7.5), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("VALIGN", (0, 0), (-1, -1), "TOP")]
    for i, f in enumerate(fnd, start=1):
        col = {"High": "#F8D7DA", "Medium": "#FFF3CD", "Low": "#E2E3E5"}[f["risk"]]
        style.append(("BACKGROUND", (1, i), (1, i), colors.HexColor(col)))
    ft.setStyle(TableStyle(style))
    el.append(ft)
    if s["outstanding_procedures"]:
        el += [PageBreak(), Paragraph("Outstanding audit procedures", h2)]
        od = [["Invoice", "Outstanding items"]] + [[Paragraph(_t(o["invoice_number"] or o["file"]), body),
                                                    Paragraph(_t("; ".join(o["items"])), body)] for o in s["outstanding_procedures"]]
        ot = Table(od, colWidths=[60 * mm, 200 * mm], repeatRows=1)
        ot.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 7.5), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F3864")),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white)]))
        el.append(ot)
    el += [Spacer(1, 10), Paragraph("Basis: invoice values were extracted by an AI-assisted process and are subject to "
                                    "auditor confirmation. A finding is not a conclusion on misstatement; successful extraction "
                                    "does not by itself evidence the validity of an invoice.", body)]
    doc.build(el)
    db.log(user, eid, "engagement", eid, "export_findings_pdf")
    return buf.getvalue()


CSV_COLS = ["sno", "file", "invoice_number", "invoice_date", "customer", "product_type", "currency", "invoice_amount",
            "vat_amount", "gross_amount", "vat_rate", "exchange_rate", "amount_sar", "ledger_amount", "difference",
            "match_status", "match_pass", "recon_category", "diff_status", "review_status", "reviewer", "confidence", "method",
            "doc_type", "qr_status"]


def extraction_csv(eid: int) -> bytes:
    rows = engine.compute_rows(eid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    for r in rows:
        w.writerow(["" if r.get(c) is None else r.get(c) for c in CSV_COLS])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def _sheet(wb, title, headers, data, widths=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F3864")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for d in data:
        ws.append(d)
    for i, wd in enumerate(widths or [18] * len(headers), start=1):
        ws.column_dimensions[get_column_letter(i)].width = wd
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def exceptions_xlsx(eid: int) -> bytes:
    rows = {r["invoice_id"]: r for r in engine.compute_rows(eid)}
    fnd = db.q("SELECT * FROM findings WHERE engagement_id=? AND active=1 ORDER BY CASE risk WHEN 'High' THEN 0 "
               "WHEN 'Medium' THEN 1 ELSE 2 END", (eid,))
    wb = Workbook(); wb.remove(wb.active)
    data = []
    for f in fnd:
        r = rows.get(f["invoice_id"]) or {}
        data.append([f["id"], f["risk"], f["code"], r.get("invoice_number"), r.get("customer"), r.get("invoice_date"),
                     r.get("amount_sar"), r.get("ledger_amount"), r.get("difference"), f["observation"], f["reason"],
                     f["document"], f["procedure"], f["review_status"], f["reviewer_note"], f["reviewed_by"]])
    _sheet(wb, "Exceptions", ["ID", "Risk", "Check", "Invoice No", "Customer", "Invoice Date", "Amount SAR", "Ledger Amount",
                              "Difference", "Observation", "Reason", "Supporting document", "Recommended procedure",
                              "Review status", "Reviewer note", "Reviewed by"],
           data, [6, 8, 18, 16, 26, 12, 14, 14, 12, 50, 36, 30, 50, 12, 30, 14])
    diff = [[r["sno"], r["invoice_number"], r["customer"], r["ledger_amount"], r["amount_sar"], r["difference"],
             r["suggestion"], (r["manual"] or {}).get("explanation")] for r in rows.values() if r["diff_status"] == "Difference Identified"]
    _sheet(wb, "Differences", ["S.no", "Invoice No", "Customer", "Ledger Amount", "Amount SAR", "Difference",
                               "AI suggested explanation (unconfirmed)", "Auditor-confirmed explanation"], diff,
           [6, 16, 26, 14, 14, 12, 60, 50])
    um = [[r["sno"], r["invoice_number"], r["customer"], r["amount_sar"], r["match_status"]] for r in rows.values()
          if r["match_status"] not in ("Matched",)]
    _sheet(wb, "Unmatched", ["S.no", "Invoice No", "Customer", "Amount SAR", "Match status"], um, [6, 16, 30, 14, 20])
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


def extraction_log_xlsx(eid: int) -> bytes:
    invs = engine.load_invoices(eid)
    wb = Workbook(); wb.remove(wb.active)
    data = []
    for i in invs:
        for k, f in (i["fields"] or {}).items():
            ai = (i["ai_fields"] or {}).get(k) or {}
            data.append([i["id"], i["original_name"], i["sha256"][:16], i["status"], i["extraction_method"], k,
                         None if ai.get("value") is None else str(ai.get("value")),
                         None if f.get("value") is None else str(f.get("value")),
                         "Auditor" if f.get("method") == "auditor" else "AI", f.get("confidence"), f.get("page"),
                         ",".join(map(str, f["bbox"])) if f.get("bbox") else None, f.get("source_text"),
                         "Yes" if f.get("confirmed") else "No", f.get("note")])
    _sheet(wb, "Field provenance", ["Invoice ID", "File", "SHA-256 (prefix)", "Status", "Method", "Field",
                                    "Original AI value", "Current value", "Origin", "Confidence", "Page", "BBox (pt)",
                                    "Source text", "Confirmed", "Note"], data,
           [8, 30, 18, 12, 10, 16, 22, 22, 9, 10, 6, 22, 50, 9, 40])
    notes = [[i["id"], i["original_name"], i["page_count"], "Yes" if i["has_text_layer"] else "No", i["language"],
              i["doc_type"], i["overall_confidence"], " | ".join(i["extraction_notes"] or []), i["error"]] for i in invs]
    _sheet(wb, "Documents", ["Invoice ID", "File", "Pages", "Text layer", "Language", "Doc type", "Overall confidence",
                             "Extraction notes", "Error"], notes, [8, 34, 6, 8, 9, 18, 10, 80, 30])
    log = db.q("SELECT * FROM audit_log WHERE engagement_id=? ORDER BY id", (eid,))
    _sheet(wb, "Audit trail", ["ID", "Timestamp (UTC)", "User", "Entity", "Entity ID", "Action", "Field", "Old value",
                               "New value", "Note"],
           [[l["id"], l["ts"], l["user"], l["entity"], l["entity_id"], l["action"], l["field"], l["old_value"], l["new_value"],
             l["note"]] for l in log], [6, 20, 12, 10, 8, 18, 18, 30, 30, 40])
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()
