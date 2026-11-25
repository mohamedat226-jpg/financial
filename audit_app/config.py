"""Runtime configuration (environment-driven)."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AUDIT_DATA_DIR", BASE_DIR.parent / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "audit.db"

# Upload limits
MAX_PDF_BYTES = int(os.environ.get("AUDIT_MAX_PDF_MB", "25")) * 1024 * 1024
MAX_XLSX_BYTES = int(os.environ.get("AUDIT_MAX_XLSX_MB", "25")) * 1024 * 1024
MAX_FILES_PER_BATCH = int(os.environ.get("AUDIT_MAX_FILES_PER_BATCH", "200"))

# Extraction
#   auto       : rule engine on text layer; AI (Claude) for scanned / low-confidence documents
#   rules_only : never call the AI service (offline mode)
#   ai_always  : AI extraction for every document, cross-checked against the rule engine
EXTRACTION_MODE = os.environ.get("AUDIT_EXTRACTION_MODE", "auto")
AI_MODEL = os.environ.get("AUDIT_AI_MODEL", "claude-opus-5-5")
AI_EFFORT = os.environ.get("AUDIT_AI_EFFORT", "high")
LOW_CONFIDENCE = float(os.environ.get("AUDIT_LOW_CONFIDENCE", "0.75"))
AI_ESCALATION_CONFIDENCE = float(os.environ.get("AUDIT_AI_ESCALATION_CONFIDENCE", "0.80"))
WORKERS = int(os.environ.get("AUDIT_WORKERS", "4"))

# Session lifetime (hours)
SESSION_HOURS = int(os.environ.get("AUDIT_SESSION_HOURS", "12"))


def ai_available() -> bool:
    """AI extraction is attempted only when credentials are configured and mode allows it."""
    if EXTRACTION_MODE == "rules_only":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


for _d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
