"""Generate a test pack: a sales-vouching working-paper template that follows the specified layout,
50 invoice PDFs (English, Arabic, bilingual, multi-page, scanned, credit note, foreign currency,
discount, duplicates), a sales ledger with planted exceptions, and ground truth for accuracy testing.

All data is fictitious. Usage:  python samples/generate_samples.py [out_dir]
"""
from __future__ import annotations

import csv
import io
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import arabic_reshaper
from bidi.algorithm import get_display
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audit_app.extraction.zatca_qr import encode_tlv  # noqa: E402

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
pdfmetrics.registerFont(TTFont("DV", FONT))
pdfmetrics.registerFont(TTFont("DVB", FONT_B))

SELLER = ("Al Waha Industrial Supplies Co.", "شركة الواحة للتوريدات الصناعية", "300123456700003")
CUSTOMERS = [
    ("Najd Construction Company", "شركة نجد للمقاولات", "310987654300003"),
    ("Red Sea Hospitality Ltd", "شركة البحر الأحمر للضيافة", "311223344500003"),
    ("Gulf Medical Services", "شركة الخليج للخدمات الطبية", "300555666700003"),
    ("Eastern Petrochem Trading", "شركة الشرقية للتجارة البتروكيماوية", "302468013500003"),
    ("Riyadh Food Distribution", "شركة الرياض لتوزيع الأغذية", "310135792400003"),
    ("Desert Logistics LLC", "شركة الصحراء للخدمات اللوجستية", "311112222300003"),
]
PRODUCTS = [("Steel pipes 2 inch", "أنابيب فولاذية ٢ بوصة"), ("Industrial valves", "صمامات صناعية"),
            ("Safety equipment", "معدات السلامة"), ("Welding consumables", "مستهلكات اللحام"),
            ("Maintenance services", "خدمات الصيانة"), ("Electrical cables", "كابلات كهربائية")]


def ar(s: str) -> str:
    return get_display(arabic_reshaper.reshape(s))


def money(v: float) -> str:
    return f"{v:,.2f}"


def gen_invoices(rng: random.Random, n=50):
    invs = []
    start = date(2025, 1, 1)
    for i in range(1, n + 1):
        cust = CUSTOMERS[rng.randrange(len(CUSTOMERS))]
        d = start + timedelta(days=rng.randrange(0, 364))
        items = []
        for _ in range(rng.choice([1, 1, 2, 3])):
            p = PRODUCTS[rng.randrange(len(PRODUCTS))]
            q = rng.choice([1, 2, 5, 10, 20, 50])
            up = round(rng.uniform(50, 4000), 2)
            items.append({"desc": p[0], "desc_ar": p[1], "qty": q, "price": up, "amount": round(q * up, 2)})
        cur = "SAR"
        if i in (7, 19, 33):
            cur = "USD"
        if i == 41:
            cur = "EUR"
        layout = ["en_a", "en_b", "ar", "bi"][i % 4]
        invs.append({"seq": i, "number": f"INV-2025-{i:04d}", "date": d, "customer": cust, "items": items,
                     "currency": cur, "discount": 0.0, "layout": layout, "doc_type": "tax_invoice", "po": f"PO-{7000 + i}"})
    # special cases
    invs[4]["discount"] = round(sum(it["amount"] for it in invs[4]["items"]) * 0.05, 2)        # discount
    invs[11]["items"] = invs[11]["items"] + [{"desc": f"Spare part {k}", "desc_ar": "قطع غيار", "qty": 1,
                                              "price": 100.0 + k, "amount": 100.0 + k} for k in range(38)]
    invs[11]["layout"] = "en_a"                                                               # multi-page
    invs[22]["doc_type"] = "credit_note"; invs[22]["layout"] = "en_a"; invs[22]["credit_ref"] = "INV-2025-0003"
    invs[30]["scanned"] = True; invs[30]["layout"] = "en_a"                                    # scanned
    invs[45]["number"] = invs[44]["number"]                                                    # duplicate number
    invs[48]["date"] = date(2026, 1, 3)                                                        # outside FY
    invs[49]["date"] = date(2025, 12, 30)                                                      # cut-off window
    invs[37]["items"] = [{"desc": "Turbine overhaul project", "desc_ar": "مشروع صيانة توربين", "qty": 1,
                          "price": 2450000.0, "amount": 2450000.0}]                            # unusual amount
    # ZATCA QR scenarios: valid Phase 1 by default; tampered PDF (QR total differs), no QR, Phase 2 tags
    for inv in invs:
        inv["qr"] = "valid"
    invs[9]["qr"] = "mismatch"
    invs[13]["qr"] = "none"
    invs[20]["qr"] = "phase2"
    for inv in invs:
        net = round(sum(it["amount"] for it in inv["items"]) - inv["discount"], 2)
        inv["taxable"] = net
        inv["vat"] = round(net * 0.15, 2)
        inv["gross"] = round(net + inv["vat"], 2)
    return invs


