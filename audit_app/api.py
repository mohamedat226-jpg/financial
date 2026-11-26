"""FastAPI application: engagements, uploads, extraction, review, matching, dashboard and exports."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, contract, db, engine, ledger, reports, security, workpaper
from .extraction import pipeline
from .extraction.normalize import norm_party, parse_amount, to_date
from .modules import MODULES
from .security import current_user, require

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    _startup()
    yield


app = FastAPI(title="AI Audit Dashboard", version="1.0.0", lifespan=_lifespan)
_pool = ThreadPoolExecutor(max_workers=config.WORKERS, thread_name_prefix="extract")
_recompute_lock = threading.Lock()


def _startup():
    db.init()
    pwd = security.ensure_bootstrap_admin()
    if pwd:
        print(f"\n*** Initial admin account created: username 'admin', password '{pwd}' - change it after first login ***\n")
    # resume extraction interrupted by a restart
    for r in db.q("SELECT id FROM invoices WHERE status IN ('queued','processing')"):
        _pool.submit(_process_invoice, r["id"])


# ------------------------------------------------------------------------------------------ helpers
def _eng(eid: int) -> dict:
    e = db.q1("SELECT * FROM engagements WHERE id=?", (eid,))
    if not e:
        raise HTTPException(404, "Engagement not found")
    return e


def _open(eid: int) -> dict:
    e = _eng(eid)
    if e["status"] == "finalized":
        raise HTTPException(409, "Engagement is finalized; changes are locked")
    return e


def _inv(iid: int) -> dict:
    i = db.q1("SELECT * FROM invoices WHERE id=?", (iid,))
    if not i:
        raise HTTPException(404, "Invoice not found")
    return i


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "file")
    return re.sub(r"[^\w؀-ۿ .()\-]+", "_", name)[:180] or "file"


def _store(eid: int, up: UploadFile, kind: str, user: str) -> dict:
    name = _safe_name(up.filename)
    ext = Path(name).suffix.lower()
    data = up.file.read()
    allowed = {"invoice": {".pdf"}, "template": {".xlsx", ".xlsm"}, "ledger": {".xlsx", ".xlsm", ".csv", ".xls"}}[kind]
    if ext not in allowed:
        raise HTTPException(400, f"{name}: file type {ext or '(none)'} not allowed for {kind}")
    limit = config.MAX_PDF_BYTES if kind == "invoice" else config.MAX_XLSX_BYTES
    if len(data) > limit:
        raise HTTPException(413, f"{name}: exceeds size limit")
    if not data:
        raise HTTPException(400, f"{name}: empty file")
    if ext == ".pdf" and not data[:1024].lstrip().startswith(b"%PDF-"):
        raise HTTPException(400, f"{name}: not a valid PDF")
    if ext in (".xlsx", ".xlsm") and not data.startswith(b"PK\x03\x04"):
        raise HTTPException(400, f"{name}: not a valid Excel workbook")
    if ext == ".xls" and not data.startswith(b"\xd0\xcf\x11\xe0"):
        raise HTTPException(400, f"{name}: not a valid Excel 97-2003 workbook")
    if ext == ".csv" and b"\x00" in data[:4096]:
        raise HTTPException(400, f"{name}: not a text CSV file")
    sha = hashlib.sha256(data).hexdigest()
    folder = config.UPLOAD_DIR / f"E{eid}" / kind
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(data)
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP)   # originals are preserved read-only
    with db.tx() as c:
        cur = c.execute("INSERT INTO files(engagement_id,kind,original_name,stored_path,sha256,size,uploaded_by,uploaded_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (eid, kind, name, str(path), sha, len(data), user, db.now()))
        fid = cur.lastrowid
        db.log(user, eid, "file", fid, f"upload_{kind}", note=f"{name} sha256={sha}", c=c)
    return {"id": fid, "name": name, "sha256": sha, "path": str(path), "size": len(data)}


def _schedule_recompute(eid: int, user: str = "system"):
    with _recompute_lock:
        pending = db.q1("SELECT COUNT(*) n FROM invoices WHERE engagement_id=? AND status IN ('queued','processing')", (eid,))["n"]
        if pending == 0:
            engine.recompute(eid, user)


def _process_invoice(iid: int):
    inv = db.q1("SELECT i.*, f.stored_path FROM invoices i JOIN files f ON f.id=i.file_id WHERE i.id=?", (iid,))
    if not inv:
        return
    with db.tx() as c:
        c.execute("UPDATE invoices SET status='processing', updated_at=? WHERE id=?", (db.now(), iid))
    try:
        res = pipeline.run(inv["stored_path"])
        prev = db.jload(inv["fields"], {}) or {}
        fields = res["fields"]
        for k, f in prev.items():   # never overwrite auditor-entered/confirmed values on re-extraction
            if f.get("method") == "auditor" or f.get("confirmed"):
                fields[k] = f
        with db.tx() as c:
            c.execute("UPDATE invoices SET status=?, doc_type=?, language=?, page_count=?, has_text_layer=?, extraction_method=?, "
                      "overall_confidence=?, fields=?, ai_fields=COALESCE(ai_fields, ?), line_items=?, extraction_notes=?, "
                      "zatca_qr=?, error=NULL, updated_at=? WHERE id=?",
                      (res["status"], res["doc_type"], res["language"], res["page_count"], int(res["has_text_layer"]),
                       res["method"], pipeline.overall_confidence(fields), db.jdump(fields), db.jdump(res["fields"]),
                       db.jdump(res["line_items"]), db.jdump(res["notes"]), db.jdump(res.get("zatca_qr")), db.now(), iid))
            db.log("system", inv["engagement_id"], "invoice", iid, "extracted",
                   note=f"{res['method']} status={res['status']} confidence={res['confidence']}", c=c)
    except Exception as e:  # noqa: BLE001
        with db.tx() as c:
            c.execute("UPDATE invoices SET status='failed', error=?, updated_at=? WHERE id=?",
                      (f"{type(e).__name__}: {e}", db.now(), iid))
            db.log("system", inv["engagement_id"], "invoice", iid, "extraction_failed", note=traceback.format_exc()[-800:], c=c)
    _schedule_recompute(inv["engagement_id"])


# ------------------------------------------------------------------------------------------- auth
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn):
    token = security.login(body.username, body.password)
    u = db.q1("SELECT id,username,full_name,role FROM users WHERE username=?", (body.username.strip().lower(),))
    return {"token": token, "user": u}


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {**user, "permissions": {p: security.can(user, p) for p in security.PERMISSIONS},
            "ai_available": config.ai_available(), "extraction_mode": config.EXTRACTION_MODE}


class UserIn(BaseModel):
    username: str
    full_name: str
    password: str
    role: str


@app.get("/api/users")
def users(user=Depends(require("manage_users"))):
    return db.q("SELECT id,username,full_name,role,active,created_at FROM users ORDER BY id")


@app.post("/api/users")
def add_user(body: UserIn, user=Depends(require("manage_users"))):
    try:
        uid = security.create_user(body.username, body.full_name, body.password, body.role)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        raise HTTPException(400, "Username already exists")
    db.log(user["username"], None, "user", uid, "create_user", new={"username": body.username, "role": body.role})
    return {"id": uid}


class UserPatch(BaseModel):
    role: str | None = None
    active: bool | None = None
    password: str | None = None


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, user=Depends(require("manage_users"))):
    with db.tx() as c:
        if body.role:
            if body.role not in security.ROLE_RANK:
                raise HTTPException(400, "invalid role")
            c.execute("UPDATE users SET role=? WHERE id=?", (body.role, uid))
        if body.active is not None:
            c.execute("UPDATE users SET active=? WHERE id=?", (int(body.active), uid))
        if body.password:
            if len(body.password) < 8:
                raise HTTPException(400, "password must be at least 8 characters")
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (security.hash_password(body.password), uid))
        db.log(user["username"], None, "user", uid, "update_user", new=body.model_dump(exclude={"password"}), c=c)
    return {"ok": True}


@app.get("/api/modules")
def modules(user=Depends(current_user)):
    return MODULES


# ------------------------------------------------------------------------------------- engagements
class EngIn(BaseModel):
    client_name: str = Field(min_length=1, max_length=200)
    financial_year: str = Field(min_length=2, max_length=20)
    fy_start: date
    fy_end: date
    tolerance: float = 1.0
    materiality: float | None = None
    cutoff_days: int = 7
    module: str = "sales_vouching"


@app.get("/api/engagements")
def list_engagements(user=Depends(current_user)):
    rows = db.q("SELECT e.*, (SELECT COUNT(*) FROM invoices i WHERE i.engagement_id=e.id) AS invoice_count "
                "FROM engagements e ORDER BY e.id DESC")
    return rows


@app.post("/api/engagements")
def create_engagement(body: EngIn, user=Depends(require("edit"))):
    if body.fy_end <= body.fy_start:
        raise HTTPException(400, "Financial year end must be after start")
    if body.module not in {m["key"] for m in MODULES if m["status"] == "available"}:
        raise HTTPException(400, "Module not available")
    with db.tx() as c:
        cur = c.execute("INSERT INTO engagements(module,client_name,financial_year,fy_start,fy_end,tolerance,materiality,cutoff_days,"
                        "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (body.module, body.client_name.strip(), body.financial_year.strip(), body.fy_start.isoformat(),
                         body.fy_end.isoformat(), body.tolerance, body.materiality, body.cutoff_days, user["username"], db.now()))
        eid = cur.lastrowid
        db.log(user["username"], eid, "engagement", eid, "create", new=body.model_dump(mode="json"), c=c)
    return {"id": eid}


@app.get("/api/engagements/{eid}")
def get_engagement(eid: int, user=Depends(current_user)):
    e = _eng(eid)
    files = db.q("SELECT id,kind,original_name,sha256,size,uploaded_by,uploaded_at,meta FROM files WHERE engagement_id=? ORDER BY id DESC", (eid,))
    for f in files:
        f["meta"] = db.jload(f["meta"], {})
    return {**e, "fx_rates": list(engine.fx_rates(eid).values()), "files": files,
            "aliases": db.q("SELECT * FROM customer_aliases WHERE engagement_id=?", (eid,)),
            "progress": _progress(eid), "summary": engine.summary(eid)}


class SettingsIn(BaseModel):
    tolerance: float | None = Field(default=None, ge=0)
    materiality: float | None = Field(default=None, ge=0)
    cutoff_days: int | None = Field(default=None, ge=0, le=90)


@app.patch("/api/engagements/{eid}/settings")
def settings(eid: int, body: SettingsIn, user=Depends(require("configure"))):
    e = _open(eid)
    with db.tx() as c:
        for k, v in body.model_dump(exclude_none=True).items():
            c.execute(f"UPDATE engagements SET {k}=? WHERE id=?", (v, eid))
            db.log(user["username"], eid, "engagement", eid, "settings", k, e[k], v, c=c)
    engine.recompute(eid, user["username"])
    return {"ok": True}


class FxIn(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    rate: float = Field(gt=0)
    source: str = Field(min_length=2, max_length=200)


@app.put("/api/engagements/{eid}/fx")
def set_fx(eid: int, body: FxIn, user=Depends(require("configure"))):
    _open(eid)
    if body.currency == "SAR" and body.rate != 1:
        raise HTTPException(400, "SAR is the functional currency (rate 1)")
    old = db.q1("SELECT * FROM fx_rates WHERE engagement_id=? AND currency=?", (eid, body.currency))
    with db.tx() as c:
        c.execute("INSERT INTO fx_rates(engagement_id,currency,rate,source,entered_by,entered_at) VALUES (?,?,?,?,?,?) "
                  "ON CONFLICT(engagement_id,currency) DO UPDATE SET rate=excluded.rate, source=excluded.source, "
                  "entered_by=excluded.entered_by, entered_at=excluded.entered_at",
                  (eid, body.currency, body.rate, body.source, user["username"], db.now()))
        db.log(user["username"], eid, "fx_rate", None, "set_fx", body.currency, old and old["rate"], body.rate, body.source, c=c)
    engine.recompute(eid, user["username"])
    return {"ok": True}


# ---------------------------------------------------------------------------------------- uploads
@app.post("/api/engagements/{eid}/template")
def upload_template(eid: int, file: UploadFile = File(...), user=Depends(require("edit"))):
    _open(eid)
    info = _store(eid, file, "template", user["username"])
    try:
        meta = workpaper.inspect_template(info["path"], _eng(eid))
    except workpaper.TemplateError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Cannot read workbook: {e}")
    with db.tx() as c:
        c.execute("UPDATE files SET meta=? WHERE id=?", (db.jdump(meta), info["id"]))
    return {**info, "meta": meta}


@app.post("/api/engagements/{eid}/ledger")
def upload_ledger(eid: int, file: UploadFile = File(...), mapping: str | None = Form(default=None),
                  user=Depends(require("edit"))):
    _open(eid)
    info = _store(eid, file, "ledger", user["username"])
    try:
        entries, used, warnings = ledger.parse(info["path"], db.jload(mapping) if mapping else None)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Ledger could not be read: {e}")
    with db.tx() as c:
        old = c.execute("SELECT COUNT(*) FROM ledger_entries WHERE engagement_id=?", (eid,)).fetchone()[0]
        c.execute("DELETE FROM ledger_entries WHERE engagement_id=?", (eid,))
        for en in entries:
            c.execute("INSERT INTO ledger_entries(engagement_id,file_id,row_number,invoice_number,customer,date,amount,currency,raw) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (eid, info["id"], en["row_number"], en["invoice_number"], en["customer"],
                                                     en["date"], en["amount"], en["currency"], db.jdump(en["raw"])))
        # a new ledger invalidates previous links (auditor-confirmed links are re-evaluated by row content)
        c.execute("UPDATE invoices SET match=NULL WHERE engagement_id=?", (eid,))
        c.execute("UPDATE files SET meta=? WHERE id=?", (db.jdump({"mapping": used, "rows": len(entries), "warnings": warnings}), info["id"]))
        db.log(user["username"], eid, "ledger", info["id"], "load_ledger", old=f"{old} rows", new=f"{len(entries)} rows",
               note="; ".join(warnings)[:500], c=c)
    engine.recompute(eid, user["username"])
    return {**info, "rows": len(entries), "mapping": used, "warnings": warnings}


@app.post("/api/engagements/{eid}/invoices")
def upload_invoices(eid: int, files: list[UploadFile] = File(...), user=Depends(require("edit"))):
    _open(eid)
    if len(files) > config.MAX_FILES_PER_BATCH:
        raise HTTPException(400, f"Maximum {config.MAX_FILES_PER_BATCH} files per upload")
    accepted, rejected = [], []
    for f in files:
        try:
            info = _store(eid, f, "invoice", user["username"])
        except HTTPException as e:
            rejected.append({"name": f.filename, "error": e.detail})
            continue
        with db.tx() as c:
            cur = c.execute("INSERT INTO invoices(engagement_id,file_id,status,created_at,updated_at) VALUES (?,?,?,?,?)",
                            (eid, info["id"], "queued", db.now(), db.now()))
            accepted.append({"invoice_id": cur.lastrowid, "name": info["name"]})
    for a in accepted:
        _pool.submit(_process_invoice, a["invoice_id"])
    return {"accepted": accepted, "rejected": rejected}


def _progress(eid: int) -> dict:
    rows = db.q("SELECT status, COUNT(*) n FROM invoices WHERE engagement_id=? GROUP BY status", (eid,))
    d = {r["status"]: r["n"] for r in rows}
    d["total"] = sum(d.values())
    return d


@app.get("/api/engagements/{eid}/progress")
def progress(eid: int, user=Depends(current_user)):
    return _progress(eid)


# --------------------------------------------------------------------------------- data & dashboard
@app.get("/api/engagements/{eid}/rows")
def rows(eid: int, user=Depends(current_user)):
    _eng(eid)
    return {"rows": engine.compute_rows(eid), "summary": engine.summary(eid), "progress": _progress(eid)}


@app.get("/api/engagements/{eid}/findings")
def findings(eid: int, include_resolved: bool = False, user=Depends(current_user)):
    sql = "SELECT * FROM findings WHERE engagement_id=?" + ("" if include_resolved else " AND active=1")
    return db.q(sql + " ORDER BY CASE risk WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, id", (eid,))


@app.get("/api/engagements/{eid}/ledger")
def ledger_rows(eid: int, user=Depends(current_user)):
    return engine.load_ledger(eid)


class FindingPatch(BaseModel):
    review_status: str = Field(pattern=r"^(open|cleared|escalated|adjustment_proposed)$")
    reviewer_note: str | None = None


@app.patch("/api/findings/{fid}")
def patch_finding(fid: int, body: FindingPatch, user=Depends(require("edit"))):
    f = db.q1("SELECT * FROM findings WHERE id=?", (fid,))
    if not f:
        raise HTTPException(404)
    _open(f["engagement_id"])
    if body.review_status == "cleared" and not (body.reviewer_note or "").strip():
        raise HTTPException(400, "A reviewer note is required to clear a finding")
    with db.tx() as c:
        c.execute("UPDATE findings SET review_status=?, reviewer_note=?, reviewed_by=? WHERE id=?",
                  (body.review_status, body.reviewer_note, user["username"], fid))
        db.log(user["username"], f["engagement_id"], "finding", fid, "review", "review_status", f["review_status"],
               body.review_status, body.reviewer_note, c=c)
    return {"ok": True}


@app.get("/api/engagements/{eid}/audit-log")
def audit_log(eid: int, limit: int = 500, user=Depends(current_user)):
    return db.q("SELECT * FROM audit_log WHERE engagement_id=? ORDER BY id DESC LIMIT ?", (eid, min(limit, 5000)))


class AliasIn(BaseModel):
    invoice_name: str
    ledger_name: str


@app.post("/api/engagements/{eid}/aliases")
def add_alias(eid: int, body: AliasIn, user=Depends(require("edit"))):
    _open(eid)
    a, b = norm_party(body.invoice_name), norm_party(body.ledger_name)
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO customer_aliases(engagement_id,invoice_name,ledger_name,confirmed_by,confirmed_at) "
                  "VALUES (?,?,?,?,?)", (eid, a, b, user["username"], db.now()))
        db.log(user["username"], eid, "customer_alias", None, "confirm_alias", new={"invoice": body.invoice_name, "ledger": body.ledger_name}, c=c)
    engine.recompute(eid, user["username"])
    return {"ok": True}


# ------------------------------------------------------------------------------------ invoice review
@app.get("/api/invoices/{iid}")
def invoice_detail(iid: int, user=Depends(current_user)):
    i = _inv(iid)
    eid = i["engagement_id"]
    inv = next(x for x in engine.load_invoices(eid) if x["id"] == iid)
    row = next((r for r in engine.compute_rows(eid) if r["invoice_id"] == iid), None)
    m = inv["match"] or {}
    le = db.q1("SELECT * FROM ledger_entries WHERE id=?", (m.get("ledger_id"),)) if m.get("ledger_id") else None
    if le:
        le["raw"] = db.jload(le["raw"], {})
    order = [x["id"] for x in db.q("SELECT id FROM invoices WHERE engagement_id=? ORDER BY id", (eid,))]
    idx = order.index(iid)
    return {"invoice": inv, "row": row, "ledger": le,
            "findings": db.q("SELECT * FROM findings WHERE invoice_id=? AND active=1", (iid,)),
            "comments": db.q("SELECT * FROM comments WHERE invoice_id=? ORDER BY id", (iid,)),
            "history": db.q("SELECT * FROM audit_log WHERE entity='invoice' AND entity_id=? ORDER BY id DESC", (iid,)),
            "prev_id": order[idx - 1] if idx > 0 else None, "next_id": order[idx + 1] if idx + 1 < len(order) else None,
            "engagement_status": _eng(eid)["status"]}


@app.get("/api/invoices/{iid}/extraction.json")
def invoice_extraction_json(iid: int, user=Depends(current_user)):
    """One invoice in the specification's JSON contract (Section 3.1), with provenance."""
    i = _inv(iid)
    inv = next(x for x in engine.load_invoices(i["engagement_id"]) if x["id"] == iid)
    row = next((r for r in engine.compute_rows(i["engagement_id"]) if r["invoice_id"] == iid), None)
    return contract.invoice_contract(inv, row, db.q("SELECT * FROM findings WHERE invoice_id=? AND active=1", (iid,)))


