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
    "A": ["s.no", "s no", "sno", "serial", "sr", "sr.", "#", "no.", "م", "رقم تسلسلي", "الرقم التسلسلي"],
    "B": ["customer name", "customer", "client name", "اسم العميل", "العميل"],
    "C": ["product type", "product", "description", "نوع المنتج", "المنتج"],
    "D": ["ledger amount", "amount per ledger", "ledger", "المبلغ", "المبلغ حسب الدفتر", "المبلغ حسب السجل",
          "المبلغ - سجل الايرادات", "مبلغ سجل الايرادات"],
    "E": ["invoice amount excluding vat", "invoice amount excl vat", "invoice amount (excluding vat)", "invoice amount",
          "المبلغ كما في الفاتوره", "مبلغ الفاتوره غير شامل الضريبه", "مبلغ الفاتوره"],
    "F": ["invoice currency", "currency", "عمله الفاتوره", "العمله"],
    "G": ["invoice date", "date", "تاريخ الفاتوره"],
    "H": ["invoice number", "invoice no", "رقم الفاتوره"],
    "I": ["exchange rate", "fx rate", "معدل الصرف", "سعر الصرف"],
    "J": ["amount in sar", "amount sar", "المبلغ بالريال السعودي", "المبلغ بالريال"],
    "K": ["difference", "variance", "الفرق"],
    "L": ["goods delivery note issued", "gdn issued", "delivery note issued", "تم اصدار اشعار ارسال البضائع",
          "اشعار ارسال البضائع", "تم اصدار اشعار التسليم"],
    "M": ["goods delivery note date", "gdn date", "delivery note date", "تاريخ اشعار ارسال البضائع", "تاريخ اشعار التسليم"],
    "N": ["selling price matched with approved price list", "price list", "selling price matched",
          "مطابقه سعر البيع مع القائمه المعتمده", "مطابقه سعر البيع"],
    "O": ["invoice approved", "approved", "تمت الموافقه علي الفاتوره", "اعتماد الفاتوره"],
    "P": ["explanation of material differences", "explanation", "comments", "التوضيح", "سبب الاختلاف ، اذا كان جوهري",
          "سبب الاختلاف"],
}

# Values accepted by Arabic yes/no validation lists in templates
YES_NO_AR = {"Yes": ["نعم", "yes", "y"], "No": ["لا", "no", "n"], "N/A": ["لا ينطبق", "غير منطبق", "n/a", "na"]}


def safe_filename(name: str, limit: int = 40) -> str:
    """Keep Latin and Arabic letters/digits; replace anything else (spaces, slashes, punctuation)."""
    s = re.sub(r"[^\w\u0600-\u06FF-]+", "_", str(name or ""), flags=re.UNICODE).strip("_")
    return (s[:limit].strip("_")) or "client"


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


def first_data_row(ws, header_row, mapping) -> int:
    """Skip legend / sub-heading / section rows between the header and the first data row."""
    cols = [column_index_from_string(c) for c in mapping.values()]
    jk = [column_index_from_string(mapping[x]) for x in ("J", "K") if x in mapping]
    a_col = column_index_from_string(mapping.get("A", "A"))
    for r in range(header_row + 1, header_row + 12):
        vals = [ws.cell(r, c).value for c in cols]
        if all(v in (None, "") for v in vals):
            return r
        if any(isinstance(ws.cell(r, c).value, str) and ws.cell(r, c).value.startswith("=") for c in jk):
            return r
        if isinstance(ws.cell(r, a_col).value, (int, float)):
            return r
    return header_row + 1


_TOTAL_RE = re.compile(r"(^|\s)(total|grand total|الاجمالي|المجموع|اجمالي)(\s|:|$)")


def _find_totals_row(ws, header_row, mapping) -> int | None:
    cols = [column_index_from_string(c) for c in mapping.values()]
    start = first_data_row(ws, header_row, mapping)
    for r in range(start, ws.max_row + 1):
        for c in cols:
            v = ws.cell(r, c).value
            if isinstance(v, str):
                n = _cell_norm(v)
                if _TOTAL_RE.search(n) or re.match(r"=\s*sum\(", v.strip().lower()):
                    return r
    return None


