"""Shared offline test setup: isolated data directory, rule-engine-only extraction and one generated
test pack (50 invoices incl. ZATCA QR scenarios, sales ledger with planted exceptions, templates)."""
from __future__ import annotations

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
os.environ["AUDIT_OCR"] = "off"   # the shared pack keeps its scanned invoice for manual entry; OCR has its own tests
os.environ["AUDIT_ADMIN_PASSWORD"] = "Admin#12345"

from samples.generate_samples import main as _gen  # noqa: E402

PACK = TMP / "pack"
_gen(str(PACK))
TRUTH = {t["file"]: t for t in json.loads((PACK / "ground_truth.json").read_text(encoding="utf-8"))}


def wait_extraction(c, eid, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = c.get(f"/api/engagements/{eid}/progress").json()
        if p.get("total") and not p.get("queued") and not p.get("processing"):
            return p
        time.sleep(0.5)
    raise AssertionError("extraction timed out")


def login(c):
    tok = c.post("/api/login", json={"username": "admin", "password": "Admin#12345"}).json()["token"]
    c.headers["Authorization"] = f"Bearer {tok}"
    return c


@pytest.fixture(scope="session")
def processed():
    """A fully processed engagement (English template, xlsx ledger, all 50 invoices, USD/EUR rates)."""
    from fastapi.testclient import TestClient
    from audit_app.api import app
    with TestClient(app) as c:
        login(c)
        eid = c.post("/api/engagements", json={"client_name": "E2E Client Co", "financial_year": "2025",
                                               "fy_start": "2025-01-01", "fy_end": "2025-12-31", "tolerance": 1.0,
                                               "materiality": 50000}).json()["id"]
        with open(PACK / "Sales_Vouching_Invoices_Sample.xlsx", "rb") as fh:
            assert c.post(f"/api/engagements/{eid}/template", files={"file": ("tpl.xlsx", fh)}).status_code == 200
        with open(PACK / "Sales_Ledger_FY2025.xlsx", "rb") as fh:
            assert c.post(f"/api/engagements/{eid}/ledger", files={"file": ("ledger.xlsx", fh)}).status_code == 200
        files = [("files", (p.name, open(p, "rb"), "application/pdf")) for p in sorted((PACK / "invoices").glob("*.pdf"))]
        assert c.post(f"/api/engagements/{eid}/invoices", files=files).status_code == 200
        progress = wait_extraction(c, eid)
        for cur, rate, src in (("USD", 3.75, "SAMA peg"), ("EUR", 4.05, "Policy rate")):
            assert c.put(f"/api/engagements/{eid}/fx", json={"currency": cur, "rate": rate, "source": src}).status_code == 200
        yield c, eid, progress
