"""Deterministic, label-driven invoice field extraction for PDFs that have a text layer
(or OCR output). Amounts are never chosen by position: taxable / VAT / gross candidates are
collected from their labels and the combination that reconciles arithmetically is selected."""
from __future__ import annotations

import itertools
import threading
import re
import unicodedata

from .normalize import (find_amounts, has_arabic, norm_text, parse_amount, parse_date, reverse_rtl,
                        valid_saudi_vat)
from .pdf_text import DocText, Line

# ---------------------------------------------------------------- label dictionaries (normalised)
L = {
    "invoice_number": [r"invoice\s*(?:no|number|num|#)\.?", r"tax invoice\s*(?:no|number)", r"inv\.?\s*(?:no|#)",
                       r"bill\s*(?:no|number)", r"document\s*(?:no|number)", r"credit note\s*(?:no|number)",
                       r"رقم الفاتوره", r"رقم فاتوره", r"رقم الاشعار", r"رقم المستند"],
    "invoice_date": [r"invoice\s*date", r"date of issue", r"issue\s*date", r"tax invoice date", r"date of supply",
                     r"تاريخ الفاتوره", r"تاريخ الاصدار", r"تاريخ اصدار الفاتوره", r"^date\b", r"\bdate\s*:"],
    "seller_name": [r"seller\s*name", r"supplier\s*name", r"vendor\s*name", r"company\s*name", r"اسم البائع",
                    r"اسم المورد", r"اسم الشركه"],
    "buyer_name": [r"customer\s*name", r"buyer\s*name", r"client\s*name", r"bill\s*to", r"sold\s*to", r"invoice\s*to",
                   r"اسم العميل", r"اسم المشتري", r"فاتوره الي", r"^customer\b", r"^buyer\b", r"^client\b"],
    "vat_number": [r"vat\s*(?:registration\s*)?(?:no|number|#|reg)", r"\btrn\b", r"tax\s*(?:registration|id)\s*(?:no|number)?",
                   r"الرقم الضريبي", r"رقم التسجيل الضريبي", r"الرقم الضريبي للعميل", r"الرقم الضريبي للبائع"],
    "currency": [r"currency", r"العمله"],
    "po": [r"purchase\s*order\s*(?:no|number|#)?", r"\bp\.?\s*o\.?\s*(?:no|number|#)", r"رقم امر الشراء", r"امر الشراء"],
    "credit_ref": [r"original invoice\s*(?:no|number|ref)", r"reference invoice", r"against invoice",
                   r"credit note\s*(?:ref|reference)", r"رقم الفاتوره الاصليه", r"مرجع الفاتوره"],
    "discount": [r"discount", r"\bخصم\b", r"الخصم"],
    "taxable": [r"total\s*(?:amount\s*)?\(?\s*excl(?:uding|\.)?\s*(?:of\s*)?vat\)?", r"total\s*excl", r"subtotal\s*excl",
                r"total\s*taxable\s*amount", r"taxable\s*amount", r"amount\s*before\s*vat", r"net\s*amount",
                r"total\s*before\s*(?:vat|tax)", r"sub\s*-?\s*total", r"total\s*\(?\s*excluding",
                r"الاجمالي غير شامل", r"المجموع غير شامل", r"الاجمالي الخاضع للضريبه", r"المبلغ الخاضع للضريبه",
                r"المجموع قبل الضريبه", r"الاجمالي قبل الضريبه", r"الاجمالي بدون الضريبه", r"المجموع الفرعي"],
    "vat_amount": [r"total\s*vat", r"vat\s*amount", r"vat\s*\(?\s*\d{1,2}(?:\.\d+)?\s*%\s*\)?", r"^vat\b(?!\s*(?:no|number|reg|#))",
                   r"value\s*added\s*tax(?!\s*(?:no|number|reg))", r"tax\s*amount",
                   r"اجمالي ضريبه القيمه المضافه", r"مبلغ ضريبه القيمه المضافه", r"ضريبه القيمه المضافه(?!\s*رقم)",
                   r"مبلغ الضريبه"],
    "gross": [r"total\s*(?:amount\s*)?\(?\s*incl(?:uding|\.)?\s*(?:of\s*)?vat\)?", r"total\s*amount\s*due", r"grand\s*total",
              r"total\s*due", r"invoice\s*total", r"amount\s*due", r"total\s*payable", r"net\s*payable",
              r"total\s*with\s*vat", r"الاجمالي شامل", r"المجموع شامل", r"المبلغ المستحق", r"الاجمالي المستحق",
              r"اجمالي المبلغ المستحق", r"الاجمالي مع الضريبه", r"صافي المستحق"],
    "total_generic": [r"^total\b", r"\btotal\s*:", r"^الاجمالي$", r"^المجموع$"],
}
SELLER_SECTION = [r"^seller\b", r"^supplier\b", r"^from\b", r"^vendor\b", r"^البائع", r"^المورد", r"بيانات البائع", r"seller details", r"supplier details"]
BUYER_SECTION = [r"^buyer\b", r"^customer\b", r"^bill\s*to", r"^sold\s*to", r"^client\b", r"^المشتري", r"^العميل",
                 r"بيانات العميل", r"بيانات المشتري", r"buyer details", r"customer details"]