@app.get("/api/invoices/{iid}/pdf")
def invoice_pdf(iid: int, user=Depends(current_user)):
    i = db.q1("SELECT f.stored_path, f.original_name FROM invoices i JOIN files f ON f.id=i.file_id WHERE i.id=?", (iid,))
    if not i:
        raise HTTPException(404)
    return FileResponse(i["stored_path"], media_type="application/pdf",
                        headers={"Content-Disposition": f"inline; filename=\"invoice_{iid}.pdf\""})


@app.get("/api/invoices/{iid}/page/{page}")
def invoice_page(iid: int, page: int, user=Depends(current_user)):
    i = db.q1("SELECT f.stored_path, i.page_count FROM invoices i JOIN files f ON f.id=i.file_id WHERE i.id=?", (iid,))
    if not i:
        raise HTTPException(404)
    from .extraction.pdf_text import PDFIUM_LOCK, render_page_png
    import pypdfium2 as pdfium
    with PDFIUM_LOCK:
        _pdf = pdfium.PdfDocument(i["stored_path"])
        try:
            n = len(_pdf)
        finally:
            _pdf.close()
    if not 1 <= page <= n:
        raise HTTPException(404, "Page out of range")
    import pdfplumber
    with pdfplumber.open(i["stored_path"]) as pdf:
        w, h = float(pdf.pages[page - 1].width), float(pdf.pages[page - 1].height)
    png = render_page_png(i["stored_path"], page)
    return Response(png, media_type="image/png", headers={"X-Page-Width": str(w), "X-Page-Height": str(h),
                                                          "X-Page-Count": str(n), "Cache-Control": "private, max-age=3600"})