class PDF:
    def __init__(self, buf):
        self.c = canvas.Canvas(buf, pagesize=A4)
        self.w, self.h = A4
        self.y = self.h - 50

    def text(self, x, s, size=10, bold=False, right=False):
        self.c.setFont("DVB" if bold else "DV", size)
        (self.c.drawRightString if right else self.c.drawString)(x, self.y, s)

    def nl(self, dy=16):
        self.y -= dy
        if self.y < 60:
            self.c.showPage()
            self.y = self.h - 50


def render_invoice(inv) -> bytes:
    buf = io.BytesIO()
    p = PDF(buf)
    cust_en, cust_ar, cust_vat = inv["customer"]
    cur = inv["currency"]
    title = "CREDIT NOTE" if inv["doc_type"] == "credit_note" else "TAX INVOICE"
    L = inv["layout"]
    if L in ("en_a", "en_b"):
        p.text(40, SELLER[0], 14, True); p.nl()
        p.text(40, f"VAT Registration No: {SELLER[2]}"); p.nl()
        p.text(40, "King Fahd Road, Riyadh, Kingdom of Saudi Arabia"); p.nl(26)
        p.text(40, title, 16, True); p.nl(26)
        if L == "en_a":
            p.text(40, f"Invoice No: {inv['number']}"); p.text(320, f"Invoice Date: {inv['date']:%d/%m/%Y}"); p.nl()
            p.text(40, f"PO Number: {inv['po']}"); p.text(320, f"Currency: {cur}"); p.nl(24)
            if inv.get("credit_ref"):
                p.text(40, f"Original Invoice No: {inv['credit_ref']}"); p.nl(24)
            p.text(40, "Bill To", 11, True); p.nl()
            p.text(40, f"Customer Name: {cust_en}"); p.nl()
            p.text(40, f"Customer VAT No: {cust_vat}"); p.nl(26)
        else:
            p.text(40, "Customer", 11, True); p.text(320, "Invoice Details", 11, True); p.nl()
            p.text(40, cust_en); p.text(320, f"Invoice Number: {inv['number']}"); p.nl()
            p.text(40, f"VAT No: {cust_vat}"); p.text(320, f"Date of Issue: {inv['date']:%d %B %Y}"); p.nl()
            p.text(320, f"Purchase Order No: {inv['po']}"); p.nl(26)
        p.text(40, "Description", 10, True); p.text(330, "Qty", 10, True, True); p.text(430, "Unit Price", 10, True, True)
        p.text(540, f"Amount ({cur})", 10, True, True); p.nl()
        for it in inv["items"]:
            p.text(40, it["desc"]); p.text(330, str(it["qty"]), right=True); p.text(430, money(it["price"]), right=True)
            p.text(540, money(it["amount"]), right=True); p.nl()
        p.nl(8)
        if inv["discount"]:
            p.text(330, "Subtotal"); p.text(540, money(inv["taxable"] + inv["discount"]), right=True); p.nl()
            p.text(330, "Discount"); p.text(540, money(inv["discount"]), right=True); p.nl()
        lbl_t = "Total Excluding VAT" if L == "en_a" else "Taxable Amount"
        lbl_g = "Total Including VAT" if L == "en_a" else "Total Amount Due"
        p.text(330, lbl_t); p.text(540, money(inv["taxable"]), right=True); p.nl()
        p.text(330, "VAT 15%"); p.text(540, money(inv["vat"]), right=True); p.nl()
        p.text(330, lbl_g, bold=True); p.text(540, f"{money(inv['gross'])} {cur}", right=True, bold=True); p.nl(30)
        p.text(40, "Payment terms: 30 days. Bank: Example Bank IBAN SA00 0000 0000 0000 0000 0000", 8)
    elif L == "ar":
        R = 555
        p.text(R, ar(SELLER[1]), 14, True, True); p.nl()
        p.text(R, ar("الرقم الضريبي للبائع:"), right=True); p.text(400, SELLER[2], right=True); p.nl(26)
        p.text(R, ar("فاتورة ضريبية"), 16, True, True); p.nl(26)
        p.text(R, ar("رقم الفاتورة:"), right=True); p.text(400, inv["number"], right=True); p.nl()
        p.text(R, ar("تاريخ الفاتورة:"), right=True); p.text(400, f"{inv['date']:%d/%m/%Y}", right=True); p.nl()
        p.text(R, ar("العملة:"), right=True); p.text(400, (ar("ريال سعودي") if cur == "SAR" else cur), right=True); p.nl(24)
        p.text(R, ar("اسم العميل:"), right=True); p.text(400, ar(cust_ar), right=True); p.nl()
        p.text(R, ar("الرقم الضريبي للعميل:"), right=True); p.text(400, cust_vat, right=True); p.nl(26)
        p.text(R, ar("الوصف"), 10, True, True); p.text(300, ar("الكمية"), 10, True, True); p.text(200, ar("سعر الوحدة"), 10, True, True)
        p.text(100, ar("المبلغ"), 10, True, True); p.nl()
        for it in inv["items"]:
            p.text(R, ar(it["desc_ar"]), right=True); p.text(300, str(it["qty"]), right=True)
            p.text(200, money(it["price"]), right=True); p.text(100, money(it["amount"]), right=True); p.nl()
        p.nl(8)
        p.text(R, ar("الإجمالي غير شامل ضريبة القيمة المضافة"), right=True); p.text(100, money(inv["taxable"]), right=True); p.nl()
        p.text(R, ar("ضريبة القيمة المضافة ١٥٪"), right=True); p.text(100, money(inv["vat"]), right=True); p.nl()
        p.text(R, ar("الإجمالي شامل ضريبة القيمة المضافة"), bold=True, right=True); p.text(100, money(inv["gross"]), right=True, bold=True); p.nl()
    else:  # bilingual
        p.text(40, SELLER[0], 13, True); p.text(555, ar(SELLER[1]), 13, True, True); p.nl()
        p.text(40, f"VAT No / {ar('الرقم الضريبي')}: {SELLER[2]}"); p.nl(24)
        p.text(40, f"Tax Invoice / {ar('فاتورة ضريبية')}", 15, True); p.nl(24)
        p.text(40, f"Invoice Number / {ar('رقم الفاتورة')}: {inv['number']}"); p.nl()
        p.text(40, f"Invoice Date / {ar('تاريخ الفاتورة')}: {inv['date']:%Y-%m-%d}"); p.nl()
        p.text(40, f"Currency / {ar('العملة')}: {cur}"); p.nl(22)
        p.text(40, f"Buyer Name / {ar('اسم المشتري')}: {cust_en}"); p.nl()
        p.text(40, f"Buyer VAT Number: {cust_vat}"); p.nl(24)
        p.text(40, f"Description / {ar('الوصف')}", 10, True); p.text(330, "Quantity", 10, True, True)
        p.text(430, "Unit Price", 10, True, True); p.text(540, "Line Total", 10, True, True); p.nl()
        for it in inv["items"]:
            p.text(40, it["desc"]); p.text(330, str(it["qty"]), right=True); p.text(430, money(it["price"]), right=True)
            p.text(540, money(it["amount"]), right=True); p.nl()
        p.nl(8)
        p.text(250, f"Total Taxable Amount / {ar('المبلغ الخاضع للضريبة')}"); p.text(555, money(inv["taxable"]), right=True); p.nl()
        p.text(250, f"Total VAT / {ar('إجمالي ضريبة القيمة المضافة')} (15%)"); p.text(555, money(inv["vat"]), right=True); p.nl()
        p.text(250, f"Grand Total / {ar('الإجمالي المستحق')}", bold=True); p.text(555, money(inv["gross"]), right=True, bold=True); p.nl()
    draw_qr(p, inv)
    p.c.save()
    data = buf.getvalue()
    if inv.get("scanned"):
        data = to_scanned(data)
    return data


