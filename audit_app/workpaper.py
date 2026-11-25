"""Populate the auditor's Excel working-paper template.

The uploaded template is never modified: a copy is written to the outputs folder. Existing sheet
names, headings, column order, formulas, formatting, merged cells, instructions and the totals /
conclusion sections are preserved. If the sample needs more rows than the template provides, rows
are inserted above the totals row and every reference (formulas in all sheets, merged cells,
validations, conditional formats, defined names) is shifted, with SUM ranges extended."""
from __future__ import annotations

import re
import shutil
from copy import copy
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.comments import Comment
from openpyxl.formula.tokenizer import Tokenizer
from openpyxl.utils import column_index_from_string, get_column_letter

from . import config, db, engine
from .extraction.normalize import norm_text, to_date

HEADER_SYNONYMS = {
    "A": ["s.no", "s no", "sno", "serial", "sr", "sr.", "#", "no.", "م", "رقم تسلسلي"],
    "B": ["customer name", "customer", "client name", "اسم العميل", "العميل"],
    "C": ["product type", "product", "description", "نوع المنتج"],
    "D": ["ledger amount", "amount per ledger", "ledger", "المبلغ حسب الدفتر"],
    "E": ["invoice amount excluding vat", "invoice amount excl vat", "invoice amount (excluding vat)", "invoice amount",
          "مبلغ الفاتوره غير شامل الضريبه"],
    "F": ["invoice currency", "currency", "العمله"],
    "G": ["invoice date", "date", "تاريخ الفاتوره"],
    "H": ["invoice number", "invoice no", "رقم الفاتوره"],
    "I": ["exchange rate", "fx rate", "سعر الصرف"],
    "J": ["amount in sar", "amount sar", "المبلغ بالريال"],
    "K": ["difference", "variance", "الفرق"],
    "L": ["goods delivery note issued", "gdn issued", "delivery note issued"],
    "M": ["goods delivery note date", "gdn date", "delivery note date"],
    "N": ["selling price matched with approved price list", "price list", "selling price matched"],
    "O": ["invoice approved", "approved"],
    "P": ["explanation of material differences", "explanation", "comments", "التوضيح"],
}


class TemplateError(Exception):
    pass


def _cell_norm(v) -> str:
    return norm_text(str(v)).replace("\n", " ").strip() if v is not None else ""


def locate(wb):
    """Find the vouching sheet, header row and column mapping (logical column -> actual letter)."""
    sheets = sorted(wb.worksheets, key=lambda s: 0 if norm_text(s.title) == "sales vouching" else
                    1 if "vouch" in norm_text(s.title) else 2)
    for ws in sheets:
        for r in range(1, min(ws.max_row, 60) + 1):
            mapping = {}
            for c in range(1, min(ws.max_column, 40) + 1):
                n = _cell_norm(ws.cell(r, c).value)
                if not n:
                    continue
                for logical, syns in HEADER_SYNONYMS.items():
                    if logical in mapping:
                        continue
                    if n in syns or (len(n) > 6 and any(n.startswith(s) for s in syns if len(s) > 6)):
                        mapping[logical] = get_column_letter(c)
                        break
            if len(mapping) >= 8 and "E" in mapping and "H" in mapping:
                return ws, r, mapping
    raise TemplateError("Could not find the Sales Vouching header row (expected headings such as 'S.no', "
                        "'Customer Name', 'Invoice Amount Excluding VAT', 'Invoice Number').")


def _find_totals_row(ws, header_row, mapping) -> int | None:
    cols = [column_index_from_string(c) for c in mapping.values()]
    for r in range(header_row + 1, ws.max_row + 1):
        for c in cols:
            v = ws.cell(r, c).value
            if isinstance(v, str):
                n = _cell_norm(v)
                if re.search(r"\btotal\b|الاجمالي|المجموع", n) or re.match(r"=\s*sum\(", v.strip().lower()):
                    return r
    return None


# --------------------------------------------------------------------------- reference shifting
_REF = re.compile(r"^(?P<sheet>(?:'[^']+'|[^!]+)!)?(?P<a>\$?[A-Z]{1,3}\$?\d+)(?::(?P<b>\$?[A-Z]{1,3}\$?\d+))?$")
_CELL = re.compile(r"^(\$?)([A-Z]{1,3})(\$?)(\d+)$")


def _shift_cell(ref: str, delta_fn) -> str:
    m = _CELL.match(ref)
    d1, col, d2, row = m.groups()
    return f"{d1}{col}{d2}{delta_fn(int(row))}"