NUMERIC_FIELDS = pipeline.NUMERIC


class FieldEdit(BaseModel):
    field: str
    value: str | float | None
    reason: str | None = None


@app.patch("/api/invoices/{iid}/fields")
def edit_field(iid: int, body: FieldEdit, user=Depends(require("edit"))):
    i = _inv(iid)
    _open(i["engagement_id"])
    if body.field not in pipeline.FIELDS:
        raise HTTPException(400, "Unknown field")
    val = body.value
    if isinstance(val, str):
        val = val.strip() or None
    if val is not None and body.field in NUMERIC_FIELDS:
        val = parse_amount(str(val)) if not isinstance(val, (int, float)) else float(val)
        if val is None:
            raise HTTPException(400, "Invalid number")
    if val is not None and body.field == "invoice_date":
        d = to_date(val)
        if not d:
            raise HTTPException(400, "Invalid date (use YYYY-MM-DD)")
        val = d.isoformat()
    if val is not None and body.field == "currency":
        if not re.fullmatch(r"[A-Za-z]{3}", str(val)):
            raise HTTPException(400, "Currency must be a 3-letter ISO code")
        val = str(val).upper()
    fields = db.jload(i["fields"], {}) or {}
    old = fields.get(body.field) or {}
    if old.get("value") == val:
        raise HTTPException(400, "Value unchanged - use confirm instead")
    fields[body.field] = {**old, "value": val, "method": "auditor", "confirmed": True, "confidence": 1.0,
                          "edited_by": user["username"], "edited_at": db.now(), "previous_value": old.get("value"),
                          "note": body.reason or old.get("note")}
    status = i["status"]
    if status == "needs_manual" and any(fields.get(k, {}).get("value") is not None for k in ("invoice_number", "taxable_amount")):
        status = "extracted"
    with db.tx() as c:
        c.execute("UPDATE invoices SET fields=?, overall_confidence=?, status=?, review_status=CASE WHEN review_status='approved' "
                  "THEN 'pending' ELSE review_status END, updated_at=? WHERE id=?",
                  (db.jdump(fields), pipeline.overall_confidence(fields), status, db.now(), iid))
        db.log(user["username"], i["engagement_id"], "invoice", iid, "edit_field", body.field, old.get("value"), val, body.reason, c=c)
        if i["review_status"] == "approved":
            db.log(user["username"], i["engagement_id"], "invoice", iid, "approval_revoked", note="Data changed after approval", c=c)
    engine.recompute(i["engagement_id"], user["username"])
    return {"ok": True}


