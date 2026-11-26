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


def _ar_lines(text: str, font: str, size: float, width: float) -> str:
    """Wrap Arabic in logical order first, then apply shaping/bidi per line. Letting the PDF engine wrap an
    already-reordered (visual) string puts the lines in the wrong order."""
    out, cur = [], ""
    for w in str(text).split():
        trial = f"{cur} {w}".strip()
        if cur and pdfmetrics.stringWidth(arabic_reshaper.reshape(trial), font, size) > width:
            out.append(cur); cur = w
        else:
            cur = trial
    if cur:
        out.append(cur)
    return "<br/>".join(_t(line) for line in out)


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


# ------------------------------------------------------------------------------ executive report
RECON_LABELS = {
    "MATCHED_CLEAN": ("Matched - no difference", "مطابقة دون فرق"),
    "WITHIN_TOLERANCE": ("Matched - within tolerance", "مطابقة ضمن حد التسامح"),
    "MATERIAL_DIFFERENCE": ("Difference above tolerance", "فرق يتجاوز حد التسامح"),
    "NOT_IN_LEDGER": ("Invoice not in ledger", "فاتورة غير مسجلة في الدفتر"),
    "POSSIBLE_MATCH": ("Proposed match - awaiting auditor", "مطابقة مقترحة بانتظار المراجع"),
    "DUPLICATE_INVOICE": ("Duplicate invoice number", "رقم فاتورة مكرر"),
    "INCOMPLETE_DATA": ("Incomplete data (e.g. FX rate)", "بيانات ناقصة (مثل سعر الصرف)"),
    "LEDGER_NOT_UPLOADED": ("Ledger not uploaded", "لم يُرفع الدفتر"),
    "UNMATCHED_LEDGER_ROW": ("Ledger entries without invoice", "قيود في الدفتر بلا فاتورة"),
}