ITEM_HEADER_DESC = [r"description", r"\bitem", r"product", r"service", r"particulars", r"الوصف", r"البيان", r"الصنف", r"المنتج"]
ITEM_HEADER_QTY = [r"\bqty\b", r"quantity", r"الكميه"]
DOC_TYPES = [("credit_note", [r"credit\s*note", r"اشعار دائن"]),
             ("debit_note", [r"debit\s*note", r"اشعار مدين"]),
             ("proforma", [r"pro\s*-?forma"]),
             ("quotation", [r"quotation", r"عرض سعر"]),
             ("simplified_tax_invoice", [r"simplified\s*tax\s*invoice", r"فاتوره ضريبيه مبسطه"]),
             ("tax_invoice", [r"tax\s*invoice", r"فاتوره ضريبيه"]),
             ("commercial_invoice", [r"commercial\s*invoice", r"^invoice\b", r"فاتوره"])]
CURRENCIES = ["SAR", "USD", "EUR", "GBP", "AED", "KWD", "BHD", "QAR", "OMR", "EGP", "JPY", "CNY", "INR", "CHF", "JOD"]
CURRENCY_WORDS = {"ريال سعودي": "SAR", "ر.س": "SAR", "ريال": "SAR", "دولار امريكي": "USD", "دولار": "USD",
                  "يورو": "EUR", "درهم اماراتي": "AED", "us$": "USD", "€": "EUR", "£": "GBP", "saudi riyal": "SAR",
                  "us dollar": "USD", "euro": "EUR"}

_COMPILED: dict[str, list[re.Pattern]] = {}


def _pats(key_or_list):
    if isinstance(key_or_list, str):
        if key_or_list not in _COMPILED:
            _COMPILED[key_or_list] = [re.compile(p) for p in L[key_or_list]]
        return _COMPILED[key_or_list]
    return [re.compile(p) for p in key_or_list]


def _match(line: Line, pats) -> tuple[re.Match | None, bool]:
    """Return (match, visual_order). Tries logical text first then RTL-reversed text."""
    for p in pats:
        m = p.search(line.norm)
        if m:
            return m, False
    if line.norm_rev != line.norm:
        for p in pats:
            m = p.search(line.norm_rev)
            if m:
                return m, True
    return None, False


def segments(line: Line, gap: float = 24.0) -> list[Line]:
    """Split a visual row into column cells using horizontal gaps."""
    groups, cur = [], [line.words[0]]
    for w in line.words[1:]:
        if w.x0 - cur[-1].x1 > gap:
            groups.append(cur); cur = [w]
        else:
            cur.append(w)
    groups.append(cur)
    return [Line(page=line.page, words=g, page_width=line.page_width, page_height=line.page_height) for g in groups]


