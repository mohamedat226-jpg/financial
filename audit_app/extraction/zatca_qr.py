"""ZATCA e-invoice QR code: Base64 TLV decoding and detection on PDF pages.

TLV tags (ZATCA guidance; Phase 1 = tags 1-5, Phase 2 adds 6-9):
  1 seller name (UTF-8)            2 seller VAT registration number   3 invoice timestamp (ISO 8601)
  4 invoice total incl. VAT        5 VAT total
  6 hash of the XML invoice        7 ECDSA signature                  8 ECDSA public key
  9 for simplified invoices: ZATCA technical CA signature of the cryptographic stamp's public key

The decoder only parses and validates the structure and the readable fields. It does NOT verify the
Phase 2 cryptographic signature (that requires the signed XML and ZATCA certificate chain); the result
says so explicitly so the auditor does not rely on it as signature verification.
"""
from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime

from .normalize import valid_saudi_vat

TAG_NAMES = {1: "seller_name", 2: "vat_registration", 3: "timestamp", 4: "invoice_total", 5: "vat_total",
             6: "xml_hash", 7: "ecdsa_signature", 8: "ecdsa_public_key", 9: "zatca_stamp_signature"}
TEXT_TAGS = {1, 2, 3, 4, 5, 6}
REQUIRED_TAGS = (1, 2, 3, 4, 5)


class TLVError(ValueError):
    pass


