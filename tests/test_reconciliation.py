"""3-pass reconciliation helpers and discrepancy categories (unit level)."""
from __future__ import annotations

from datetime import date

import pytest

from audit_app.engine import recon_category
from audit_app.extraction.normalize import business_days_between, clean_exact, levenshtein_ratio, norm_doc_no


def test_pass1_exact_key_only_ignores_case_and_outer_spaces():
    assert clean_exact(" inv-2025-0001 ") == "INV-2025-0001"
    assert clean_exact("INV-2025-0001") != clean_exact("INV2025-0001")
    assert clean_exact(12345.0) == "12345" and clean_exact("١٢٣") == "123"


@pytest.mark.parametrize("a, b", [
    ("INV-2025-0018", "INV2025-0018"), ("INV-2025-0018", "#2025/18"), ("TAX-INV-00042", "42"),
    ("Invoice 0007", "inv-7"), ("TAX 2025-001", "2025-1"),
])
def test_pass2_normalised_numbers_agree(a, b):
    assert norm_doc_no(a) == norm_doc_no(b) != ""


def test_pass2_keeps_other_series_apart():
    assert norm_doc_no("CN-0001") != norm_doc_no("INV-0001")
    assert norm_doc_no("2025-0018") != norm_doc_no("2025-0019")


def test_levenshtein_threshold():
    assert levenshtein_ratio("Najd Construction Company", "Najd Constructions Co.") > 0.85
    assert levenshtein_ratio("Najd Construction Company", "Desert Logistics LLC") < 0.5
    assert levenshtein_ratio(None, "x") == 0.0


def test_business_days_use_ksa_weekend():
    thu, next_thu = date(2025, 11, 27), date(2025, 12, 4)     # Fri/Sat excluded
    assert business_days_between(thu, next_thu) == 5 == business_days_between(next_thu, thu)
    assert business_days_between(date(2025, 11, 28), date(2025, 11, 29)) == 0   # Fri -> Sat


@pytest.mark.parametrize("status, D, J, K, expected", [
    ("Matched", 100.0, 100.0, 0.0, "MATCHED_CLEAN"),
    ("Matched", 100.4, 100.0, 0.4, "WITHIN_TOLERANCE"),
    ("Amount Mismatch", 1600.0, 100.0, 1500.0, "MATERIAL_DIFFERENCE"),
    ("Customer Mismatch", 100.0, 100.0, 0.0, "MATCHED_CLEAN"),
    ("Not Found in Ledger", None, 100.0, None, "NOT_IN_LEDGER"),
    ("Possible Match", None, 100.0, None, "POSSIBLE_MATCH"),
    ("Duplicate Invoice", None, 100.0, None, "DUPLICATE_INVOICE"),
    ("Matched", 100.0, None, None, "INCOMPLETE_DATA"),
    ("Ledger Not Uploaded", None, 100.0, None, "LEDGER_NOT_UPLOADED"),
])
def test_recon_category(status, D, J, K, expected):
    assert recon_category(status, D, J, K, 1.0) == expected


def test_every_finding_code_has_arabic_procedure():
    from audit_app.engine import PROC
    from audit_app.findings_ar import PROC_AR
    assert set(PROC) <= set(PROC_AR), sorted(set(PROC) - set(PROC_AR))