_SEP = re.compile(r"^[\s:：/|\-#.]+$")


def _strip_label(seg: Line, pats):
    """Return (value_words, visual) after removing the label words matched by pats, or None."""
    m, visual = _match(seg, pats)
    if not m:
        return None
    words = list(seg.words)
    target = m.end()
    if not visual:
        for k in range(1, len(words) + 1):
            if len(norm_text(" ".join(w.text for w in words[:k]))) >= target:
                rest = words[k:]
                break
        else:
            rest = []
    else:
        for k in range(1, len(words) + 1):
            if len(norm_text(reverse_rtl(" ".join(w.text for w in words[-k:])))) >= target:
                rest = words[:-k]
                break
        else:
            rest = []
    rest = [w for w in rest if not _SEP.match(w.text)]
    return rest, visual


def _script_runs(words):
    runs = []
    for w in words:
        a = has_arabic(w.text)
        if runs and runs[-1][0] == a:
            runs[-1][1].append(w)
        else:
            runs.append((a, [w]))
    return runs


def _value_after_label(seg: Line, pats):
    res = _strip_label(seg, pats)
    if res is None:
        return None
    rest, visual = res
    rest = [w for w in rest if not _SEP.match(w.text)]
    # bilingual labels: "Invoice Number / رقم الفاتورة: X" -> drop a leading/trailing run of the other
    # script if that run is itself a label for the same field
    for _ in range(2):
        runs = _script_runs(rest)
        if len(runs) < 2:
            break
        for idx in (0, -1):
            run = runs[idx][1]
            sub = Line(page=seg.page, words=run, page_width=seg.page_width, page_height=seg.page_height)
            if _match(sub, pats)[0]:
                ids = {id(w) for w in run}
                rest = [w for w in rest if id(w) not in ids and not _SEP.match(w.text)]
                break
        else:
            break
    rest = [w for w in rest if not _SEP.match(w.text.strip(":"))] if rest else rest
    return rest, visual


_TL = threading.local()


def _text_of(words, visual: bool) -> str:
    s = " ".join(w.text for w in words)
    if has_arabic(s) and (visual or getattr(_TL, 'visual', False) or _looks_visual(unicodedata.normalize("NFKC", s))):
        s = reverse_rtl(s)
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"\s+", " ", s).strip(" :-|/")


_COMMON_AR = ["شركه", "مؤسسه", "المحدوده", "للتجاره", "التجاريه", "العربيه", "السعوديه", "الشركه", "خدمات",
              "للخدمات", "للمقاولات", "الاحمر", "الخليج", "الرياض", "نجد", "الصحراء", "الشرقيه"]


def _looks_visual(s: str) -> bool:
    n = norm_text(s)
    fwd = sum(1 for w in _COMMON_AR if norm_text(w) in n)
    rev = sum(1 for w in _COMMON_AR if norm_text(w)[::-1] in n)
    return rev > fwd


def _field(value, conf, line: Line | None = None, words=None, method="rule", note=None, src=None):
    bbox = None
    page = None
    if line is not None:
        page = line.page
        ws = words or line.words
        bbox = [round(min(w.x0 for w in ws), 1), round(min(w.top for w in ws), 1),
                round(max(w.x1 for w in ws), 1), round(max(w.bottom for w in ws), 1)]
    return {"value": value, "confidence": round(conf, 3), "page": page, "bbox": bbox,
            "source_text": src if src is not None else (line.text if line else None), "method": method, "note": note}


_PCT = re.compile(r"\d{1,3}(?:\.\d+)?\s*[%٪]|[%٪]\s*\d{1,3}")


def _line_amounts(line: Line):
    """Monetary amounts on a line with the word(s) they came from. Excludes percentages, VAT numbers,
    dates, and long digit identifiers."""
    out = []
    for w in line.words:
        t = unicodedata.normalize("NFKC", w.text)
        if _PCT.search(t) or re.search(r"\d{1,4}[/-]\d{1,2}[/-]\d{2,4}", t):
            continue
        digits = re.sub(r"\D", "", t)
        if len(digits) >= 10 and "," not in t and "." not in t:
            continue
        for v, raw in find_amounts(t):
            if raw.strip() and len(re.sub(r"\D", "", raw)) <= 13:
                out.append((v, w))
    return out