def qr_payload(inv) -> str | None:
    if inv.get("qr") == "none":
        return None
    gross = inv["gross"] + (1000.0 if inv.get("qr") == "mismatch" else 0.0)   # tampered: printed total was altered
    tags = {1: SELLER[1] if inv["layout"] == "ar" else SELLER[0], 2: SELLER[2],
            3: f"{inv['date'].isoformat()}T10:30:00Z", 4: f"{gross:.2f}", 5: f"{inv['vat']:.2f}"}
    if inv.get("qr") == "phase2":   # dummy (non-cryptographic) bytes in the Phase 2 tag positions
        tags.update({6: "dGVzdC1oYXNoLW5vdC1yZWFs", 7: bytes(range(64)), 8: bytes(range(88))})
    return encode_tlv(tags)


def draw_qr(p: "PDF", inv) -> None:
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    payload = qr_payload(inv)
    if not payload:
        return
    size = 95
    if p.y < size + 70:
        p.c.showPage(); p.y = p.h - 50
    w = QrCodeWidget(payload, barLevel="M")
    x1, y1, x2, y2 = w.getBounds()
    d = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    d.add(w)
    renderPDF.draw(d, p.c, 40, p.y - size - 10)


def to_scanned(pdf_bytes: bytes) -> bytes:
    """Rasterise a PDF so it has no text layer (simulates a scanned invoice)."""
    import pypdfium2 as pdfium
    from reportlab.lib.utils import ImageReader
    src = pdfium.PdfDocument(pdf_bytes)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(len(src)):
        img = src[i].render(scale=2).to_pil().convert("L")
        c.drawImage(ImageReader(img), 0, 0, width=A4[0], height=A4[1])
        c.showPage()
    c.save()
    return buf.getvalue()


