"""ZATCA QR: Base64 TLV decoding (Phase 1 tags 1-5, Phase 2 tags 6-9) and detection on invoice PDFs."""
from __future__ import annotations

import base64

import pytest

from audit_app.extraction import zatca_qr as z
from tests.conftest import PACK, TRUTH

P1 = {1: "شركة الواحة للتوريدات الصناعية", 2: "300123456700003", 3: "2025-04-15T10:30:00Z", 4: "1150.00", 5: "150.00"}


def test_phase1_roundtrip_with_arabic_seller():
    d = z.decode(z.encode_tlv(P1))
    assert d["tlv_valid"] and d["phase"] == 1 and not d["errors"]
    assert d["seller_name"] == P1[1] and d["vat_registration"] == "300123456700003"
    assert d["invoice_total"] == 1150.00 and d["vat_total"] == 150.00
    assert str(z.qr_date(d)) == "2025-04-15"


def test_phase2_tags_detected_but_signature_never_claimed_verified():
    d = z.decode(z.encode_tlv({**P1, 6: "aGFzaA==", 7: bytes(range(64)), 8: bytes(range(88)), 9: bytes(70)}))
    assert d["tlv_valid"] and d["phase"] == 2 and d["has_signature_tags"]
    assert d["signature_verified"] is False
    assert any("NOT verified" in w for w in d["warnings"])
    assert base64.b64decode(d["ecdsa_public_key"]) == bytes(range(88))


def test_long_form_length_is_supported():
    long_name = "X" * 200
    d = z.decode(z.encode_tlv({**P1, 1: long_name}))
    assert d["tlv_valid"] and d["seller_name"] == long_name


@pytest.mark.parametrize("payload, msg", [
    ("not base64 at all!!", "not valid Base64"),
    (z.encode_tlv({k: v for k, v in P1.items() if k != 4}), "Mandatory tags missing: 4"),
    (base64.b64encode(base64.b64decode(z.encode_tlv(P1))[:-5]).decode(), "declares"),
    (z.encode_tlv({**P1, 4: "ABC"}), "not numeric"),
])
def test_invalid_payloads_are_reported_not_raised(payload, msg):
    d = z.decode(payload)
    assert not d["tlv_valid"]
    assert any(msg in e for e in d["errors"]), d["errors"]


def test_vat_format_and_timestamp_warnings():
    d = z.decode(z.encode_tlv({**P1, 2: "123", 3: "15/04/2025"}))
    assert d["tlv_valid"]          # structure is valid; content issues are warnings
    assert any("15-digit" in w for w in d["warnings"]) and any("ISO 8601" in w for w in d["warnings"])


@pytest.mark.skipif(not z.detector_available(), reason="opencv not installed")
def test_detection_on_generated_invoices():
    by_q = {}
    for name, t in TRUTH.items():
        by_q.setdefault(t["qr"], []).append(name)
    for scenario in ("valid", "mismatch", "phase2", "none"):
        name = by_q[scenario][0]
        r = z.find_in_pdf(str(PACK / "invoices" / name))
        if scenario == "none":
            assert r["payload"] is None
            continue
        d = r["decoded"]
        assert d["tlv_valid"] and r["page"] >= 1 and len(r["bbox"]) == 4
        assert d["vat_registration"] == "300123456700003"
        expected = TRUTH[name]["gross_amount"] + (1000 if scenario == "mismatch" else 0)
        assert d["invoice_total"] == pytest.approx(expected, abs=0.01)
        assert d["vat_total"] == pytest.approx(TRUTH[name]["vat_amount"], abs=0.01)
        assert d["phase"] == (2 if scenario == "phase2" else 1)


@pytest.mark.skipif(not z.detector_available(), reason="opencv not installed")
def test_detection_on_scanned_invoice():
    name = next(n for n, t in TRUTH.items() if t["scanned"])
    d = z.find_in_pdf(str(PACK / "invoices" / name))["decoded"]
    assert d["tlv_valid"] and d["invoice_total"] == pytest.approx(TRUTH[name]["gross_amount"], abs=0.01)
