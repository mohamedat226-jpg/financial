"""Full-cycle integration (Phase 6): batch extraction -> 3-pass reconciliation -> checks (incl. ZATCA QR and
VAT arithmetic) -> dashboard summary -> working paper and executive findings report."""
from __future__ import annotations

import pytest

from tests.conftest import TRUTH


def _rows(c, eid):
    return c.get(f"/api/engagements/{eid}/rows").json()


def _by_no(rows, no):
    return [r for r in rows if r["invoice_number"] == no]


def test_batch_processing_has_no_failures(processed):
    c, eid, progress = processed
    assert progress["total"] == 50 and not progress.get("failed")
    rows = _rows(c, eid)["rows"]
    assert len(rows) == 50 and all(r["status"] in ("extracted", "needs_manual") for r in rows)


def test_net_amount_never_gross(processed):
    c, eid, _ = processed
    for r in _rows(c, eid)["rows"]:
        t = TRUTH[r["file"]]
        if not t["scanned"]:
            exp = -t["taxable_amount"] if t["doc_type"] == "credit_note" else t["taxable_amount"]
            assert r["invoice_amount"] == exp and r["invoice_amount"] != r["gross_amount"]


def test_three_pass_matching(processed):
    c, eid, _ = processed
    d = _rows(c, eid)
    rows, s = d["rows"], d["summary"]
    r4 = _by_no(rows, "INV-2025-0004")[0]                   # English invoice, same number and customer
    assert r4["match_pass"] == 1 and r4["recon_category"] == "MATCHED_CLEAN"
    r2 = _by_no(rows, "INV-2025-0002")[0]                   # Arabic customer name vs English ledger -> not Pass 1
    assert r2["match_pass"] == 2
    r18 = _by_no(rows, "INV-2025-0018")[0]                  # ledger 'INV2025-0018' -> normalised number
    assert r18["match_pass"] == 2 and r18["ledger_row"] is not None
    r29 = _by_no(rows, "INV-2025-0029")[0]                  # ledger row without number -> heuristic proposal
    assert r29["match_pass"] == 3 and r29["recon_category"] == "POSSIBLE_MATCH" and r29["ledger_amount"] is None
    m = c.get(f"/api/invoices/{r29['invoice_id']}").json()["invoice"]["match"]
    top = m["candidates"][0]
    assert top["pass3_criteria_met"] and top["levenshtein"] > 0.85 and top["business_days"] <= 5
    assert s["match_passes"]["pass1"] >= 30 and s["match_passes"]["pass2"] >= 1 and s["match_passes"]["pass3"] >= 1


def test_discrepancy_categories(processed):
    c, eid, _ = processed
    d = _rows(c, eid)
    rows, s = d["rows"], d["summary"]
    cat = lambda no: _by_no(rows, no)[0]["recon_category"]
    assert cat("INV-2025-0001") == "WITHIN_TOLERANCE"         # +0.40 vs SAR 1 tolerance
    assert cat("INV-2025-0003") == "MATERIAL_DIFFERENCE"      # +1,500
    assert cat("INV-2025-0009") == "MATERIAL_DIFFERENCE"      # ledger includes VAT
    assert cat("INV-2025-0026") == "NOT_IN_LEDGER"
    assert cat("INV-2025-0006") == "DUPLICATE_INVOICE"
    assert all(r["recon_category"] == "DUPLICATE_INVOICE" for r in _by_no(rows, "INV-2025-0045"))
    rc = s["recon_categories"]
    # INV-2025-0999 has no invoice at all; INV-2025-0031's invoice is scanned and still awaits manual entry,
    # so its ledger row cannot be vouched yet (nothing is guessed from the image)
    unmatched = {f["observation"].split("(")[1].split(",")[0] for f in c.get(f"/api/engagements/{eid}/findings").json()
                 if f["code"] == "LEDGER_NO_INVOICE"}
    assert unmatched == {"INV-2025-0999", "INV-2025-0031"}
    assert rc["UNMATCHED_LEDGER_ROW"] == len(unmatched)
    assert sum(v for k, v in rc.items() if k != "UNMATCHED_LEDGER_ROW") == len(rows)   # categories reconcile to KPIs