def build_template(path: Path, data_rows=10):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales Vouching"
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill = PatternFill("solid", fgColor="1F3864")
    ws["A1"] = "Client:"; ws["C1"] = None
    ws["A2"] = "Period / Financial Year:"; ws["C2"] = None
    ws["A3"] = "Working Paper: Sales Vouching (Occurrence, Accuracy, Cut-off)"
    ws.merge_cells("A3:P3")
    ws["A4"] = ("Objective: Vouch sales recorded in the ledger to supporting invoices and delivery notes. "
                "Instructions: Record invoice amounts EXCLUDING VAT. Explain all differences exceeding the tolerance.")
    ws.merge_cells("A4:P4")
    ws["A4"].alignment = Alignment(wrap_text=True)
    ws.row_dimensions[4].height = 32
    for c in ("A1", "A2", "A3"):
        ws[c].font = Font(bold=True)
    headers = ["S.no", "Customer Name", "Product Type", "Ledger Amount", "Invoice Amount Excluding VAT",
               "Invoice Currency", "Invoice Date", "Invoice Number", "Exchange Rate", "Amount in SAR", "Difference",
               "Goods Delivery Note Issued", "Goods Delivery Note Date",
               "Selling Price Matched with Approved Price List", "Invoice Approved", "Explanation of Material Differences"]
    hr = 6
    for j, h in enumerate(headers, start=1):
        cell = ws.cell(hr, j, h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = hdr_fill
        cell.alignment = Alignment(wrap_text=True, vertical="center", horizontal="center")
        cell.border = border
    ws.row_dimensions[hr].height = 45
    first = hr + 1
    last = first + data_rows - 1
    for r in range(first, last + 1):
        for j in range(1, 17):
            ws.cell(r, j).border = border
        ws.cell(r, 4).number_format = "#,##0.00"
        ws.cell(r, 5).number_format = "#,##0.00"
        ws.cell(r, 7).number_format = "dd/mm/yyyy"
        ws.cell(r, 9).number_format = "0.0000"
        ws.cell(r, 10, f'=IF(E{r}="","",E{r}*I{r})').number_format = "#,##0.00"
        ws.cell(r, 11, f'=IF(D{r}="","",D{r}-J{r})').number_format = "#,##0.00;[Red](#,##0.00)"
    tr = last + 1
    ws.cell(tr, 2, "Total").font = Font(bold=True)
    for col in ("D", "E", "J", "K"):
        c = ws[f"{col}{tr}"]
        c.value = f"=SUM({col}{first}:{col}{last})"
        c.font = Font(bold=True)
        c.number_format = "#,##0.00"
    ws.cell(tr + 2, 1, "Conclusion:").font = Font(bold=True)
    ws.merge_cells(start_row=tr + 2, start_column=2, end_row=tr + 2, end_column=16)
    ws.cell(tr + 3, 1, "Prepared by:"); ws.cell(tr + 3, 5, "Date:")
    ws.cell(tr + 4, 1, "Reviewed by:"); ws.cell(tr + 4, 5, "Date:")
    ws.cell(tr + 6, 1, "Tickmarks: ✓ Agreed to invoice  ∆ Agreed to delivery note  ¥ Recalculated")
    ws["D" + str(hr)].comment = Comment("Amount per sales ledger (SAR)", "Template")
    widths = [6, 28, 22, 14, 16, 10, 12, 16, 10, 14, 12, 12, 12, 16, 10, 36]
    for j, wd in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + j)].width = wd
    ws.freeze_panes = f"C{first}"
    notes = wb.create_sheet("Instructions")
    notes["A1"] = "Sample selection: all sales invoices above threshold plus random sample."
    notes["A2"] = "Sales Vouching total tested:"; notes["B2"] = "='Sales Vouching'!J" + str(tr)
    wb.save(path)