def _next_line(lines: list[Line], idx: int, max_gap=28):
    ln = lines[idx]
    for j in range(idx + 1, min(idx + 4, len(lines))):
        nx = lines[j]
        if nx.page != ln.page or nx.top - ln.top > max_gap:
            return None
        return nx
    return None


# ---------------------------------------------------------------------------------- main extractor
def extract(doc: DocText) -> dict:
    lines = doc.lines
    fields: dict[str, dict] = {}
    notes: list[str] = []
    full_norm = "\n".join(l.norm + " " + l.norm_rev for l in lines)

    # document classification
    doc_type = None
    for dt, pats in DOC_TYPES:
        if any(re.search(p, full_norm, re.M) for p in pats):
            doc_type = dt
            break
    arabic_chars = sum(1 for c in "".join(l.text for l in lines) if has_arabic(c))
    latin_chars = sum(1 for c in "".join(l.text for l in lines) if c.isascii() and c.isalpha())
    language = "ar" if arabic_chars and not latin_chars else "en" if latin_chars and not arabic_chars else \
        "bilingual" if arabic_chars and latin_chars else "unknown"

    vocab = ["الفاتوره", "ضريبه", "القيمه", "المضافه", "العميل", "الاجمالي", "رقم", "تاريخ", "الضريبي", "شركه"]
    fwd = sum(1 for l in lines for w in vocab if w in l.norm)
    rev = sum(1 for l in lines for w in vocab if l.norm_rev != l.norm and w in l.norm_rev)
    _TL.visual = rev > fwd

    cands = {k: [] for k in ("taxable", "vat_amount", "gross", "discount", "total_generic")}
    section = None
    vat_numbers: list[tuple[str, Line, str | None]] = []
    rate_found: list[tuple[float, Line]] = []
    for i, ln in enumerate(lines):
        # section tracking for seller/buyer
        if _match(ln, _pats(SELLER_SECTION))[0]:
            section = "seller"
        elif _match(ln, _pats(BUYER_SECTION))[0]:
            section = "buyer"

        # VAT registration numbers (15 digits)
        for w in ln.words:
            d = re.sub(r"\D", "", unicodedata.normalize("NFKC", w.text).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
            if len(d) == 15:
                ctx = "buyer" if re.search(r"customer|buyer|client|العميل|المشتري", ln.norm + ln.norm_rev) else \
                    "seller" if re.search(r"seller|supplier|vendor|البائع|المورد", ln.norm + ln.norm_rev) else section
                vat_numbers.append((d, ln, ctx))

        for m in re.finditer(r"(?<![\d.,])(\d{1,2}(?:\.\d+)?)\s*[%٪]|[%٪]\s*(\d{1,2}(?:\.\d+)?)(?![\d.,])", ln.norm):
            if re.search(r"vat|tax|ضريبه", ln.norm + ln.norm_rev):
                rate_found.append((float(m.group(1) or m.group(2)), ln))

        # amount labels (most specific first)
        for key in ("gross", "taxable", "vat_amount", "discount", "total_generic"):
            m, visual = _match(ln, _pats(key))
            if not m:
                continue
            if key == "total_generic" and any(_match(ln, _pats(k))[0] for k in ("gross", "taxable", "vat_amount")):
                continue
            if key == "vat_amount" and _match(ln, _pats("vat_number"))[0]:
                continue
            amts = _line_amounts(ln)
            src_line = ln
            conf = 0.95
            if not amts:
                nx = _next_line(lines, i)
                if nx is not None and not any(_match(nx, _pats(k))[0] for k in ("gross", "taxable", "vat_amount")):
                    amts = _line_amounts(nx)
                    src_line, conf = nx, 0.8
            for v, w in amts:
                cands[key].append((v, src_line, w, conf / (1 if len(amts) == 1 else 1.15)))
            break

    # ---------------- text fields
    def first_text_field(key, validator=None, conf=0.92):
        pats = _pats(key)
        other = [k for k in L if k != key]
        for i, ln in enumerate(lines):
            if not _match(ln, pats)[0]:
                continue
            segs = segments(ln)
            for si, seg in enumerate(segs):
                if key in ("buyer_name", "seller_name") and _match(seg, _pats("vat_number"))[0]:
                    continue
                res = _value_after_label(seg, pats)
                if res is None:
                    continue
                ws, visual = res
                cands = []
                if ws:
                    cands.append((ws, seg, conf))
                # adjacent cell on the same row: right of an LTR label, left of an Arabic label
                arabic_label = has_arabic(seg.text)
                nb = si - 1 if arabic_label else si + 1
                gap = (seg.bbox[0] - segs[nb].bbox[2]) if arabic_label else (segs[nb].bbox[0] - seg.bbox[2]) \
                    if 0 <= nb < len(segs) else 1e9
                if 0 <= nb < len(segs) and gap < 120 and not _match(segs[nb], pats)[0] and \
                        not any(_match(segs[nb], _pats(k))[0] for k in other):
                    cands.append((segs[nb].words, segs[nb], conf * 0.95))
                # value in the cell directly below the label (heading style layouts)
                nx = _next_line(lines, i)
                if nx is not None:
                    x0, x1 = seg.bbox[0], seg.bbox[2]
                    for nseg in segments(nx):
                        b = nseg.bbox
                        if not (b[0] <= x1 + 5 and b[2] >= x0 - 5):
                            continue
                        sub = _value_after_label(nseg, pats)
                        if sub is not None:
                            if sub[0] and not ws and not _match(nseg, _pats("vat_number"))[0]:
                                cands.insert(0, (sub[0], nseg, conf))
                        elif not any(_match(nseg, _pats(k))[0] for k in other):
                            cands.append((nseg.words, nseg, conf * 0.85))
                        break
                for words, src, c in cands:
                    txt = _text_of(words, visual)
                    if validator:
                        val = validator(txt, words)
                        if val is not None:
                            return _field(val[0], c, src, val[1] or None)
                    elif txt:
                        return _field(txt, c, src, words)
        return None

    def inv_no_validator(txt, ws):
        best = None
        for w in ws:
            t = unicodedata.normalize("NFKC", w.text).strip(":#")
            if re.search(r"\d", t) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-/_.]*", t) and not parse_date(t)[0]:
                if best is None or len(t) > len(best[0]):
                    best = (t, [w])
        return best

    def date_validator(txt, ws):
        d, raw, note = parse_date(txt)
        if d:
            return d.isoformat(), [w for w in ws if any(ch.isdigit() for ch in w.text)]
        return None

    fields["invoice_number"] = first_text_field("invoice_number", inv_no_validator, 0.95) or _field(None, 0)
    fields["invoice_date"] = first_text_field("invoice_date", date_validator, 0.95) or _field(None, 0)
    if fields["invoice_date"]["value"] is None:
        for ln in lines:  # any date on page 1 as a weak fallback, clearly flagged
            d, raw, note = parse_date(ln.text)
            if d and ln.page == 1:
                fields["invoice_date"] = _field(d.isoformat(), 0.45, ln, note="Unlabelled date - verify it is the invoice date")
                break
    if fields["invoice_date"]["value"]:
        _, _, dnote = parse_date(fields["invoice_date"]["source_text"] or "")
        if dnote:
            fields["invoice_date"]["note"] = dnote
            fields["invoice_date"]["confidence"] = min(fields["invoice_date"]["confidence"], 0.8)

    fields["buyer_name"] = first_text_field("buyer_name", lambda t, ws: (t, ws) if len(t) > 2 and not re.fullmatch(r"[\d\s:/-]+", t) else None) or _field(None, 0)
    fields["seller_name"] = first_text_field("seller_name", lambda t, ws: (t, ws) if len(t) > 2 else None) or _field(None, 0)
    if fields["seller_name"]["value"] is None and lines:
        top = next((l for l in lines if l.page == 1 and len(l.text) > 3 and not _match(l, [re.compile(p) for _, ps in DOC_TYPES for p in ps])[0]), None)
        if top is not None:
            top = segments(top)[0]
            fields["seller_name"] = _field(_text_of(top.words, False), 0.5, top, note="Taken from document header - verify")

    # VAT numbers -> seller / buyer
    seller_vat = next(((v, l) for v, l, c in vat_numbers if c == "seller"), None)
    buyer_vat = next(((v, l) for v, l, c in vat_numbers if c == "buyer"), None)
    unassigned = [(v, l) for v, l, c in vat_numbers if c not in ("seller", "buyer")]
    if not seller_vat and unassigned:
        seller_vat = unassigned.pop(0)
    if not buyer_vat and unassigned:
        buyer_vat = unassigned.pop(0)
    for key, item in (("seller_vat", seller_vat), ("buyer_vat", buyer_vat)):
        if item:
            v, ln = item
            fields[key] = _field(v, 0.93 if valid_saudi_vat(v) else 0.7, ln, [w for w in ln.words if v in re.sub(r"\D", "", w.text)] or None)
        else:
            fields[key] = _field(None, 0)

    # currency
    cur = None
    labelled = first_text_field("currency", lambda t, ws: next(((c, ws) for c in CURRENCIES if c in t.upper()), None))
    if labelled:
        cur = labelled
    else:
        counts = {}
        for ln in lines:
            up = unicodedata.normalize("NFKC", ln.text).upper()
            for c in CURRENCIES:
                if re.search(rf"\b{c}\b", up):
                    counts.setdefault(c, [0, ln])[0] += 1
            for wd, c in CURRENCY_WORDS.items():
                if norm_text(wd) in ln.norm or norm_text(wd) in ln.norm_rev:
                    counts.setdefault(c, [0, ln])[0] += 1
        if counts:
            c, (n, ln) = max(counts.items(), key=lambda kv: kv[1][0])
            conf = 0.85 if len(counts) == 1 else 0.6
            cur = _field(c, conf, ln, note=None if len(counts) == 1 else f"Multiple currencies on document: {sorted(counts)}")
    fields["currency"] = cur or _field(None, 0, note="Currency not stated on document")

    fields["purchase_order"] = first_text_field("po", lambda t, ws: next(((unicodedata.normalize('NFKC', w.text), [w]) for w in ws if re.search(r"\d", w.text)), None), 0.85) or _field(None, 0)
    fields["credit_note_ref"] = first_text_field("credit_ref", inv_no_validator, 0.85) or _field(None, 0)

    # ---------------- line items
    items = _line_items(lines)
    if items:
        descs = [it["description"] for it in items if it.get("description")]
        first = items[0]
        fields["description"] = _field("; ".join(dict.fromkeys(descs))[:250] or None, 0.85 if descs else 0, None,
                                       src="; ".join(descs)[:250])
        fields["description"].update(page=first["page"], bbox=first["bbox"])
        if len(items) == 1:
            fields["quantity"] = {**fields["description"], "value": first.get("quantity"), "confidence": 0.85 if first.get("quantity") is not None else 0}
            fields["unit_price"] = {**fields["description"], "value": first.get("unit_price"), "confidence": 0.85 if first.get("unit_price") is not None else 0}
        else:
            q = sum(it["quantity"] for it in items if it.get("quantity") is not None)
            fields["quantity"] = {**fields["description"], "value": q, "confidence": 0.7, "note": f"Sum of {len(items)} line items"}
            fields["unit_price"] = {**fields["description"], "value": None, "confidence": 0, "note": "Multiple line items - see line item detail"}
    else:
        for k in ("description", "quantity", "unit_price"):
            fields[k] = _field(None, 0)

    # ---------------- amounts: reconcile
    amt_fields, amt_notes = _reconcile(cands, rate_found, items)
    fields.update(amt_fields)
    notes.extend(amt_notes)

    return {"fields": fields, "doc_type": doc_type, "language": language, "line_items": items, "notes": notes}


