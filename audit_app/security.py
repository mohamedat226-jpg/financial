"""Authentication and role-based access control."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException

from . import config, db

# Permission matrix. Roles are cumulative from viewer upward except admin (user management).
ROLE_RANK = {"viewer": 0, "auditor": 1, "manager": 2, "partner": 3, "admin": 4}
PERMISSIONS = {
    "read": "viewer",
    "edit": "auditor",          # upload, edit extracted data, review, comment
    "approve": "auditor",       # approve individual invoices
    "configure": "manager",     # tolerance, FX rates, materiality
    "finalize": "manager",      # finalize the working paper
    "manage_users": "admin",
}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 260_000)
    return hmac.compare_digest(dk.hex(), dk_hex)


def create_user(username: str, full_name: str, password: str, role: str) -> int:
    if role not in ROLE_RANK:
        raise ValueError("invalid role")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    with db.tx() as c:
        cur = c.execute("INSERT INTO users(username,full_name,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                        (username.strip().lower(), full_name.strip(), hash_password(password), role, db.now()))
        return cur.lastrowid


def ensure_bootstrap_admin() -> str | None:
    """Create an initial admin on first run. Returns the generated password if one was generated."""
    if db.q1("SELECT id FROM users LIMIT 1"):
        return None
    pwd = os.environ.get("AUDIT_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
    create_user("admin", "System Administrator", pwd, "admin")
    return None if os.environ.get("AUDIT_ADMIN_PASSWORD") else pwd


def login(username: str, password: str) -> str:
    u = db.q1("SELECT * FROM users WHERE username=? AND active=1", (username.strip().lower(),))
    if not u or not verify_password(password, u["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(hours=config.SESSION_HOURS)).isoformat()
    with db.tx() as c:
        c.execute("INSERT INTO sessions(token,user_id,expires_at) VALUES (?,?,?)", (token, u["id"], exp))
    db.log(u["username"], None, "user", u["id"], "login")
    return token


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not authenticated")
    token = authorization.split(" ", 1)[1].strip()
    s = db.q1("SELECT * FROM sessions WHERE token=?", (token,))
    if not s or s["expires_at"] < datetime.now(timezone.utc).isoformat():
        raise HTTPException(401, "Session expired")
    u = db.q1("SELECT id,username,full_name,role,active FROM users WHERE id=?", (s["user_id"],))
    if not u or not u["active"]:
        raise HTTPException(401, "User disabled")
    return u


def can(user: dict, perm: str) -> bool:
    return ROLE_RANK[user["role"]] >= ROLE_RANK[PERMISSIONS[perm]]


def require(perm: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if not can(user, perm):
            raise HTTPException(403, f"Role '{user['role']}' is not permitted to {perm}")
        return user
    return dep