def build_template_ar(path: Path, data_rows=20):
    """Arabic, right-to-left working paper mirroring a typical firm layout: title block, objective/risk
    table, tolerance input cell, header + legend + section rows, row formulas (J, K, P), totals, work
    done, conclusion and notes. Fictitious content."""
    from openpyxl.formatting.rule import Rule
    from openpyxl.styles.differential import DifferentialStyle
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook(); ws = wb.active; ws.title = "Sales Vouching"; ws.sheet_view.rightToLeft = True
    thin = Side(style="thin"); bd = Border(left=thin, right=thin, top=thin, bottom=thin)
    ws["A1"] = "شركة الأفق النموذجية للتجارة (بيانات وهمية)"; ws.merge_cells("A1:C1")
    ws["A2"] = "للسنة المنتهية في 31 ديسمبر 2025"; ws.merge_cells("A2:C2")
    ws["A3"] = "المبيعات / الإيرادات"; ws["A4"] = "إعادة احتساب المبيعات"
    ws["F4"] = "إعداد:"; ws["G4"] = "مُعِدّ الورقة"; ws["F5"] = "مراجعة:"; ws["G5"] = "المراجع"
    for i, (k, v) in enumerate([("الهدف", "إعادة حساب المبيعات / الإيرادات"), ("الخطر", "مخاطر التحريف الجوهري في المبيعات"),
                                ("المصدر", "سجل المبيعات"), ("التأكيدات", "الحدوث والدقة"), ("النطاق", "Refer template"),
                                ("الطبيعة", "اختبار التفاصيل"), ("التوقيت", "قبل إصدار تقرير المراجعة")], start=6):
        ws.cell(i, 1, k); ws.cell(i, 2, v); ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=6)
    ws["A13"] = "حد التسامح (ريال)"; ws["B13"] = 1; ws["C13"] = "تفاوت التقريب المسموح به عند المقارنة"
    hdr = ["S.no", "اسم العميل", "نوع المنتج", "المبلغ", "المبلغ كما في الفاتورة", "عملة الفاتورة", "تاريخ الفاتورة",
           "رقم الفاتورة", "معدل الصرف", "المبلغ بالريال السعودي", "الفرق", "تم إصدار إشعار إرسال البضائع",
           "تاريخ إشعار إرسال البضائع", "مطابقة سعر البيع مع القائمة المعتمدة", "تمت الموافقة على الفاتورة",
           "سبب الاختلاف ، إذا كان جوهري"]
    legend = [None, "سجل الإيرادات", "سجل الإيرادات", "A = سجل الإيرادات", "B = الفاتورة", "الفاتورة", "الفاتورة", "الفاتورة",
              "C = Reputable Site", "D = B*C", "E = A - D", "نعم / لا", "GDN", "نعم / لا", "Approval", "Description"]
    for j, (h, l) in enumerate(zip(hdr, legend), start=1):
        ws.cell(15, j, h).font = Font(bold=True); ws.cell(15, j).border = bd
        ws.cell(16, j, l).border = bd
    ws["A17"] = "Target Testing"; ws.merge_cells("A17:B17")
    first, last = 18, 18 + data_rows - 1
    for r in range(first, last + 1):
        ws.cell(r, 1, r - first + 1); ws.cell(r, 2, "عميل نموذجي"); ws.cell(r, 3, "المبيعات")
        ws.cell(r, 4, 1000.0); ws.cell(r, 5, 1000.0); ws.cell(r, 6, "SAR"); ws.cell(r, 8, f"SMP/{r}"); ws.cell(r, 9, 1)
        ws.cell(r, 10, f"=E{r}*I{r}"); ws.cell(r, 11, f'=IF(D{r}="","",D{r}-J{r})')
        ws.cell(r, 16, f'=IF(D{r}="","أدخل مبلغ سجل الإيرادات",IF(ABS(K{r})<=$B$13,"لا يوجد اختلافات","يوجد اختلاف - يُراجع"))')
        ws.cell(r, 7).number_format = "yyyy/mm/dd"
        for j in range(1, 17):
            ws.cell(r, j).border = bd
    tr = last + 1
    ws.cell(tr, 1, "الإجمالي"); ws.merge_cells(start_row=tr, start_column=1, end_row=tr, end_column=3)
    for col in "DEJK":
        ws[f"{col}{tr}"] = f"=SUM({col}{first}:{col}{last})"
    dv = DataValidation(type="list", formula1='"نعم,لا"', allow_blank=True)
    dv.add(f"L{first}:L{last}"); dv.add(f"N{first}:N{last}"); ws.add_data_validation(dv)
    ws.conditional_formatting.add(f"H{first}:H{last + 70}", Rule(type="duplicateValues", dxf=DifferentialStyle(font=Font(color="9C0006"))))
    ws.cell(tr + 2, 1, "Work Done"); ws.merge_cells(start_row=tr + 2, start_column=1, end_row=tr + 2, end_column=5)
    ws.cell(tr + 3, 1, "الحصول على سجل المبيعات من العميل ؛")
    ws.cell(tr + 5, 1, "الخلاصة "); ws.merge_cells(start_row=tr + 5, start_column=1, end_row=tr + 5, end_column=2)
    ws.cell(tr + 7, 1, "ملاحظات على الورقة: أعمدة إشعار إرسال البضائع ومطابقة السعر والموافقة تُترك لمُعِدّ الورقة.")
    ws.freeze_panes = "C18"
    wb.save(path)