def encode_tlv(values: dict[int, str | bytes]) -> str:
    """Build a Base64 TLV string (used for test data and sample generation)."""
    out = bytearray()
    for tag in sorted(values):
        v = values[tag]
        b = v if isinstance(v, (bytes, bytearray)) else str(v).encode("utf-8")
        out.append(tag)
        if len(b) < 0x80:
            out.append(len(b))
        else:  # long-form length, defensive: ZATCA values normally fit in one byte
            n = len(b).to_bytes((len(b).bit_length() + 7) // 8, "big")
            out.append(0x80 | len(n)); out += n
        out += b
    return base64.b64encode(bytes(out)).decode("ascii")


def parse_tlv(raw: bytes) -> dict[int, bytes]:
    tags: dict[int, bytes] = {}
    i = 0
    while i < len(raw):
        if i + 2 > len(raw):
            raise TLVError(f"Truncated TLV header at byte {i}")
        tag, ln = raw[i], raw[i + 1]
        i += 2
        if ln & 0x80:
            k = ln & 0x7F
            if k == 0 or k > 3 or i + k > len(raw):
                raise TLVError(f"Invalid length encoding for tag {tag}")
            ln = int.from_bytes(raw[i:i + k], "big")
            i += k
        if i + ln > len(raw):
            raise TLVError(f"Tag {tag} declares {ln} bytes but only {len(raw) - i} remain")
        if tag in tags:
            raise TLVError(f"Tag {tag} repeated")
        tags[tag] = raw[i:i + ln]
        i += ln
    return tags


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return round(float(s.replace(",", "").strip()), 2)
    except ValueError:
        return None


def decode(payload: str) -> dict:
    """Decode a ZATCA QR payload. Never raises; problems are returned in `errors`."""
    res = {"raw": payload, "tlv_valid": False, "phase": None, "seller_name": None, "vat_registration": None,
           "timestamp": None, "invoice_total": None, "vat_total": None, "has_signature_tags": False,
           "signature_verified": False, "errors": [], "warnings": []}
    s = re.sub(r"\s+", "", payload or "")
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (binascii.Error, ValueError):
        res["errors"].append("QR content is not valid Base64 (not a ZATCA TLV QR)")
        return res
    try:
        tags = parse_tlv(raw)
    except TLVError as e:
        res["errors"].append(f"TLV structure invalid: {e}")
        return res
    unknown = sorted(t for t in tags if t not in TAG_NAMES)
    if unknown:
        res["warnings"].append(f"Unknown TLV tags present: {unknown}")
    for t in TEXT_TAGS & tags.keys():
        try:
            val = tags[t].decode("utf-8")
        except UnicodeDecodeError:
            res["errors"].append(f"Tag {t} ({TAG_NAMES[t]}) is not valid UTF-8")
            continue
        res[TAG_NAMES[t]] = val
    for t in (7, 8, 9):
        if t in tags:
            res[TAG_NAMES[t]] = base64.b64encode(tags[t]).decode("ascii")
    missing = [f"{t} ({TAG_NAMES[t]})" for t in REQUIRED_TAGS if t not in tags]
    if missing:
        res["errors"].append("Mandatory tags missing: " + ", ".join(missing))
    res["invoice_total"] = _num(res["invoice_total"]) if res["invoice_total"] is not None else None
    res["vat_total"] = _num(res["vat_total"]) if res["vat_total"] is not None else None
    if 4 in tags and res["invoice_total"] is None:
        res["errors"].append("Tag 4 (invoice total) is not numeric")
    if 5 in tags and res["vat_total"] is None:
        res["errors"].append("Tag 5 (VAT total) is not numeric")
    if res["vat_registration"] and not valid_saudi_vat(res["vat_registration"]):
        res["warnings"].append(f"Seller VAT number {res['vat_registration']} does not follow the 15-digit format (3…3)")
    if res["timestamp"]:
        try:
            datetime.fromisoformat(res["timestamp"].replace("Z", "+00:00"))
        except ValueError:
            res["warnings"].append(f"Timestamp '{res['timestamp']}' is not ISO 8601")
    res["has_signature_tags"] = any(t in tags for t in (6, 7, 8))
    res["phase"] = 2 if res["has_signature_tags"] else 1
    if res["has_signature_tags"]:
        res["warnings"].append("Phase 2 cryptographic tags present - signature NOT verified by this tool "
                               "(requires the signed XML and the ZATCA certificate chain)")
    res["tlv_valid"] = not res["errors"]
    return res


def qr_date(res: dict):
    ts = (res or {}).get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", ts)
        return datetime.fromisoformat(m.group(1)).date() if m else None


# ---------------------------------------------------------------------------------- QR detection
def detector_available() -> bool:
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def find_in_pdf(pdf_path: str, max_pages: int = 2, scale: float = 2.0) -> dict | None:
    """Render the last and first pages and look for a QR code. Returns
    {"payload", "page", "bbox", "decoded"} or None. Raises nothing if the detector is missing (returns None)."""
    if not detector_available():
        return None
    import cv2
    import numpy as np
    import pypdfium2 as pdfium

    from .pdf_text import PDFIUM_LOCK

    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(pdf_path)
    try:
        with PDFIUM_LOCK:
            n = len(pdf)
            sizes = {i: pdf[i].get_size() for i in range(n)}
        order = list(dict.fromkeys([n - 1, 0] + list(range(n))))[:max_pages]
        detectors = [cv2.QRCodeDetector()]
        if hasattr(cv2, "QRCodeDetectorAruco"):
            detectors.append(cv2.QRCodeDetectorAruco())
        for idx in order:
            pw, ph = sizes[idx]
            for sc in (scale, scale * 1.5, scale * 2.5):
                with PDFIUM_LOCK:   # render under the lock; QR detection runs outside it
                    img = np.array(pdf[idx].render(scale=sc, grayscale=True).to_pil().convert("L"))
                for det in detectors:
                    try:
                        txt, pts, _ = det.detectAndDecode(img)
                    except cv2.error:   # OpenCV can assert on some page images; try the next detector/scale
                        continue
                    if txt and pts is not None:
                        pts = pts.reshape(-1, 2)
                        xs, ys = pts[:, 0] / sc, pts[:, 1] / sc
                        # bbox in PDF points with origin top-left (same convention as the text layer)
                        bbox = [round(float(xs.min()), 1), round(float(ys.min()), 1),
                                round(float(xs.max()), 1), round(float(ys.max()), 1)]
                        return {"payload": txt, "page": idx + 1, "bbox": bbox, "page_size": [pw, ph], "decoded": decode(txt)}
        return {"payload": None, "page": None, "bbox": None, "decoded": None}
    finally:
        with PDFIUM_LOCK:
            pdf.close()