def find_tolerance_cell(ws, header_row):
    """A numeric input cell next to a 'tolerance' label above the table (formulas may reference it)."""
    for r in range(1, header_row):
        for c in range(1, min(ws.max_column, 20) + 1):
            n = _cell_norm(ws.cell(r, c).value)
            if n and re.search(r"tolerance|حد التسامح|التسامح", n):
                for cc in range(c + 1, c + 4):
                    v = ws.cell(r, cc).value
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        return ws.cell(r, cc).coordinate
    return None


def inspect_template(path: str, eng: dict | None = None) -> dict:
    """Structure + warnings shown to the auditor when the template is uploaded."""
    wb = load_workbook(path)
    ws, hr, mp = locate(wb)
    first = first_data_row(ws, hr, mp)
    totals = _find_totals_row(ws, hr, mp)
    last = (totals - 1) if totals else ws.max_row
    existing = sum(1 for r in range(first, last + 1)
                   if any(ws[f"{mp[x]}{r}"].value not in (None, "") and not str(ws[f"{mp[x]}{r}"].value).startswith("=")
                          for x in ("B", "E", "H") if x in mp))
    formulas = {x: ws[f"{mp[x]}{first}"].value for x in mp
                if isinstance(ws[f"{mp[x]}{first}"].value, str) and ws[f"{mp[x]}{first}"].value.startswith("=")}
    tol = find_tolerance_cell(ws, hr)
    warnings = []
    if existing:
        warnings.append(f"Template already contains {existing} data row(s); their constant values will be replaced in the "
                        "output copy (formulas kept). The uploaded file itself is not changed.")
    if tol and eng and float(ws[tol].value) != float(eng["tolerance"]):
        warnings.append(f"Template tolerance {ws[tol]} = {ws[tol].value} differs from the engagement tolerance "
                        f"{eng['tolerance']}; the engagement value will be written to {tol} on export.")
    if eng:
        title = " ".join(str(ws.cell(r, c).value) for r in range(1, min(hr, 4)) for c in range(1, 4)
                         if isinstance(ws.cell(r, c).value, str))
        if title and eng["client_name"] and norm_text(eng["client_name"]) not in norm_text(title) and \
                not any(norm_text(eng["client_name"]) in norm_text(str(ws.cell(r, c).value or "")) for r in range(1, hr) for c in range(1, 10)):
            warnings.append(f"Template header shows '{title[:80]}' - does not mention engagement client "
                            f"'{eng['client_name']}'. The header is not changed automatically; correct it if needed.")
        yr = str(eng["fy_end"])[:4]
        if title and yr not in title and not any(yr in str(ws.cell(r, c).value or "") for r in range(1, hr) for c in range(1, 10)):
            warnings.append(f"Template header does not mention the financial year end {eng['fy_end']}.")
    dvs = [(str(d.sqref), d.formula1) for d in ws.data_validations.dataValidation]
    return {"sheet": ws.title, "header_row": hr, "first_data_row": first, "totals_row": totals,
            "capacity": (totals - first) if totals else None, "mapping": mp, "existing_rows": existing,
            "row_formulas": formulas, "tolerance_cell": tol, "data_validations": dvs, "sheets": wb.sheetnames,
            "rtl": bool(ws.sheet_view.rightToLeft), "warnings": warnings}


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
    # copy style and the template's row formulas (translated) from the last data row into the new rows
    from openpyxl.formula.translate import Translator
    src = at - 1
    for r in range(at, at + n):
        ws.row_dimensions[r].height = ws.row_dimensions[src].height
        for c in range(1, ws.max_column + 1):
            s, d = ws.cell(src, c), ws.cell(r, c)
            if s.has_style:
                d._style = copy(s._style)
            if isinstance(s.value, str) and s.value.startswith("="):
                d.value = Translator(s.value, origin=s.coordinate).translate_formula(d.coordinate)


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
    safe = safe_filename(eng["client_name"])
    out = config.OUTPUT_DIR / f"E{eid}_{safe}_FY{eng['financial_year']}_Sales_Vouching_{tag}_{stamp}{src.suffix}"
    shutil.copyfile(src, out)   # original upload is never opened for writing
    wb = load_workbook(out, keep_vba=src.suffix.lower() == ".xlsm")
    ws, hr, mp = locate(wb)
    col = lambda logical: mp.get(logical, logical)
    first = first_data_row(ws, hr, mp)
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
    tol_cell = find_tolerance_cell(ws, hr)
    if tol_cell and float(ws[tol_cell].value) != float(eng["tolerance"]):
        db.log(user, eid, "workpaper", None, "set_tolerance_cell", tol_cell, ws[tol_cell].value, eng["tolerance"],
               "Engagement tolerance written to template input cell used by its formulas")
        ws[tol_cell].value = float(eng["tolerance"])
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
            if not v:
                continue
            addr = f"{col(logical)}{r}"
            if key == "gdn_date":
                dv = to_date(v)
                _write(ws, col(logical), r, datetime(dv.year, dv.month, dv.day) if dv else v, "dd/mm/yyyy" if dv else None)
            else:
                _write(ws, col(logical), r, _validated_value(ws, addr, v))
        if man.get("explanation"):
            pc = ws[f"{col('P')}{r}"]
            if isinstance(pc.value, str) and pc.value.startswith("="):
                # the template computes P with a formula: keep it, record the auditor's explanation as a note
                pc.comment = Comment(f"Auditor explanation ({man.get('explanation_by', '')}): {man['explanation']}",
                                     "Auditor", width=300, height=110)
            else:
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
    warnings = []
    no_fx = [r["invoice_number"] or r["file"] for r in rows if r["currency"] and r["exchange_rate"] is None]
    if no_fx:
        warnings.append(f"{len(no_fx)} invoice(s) without an authorised exchange rate ({', '.join(map(str, no_fx[:8]))}); "
                        "column I is blank - check how the template's column J formula treats a blank rate.")
    no_amt = [r["invoice_number"] or r["file"] for r in rows if r["invoice_amount"] is None]
    if no_amt:
        warnings.append(f"{len(no_amt)} invoice(s) without an amount excluding VAT ({', '.join(map(str, no_amt[:8]))}).")
    unconfirmed = sum(1 for r in rows if not r["auditor_confirmed"])
    if unconfirmed:
        warnings.append(f"{unconfirmed} row(s) contain AI-extracted values not yet confirmed by the auditor (cell comments).")
    if not final:
        warnings.append("DRAFT - the working paper has not been finalised.")
    _add_summary_sheets(wb, eid, eng, rows, final, user, tf["original_name"], warnings)
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
    return {"path": str(out), "name": out.name, "rows": len(rows), "inserted_rows": inserted, "warnings": warnings, "sheet": ws.title,
            "header_row": hr, "first_data_row": first, "totals_row": totals, "cleared_cells": cleared}


def _allowed_values(ws, addr):
    for dv in ws.data_validations.dataValidation:
        if dv.type == "list" and dv.formula1 and addr in dv.sqref:
            f = dv.formula1.strip()
            if f.startswith('"'):
                return [x.strip() for x in f.strip('"').split(",")]
    return None


def _validated_value(ws, addr, v):
    """Translate Yes/No/N/A to the value list the template's data validation accepts (e.g. نعم / لا)."""
    allowed = _allowed_values(ws, addr)
    if not allowed:
        return v
    if v in allowed:
        return v
    for cand in YES_NO_AR.get(v, []):
        for a in allowed:
            if norm_text(a) == norm_text(cand):
                return a
    return v


def _unique_title(wb, base):
    t, i = base, 2
    while t in wb.sheetnames:
        t = f"{base} {i}"; i += 1
    return t


def _add_summary_sheets(wb, eid, eng, rows, final, user, template_name, warnings=()):
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
    ws.cell(r, 1, "Export warnings").font = bold
    for w_ in warnings or ["None"]:
        r += 1
        ws.cell(r, 2, w_)
    r += 2
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
