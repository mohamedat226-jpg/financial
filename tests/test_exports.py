"""Specification JSON contract (Section 3.1) and the bilingual executive report."""
from __future__ import annotations

import io

import pypdfium2 as pdfium
import pytest

from tests.conftest import TRUTH

TOP = {"document_id", "source_filename", "page_count", "extraction_confidence", "seller", "buyer", "invoice_metadata",
       "financials", "zatca_qr_data", "audit_flags", "provenance"}


@pytest.fixture(scope="module")
def contract(processed):
    c, eid, _ = processed
    r = c.get(f"/api/engagements/{eid}/export/extraction.json")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    return r.json()


def _by_file(contract, prefix):
    return next(i for i in contract["invoices"] if i["source_filename"].startswith(prefix))


def test_contract_shape(contract):
    assert len(contract["invoices"]) == 50
    for inv in contract["invoices"]:
        assert TOP <= inv.keys()
        assert {"name", "vat_number", "cr_number"} == inv["seller"].keys()
        assert {"name", "vat_number", "address"} == inv["buyer"].keys()
        assert {"invoice_number", "invoice_date", "supply_date", "currency", "exchange_rate", "purchase_order",
                "credit_note_ref"} == inv["invoice_metadata"].keys()
        assert {"line_items_summary", "total_quantity", "gross_discount", "taxable_amount_excl_vat", "vat_rate",
                "vat_amount", "gross_amount_incl_vat", "arithmetic_check_passed"} == inv["financials"].keys()
        # not extracted by the engine -> always null, never inferred
        assert inv["seller"]["cr_number"] is None and inv["buyer"]["address"] is None and inv["invoice_metadata"]["supply_date"] is None


def test_contract_values_match_truth_and_are_net_of_vat(contract):
    for inv in contract["invoices"]:
        t = TRUTH[inv["source_filename"]]
        if t["scanned"]:
            continue
        f = inv["financials"]
        assert f["taxable_amount_excl_vat"] == t["taxable_amount"] and f["gross_amount_incl_vat"] == t["gross_amount"]
        assert f["vat_rate"] == 0.15 and f["arithmetic_check_passed"] is True
        assert inv["invoice_metadata"]["invoice_date"] == t["invoice_date"]
        p = inv["provenance"]["financials.taxable_amount_excl_vat"]
        assert p["page"] >= 1 and p["raw_text"] and len(p["bbox"]) == 4 and p["source_filename"] == inv["source_filename"]


def test_scanned_invoice_has_no_fabricated_values(contract):
    inv = next(i for i in contract["invoices"] if TRUTH[i["source_filename"]]["scanned"])
    f = inv["financials"]
    assert f["taxable_amount_excl_vat"] is None and f["arithmetic_check_passed"] is None
    assert inv["invoice_metadata"]["invoice_number"] is None and inv["provenance"] == {}
    assert inv["zatca_qr_data"]["tlv_valid"] is True            # QR read from the image is reported separately
    assert "NEEDS_MANUAL" in inv["audit_flags"]


def test_qr_mismatch_in_contract(contract):
    name = next(n for n, t in TRUTH.items() if t["qr"] == "mismatch")
    inv = _by_file(contract, name[:3])
    assert inv["zatca_qr_data"]["invoice_total"] == pytest.approx(TRUTH[name]["gross_amount"] + 1000)
    assert inv["zatca_qr_data"]["signature_verified"] is False and "ZATCA_QR_MISMATCH" in inv["audit_flags"]


def test_single_invoice_endpoint_matches_engagement_export(processed, contract):
    c, eid, _ = processed
    rows = c.get(f"/api/engagements/{eid}/rows").json()["rows"]
    row = rows[3]
    one = c.get(f"/api/invoices/{row['invoice_id']}/extraction.json").json()
    assert one == _by_file(contract, row["file"])


def test_executive_report(processed):
    c, eid, _ = processed
    r = c.get(f"/api/engagements/{eid}/export/executive.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert "Executive_Summary" in r.headers["content-disposition"]
    pdf = pdfium.PdfDocument(io.BytesIO(r.content))
    text = " ".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
    s = c.get(f"/api/engagements/{eid}/rows").json()["summary"]
    for needle in ("Executive Audit Summary", "Scope of testing", "reconciliation", "ZATCA",   # bilingual heading rows interleave in text extraction
                   "Engagement team conclusion", "This report does not", "DRAFT",
                   f"{s['total_value_tested_sar']:,.2f}", "Difference above tolerance"):
        assert needle in text, needle