def executive_pdf(eid: int, user: str) -> bytes:
    """Bilingual (English / Arabic) executive summary for the partner and the client. States facts from the
    same canonical rows as every other output; the audit conclusion is left for the engagement team."""
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    eng = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    s = engine.summary(eid)
    rows = engine.compute_rows(eid)
    fnd = db.q("SELECT * FROM findings WHERE engagement_id=? AND active=1", (eid,))
    tol = float(eng["tolerance"])
    bold = _FONT + ("-B" if _FONT != "Helvetica" else "")
    ss = getSampleStyleSheet()
    en = ParagraphStyle("en", parent=ss["BodyText"], fontName=_FONT, fontSize=8.5, leading=11, alignment=TA_LEFT)
    arS = ParagraphStyle("ar", parent=en, alignment=TA_RIGHT)
    small = ParagraphStyle("sm", parent=en, fontSize=7, leading=9, textColor=colors.HexColor("#444444"))
    smallAr = ParagraphStyle("smar", parent=small, alignment=TA_RIGHT)
    h1 = ParagraphStyle("h1", parent=en, fontName=bold, fontSize=15, leading=19)
    h1ar = ParagraphStyle("h1ar", parent=h1, alignment=TA_RIGHT)
    h2 = ParagraphStyle("h2", parent=en, fontName=bold, fontSize=10.5, leading=14, spaceBefore=8,
                        textColor=colors.HexColor("#1F3864"))
    navy, light = colors.HexColor("#1F3864"), colors.HexColor("#EEF2F7")

    def head(e, a):   # bilingual section heading: English left, Arabic right
        t = Table([[Paragraph(e, h2), Paragraph(_t(a), ParagraphStyle("h2ar", parent=h2, alignment=TA_RIGHT))]],
                  colWidths=[93 * mm, 93 * mm])
        t.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, 0), 0.6, navy), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
        return t

    def lab(e, a):
        return Paragraph(f"{e}<br/>{_t(a)}", small)

    def grid(data, widths, num_cols=()):
        t = Table(data, colWidths=widths, repeatRows=1)
        st = [("FONT", (0, 0), (-1, -1), _FONT, 8), ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
              ("BACKGROUND", (0, 0), (-1, 0), navy), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
              ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
        st += [("ALIGN", (c, 1), (c, -1), "RIGHT") for c in num_cols]
        t.setStyle(TableStyle(st))
        return t

    hdr = lambda e, a: Paragraph(f"<font color='white'>{e}<br/>{_t(a)}</font>", small)
    final = eng["status"] == "finalized"
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm,
                            bottomMargin=16 * mm, title=f"Executive Audit Summary - {eng['client_name']}")

    def footer(c, d):
        c.saveState(); c.setFont(_FONT, 7); c.setFillColor(colors.grey)
        c.drawString(12 * mm, 8 * mm, f"{'FINAL' if final else 'DRAFT - subject to review'} | page {d.page}")
        c.drawRightString(A4[0] - 12 * mm, 8 * mm, _t("مسودة - خاضعة للمراجعة") if not final else _t("نهائي"))
        if not final:   # diagonal watermark on drafts
            c.setFont(bold, 60); c.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.07))
            c.translate(A4[0] / 2, A4[1] / 2); c.rotate(40); c.drawCentredString(0, 0, "DRAFT")
        c.restoreState()

    el = [Table([[Paragraph("Sales Vouching<br/>Executive Audit Summary", h1),
                  Paragraph(_t("اختبار المبيعات") + "<br/>" + _t("الملخص التنفيذي للمراجعة"), h1ar)]], colWidths=[93 * mm, 93 * mm]),
          Spacer(1, 4)]
    info = [[lab("Client", "العميل"), Paragraph(_t(eng["client_name"]), en)],
            [lab("Financial year", "السنة المالية"), Paragraph(f"{eng['financial_year']} ({eng['fy_start']} → {eng['fy_end']})", en)],
            [lab("Status", "الحالة"), Paragraph(("FINAL" if final else "DRAFT - subject to auditor review") + " / "
                                                + _t("نهائي" if final else "مسودة - خاضعة لمراجعة المراجع"), en)],
            [lab("Tolerance / materiality (SAR)", "حد التسامح / الأهمية النسبية (ريال)"),
             Paragraph(f"{tol:,.2f} / " + (f"{float(eng['materiality']):,.2f}" if eng["materiality"] else "not set / غير محدد"), en)],
            [lab("Prepared by / on", "أعده / بتاريخ"), Paragraph(f"{_t(user)} · {datetime.now():%Y-%m-%d %H:%M}", en)]]
    it = Table(info, colWidths=[55 * mm, 131 * mm])
    it.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("BACKGROUND", (0, 0), (0, -1), light),
                            ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    el += [it]

    # 1. scope
    cov = (s["total_value_tested_sar"] / s["ledger_total"]) if s.get("ledger_total") else None
    el += [head("1. Scope of testing", "١. نطاق الاختبار")]
    scope = [[hdr("Measure", "البند"), hdr("Value", "القيمة")],
             [lab("Invoices tested", "عدد الفواتير المختبرة"), f"{s['invoices_tested']:,}"],
             [lab("Value tested, SAR excl. VAT", "القيمة المختبرة بالريال (غير شاملة الضريبة)"), _m(s["total_value_tested_sar"])],
             [lab("Sales ledger rows / total, SAR", "قيود دفتر المبيعات / إجماليها بالريال"), f"{s['ledger_rows']:,} / {_m(s['ledger_total'])}"],
             [lab("Coverage of ledger value", "نسبة التغطية من قيمة الدفتر"), "n/a" if cov is None else f"{cov:.1%}"],
             [lab("Invoices approved by auditor", "فواتير اعتمدها المراجع"), f"{s['approved']:,} / {s['invoices_tested']:,}"]]
    el += [grid(scope, [120 * mm, 66 * mm], num_cols=(1,))]

    # 2. reconciliation
    rc = s["recon_categories"]
    amt = {k: sum(r["amount_sar"] or 0 for r in rows if r["recon_category"] == k) for k in RECON_LABELS}
    led_amt = sum(le["amount"] or 0 for le in engine.unmatched_ledger_rows(eid))
    rec = [[hdr("Result", "النتيجة"), hdr("Count", "العدد"), hdr("SAR", "ريال")]]
    for k, (e_, a_) in RECON_LABELS.items():
        if rc.get(k):
            rec.append([lab(e_, a_), f"{rc[k]:,}", _m(led_amt if k == "UNMATCHED_LEDGER_ROW" else amt[k])])
    el += [head("2. Invoice-to-ledger reconciliation", "٢. مطابقة الفواتير مع دفتر المبيعات"),
           grid(rec, [120 * mm, 26 * mm, 40 * mm], num_cols=(1, 2)),
           Paragraph(f"Matching passes - exact: {s['match_passes']['pass1']}, normalised number: {s['match_passes']['pass2']}, "
                     f"heuristic proposals (auditor sign-off required): {s['match_passes']['pass3']}. "
                     "SAR column: invoice amount in SAR; for ledger entries without invoice, the ledger amount.", small)]

    # 3. findings
    fr = s["findings_by_risk"]
    el += [head("3. Audit findings", "٣. ملاحظات المراجعة"),
           grid([[hdr("High", "مرتفع"), hdr("Medium", "متوسط"), hdr("Low", "منخفض"), hdr("Open", "مفتوحة")],
                 [str(fr["High"]), str(fr["Medium"]), str(fr["Low"]), str(s["exceptions"])]],
                [46.5 * mm] * 4, num_cols=(0, 1, 2, 3))]
    by_code: dict[str, list] = {}
    for f in fnd:
        if f["risk"] == "High":
            by_code.setdefault(f["code"], []).append(f)
    if by_code:
        hi = [[hdr("High-risk finding", "ملاحظة عالية الخطورة"), hdr("Count", "العدد"), hdr("Example", "مثال")]]
        for code, fs in sorted(by_code.items(), key=lambda kv: -len(kv[1])):
            hi.append([Paragraph(code, small), str(len(fs)), Paragraph(_t(fs[0]["observation"])[:220], small)])
        el += [Spacer(1, 4), grid(hi, [48 * mm, 16 * mm, 122 * mm], num_cols=(1,))]

    # 4. differences above tolerance
    diffs = sorted([r for r in rows if r["recon_category"] == "MATERIAL_DIFFERENCE"], key=lambda r: -abs(r["difference"]))
    el += [head("4. Differences above tolerance", "٤. الفروقات التي تتجاوز حد التسامح")]
    if diffs:
        dd = [[hdr("Invoice", "الفاتورة"), hdr("Customer", "العميل"), hdr("Ledger", "الدفتر"), hdr("Invoice SAR", "الفاتورة بالريال"),
               hdr("Difference", "الفرق"), hdr("Explanation", "التفسير")]]
        for r in diffs[:12]:
            expl = r["manual"].get("explanation")
            txt = (f"Auditor: {expl}" if expl else f"Unconfirmed AI hypothesis: {r['suggestion'] or '-'}")
            dd.append([Paragraph(_t(r["invoice_number"]), small), Paragraph(_t(r["customer"]), small), _m(r["ledger_amount"]),
                       _m(r["amount_sar"]), _m(r["difference"]), Paragraph(_t(txt)[:260], small)])
        el += [grid(dd, [26 * mm, 34 * mm, 24 * mm, 24 * mm, 22 * mm, 56 * mm], num_cols=(2, 3, 4)),
               Paragraph(f"{len(diffs)} invoice(s); total {_m(sum(r['difference'] for r in diffs))} SAR"
                         + (" (top 12 shown)" if len(diffs) > 12 else "") + ".", small)]
    else:
        el += [Paragraph("None. / " + _t("لا توجد."), en)]

    # 5. ZATCA QR
    q = s["qr"]
    qcodes = {c: sum(1 for f in fnd if f["code"] == c) for c in ("ZATCA_QR_MISMATCH", "ZATCA_QR_INVALID", "ZATCA_QR_MISSING")}
    el += [head("5. ZATCA e-invoice QR codes", "٥. رموز QR للفوترة الإلكترونية (ZATCA)"),
           grid([[hdr("Checked", "تم فحصها"), hdr("QR found", "وُجد رمز"), hdr("Valid TLV", "صالح"),
                  hdr("Disagrees with invoice", "يخالف الفاتورة"), hdr("Invalid", "غير صالح"), hdr("Missing", "غير موجود")],
                 [str(q["checked"]), str(q["found"]), str(q["valid"]), str(qcodes["ZATCA_QR_MISMATCH"]),
                  str(qcodes["ZATCA_QR_INVALID"]), str(qcodes["ZATCA_QR_MISSING"])]], [31 * mm] * 6, num_cols=range(6)),
           Paragraph("QR data is compared with the printed invoice; Phase 2 cryptographic signatures are not verified by this tool.", small),
           Paragraph(_ar_lines("تُقارن بيانات رمز QR بالفاتورة المطبوعة، ولا يتحقق هذا النظام من التوقيع الرقمي للمرحلة الثانية.",
                               _FONT, 7, 180 * mm), smallAr)]

    # 6. outstanding + conclusion
    el += [head("6. Outstanding procedures", "٦. الإجراءات المتبقية"),
           Paragraph(f"{len(s['outstanding_procedures'])} invoice(s) with outstanding manual evidence (GDN, price list, approval), "
                     "difference explanations or auditor approval - detail in the Audit Findings report.", en),
           Paragraph(_ar_lines(f"{len(s['outstanding_procedures'])} فاتورة عليها إجراءات متبقية (إشعار التسليم، قائمة الأسعار، "
                               "الاعتماد، تفسير الفروقات أو اعتماد المراجع) - التفاصيل في تقرير ملاحظات المراجعة.",
                               _FONT, 8.5, 180 * mm), arS),
           head("7. Engagement team conclusion", "٧. استنتاج فريق المراجعة")]
    concl = Table([[Paragraph("To be completed by the engagement team. This report does not express an audit conclusion.", small),
                    Paragraph(_ar_lines("يُستكمل من فريق المراجعة. لا يتضمن هذا التقرير رأيًا أو استنتاجًا مهنيًا.",
                                        _FONT, 7, 85 * mm), smallAr)],
                   ["", ""], [Paragraph("Prepared by / date:", small), Paragraph(_t("أعده / التاريخ:"), smallAr)],
                   [Paragraph("Reviewed by / date:", small), Paragraph(_t("راجعه / التاريخ:"), smallAr)]],
                  colWidths=[93 * mm, 93 * mm], rowHeights=[None, 28 * mm, 9 * mm, 9 * mm])
    concl.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("LINEBELOW", (0, 1), (-1, 2), 0.25, colors.grey),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    el += [concl, Spacer(1, 6),
           Paragraph("Basis: values were extracted from the invoice PDFs by an automated (rule-based / AI-assisted) process and are "
                     "subject to auditor confirmation; every figure is traceable to its page in the extraction log. A finding is "
                     "not a conclusion on misstatement.", small),
           Paragraph(_ar_lines("أساس الإعداد: استُخرجت القيم من فواتير PDF بعملية آلية وتخضع لتأكيد المراجع، وكل رقم قابل للتتبع "
                               "إلى صفحته في سجل الاستخراج. الملاحظة لا تعني وجود تحريف.", _FONT, 7, 180 * mm), smallAr)]
    doc.build(el, onFirstPage=footer, onLaterPages=footer)
    db.log(user, eid, "engagement", eid, "export_executive_pdf")
    return buf.getvalue()