class ConfirmIn(BaseModel):
    fields: list[str]


@app.post("/api/invoices/{iid}/confirm")
def confirm_fields(iid: int, body: ConfirmIn, user=Depends(require("edit"))):
    i = _inv(iid)
    _open(i["engagement_id"])
    fields = db.jload(i["fields"], {}) or {}
    done = []
    for k in body.fields:
        f = fields.get(k)
        if f is None or f.get("value") is None:
            continue
        if not f.get("confirmed"):
            f.update(confirmed=True, confirmed_by=user["username"], confirmed_at=db.now())
            done.append(k)
    with db.tx() as c:
        c.execute("UPDATE invoices SET fields=?, updated_at=? WHERE id=?", (db.jdump(fields), db.now(), iid))
        for k in done:
            db.log(user["username"], i["engagement_id"], "invoice", iid, "confirm_field", k, None, fields[k]["value"], c=c)
    engine.run_checks(i["engagement_id"])
    return {"confirmed": done}


class ManualIn(BaseModel):
    gdn_issued: str | None = Field(default=None, pattern=r"^(Yes|No|N/A)?$")
    gdn_date: str | None = None
    price_list_matched: str | None = Field(default=None, pattern=r"^(Yes|No|N/A)?$")
    invoice_approved: str | None = Field(default=None, pattern=r"^(Yes|No|N/A)?$")
    explanation: str | None = Field(default=None, max_length=1000)