def _line_items(lines: list[Line]) -> list[dict]:
    hdr_idx = None
    for i, ln in enumerate(lines):
        if _match(ln, _pats(ITEM_HEADER_DESC))[0] and _match(ln, _pats(ITEM_HEADER_QTY))[0]:
            hdr_idx = i
            break
    if hdr_idx is None:
        return []
    items = []
    stop_keys = ("taxable", "gross", "vat_amount", "total_generic", "discount")
    for ln in lines[hdr_idx + 1: hdr_idx + 60]:
        if any(_match(ln, _pats(k))[0] for k in stop_keys):
            break
        amts = _line_amounts(ln)
        if len(amts) < 2:
            continue
        vals = [v for v, _ in amts]
        qty = price = amount = None
        for a, b, c in itertools.permutations(range(len(vals)), 3):
            if vals[c] and abs(vals[a] * vals[b] - vals[c]) <= max(0.02, abs(vals[c]) * 1e-6) and vals[a] <= vals[b]:
                qty, price, amount = vals[a], vals[b], vals[c]
                # If a VAT-inclusive line total exists, keep the net (smallest consistent) amount
                break
        used_vals = {qty, price, amount} if amount is not None else set(vals)
        num_words = {id(w) for v, w in amts if v in used_vals}
        desc_words = [w for w in ln.words if id(w) not in num_words]
        desc = _text_of(desc_words, False) if desc_words else None
        if desc and re.fullmatch(r"(sar|usd|eur|ر\.س)?", desc.lower()):
            desc = None
        items.append({"description": desc, "quantity": qty, "unit_price": price, "amount": amount,
                      "page": ln.page, "bbox": ln.bbox, "source_text": ln.text})
    return items


