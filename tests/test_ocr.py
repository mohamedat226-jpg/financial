"""Local OCR (Tesseract ara+eng) on scanned invoices: values are read in Arabic, English and bilingual layouts,
never guessed, and their confidence never exceeds the OCR reading they came from."""
from __future__ import annotations

import pytest

from audit_app import config
from audit_app.extraction import pipeline
from audit_app.extraction.pdf_text import tesseract_available
from samples.generate_samples import to_scanned
from tests.conftest import PACK, TMP, TRUTH

KEYS = [("invoice_number", "invoice_number"), ("invoice_date", "invoice_date"), ("buyer_name", "buyer_name"),
        ("currency", "currency"), ("taxable_amount", "taxable_amount"), ("vat_amount", "vat_amount"),
        ("gross_amount", "gross_amount")]


@pytest.fixture(scope="module")
def scanned():
    config.OCR_MODE = "auto"
    if not tesseract_available():
        config.OCR_MODE = "off"
        pytest.skip("tesseract / pytesseract not installed")
    picks = {}
    for name, t in sorted(TRUTH.items()):
        if not t["scanned"] and t["doc_type"] == "tax_invoice" and t["qr"] == "valid" and not t["hijri"]:
            picks.setdefault(t["layout"], [])
            if len(picks[t["layout"]]) < 2:
                picks[t["layout"]].append(name)
    out = {}
    d = TMP / "ocr"; d.mkdir(exist_ok=True)
    for layout, names in picks.items():
        for name in names:
            p = d / name
            p.write_bytes(to_scanned((PACK / "invoices" / name).read_bytes()))
            out[name] = pipeline.run(str(p))
    yield out
    config.OCR_MODE = "off"


def test_ocr_reads_scanned_invoices_without_guessing(scanned):
    correct = total = 0
    for name, r in scanned.items():
        t = TRUTH[name]
        assert r["method"] == "ocr" and r["status"] == "extracted" and not r["has_text_layer"]
        for k, tk in KEYS:
            v = r["fields"][k]["value"]
            exp = t[tk]
            ok = v == exp or (k == "buyer_name" and v in (t["buyer_name"], t["buyer_name_ar"]))
            assert ok or v is None, (name, k, v, exp)          # a value is either right or left blank - never wrong
            correct += ok
            total += 1
    assert correct / total >= 0.9, f"{correct}/{total}"


@pytest.mark.parametrize("layout", ["en_a", "en_b", "ar"])
def test_ocr_key_fields_by_layout(scanned, layout):
    for name, r in scanned.items():
        if TRUTH[name]["layout"] != layout:
            continue
        for k, tk in KEYS:
            v, exp = r["fields"][k]["value"], TRUTH[name][tk]
            assert v == exp or (k == "buyer_name" and v in (TRUTH[name]["buyer_name"], TRUTH[name]["buyer_name_ar"])), (name, k, v)


def test_ocr_confidence_and_note(scanned):
    for r in scanned.values():
        assert pipeline.OCR_NOTE in r["notes"]
        assert not any("not configured" in n for n in r["notes"])
        for k, _ in KEYS:
            f = r["fields"][k]
            if f["value"] is not None and f.get("ocr_confidence") is not None:
                assert f["confidence"] <= f["ocr_confidence"] and f["method"] in ("ocr", "derived")