@app.patch("/api/invoices/{iid}/manual")
def edit_manual(iid: int, body: ManualIn, user=Depends(require("edit"))):
    i = _inv(iid)
    _open(i["engagement_id"])
    man = db.jload(i["manual"], {}) or {}
    upd = body.model_dump(exclude_unset=True)
    if upd.get("gdn_date"):
        d = to_date(upd["gdn_date"])
        if not d:
            raise HTTPException(400, "Invalid GDN date")
        upd["gdn_date"] = d.isoformat()
    with db.tx() as c:
        for k, v in upd.items():
            v = v or None
            if man.get(k) != v:
                db.log(user["username"], i["engagement_id"], "invoice", iid, "manual_evidence", k, man.get(k), v, c=c)
                man[k] = v
                man[f"{k}_by"] = user["username"]
        c.execute("UPDATE invoices SET manual=?, updated_at=? WHERE id=?", (db.jdump(man), db.now(), iid))
    engine.run_checks(i["engagement_id"])
    return {"ok": True}


class ReviewIn(BaseModel):
    status: str = Field(pattern=r"^(pending|approved|investigate|rejected)$")
    comment: str | None = None


@app.post("/api/invoices/{iid}/review")
def review(iid: int, body: ReviewIn, user=Depends(require("approve"))):
    i = _inv(iid)
    _open(i["engagement_id"])
    fields = db.jload(i["fields"], {}) or {}
    if body.status == "approved":
        unconfirmed = [k for k in engine.REQUIRED if not (fields.get(k) or {}).get("confirmed")]
        if unconfirmed:
            raise HTTPException(400, "Confirm these fields before approval: " + ", ".join(unconfirmed))
        open_high = db.q("SELECT code FROM findings WHERE invoice_id=? AND active=1 AND risk='High' AND review_status='open'", (iid,))
        if open_high and not (body.comment or "").strip():
            raise HTTPException(400, "Open high-risk findings (" + ", ".join(f["code"] for f in open_high) +
                                ") - clear them or provide an approval comment")
    if body.status in ("investigate", "rejected") and not (body.comment or "").strip():
        raise HTTPException(400, "A comment is required")
    with db.tx() as c:
        c.execute("UPDATE invoices SET review_status=?, reviewer=?, reviewed_at=?, updated_at=? WHERE id=?",
                  (body.status, user["full_name"], db.now(), db.now(), iid))
        if body.comment:
            c.execute("INSERT INTO comments(invoice_id,user,ts,text) VALUES (?,?,?,?)",
                      (iid, user["full_name"], db.now(), f"[{body.status}] {body.comment}"))
        db.log(user["username"], i["engagement_id"], "invoice", iid, "review", "review_status", i["review_status"], body.status,
               body.comment, c=c)
    return {"ok": True}