def _shift_range_text(text: str, at: int, n: int, on_target: bool) -> str:
    m = _REF.match(text)
    if not m or not on_target:
        return text
    sheet, a, b = m.group("sheet") or "", m.group("a"), m.group("b")
    ra = int(_CELL.match(a).group(4))
    if b is None:
        return sheet + _shift_cell(a, lambda r: r + n if r >= at else r)
    rb = int(_CELL.match(b).group(4))
    na = _shift_cell(a, lambda r: r + n if r >= at else r)
    # extend a range that ends on the last data row (rb == at-1) or spans the insertion point
    nb = _shift_cell(b, lambda r: r + n if (r >= at or (r == at - 1 and ra < at)) else r)
    return f"{sheet}{na}:{nb}"


def _shift_formula(formula: str, at: int, n: int, target_title: str, formula_on_target: bool) -> str:
    try:
        tok = Tokenizer(formula)
    except Exception:
        return formula
    changed = False
    for t in tok.items:
        if t.type == "OPERAND" and t.subtype == "RANGE":
            m = _REF.match(t.value)
            if not m:
                continue
            sh = m.group("sheet")
            on_target = (sh is None and formula_on_target) or (
                sh is not None and sh.rstrip("!").strip("'") == target_title)
            nv = _shift_range_text(t.value, at, n, on_target)
            if nv != t.value:
                t.value = nv
                changed = True
    return tok.render() if changed else formula


def _shift_sqref(sqref: str, at: int, n: int) -> str:
    return " ".join(_shift_range_text(part, at, n, True) for part in str(sqref).split())


def insert_rows(wb, ws, at: int, n: int) -> None:
    """Insert n rows before row `at` keeping formulas, merges, heights, validations and names intact."""
    merged = [str(m) for m in ws.merged_cells.ranges]
    for m in merged:
        ws.unmerge_cells(m)
    heights = {r: ws.row_dimensions[r].height for r in range(at, ws.max_row + 1) if ws.row_dimensions[r].height}
    ws.insert_rows(at, n)
    for r in sorted(heights, reverse=True):
        ws.row_dimensions[r + n].height = heights[r]
        ws.row_dimensions[r].height = None
    for m in merged:
        ws.merge_cells(_shift_sqref(m, at, n))
    # formulas everywhere
    for sh in wb.worksheets:
        for row in sh.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    c.value = _shift_formula(c.value, at, n, ws.title, sh is ws)
    for dv in ws.data_validations.dataValidation:
        dv.sqref = _shift_sqref(str(dv.sqref), at, n)
    try:
        from openpyxl.formatting.formatting import ConditionalFormattingList
        old = ws.conditional_formatting
        new = ConditionalFormattingList()
        for cf in old:
            rng = _shift_sqref(str(cf.sqref), at, n)
            for rule in cf.rules:
                new.add(rng, rule)
        ws.conditional_formatting = new
    except Exception:
        pass
    for name in list(wb.defined_names.values()) if hasattr(wb.defined_names, "values") else []:
        if name.attr_text:
            name.attr_text = _shift_formula("=" + name.attr_text, at, n, ws.title, False)[1:]
    if ws.print_area:
        pa = ws.print_area if isinstance(ws.print_area, str) else ",".join(ws.print_area)
        ws.print_area = ",".join(_shift_range_text(p.split("!")[-1].replace("$", ""), at, n, True) for p in pa.split(","))
    # copy style from the last template data row into the new rows
    src = at - 1
    for r in range(at, at + n):
        ws.row_dimensions[r].height = ws.row_dimensions[src].height
        for c in range(1, ws.max_column + 1):
            s, d = ws.cell(src, c), ws.cell(r, c)
            if s.has_style:
                d._style = copy(s._style)


# --------------------------------------------------------------------------------- population
def _fill_header_info(ws, header_row, eng):
    for r in range(1, header_row):
        for c in range(1, min(ws.max_column, 20) + 1):
            n = _cell_norm(ws.cell(r, c).value)
            if not n or len(n) > 40:
                continue
            val = None
            if re.match(r"^(client|client name|entity|company)\s*:?$|^العميل|^اسم المنشاه", n):
                val = eng["client_name"]
            elif re.match(r"^(period|financial year|year end|fy|period / financial year|period/financial year)\s*:?$|^السنه الماليه|^الفتره", n):
                val = f"FY {eng['financial_year']} ({eng['fy_start']} to {eng['fy_end']})"
            if val is None:
                continue
            for cc in range(c + 1, c + 4):
                tgt = ws.cell(r, cc)
                if type(tgt).__name__ == "MergedCell":
                    continue
                if tgt.value in (None, ""):
                    tgt.value = val
                    break
                break


def _write(ws, col_letter, row, value, number_format=None):
    cell = ws[f"{col_letter}{row}"]
    if type(cell).__name__ == "MergedCell":
        return
    cell.value = value
    if number_format and (cell.number_format in (None, "General")):
        cell.number_format = number_format


