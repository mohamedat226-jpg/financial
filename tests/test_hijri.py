"""Hijri (Umm al-Qura) dates: converted to Gregorian, never silently accepted."""
from __future__ import annotations

from datetime import date

import pytest

from audit_app.extraction.normalize import HIJRI_NOTE, parse_date
from tests.conftest import TRUTH


@pytest.mark.parametrize("text, expected", [
    ("تاريخ الفاتورة: 01/09/1446 هـ", date(2025, 3, 1)),     # 1 Ramadan 1446
    ("1446/10/01", date(2025, 3, 30)),                         # 1 Shawwal 1446 (Eid al-Fitr)
    ("Date: 01/01/1447H", date(2025, 6, 26)),                  # 1 Muharram 1447, trailing H
    ("التاريخ: 1 رمضان 1446", date(2025, 3, 1)),
    ("22 محرم 1447 هـ", date(2025, 7, 17)),
    ("15 ربيع الأول 1447", date(2025, 9, 7)),
])
def test_hijri_dates_convert_with_a_review_note(text, expected):
    d, raw, note = parse_date(text)
    assert d == expected
    assert note.startswith(HIJRI_NOTE) and "auditor confirmation required" in note


def test_invalid_hijri_date_is_not_guessed():
    d, raw, note = parse_date("31/13/1446")
    assert d is None and "could not be converted" in note


def test_gregorian_dates_unchanged():
    assert parse_date("15/03/2025") == (date(2025, 3, 15), "15/03/2025", None)
    assert parse_date("05/03/2025") == (date(2025, 3, 5), "05/03/2025", "Ambiguous numeric date interpreted as DD/MM/YYYY")
    assert parse_date("2025-03-15")[2] is None


def test_hijri_invoice_end_to_end(processed):
    c, eid, _ = processed
    name = next(n for n, t in TRUTH.items() if t["hijri"])
    row = next(r for r in c.get(f"/api/engagements/{eid}/rows").json()["rows"] if r["file"] == name)
    assert row["invoice_date"] == TRUTH[name]["invoice_date"]           # converted correctly
    d = c.get(f"/api/invoices/{row['invoice_id']}").json()
    f = d["invoice"]["fields"]["invoice_date"]
    assert f["confidence"] < 0.85 and f["note"].startswith(HIJRI_NOTE)
    assert {"HIJRI_DATE", "LOW_CONFIDENCE"} <= {x["code"] for x in d["findings"]}