class MatchIn(BaseModel):
    action: str = Field(pattern=r"^(confirm|reject|reset)$")
    ledger_id: int | None = None
    reason: str | None = None


@app.post("/api/invoices/{iid}/match")
def match_action(iid: int, body: MatchIn, user=Depends(require("edit"))):
    i = _inv(iid)
    eid = i["engagement_id"]
    _open(eid)
    m = db.jload(i["match"], {}) or {}
    if body.action == "confirm":
        le = db.q1("SELECT * FROM ledger_entries WHERE id=? AND engagement_id=?", (body.ledger_id, eid))
        if not le:
            raise HTTPException(400, "Ledger entry not found")
        other = [x["id"] for x in engine.load_invoices(eid)
                 if x["id"] != iid and (x["match"] or {}).get("ledger_id") == le["id"]
                 and (x["match"] or {}).get("method") == "auditor_confirmed"]
        if other:
            raise HTTPException(409, f"Ledger row already confirmed against invoice {other[0]}")
        new = {**m, "method": "auditor_confirmed", "ledger_id": le["id"], "confirmed_by": user["full_name"], "confirmed_at": db.now(),
               "reason": body.reason}
    elif body.action == "reject":
        new = {"method": "auditor_rejected", "ledger_id": None, "status": "Not Found in Ledger", "confirmed_by": user["full_name"],
               "confirmed_at": db.now(), "reason": body.reason, "evidence": [f"Proposed matches rejected by {user['full_name']}"],
               "candidates": []}
    else:
        new = None
    with db.tx() as c:
        c.execute("UPDATE invoices SET match=? WHERE id=?", (db.jdump(new) if new else None, iid))
        db.log(user["username"], eid, "invoice", iid, f"match_{body.action}", "ledger_id", m.get("ledger_id"),
               (new or {}).get("ledger_id"), body.reason, c=c)
    engine.recompute(eid, user["username"])
    return {"ok": True}