def generate(eid: int, user: str, final: bool = False) -> dict:
    eng = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    tf = db.q1("SELECT * FROM files WHERE engagement_id=? AND kind='template' ORDER BY id DESC LIMIT 1", (eid,))
    if not tf:
        raise TemplateError("No working-paper template uploaded")
    rows = engine.compute_rows(eid)
    invs = {i["id"]: i for i in engine.load_invoices(eid)}
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    src = Path(tf["stored_path"])
    tag = "FINAL" if final else "DRAFT"
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", eng["client_name"])[:40]
    out = config.OUTPUT_DIR / f"E{eid}_{safe}_FY{eng['financial_year']}_Sales_Vouching_{tag}_{stamp}{src.suffix}"
    shutil.copyfile(src, out)   # original upload is never opened for writing
    wb = load_workbook(out, keep_vba=src.suffix.lower() == ".xlsm")
    ws, hr, mp = locate(wb)
    col = lambda logical: mp.get(logical, logical)
    first = hr + 1
    totals = _find_totals_row(ws, hr, mp)
    capacity = (totals - first) if totals else 10 ** 6
    inserted = 0
    if len(rows) > capacity:
        inserted = len(rows) - capacity
        insert_rows(wb, ws, totals, inserted)
        totals += inserted
    last_data = (totals - 1) if totals else first + len(rows) - 1

    # clear constant sample values from the data area (formulas are kept)
    cleared = 0
    for r in range(first, last_data + 1):
        for logical in HEADER_SYNONYMS:
            c = ws[f"{col(logical)}{r}"]
            if type(c).__name__ == "MergedCell":
                continue
            if c.value is not None and not (isinstance(c.value, str) and c.value.startswith("=")):
                c.value = None
                cleared += 1

    _fill_header_info(ws, hr, eng)
    E, I, J, D, K = (col(x) for x in "EIJDK")
    for idx, row in enumerate(rows):
        r = first + idx
        inv = invs[row["invoice_id"]]
        F = inv["fields"] or {}
        _write(ws, col("A"), r, row["sno"])
        _write(ws, col("B"), r, row["customer"])
        _write(ws, col("C"), r, row["product_type"])
        _write(ws, D, r, row["ledger_amount"], "#,##0.00")
        _write(ws, E, r, row["invoice_amount"], "#,##0.00")
        _write(ws, col("F"), r, row["currency"])
        d = to_date(row["invoice_date"])
        _write(ws, col("G"), r, datetime(d.year, d.month, d.day) if d else None, "dd/mm/yyyy")
        _write(ws, col("H"), r, row["invoice_number"])
        _write(ws, I, r, row["exchange_rate"], "0.0000")
        jc, kc = ws[f"{J}{r}"], ws[f"{K}{r}"]
        if not (isinstance(jc.value, str) and jc.value.startswith("=")):
            jc.value = f'=IF(OR({E}{r}="",{I}{r}=""),"",{E}{r}*{I}{r})'
            jc.number_format = jc.number_format if jc.number_format != "General" else "#,##0.00"
        if not (isinstance(kc.value, str) and kc.value.startswith("=")):
            kc.value = f'=IF(OR({D}{r}="",{J}{r}=""),"",{D}{r}-{J}{r})'
            kc.number_format = kc.number_format if kc.number_format != "General" else "#,##0.00"
        man = row["manual"] or {}
        # Manual audit evidence: only values entered by an auditor are written
        for logical, key in (("L", "gdn_issued"), ("M", "gdn_date"), ("N", "price_list_matched"), ("O", "invoice_approved")):
            v = man.get(key)
            if v:
                dv = to_date(v) if key == "gdn_date" else None
                _write(ws, col(logical), r, datetime(dv.year, dv.month, dv.day) if dv else v, "dd/mm/yyyy" if dv else None)
        if man.get("explanation"):
            _write(ws, col("P"), r, man["explanation"])
        # provenance comments for AI-extracted values not yet confirmed by the auditor
        for logical, fk in (("B", "buyer_name"), ("E", "taxable_amount"), ("G", "invoice_date"), ("H", "invoice_number"), ("F", "currency")):
            f = F.get(fk) or {}
            if f.get("value") is None:
                continue
            c = ws[f"{col(logical)}{r}"]
            if c.comment is None and not f.get("confirmed") and f.get("method") != "auditor":
                c.comment = Comment(f"AI-extracted ({f.get('method')}), confidence {float(f.get('confidence') or 0):.0%}; "
                                    f"source: {row['file']} p.{f.get('page') or '?'}. Not yet auditor-confirmed.", "AI Audit Assistant",
                                    width=260, height=80)
    _add_summary_sheets(wb, eid, eng, rows, final, user, tf["original_name"])
    wb.save(out)
    # verify the file re-opens
    load_workbook(out).close()
    with db.tx() as c:
        import hashlib
        sha = hashlib.sha256(out.read_bytes()).hexdigest()
        cur = c.execute("INSERT INTO files(engagement_id,kind,original_name,stored_path,sha256,size,uploaded_by,uploaded_at,meta) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (eid, "output", out.name, str(out), sha, out.stat().st_size, user, db.now(),
                                                         db.jdump({"type": "workpaper", "final": final, "rows": len(rows),
                                                                   "inserted_rows": inserted, "cleared_cells": cleared})))
        db.log(user, eid, "file", cur.lastrowid, "export_workpaper", note=f"{tag}: {len(rows)} rows, {inserted} rows inserted", c=c)
    return {"path": str(out), "name": out.name, "rows": len(rows), "inserted_rows": inserted, "sheet": ws.title,
            "header_row": hr, "first_data_row": first, "totals_row": totals, "cleared_cells": cleared}