def _reconcile(cands, rate_found, items):
    notes = []
    T = [(v, ln, w, c) for v, ln, w, c in cands["taxable"] if v >= 0 or True]
    V = cands["vat_amount"]
    G = cands["gross"] or [(v, ln, w, c * 0.75) for v, ln, w, c in cands["total_generic"]]
    D = cands["discount"]
    rates = sorted({r for r, _ in rate_found if r in (0, 5, 15)} or {r for r, _ in rate_found})
    disc_vals = [abs(d[0]) for d in D] or [0.0]

    def f(c, conf, method="rule", note=None, value=None):
        v, ln, w, _ = c
        out = _field(round(v if value is None else value, 2), conf, ln, [w], method, note)
        return out

    best = None
    # 1) full triple consistent: t (- discount) + v == g
    for t, v, g in itertools.product(T, V, G):
        for dsc in disc_vals:
            net = round(t[0] - dsc, 2) if dsc else t[0]
            if abs(net + v[0] - g[0]) <= 0.02 and g[0] > 0:
                score = t[3] + v[3] + g[3] - (0.05 if dsc else 0)
                if best is None or score > best[0]:
                    best = (score, t, v, g, dsc)
    if best:
        _, t, v, g, dsc = best
        rate = round(v[0] / (t[0] - dsc) * 100, 2) if (t[0] - dsc) else None
        out = {"taxable_amount": f(t, 0.97, note=None if not dsc else f"Subtotal {t[0]:,.2f} less discount {dsc:,.2f}",
                                   value=round(t[0] - dsc, 2)),
               "vat_amount": f(v, 0.97), "gross_amount": f(g, 0.97)}
        out["discount"] = _disc_field(D, dsc)
        out["vat_rate"] = _rate_field(rate, rates, rate_found)
        return out, notes

    # 2) t & v consistent with a stated rate
    for t, v in itertools.product(T, V):
        for r in rates or [15]:
            if abs(t[0] * r / 100 - v[0]) <= 0.05 + t[0] * 0.0001:
                out = {"taxable_amount": f(t, 0.88), "vat_amount": f(v, 0.88),
                       "gross_amount": {**f(t, 0.6, "derived", "Gross derived as taxable + VAT (no labelled total found)"),
                                        "value": round(t[0] + v[0], 2)}}
                out["discount"] = _disc_field(D, 0)
                out["vat_rate"] = _rate_field(r, rates, rate_found)
                return out, notes
    # 3) v & g: derive taxable
    for v, g in itertools.product(V, G):
        t_val = round(g[0] - v[0], 2)
        if t_val > 0:
            r = round(v[0] / t_val * 100, 2)
            plausible = any(abs(r - x) < 0.1 for x in (0, 5, 15))
            out = {"taxable_amount": {**f(g, 0.72 if plausible else 0.45, "derived",
                                         f"Taxable derived as gross {g[0]:,.2f} - VAT {v[0]:,.2f}; no labelled net total"),
                                      "value": t_val},
                   "vat_amount": f(v, 0.85), "gross_amount": f(g, 0.85)}
            out["discount"] = _disc_field(D, 0)
            out["vat_rate"] = _rate_field(r, rates, rate_found)
            return out, notes
    # 4) t & g
    for t, g in itertools.product(T, G):
        if g[0] >= t[0] > 0:
            out = {"taxable_amount": f(t, 0.85), "gross_amount": f(g, 0.85),
                   "vat_amount": {**f(g, 0.6, "derived", "VAT derived as gross - taxable"), "value": round(g[0] - t[0], 2)}}
            out["discount"] = _disc_field(D, 0)
            out["vat_rate"] = _rate_field(round((g[0] - t[0]) / t[0] * 100, 2), rates, rate_found)
            return out, notes
    # 5) single labelled values only
    out = {"taxable_amount": _field(None, 0), "vat_amount": _field(None, 0), "gross_amount": _field(None, 0)}
    if T:
        t = max(T, key=lambda c: c[3])
        out["taxable_amount"] = f(t, 0.7 if len({c[0] for c in T}) == 1 else 0.45, note="Not arithmetically verified against VAT and total")
    if V:
        out["vat_amount"] = f(max(V, key=lambda c: c[3]), 0.6)
    if G:
        out["gross_amount"] = f(max(G, key=lambda c: c[3]), 0.6)
    if not T and G and rates and len(rates) == 1:
        g = max(G, key=lambda c: c[3])
        out["taxable_amount"] = {**f(g, 0.4, "derived", f"Taxable back-calculated from gross at {rates[0]}% - must be verified"),
                                 "value": round(g[0] / (1 + rates[0] / 100), 2)}
    if not T and not G and items and all(it.get("amount") is not None for it in items):
        s = round(sum(it["amount"] for it in items), 2)
        out["taxable_amount"] = {**_field(s, 0.4, None, method="derived", note="Sum of line items - no labelled totals found"),
                                 "page": items[0]["page"], "bbox": items[0]["bbox"]}
    out["discount"] = _disc_field(D, 0)
    out["vat_rate"] = _rate_field(None, rates, rate_found)
    notes.append("Invoice totals could not be fully reconciled (taxable + VAT = total)")
    return out, notes


def _disc_field(D, used):
    if not D:
        return _field(None, 0)
    v, ln, w, c = max(D, key=lambda d: d[3])
    return _field(round(abs(v), 2), 0.9 if used else 0.75, ln, [w])


def _rate_field(computed, stated, rate_found):
    if stated:
        r, ln = next(((r, l) for r, l in rate_found if r in stated), rate_found[0])
        conf = 0.95 if computed is None or abs(computed - r) < 0.1 else 0.6
        note = None if computed is None or abs(computed - r) < 0.1 else f"Stated rate {r}% differs from computed {computed}%"
        return _field(r, conf, ln, note=note)
    if computed is not None:
        return _field(computed, 0.7, None, method="derived", note="Rate computed from VAT / taxable amount")
    return _field(None, 0)