class FxOverride(BaseModel):
    rate: float | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3)


@app.post("/api/invoices/{iid}/fx")
def fx_override(iid: int, body: FxOverride, user=Depends(require("edit"))):
    i = _inv(iid)
    _open(i["engagement_id"])
    with db.tx() as c:
        c.execute("UPDATE invoices SET fx_override=? WHERE id=?", (body.rate, iid))
        db.log(user["username"], i["engagement_id"], "invoice", iid, "fx_override", "exchange_rate", i["fx_override"], body.rate, body.reason, c=c)
    engine.recompute(i["engagement_id"], user["username"])
    return {"ok": True}


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/invoices/{iid}/comments")
def add_comment(iid: int, body: CommentIn, user=Depends(require("edit"))):
    i = _inv(iid)
    with db.tx() as c:
        c.execute("INSERT INTO comments(invoice_id,user,ts,text) VALUES (?,?,?,?)", (iid, user["full_name"], db.now(), body.text))
        db.log(user["username"], i["engagement_id"], "invoice", iid, "comment", new=body.text, c=c)
    return {"ok": True}


@app.post("/api/invoices/{iid}/reextract")
def reextract(iid: int, user=Depends(require("edit"))):
    i = _inv(iid)
    _open(i["engagement_id"])
    with db.tx() as c:
        c.execute("UPDATE invoices SET status='queued' WHERE id=?", (iid,))
        db.log(user["username"], i["engagement_id"], "invoice", iid, "reextract", c=c)
    _pool.submit(_process_invoice, iid)
    return {"ok": True}


@app.delete("/api/invoices/{iid}")
def exclude_invoice(iid: int, reason: str, user=Depends(require("configure"))):
    """Excludes an invoice from the population (e.g. a duplicate upload). The file and record are retained."""
    i = _inv(iid)
    _open(i["engagement_id"])
    with db.tx() as c:
        c.execute("UPDATE invoices SET status='superseded', updated_at=? WHERE id=?", (db.now(), iid))
        db.log(user["username"], i["engagement_id"], "invoice", iid, "exclude", "status", i["status"], "superseded", reason, c=c)
    engine.recompute(i["engagement_id"], user["username"])
    return {"ok": True}


# ---------------------------------------------------------------------------------------- exports
def _content_disposition(name: str) -> str:
    """RFC 6266 / 5987: ASCII fallback plus UTF-8 filename* so Arabic names survive the download."""
    from urllib.parse import quote
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


