"""End-to-end acceptance tests (Section 13 of the specification).

Runs fully offline (rule engine; AI disabled) against a generated pack of 50 invoices, a sales ledger
with planted exceptions and a working-paper template."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="audit_test_"))
os.environ["AUDIT_DATA_DIR"] = str(TMP / "data")
os.environ["AUDIT_EXTRACTION_MODE"] = "rules_only"
os.environ["AUDIT_ADMIN_PASSWORD"] = "Admin#12345"

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

from samples.generate_samples import main as gen  # noqa: E402

PACK = TMP / "pack"
gen(str(PACK))
TRUTH = {t["file"]: t for t in json.loads((PACK / "ground_truth.json").read_text(encoding="utf-8"))}

from audit_app.api import app  # noqa: E402


@pytest.fixture(scope="module")
def ctx():
    with TestClient(app) as c:
        tok = c.post("/api/login", json={"username": "admin", "password": "Admin#12345"}).json()["token"]
        c.headers["Authorization"] = f"Bearer {tok}"
        r = c.post("/api/engagements", json={"client_name": "Test Client Co", "financial_year": "2025",
                                             "fy_start": "2025-01-01", "fy_end": "2025-12-31", "tolerance": 1.0,
                                             "materiality": 50000})
        assert r.status_code == 200, r.text
        eid = r.json()["id"]
        yield c, eid


def _wait(c, eid, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = c.get(f"/api/engagements/{eid}/progress").json()
        if p.get("total") and not p.get("queued") and not p.get("processing"):
            return p
        time.sleep(0.5)
    raise AssertionError("extraction timed out")


def _rows(c, eid):
    return c.get(f"/api/engagements/{eid}/rows").json()


def test_01_uploads_and_bulk_extraction(ctx):
    c, eid = ctx
    with open(PACK / "Sales_Vouching_Invoices_Sample.xlsx", "rb") as fh:
        r = c.post(f"/api/engagements/{eid}/template", files={"file": ("Sales_Vouching_Invoices_Sample.xlsx", fh)})
    assert r.status_code == 200, r.text
    assert r.json()["meta"]["header_row"] == 6 and r.json()["meta"]["capacity"] == 10
    with open(PACK / "Sales_Ledger_FY2025.xlsx", "rb") as fh:
        r = c.post(f"/api/engagements/{eid}/ledger", files={"file": ("Sales_Ledger_FY2025.xlsx", fh)})
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 51
    files = [("files", (p.name, open(p, "rb"), "application/pdf")) for p in sorted((PACK / "invoices").glob("*.pdf"))]
    assert len(files) == 50
    r = c.post(f"/api/engagements/{eid}/invoices", files=files)
    assert r.status_code == 200, r.text
    assert len(r.json()["accepted"]) == 50 and not r.json()["rejected"]
    p = _wait(c, eid)
    assert p["total"] == 50 and p.get("failed", 0) == 0


def test_02_rejects_unsafe_uploads(ctx):
    c, eid = ctx
    r = c.post(f"/api/engagements/{eid}/invoices", files=[("files", ("evil.pdf", io.BytesIO(b"MZ\x90 not a pdf"), "application/pdf")),
                                                         ("files", ("x.exe", io.BytesIO(b"%PDF-1.4"), "application/octet-stream"))])
    assert r.status_code == 200 and len(r.json()["rejected"]) == 2 and not r.json()["accepted"]


def test_03_extraction_accuracy(ctx):
    c, eid = ctx
    rows = _rows(c, eid)["rows"]
    errors = []
    for row in rows:
        t = TRUTH[row["file"]]
        if t["scanned"]:
            # no text layer and AI disabled -> must NOT invent values
            assert row["status"] == "needs_manual" and row["invoice_amount"] is None and row["invoice_number"] is None
            continue
        for k, tk in (("invoice_number", "invoice_number"), ("invoice_date", "invoice_date"), ("currency", "currency"),
                      ("invoice_amount", "taxable_amount"), ("vat_amount", "vat_amount"), ("gross_amount", "gross_amount")):
            exp = -t[tk] if (k == "invoice_amount" and t["doc_type"] == "credit_note") else t[tk]
            if row[k] != exp:
                errors.append((row["file"], k, row[k], t[tk]))
        if row["customer"] not in (t["buyer_name"], t["buyer_name_ar"]):
            errors.append((row["file"], "customer", row["customer"], t["buyer_name"]))
        # the working-paper amount is the taxable amount, never the VAT-inclusive total
        assert row["invoice_amount"] != row["gross_amount"]
    assert not errors, errors
    langs = {TRUTH[r["file"]]["layout"] for r in rows if r["status"] == "extracted"}
    assert {"en_a", "en_b", "ar", "bi"} <= langs


def test_04_fx_and_sar_calculation(ctx):
    c, eid = ctx
    usd = [r for r in _rows(c, eid)["rows"] if r["currency"] == "USD"]
    assert usd and all(r["exchange_rate"] is None and r["amount_sar"] is None for r in usd)
    assert c.put(f"/api/engagements/{eid}/fx", json={"currency": "USD", "rate": 3.75, "source": "SAMA peg"}).status_code == 200
    assert c.put(f"/api/engagements/{eid}/fx", json={"currency": "EUR", "rate": 4.05, "source": "Policy rate"}).status_code == 200
    for r in _rows(c, eid)["rows"]:
        if r["invoice_amount"] is not None:
            assert r["amount_sar"] == round(r["invoice_amount"] * r["exchange_rate"], 2)
            if r["ledger_amount"] is not None:
                assert r["difference"] == round(r["ledger_amount"] - r["amount_sar"], 2)


def _by_no(rows, no):
    return [r for r in rows if r["invoice_number"] == no]


def test_05_matching_and_exceptions(ctx):
    c, eid = ctx
    rows = _rows(c, eid)["rows"]
    st = lambda no: _by_no(rows, no)[0]["match_status"]
    assert st("INV-2025-0003") == "Amount Mismatch"          # +1,500 in ledger
    assert st("INV-2025-0009") == "Amount Mismatch"          # ledger includes VAT
    r9 = _by_no(rows, "INV-2025-0009")[0]
    assert "include VAT" in (r9["suggestion"] or "")
    assert st("INV-2025-0015") == "Customer Mismatch"
    r18 = _by_no(rows, "INV-2025-0018")[0]                   # ledger shows INV2025-0018 -> same normalised key
    assert r18["ledger_row"] is not None and r18["match_status"] == "Customer Mismatch"   # Arabic name vs English ledger
    assert st("INV-2025-0026") == "Not Found in Ledger"      # removed from ledger
    assert st("INV-2025-0029") == "Possible Match"           # ledger row without invoice number
    assert all(r["match_status"] == "Duplicate Invoice" for r in _by_no(rows, "INV-2025-0045"))
    assert st("INV-2025-0006") == "Duplicate Invoice"        # posted twice in ledger
    cn = _by_no(rows, "INV-2025-0023")[0]                    # credit note: negative, agrees to negative ledger posting
    assert cn["doc_type"] == "credit_note" and cn["invoice_amount"] < 0 and cn["diff_status"] == "No Difference"
    r1 = _by_no(rows, "INV-2025-0001")[0]
    assert r1["diff_status"] == "No Difference" and abs(r1["difference"]) == 0.4   # within SAR 1 tolerance
    f = c.get(f"/api/engagements/{eid}/findings").json()
    codes = {x["code"] for x in f}
    for code in ("NOT_IN_LEDGER", "AMOUNT_DIFF", "DUP_INV_NO", "LEDGER_DUP", "LEDGER_NO_INVOICE", "POSSIBLE_MATCH",
                 "CUSTOMER_MISMATCH", "NEEDS_MANUAL", "DATE_OUTSIDE_FY", "CUTOFF_RISK", "CREDIT_NOTE", "UNUSUAL_AMOUNT"):
        assert code in codes, code
    # ambiguous matches are never linked automatically
    pm = _by_no(rows, "INV-2025-0029")[0]
    assert pm["ledger_amount"] is None


def test_06_tolerance_configurable(ctx):
    c, eid = ctx
    assert c.patch(f"/api/engagements/{eid}/settings", json={"tolerance": 0.1}).status_code == 200
    r1 = _by_no(_rows(c, eid)["rows"], "INV-2025-0001")[0]
    assert r1["diff_status"] == "Difference Identified"
    c.patch(f"/api/engagements/{eid}/settings", json={"tolerance": 1.0})


def test_07_review_workflow_and_audit_trail(ctx):
    c, eid = ctx
    rows = _rows(c, eid)["rows"]
    scanned = next(r for r in rows if TRUTH[r["file"]]["scanned"])
    t = TRUTH[scanned["file"]]
    iid = scanned["invoice_id"]
    for k, v in (("invoice_number", t["invoice_number"]), ("invoice_date", t["invoice_date"]), ("buyer_name", t["buyer_name"]),
                 ("taxable_amount", t["taxable_amount"]), ("currency", "SAR"), ("vat_amount", t["vat_amount"]),
                 ("gross_amount", t["gross_amount"])):
        r = c.patch(f"/api/invoices/{iid}/fields", json={"field": k, "value": v, "reason": "Manual entry from scanned PDF"})
        assert r.status_code == 200, r.text
    d = c.get(f"/api/invoices/{iid}").json()
    assert d["invoice"]["fields"]["taxable_amount"]["method"] == "auditor"
    assert d["invoice"]["ai_fields"]["taxable_amount"]["value"] is None      # original AI record preserved
    assert d["row"]["match_status"] == "Matched"
    assert any(h["action"] == "edit_field" for h in d["history"])
    # approval requires confirmed data
    other = _by_no(rows, "INV-2025-0004")[0]["invoice_id"]
    assert c.post(f"/api/invoices/{other}/review", json={"status": "approved"}).status_code == 400
    assert c.post(f"/api/invoices/{other}/confirm", json={"fields": ["invoice_number", "invoice_date", "buyer_name",
                                                                       "taxable_amount", "currency"]}).status_code == 200
    assert c.post(f"/api/invoices/{other}/review", json={"status": "approved"}).status_code == 200
    # manual evidence columns and alias confirmation
    assert c.patch(f"/api/invoices/{other}/manual", json={"gdn_issued": "Yes", "gdn_date": "2025-03-01",
                                                          "invoice_approved": "Yes"}).status_code == 200
    r15 = _by_no(rows, "INV-2025-0015")[0]
    c.post(f"/api/engagements/{eid}/aliases", json={"invoice_name": r15["customer"], "ledger_name": r15["ledger_customer"]})
    assert _by_no(_rows(c, eid)["rows"], "INV-2025-0015")[0]["match_status"] == "Matched"
    # confirm the proposed match
    pm = _by_no(_rows(c, eid)["rows"], "INV-2025-0029")[0]
    cand = c.get(f"/api/invoices/{pm['invoice_id']}").json()["invoice"]["match"]["candidates"][0]
    assert c.post(f"/api/invoices/{pm['invoice_id']}/match", json={"action": "confirm", "ledger_id": cand["ledger_id"],
                                                                    "reason": "Agreed customer/date/amount"}).status_code == 200
    assert _by_no(_rows(c, eid)["rows"], "INV-2025-0029")[0]["match_status"] == "Matched"


def test_08_dashboard_reconciles(ctx):
    c, eid = ctx
    d = _rows(c, eid)
    rows, s = d["rows"], d["summary"]
    assert s["invoices_tested"] == len(rows) == 50
    assert abs(s["total_value_tested_sar"] - sum(r["amount_sar"] or 0 for r in rows)) < 0.01
    assert abs(s["total_ledger_amount"] - sum(r["ledger_amount"] or 0 for r in rows)) < 0.01
    assert s["matched"] == sum(1 for r in rows if r["match_status"] == "Matched")


def test_09_workpaper_preserves_template(ctx):
    c, eid = ctx
    r = c.post(f"/api/engagements/{eid}/export/workpaper")
    assert r.status_code == 200, r.text
    out = TMP / "wp.xlsx"
    out.write_bytes(r.content)
    orig = load_workbook(PACK / "Sales_Vouching_Invoices_Sample.xlsx")
    wb = load_workbook(out)
    assert wb.sheetnames[:2] == orig.sheetnames
    ws, o = wb["Sales Vouching"], orig["Sales Vouching"]
    assert [ws.cell(6, j).value for j in range(1, 17)] == [o.cell(6, j).value for j in range(1, 17)]
    assert ws["A4"].value == o["A4"].value and "A3:P3" in [str(m) for m in ws.merged_cells.ranges]
    assert ws.cell(6, 1).fill.fgColor.rgb == o.cell(6, 1).fill.fgColor.rgb
    # 50 rows needed vs 10 in template -> totals row moves from 17 to 57; SUM ranges extended
    assert ws["B57"].value == "Total"
    assert ws["E57"].value == "=SUM(E7:E56)" and ws["K57"].value == "=SUM(K7:K56)"
    assert "B59:P59" in [str(m) for m in ws.merged_cells.ranges]                  # conclusion merge shifted
    assert wb["Instructions"]["B2"].value == "='Sales Vouching'!J57"             # cross-sheet reference updated
    rows = _rows(c, eid)["rows"]
    for i, row in enumerate(rows):
        rr = 7 + i
        assert ws[f"A{rr}"].value == row["sno"]
        assert ws[f"E{rr}"].value == row["invoice_amount"]
        assert ws[f"H{rr}"].value == row["invoice_number"]
        assert ws[f"D{rr}"].value == row["ledger_amount"]                       # blank where not matched
        assert ws[f"J{rr}"].value.startswith("=") and ws[f"K{rr}"].value.startswith("=")
        if not row["manual"].get("gdn_issued"):
            assert ws[f"L{rr}"].value is None
        assert ws[f"P{rr}"].value is None                                        # no unconfirmed AI explanations
    assert ws["J7"].value == o["J7"].value                                       # template formulas kept verbatim
    assert "AI Audit Summary" in wb.sheetnames and "AI Extraction Log" in wb.sheetnames
    assert "Test Client Co" in (ws["B1"].value, ws["C1"].value)
    # original template file untouched
    tf = [f for f in c.get(f"/api/engagements/{eid}").json()["files"] if f["kind"] == "template"][0]
    import hashlib
    assert hashlib.sha256((PACK / "Sales_Vouching_Invoices_Sample.xlsx").read_bytes()).hexdigest() == tf["sha256"]


def test_10_reports(ctx):
    c, eid = ctx
    for kind, magic in (("findings.pdf", b"%PDF"), ("exceptions.xlsx", b"PK"), ("log.xlsx", b"PK"), ("extraction.csv", b"\xef\xbb\xbf")):
        r = c.get(f"/api/engagements/{eid}/export/{kind}")
        assert r.status_code == 200 and r.content.startswith(magic), kind
    load_workbook(io.BytesIO(c.get(f"/api/engagements/{eid}/export/log.xlsx").content))


def test_11_finalization_requires_approval(ctx):
    c, eid = ctx
    r = c.post(f"/api/engagements/{eid}/finalize")
    assert r.status_code == 409 and "not approved" in json.dumps(r.json())


def test_12_rbac(ctx):
    c, eid = ctx
    assert c.post("/api/users", json={"username": "viewer1", "full_name": "View Only", "password": "Viewer#123",
                                      "role": "viewer"}).status_code == 200
    with TestClient(app) as v:
        tok = v.post("/api/login", json={"username": "viewer1", "password": "Viewer#123"}).json()["token"]
        v.headers["Authorization"] = f"Bearer {tok}"
        assert v.get(f"/api/engagements/{eid}/rows").status_code == 200
        assert v.patch(f"/api/engagements/{eid}/settings", json={"tolerance": 5}).status_code == 403
        iid = _rows(c, eid)["rows"][0]["invoice_id"]
        assert v.patch(f"/api/invoices/{iid}/fields", json={"field": "currency", "value": "USD"}).status_code == 403
    assert TestClient(app).get(f"/api/engagements/{eid}/rows").status_code == 401


def test_13_arabic_firm_template_structure(ctx):
    """Arabic RTL template with legend/section rows, tolerance cell, P-column formula, yes/no validation,
    duplicate-number conditional format; 20 template rows vs 50 invoices forces row insertion."""
    c, _ = ctx
    eid = c.post("/api/engagements", json={"client_name": "شركة الأفق النموذجية للتجارة", "financial_year": "2025",
                                           "fy_start": "2025-01-01", "fy_end": "2025-12-31", "tolerance": 2}).json()["id"]
    with open(PACK / "Sales_Vouching_AR_Template.xlsx", "rb") as fh:
        meta = c.post(f"/api/engagements/{eid}/template", files={"file": ("ar.xlsx", fh)}).json()["meta"]
    assert (meta["header_row"], meta["first_data_row"], meta["totals_row"], meta["capacity"]) == (15, 18, 38, 20)
    assert meta["tolerance_cell"] == "B13" and meta["existing_rows"] == 20 and len(meta["mapping"]) == 16
    with open(PACK / "Sales_Ledger_FY2025.csv", "rb") as fh:
        assert c.post(f"/api/engagements/{eid}/ledger", files={"file": ("l.csv", fh)}).status_code == 200
    files = [("files", (p.name, open(p, "rb"), "application/pdf")) for p in sorted((PACK / "invoices").glob("*.pdf"))]
    c.post(f"/api/engagements/{eid}/invoices", files=files)
    _wait(c, eid)
    rows = _rows(c, eid)["rows"]
    iid = _by_no(rows, "INV-2025-0004")[0]["invoice_id"]
    c.patch(f"/api/invoices/{iid}/manual", json={"gdn_issued": "Yes", "price_list_matched": "No", "explanation": "Auditor note"})
    r = c.post(f"/api/engagements/{eid}/export/workpaper")
    assert r.status_code == 200 and "exchange rate" in r.headers.get("X-Workpaper-Warnings", "").replace("%20", " ")
    from urllib.parse import quote
    assert quote("شركة_الأفق") in r.headers["content-disposition"]          # Arabic client name kept in file name
    pdf = c.get(f"/api/engagements/{eid}/export/findings.pdf")
    assert "filename*=UTF-8''" + quote("شركة_الأفق") in pdf.headers["content-disposition"]
    out = TMP / "wp_ar.xlsx"; out.write_bytes(r.content)
    o = load_workbook(PACK / "Sales_Vouching_AR_Template.xlsx")["Sales Vouching"]
    ws = load_workbook(out)["Sales Vouching"]
    for rr in range(1, 18):                                         # title, objective table, header, legend, section rows
        for cc in range(1, 17):
            if (rr, cc) != (13, 2):
                assert ws.cell(rr, cc).value == o.cell(rr, cc).value, (rr, cc)
    assert ws["B13"].value == 2                                     # engagement tolerance drives the template's P formula
    assert ws.sheet_view.rightToLeft and ws.freeze_panes == "C18"
    assert ws["A68"].value == "الإجمالي" and ws["D68"].value == "=SUM(D18:D67)" and ws["K68"].value == "=SUM(K18:K67)"
    assert ws["A70"].value == "Work Done" and ws["A73"].value == "الخلاصة "
    assert {"A68:C68", "A70:E70", "A73:B73", "A17:B17", "A1:C1"} <= {str(m) for m in ws.merged_cells.ranges}
    assert ws["J67"].value == "=E67*I67"                            # template row formulas replicated into inserted rows
    assert ws["P67"].value.startswith('=IF(D67="","') and "$B$13" in ws["P67"].value
    assert str(ws.data_validations.dataValidation[0].sqref) == "L18:L67 N18:N67"
    assert str(list(ws.conditional_formatting)[0].sqref) == "H18:H137"
    rr = 18 + [x["invoice_id"] for x in rows].index(iid)
    assert ws[f"L{rr}"].value == "نعم" and ws[f"N{rr}"].value == "لا" and ws[f"M{rr}"].value is None
    assert ws[f"P{rr}"].value.startswith("=") and "Auditor note" in ws[f"P{rr}"].comment.text
    assert ws["H18"].value == rows[0]["invoice_number"] and ws["E18"].value == rows[0]["invoice_amount"]