def _unique_title(wb, base):
    t, i = base, 2
    while t in wb.sheetnames:
        t = f"{base} {i}"; i += 1
    return t


def _add_summary_sheets(wb, eid, eng, rows, final, user, template_name):
    from openpyxl.styles import Font, PatternFill
    s = engine.summary(eid)
    ws = wb.create_sheet(_unique_title(wb, "AI Audit Summary"))
    bold = Font(bold=True)
    lines = [
        ("Client", eng["client_name"]), ("Financial year", f"{eng['financial_year']} ({eng['fy_start']} to {eng['fy_end']})"),
        ("Status", "FINAL - approved by auditor" if final else "DRAFT - not finalised; subject to auditor review"),
        ("Generated by / at", f"{user} / {db.now()}"), ("Template", template_name), ("Tolerance (SAR)", eng["tolerance"]),
        ("", ""),
        ("Number of invoices tested", s["invoices_tested"]), ("Total value tested (SAR, excl. VAT)", s["total_value_tested_sar"]),
        ("Total ledger amount (matched)", s["total_ledger_amount"]), ("Matched invoices", s["matched"]),
        ("Unmatched invoices", s["unmatched"]), ("Open exceptions", s["exceptions"]),
        ("High / Medium / Low findings", f"{s['findings_by_risk']['High']} / {s['findings_by_risk']['Medium']} / {s['findings_by_risk']['Low']}"),
        ("Total identified differences (SAR)", s["total_differences"]), ("Invoices approved by auditor", s["approved"]),
        ("Invoices with outstanding procedures", len(s["outstanding_procedures"])),
    ]
    ws["A1"] = "AI-assisted Sales Vouching - Summary"; ws["A1"].font = Font(bold=True, size=13)
    for i, (k, v) in enumerate(lines, start=3):
        ws.cell(i, 1, k).font = bold
        ws.cell(i, 2, v)
    r = len(lines) + 5
    ws.cell(r, 1, "Outstanding audit procedures").font = bold
    for o in s["outstanding_procedures"]:
        r += 1
        ws.cell(r, 1, o["invoice_number"] or o["file"]); ws.cell(r, 2, "; ".join(o["items"]))
    r += 2
    ws.cell(r, 1, "Note: values marked with a cell comment on the 'Sales Vouching' sheet were extracted by AI and have "
                  "not been confirmed by the auditor. Columns L-O contain only auditor-entered evidence.")
    ws.column_dimensions["A"].width = 42; ws.column_dimensions["B"].width = 90

    log = wb.create_sheet(_unique_title(wb, "AI Extraction Log"))
    hdr = ["S.no", "File", "Invoice Number", "Field", "Value", "Origin", "Method", "Confidence", "Page", "BBox (pt)",
           "Source text", "Auditor confirmed", "Note", "Match status", "Difference status", "Review status", "Reviewer"]
    log.append(hdr)
    for c in log[1]:
        c.font = Font(bold=True, color="FFFFFF"); c.fill = PatternFill("solid", fgColor="1F3864")
    invs = {i["id"]: i for i in engine.load_invoices(eid)}
    for row in rows:
        F = invs[row["invoice_id"]]["fields"] or {}
        for k, f in F.items():
            log.append([row["sno"], row["file"], row["invoice_number"], k, None if f.get("value") is None else str(f.get("value")),
                        "Auditor" if f.get("method") == "auditor" else "AI-extracted", f.get("method"), f.get("confidence"),
                        f.get("page"), ",".join(str(x) for x in f["bbox"]) if f.get("bbox") else None, f.get("source_text"),
                        "Yes" if f.get("confirmed") else "No", f.get("note"), row["match_status"], row["diff_status"],
                        row["review_status"], row["reviewer"]])
    for i, w in enumerate([6, 30, 18, 16, 28, 12, 10, 10, 6, 22, 50, 10, 40, 18, 20, 12, 14], start=1):
        log.column_dimensions[get_column_letter(i)].width = w
    log.freeze_panes = "A2"