def test_zatca_qr_checks(processed):
    c, eid, _ = processed
    rows = _rows(c, eid)["rows"]
    findings = c.get(f"/api/engagements/{eid}/findings").json()
    by_inv = {}
    for f in findings:
        by_inv.setdefault(f.get("invoice_id"), set()).add(f["code"])
    q = {r["file"]: r for r in rows}
    for name, t in TRUTH.items():
        r = q[name]
        codes = by_inv.get(r["invoice_id"], set())
        if t["qr"] == "mismatch":
            assert "ZATCA_QR_MISMATCH" in codes and r["qr_status"] == "valid_phase1"
        elif t["qr"] == "none":
            assert "ZATCA_QR_MISSING" in codes and r["qr_status"] == "not_found"
        elif t["qr"] == "phase2":
            assert r["qr_status"] == "valid_phase2" and r["zatca_qr"]["signature_verified"] is False
        else:
            assert r["qr_status"] == "valid_phase1", name
            assert not codes & {"ZATCA_QR_MISMATCH", "ZATCA_QR_INVALID", "ZATCA_QR_MISSING"}, (name, codes)
    scanned = next(r for r in rows if TRUTH[r["file"]]["scanned"])
    # QR read from the scanned image is evidence only: it never fills the working-paper fields
    assert scanned["qr_status"] == "valid_phase1" and scanned["invoice_amount"] is None and scanned["invoice_number"] is None


def test_vat_arithmetic_error_threshold(processed):
    c, eid, _ = processed
    r = _by_no(_rows(c, eid)["rows"], "INV-2025-0002")[0]
    iid, g = r["invoice_id"], r["gross_amount"]
    codes = lambda: {f["code"] for f in c.get(f"/api/invoices/{iid}").json()["findings"]}
    assert "VAT_ARITHMETIC_ERROR" not in codes()
    edit = lambda v: c.patch(f"/api/invoices/{iid}/fields", json={"field": "gross_amount", "value": v, "reason": "test"})
    assert edit(round(g + 0.04, 2)).status_code == 200
    assert "VAT_ARITHMETIC_ERROR" not in codes()               # within 0.05
    assert edit(round(g + 0.06, 2)).status_code == 200
    assert "VAT_ARITHMETIC_ERROR" in codes()                   # beyond 0.05
    assert edit(g).status_code == 200
    assert "VAT_ARITHMETIC_ERROR" not in codes()


def test_low_confidence_threshold_is_85_percent(processed):
    c, eid, _ = processed
    assert _rows(c, eid)["summary"]["low_confidence_threshold"] == pytest.approx(0.85)


def test_outputs_generated(processed):
    c, eid, _ = processed
    assert c.post(f"/api/engagements/{eid}/export/workpaper").status_code == 200
    pdf = c.get(f"/api/engagements/{eid}/export/findings.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    csv_ = c.get(f"/api/engagements/{eid}/export/extraction.csv").content.decode("utf-8-sig")
    header = csv_.splitlines()[0].split(",")
    assert {"recon_category", "match_pass", "qr_status"} <= set(header)


def test_row_risk_matches_open_findings(processed):
    """The dashboard's risk filter/chart uses `risk` on each row: the highest risk among its open findings."""
    c, eid, _ = processed
    rows = _rows(c, eid)["rows"]
    order = {"High": 0, "Medium": 1, "Low": 2}
    by_inv = {}
    for f in c.get(f"/api/engagements/{eid}/findings").json():
        if f.get("invoice_id") and f["review_status"] != "cleared":
            by_inv.setdefault(f["invoice_id"], []).append(f["risk"])
    for r in rows:
        risks = by_inv.get(r["invoice_id"], [])
        assert r["open_findings"] == len(risks)
        assert r["risk"] == (min(risks, key=order.get) if risks else None)
    assert any(r["risk"] == "High" for r in rows) and any(r["risk"] is None for r in rows)