def build_ledger(invs, out_dir: Path, rng):
    rows = []
    fx = {"SAR": 1.0, "USD": 3.75, "EUR": 4.05}
    for inv in invs:
        sign = -1 if inv["doc_type"] == "credit_note" else 1
        amt = round(inv["taxable"] * fx[inv["currency"]], 2) * sign
        rows.append({"Invoice No": inv["number"], "Customer": inv["customer"][0], "Date": inv["date"].isoformat(),
                     "Amount (SAR)": amt, "Currency": "SAR"})
    # planted exceptions
    rows[2]["Amount (SAR)"] = round(rows[2]["Amount (SAR)"] + 1500, 2)             # amount mismatch
    rows[8]["Amount (SAR)"] = round(invs[8]["gross"], 2)                           # ledger includes VAT
    rows[14]["Customer"] = "Unrelated Party Holding"                               # customer mismatch
    rows[17]["Invoice No"] = "INV2025-0018"                                        # formatting variant -> same key
    del_row = rows.pop(25)                                                         # invoice 26 not in ledger
    rows[27]["Invoice No"] = ""                                                    # missing number -> possible match
    rows.append({"Invoice No": "INV-2025-0999", "Customer": "Najd Construction Company", "Date": "2025-11-11",
                 "Amount (SAR)": 18500.0, "Currency": "SAR"})                      # ledger entry without invoice
    rows.append(dict(rows[5]))                                                     # duplicate ledger posting
    rows[0]["Amount (SAR)"] = round(rows[0]["Amount (SAR)"] + 0.40, 2)            # within tolerance
    with open(out_dir / "Sales_Ledger_FY2025.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    wb = Workbook(); ws = wb.active; ws.title = "Sales Ledger"
    ws.append(["Sales Ledger - FY2025 (Fictitious)"]); ws.append([])
    ws.append(list(rows[0].keys()))
    for r in rows:
        ws.append(list(r.values()))
    wb.save(out_dir / "Sales_Ledger_FY2025.xlsx")
    return del_row


def main(out=None):
    out_dir = Path(out or Path(__file__).parent / "pack")
    (out_dir / "invoices").mkdir(parents=True, exist_ok=True)
    rng = random.Random(20251231)
    invs = gen_invoices(rng)
    truth = []
    for inv in invs:
        name = f"{inv['seq']:02d}_{inv['number']}_{inv['layout']}{'_scanned' if inv.get('scanned') else ''}.pdf"
        (out_dir / "invoices" / name).write_bytes(render_invoice(inv))
        truth.append({"file": name, "invoice_number": inv["number"], "invoice_date": inv["date"].isoformat(),
                      "buyer_name": inv["customer"][0], "buyer_name_ar": inv["customer"][1], "currency": inv["currency"],
                      "taxable_amount": inv["taxable"], "vat_amount": inv["vat"], "gross_amount": inv["gross"],
                      "doc_type": inv["doc_type"], "layout": inv["layout"], "scanned": bool(inv.get("scanned")),
                      "qr": inv["qr"]})
    build_template(out_dir / "Sales_Vouching_Invoices_Sample.xlsx")
    build_template_ar(out_dir / "Sales_Vouching_AR_Template.xlsx")
    build_ledger(invs, out_dir, rng)
    (out_dir / "ground_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {len(invs)} invoices, template and ledger to {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