def _download(data: bytes, name: str, media: str):
    return Response(data, media_type=media, headers={"Content-Disposition": _content_disposition(name)})


@app.post("/api/engagements/{eid}/export/workpaper")
def export_workpaper(eid: int, user=Depends(require("edit"))):
    e = _eng(eid)
    try:
        res = workpaper.generate(eid, user["username"], final=e["status"] == "finalized")
    except workpaper.TemplateError as ex:
        raise HTTPException(400, str(ex))
    from urllib.parse import quote
    return FileResponse(res["path"], filename=res["name"],
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"X-Workpaper-Warnings": quote(json.dumps(res["warnings"], ensure_ascii=False)),
                                 "Access-Control-Expose-Headers": "X-Workpaper-Warnings"})


@app.get("/api/engagements/{eid}/export/{kind}")
def export(eid: int, kind: str, user=Depends(current_user)):
    e = _eng(eid)
    base = workpaper.safe_filename(e["client_name"]) + f"_FY{e['financial_year']}"
    if kind == "findings.pdf":
        return _download(reports.findings_pdf(eid, user["username"]), f"{base}_Audit_Findings.pdf", "application/pdf")
    if kind == "extraction.csv":
        return _download(reports.extraction_csv(eid), f"{base}_Invoice_Extraction.csv", "text/csv; charset=utf-8")
    if kind == "exceptions.xlsx":
        return _download(reports.exceptions_xlsx(eid), f"{base}_Exceptions.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if kind == "executive.pdf":
        return _download(reports.executive_pdf(eid, user["username"]), f"{base}_Executive_Summary.pdf", "application/pdf")
    if kind == "extraction.json":
        data = json.dumps(contract.engagement_contract(eid), ensure_ascii=False, indent=1, default=str).encode("utf-8")
        db.log(user["username"], eid, "engagement", eid, "export_extraction_json")
        return _download(data, f"{base}_Extraction.json", "application/json")
    if kind == "log.xlsx":
        return _download(reports.extraction_log_xlsx(eid), f"{base}_Extraction_Log.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    raise HTTPException(404, "Unknown export")


@app.get("/api/files/{fid}/download")
def download_file(fid: int, user=Depends(current_user)):
    f = db.q1("SELECT * FROM files WHERE id=?", (fid,))
    if not f:
        raise HTTPException(404)
    return FileResponse(f["stored_path"], filename=f["original_name"])


@app.post("/api/engagements/{eid}/finalize")
def finalize(eid: int, user=Depends(require("finalize"))):
    e = _open(eid)
    rows = engine.compute_rows(eid)
    blockers = []
    if not rows:
        blockers.append("No invoices in the engagement")
    if db.q1("SELECT 1 FROM invoices WHERE engagement_id=? AND status IN ('queued','processing')", (eid,)):
        blockers.append("Extraction still in progress")
    not_reviewed = [r["invoice_number"] or r["file"] for r in rows if r["review_status"] not in ("approved", "rejected")]
    if not_reviewed:
        blockers.append(f"{len(not_reviewed)} invoice(s) not approved/rejected: " + ", ".join(map(str, not_reviewed[:10])))
    open_high = db.q("SELECT COUNT(*) n FROM findings WHERE engagement_id=? AND active=1 AND risk='High' AND review_status='open'", (eid,))[0]["n"]
    if open_high:
        blockers.append(f"{open_high} open high-risk finding(s)")
    if not db.q1("SELECT 1 FROM files WHERE engagement_id=? AND kind='template'", (eid,)):
        blockers.append("No working-paper template uploaded")
    if blockers:
        raise HTTPException(409, {"blockers": blockers})
    with db.tx() as c:
        c.execute("UPDATE engagements SET status='finalized', finalized_by=?, finalized_at=? WHERE id=?", (user["full_name"], db.now(), eid))
        db.log(user["username"], eid, "engagement", eid, "finalize", c=c)
    res = workpaper.generate(eid, user["username"], final=True)
    return {"ok": True, "workpaper": res["name"]}


@app.post("/api/engagements/{eid}/reopen")
def reopen(eid: int, reason: str, user=Depends(require("finalize"))):
    e = _eng(eid)
    if e["status"] != "finalized":
        raise HTTPException(400, "Engagement is not finalized")
    with db.tx() as c:
        c.execute("UPDATE engagements SET status='open' WHERE id=?", (eid,))
        db.log(user["username"], eid, "engagement", eid, "reopen", note=reason, c=c)
    return {"ok": True}


# ------------------------------------------------------------------------------------------- static
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"),
                        headers={"Content-Security-Policy": "default-src 'self'; img-src 'self' blob: data:; "
                                                            "style-src 'self' 'unsafe-inline'; frame-src 'self' blob:; object-src 'self' blob:",
                                 "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})
