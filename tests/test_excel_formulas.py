"""Working-paper formula integrity (Section 2.3 / Phase 4): J and K stay live formulas, never static values;
layout, merged cells and header styles are preserved; L-O left blank; D blank when unrecorded; E is net of VAT."""
from __future__ import annotations

import csv
import io
import shutil
import subprocess
import zipfile

import pytest
from openpyxl import load_workbook

from tests.conftest import PACK, TMP, TRUTH, login, wait_extraction

TEMPLATE = PACK / "Sales_Vouching_Invoices_Sample.xlsx"
HEADER_ROW, FIRST = 6, 7


@pytest.fixture(scope="module")
def wp(processed):
    c, eid, _ = processed
    r = c.post(f"/api/engagements/{eid}/export/workpaper")
    assert r.status_code == 200, r.text
    path = TMP / "formula_wp.xlsx"
    path.write_bytes(r.content)
    rows = c.get(f"/api/engagements/{eid}/rows").json()["rows"]
    return path, rows


def test_file_is_a_valid_xlsx_package(wp):
    path, _ = wp
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        assert "xl/workbook.xml" in z.namelist()
    load_workbook(path)                                   # parses without error


def test_columns_j_and_k_are_dynamic_formulas_on_every_row(wp):
    path, rows = wp
    ws = load_workbook(path)["Sales Vouching"]
    for i in range(len(rows)):
        r = FIRST + i
        j, k = ws[f"J{r}"].value, ws[f"K{r}"].value
        assert isinstance(j, str) and j.startswith("="), (r, j)
        assert isinstance(k, str) and k.startswith("="), (r, k)
        assert f"E{r}*I{r}" in j                          # J = E x I
        assert f'IF(D{r}=""' in k and f"D{r}-J{r}" in k     # K = IF(D="","",D-J)
        assert not isinstance(ws[f"J{r}"].value, (int, float)) and not isinstance(ws[f"K{r}"].value, (int, float))


def test_template_formulas_kept_verbatim(wp):
    path, _ = wp
    ws, o = load_workbook(path)["Sales Vouching"], load_workbook(TEMPLATE)["Sales Vouching"]
    for r in range(FIRST, FIRST + 10):                   # rows that existed in the template
        assert ws[f"J{r}"].value == o[f"J{r}"].value and ws[f"K{r}"].value == o[f"K{r}"].value


def test_layout_header_and_merges_preserved(wp):
    path, _ = wp
    ws, o = load_workbook(path)["Sales Vouching"], load_workbook(TEMPLATE)["Sales Vouching"]
    for col in range(1, 17):
        a, b = ws.cell(HEADER_ROW, col), o.cell(HEADER_ROW, col)
        assert a.value == b.value
        assert (a.font.name, a.font.b, a.fill.fgColor.rgb, a.alignment.wrap_text) == (b.font.name, b.font.b, b.fill.fgColor.rgb, b.alignment.wrap_text)
    orig_above = {str(m) for m in o.merged_cells.ranges if m.max_row < FIRST}
    assert orig_above <= {str(m) for m in ws.merged_cells.ranges}
    assert ws.column_dimensions["B"].width == o.column_dimensions["B"].width


def test_manual_columns_blank_and_ledger_blank_when_unrecorded(wp):
    path, rows = wp
    ws = load_workbook(path)["Sales Vouching"]
    for i, row in enumerate(rows):
        r = FIRST + i
        for col in "LMNO":
            assert ws[f"{col}{r}"].value is None, (col, r)
        if row["recon_category"] in ("NOT_IN_LEDGER", "POSSIBLE_MATCH", "DUPLICATE_INVOICE"):
            assert ws[f"D{r}"].value is None                  # never fabricated
        if row["invoice_amount"] is not None and row["gross_amount"] is not None:
            assert ws[f"E{r}"].value == row["invoice_amount"] != row["gross_amount"]   # net of VAT


def test_guarded_formulas_written_when_template_row_has_none(processed):
    """A template whose data rows carry no J/K formulas gets formulas written (never evaluated numbers)."""
    c, _, _ = processed
    wb = load_workbook(TEMPLATE)
    ws = wb["Sales Vouching"]
    for r in range(FIRST, FIRST + 10):
        ws[f"J{r}"].value = None; ws[f"K{r}"].value = None
    bare = TMP / "bare_template.xlsx"; wb.save(bare)
    eid = c.post("/api/engagements", json={"client_name": "Bare Template Co", "financial_year": "2025",
                                           "fy_start": "2025-01-01", "fy_end": "2025-12-31", "tolerance": 1}).json()["id"]
    with open(bare, "rb") as fh:
        assert c.post(f"/api/engagements/{eid}/template", files={"file": ("bare.xlsx", fh)}).status_code == 200
    with open(PACK / "Sales_Ledger_FY2025.xlsx", "rb") as fh:
        c.post(f"/api/engagements/{eid}/ledger", files={"file": ("l.xlsx", fh)})
    pdfs = sorted((PACK / "invoices").glob("*.pdf"))[:3]
    c.post(f"/api/engagements/{eid}/invoices", files=[("files", (p.name, open(p, "rb"), "application/pdf")) for p in pdfs])
    wait_extraction(c, eid)
    out = load_workbook(io.BytesIO(c.post(f"/api/engagements/{eid}/export/workpaper").content))["Sales Vouching"]
    for r in range(FIRST, FIRST + 3):
        assert out[f"J{r}"].value == f'=IF(OR(E{r}="",I{r}=""),"",E{r}*I{r})'
        assert out[f"K{r}"].value == f'=IF(OR(D{r}="",J{r}=""),"",D{r}-J{r})'


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed")
def test_libreoffice_opens_and_recalculates(wp):
    """Independent spreadsheet engine: file opens headless, formulas evaluate without error values,
    and J/K agree with the application's own calculation."""
    path, rows = wp
    outdir = TMP / "lo"; outdir.mkdir(exist_ok=True)
    res = subprocess.run(["soffice", "--headless", "--norestore", "--convert-to", "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false,false,-1",
                          "--outdir", str(outdir), str(path)], capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, res.stderr
    csvs = list(outdir.glob("*Sales Vouching*.csv")) or list(outdir.glob("*.csv"))
    assert csvs, res.stdout
    data = list(csv.reader(open(csvs[0], encoding="utf-8")))
    flat = " ".join(" ".join(r) for r in data)
    for err in ("#VALUE!", "#REF!", "#NAME?", "#DIV/0!", "Err:"):
        assert err not in flat, err
    num = lambda s: float(s.replace(",", "")) if s not in ("", None) else None
    for i, row in enumerate(rows):
        cells = data[FIRST - 1 + i]
        if row["amount_sar"] is not None:
            assert num(cells[9]) == pytest.approx(row["amount_sar"], abs=0.01)
        if row["difference"] is not None:
            assert num(cells[10]) == pytest.approx(row["difference"], abs=0.01)
        if row["ledger_amount"] is None:
            assert cells[10] == ""
