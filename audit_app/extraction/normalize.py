"""Normalisation helpers for Arabic/English invoice text, numbers and dates."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_TATWEEL = "ـ"
_DIACRITICS = re.compile(r"[ً-ْٰ]")


def has_arabic(s: str) -> bool:
    return bool(_ARABIC_RE.search(s or ""))


def norm_text(s: str) -> str:
    """NFKC (folds Arabic presentation forms to base letters), unify alef/ya/ta-marbuta,
    drop tatweel & diacritics, convert Arabic-Indic digits, collapse whitespace, lowercase."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(ARABIC_DIGITS).replace("٫", ".").replace("٬", ",").replace(_TATWEEL, "")
    s = _DIACRITICS.sub("", s)
    s = re.sub("[إأآٱ]", "ا", s).replace("ى", "ي").replace("ة", "ه")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


_DIGIT_RUN = re.compile(r"[0-9\u0660-\u0669\u06F0-\u06F9][0-9\u0660-\u0669\u06F0-\u06F9.,]*")


def _rev_word(w: str) -> str:
    # reverse characters but keep digit runs (numbers are always stored left-to-right)
    parts = []
    pos = 0
    for m in _DIGIT_RUN.finditer(w):
        if m.start() > pos:
            parts.append(w[pos:m.start()][::-1])
        parts.append(m.group(0))
        pos = m.end()
    if pos < len(w):
        parts.append(w[pos:][::-1])
    return "".join(reversed(parts))


def reverse_rtl(s: str) -> str:
    """Visual-order Arabic text (as emitted by many PDF generators) reversed back to logical order."""
    return " ".join(_rev_word(w) if has_arabic(w) else w for w in reversed(s.split(" ")))


_NUM_RE = re.compile(r"(?<![\w.])[-(]?\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?\)?(?![\w])|(?<![\w.])[-(]?\d+(?:\.\d+)?\)?(?![\w])")


def parse_amount(tok: str) -> float | None:
    if tok is None:
        return None
    t = unicodedata.normalize("NFKC", str(tok)).translate(ARABIC_DIGITS).replace("٫", ".").replace("٬", ",")
    t = t.strip().replace("SAR", "").replace("SR", "").replace("ر.س", "").strip()
    neg = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = t.strip("()-+ ")
    if not t:
        return None
    # European style 1.234,56
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", t):
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


def find_amounts(text: str) -> list[tuple[float, str]]:
    t = unicodedata.normalize("NFKC", text).translate(ARABIC_DIGITS).replace("٫", ".").replace("٬", ",")
    out = []
    for m in _NUM_RE.finditer(t):
        v = parse_amount(m.group(0))
        if v is not None:
            out.append((v, m.group(0)))
    return out


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
AR_MONTHS = {"يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "مايو": 5, "يونيو": 6, "يوليو": 7,
             "اغسطس": 8, "سبتمبر": 9, "اكتوبر": 10, "نوفمبر": 11, "ديسمبر": 12}


def parse_date(text: str) -> tuple[date | None, str | None, str | None]:
    """Return (date, matched_text, note). DD/MM/YYYY is assumed for ambiguous numeric dates
    (Saudi convention) and reported in the note so the auditor can verify."""
    if not text:
        return None, None, None
    t = norm_text(text)
    # ISO yyyy-mm-dd
    m = re.search(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", t)
    if m:
        y, mo, d = map(int, m.groups())
        return _mk(y, mo, d, m.group(0), None)
    m = re.search(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)", t)   # (?!\d) also accepts a trailing "H" / "هـ"
    if m:
        a, b, y = map(int, m.groups())
        note = None
        if a <= 12 and b <= 12 and a != b:
            note = "Ambiguous numeric date interpreted as DD/MM/YYYY"
        if a > 12 >= b or a <= 12 and b <= 12:
            return _mk(y, b, a, m.group(0), note)
        return _mk(y, a, b, m.group(0), "Date interpreted as MM/DD/YYYY (day > 12)")
    m = re.search(r"\b(\d{1,2})[\s-]+([a-z]{3,9})[\s,-]+(\d{4})\b", t)
    if m and m.group(2)[:3] in MONTHS:
        return _mk(int(m.group(3)), MONTHS[m.group(2)[:3]], int(m.group(1)), m.group(0), None)
    m = re.search(r"\b([a-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b", t)
    if m and m.group(1)[:3] in MONTHS:
        return _mk(int(m.group(3)), MONTHS[m.group(1)[:3]], int(m.group(2)), m.group(0), None)
    for name, mo in AR_MONTHS.items():
        m = re.search(rf"(\d{{1,2}})\s+{name}\s+(\d{{4}})", t)
        if m:
            return _mk(int(m.group(2)), mo, int(m.group(1)), m.group(0), None)
    for name, mo in HIJRI_MONTHS:
        m = re.search(rf"(\d{{1,2}})\s+{name}\s+(1[345]\d\d)", t)
        if m:
            return _mk(int(m.group(2)), mo, int(m.group(1)), m.group(0), None)
    return None, None, None


# Hijri month names after norm_text (alef / ya / ta-marbuta unified). Longer names first so that
# "ربيع الاول" is not matched as a bare "ربيع".
HIJRI_MONTHS = sorted({
    "محرم": 1, "صفر": 2, "ربيع الاول": 3, "ربيع الثاني": 4, "ربيع الاخر": 4, "جمادي الاولي": 5, "جمادي الاول": 5,
    "جمادي الثانيه": 6, "جمادي الاخره": 6, "جمادي الاخر": 6, "رجب": 7, "شعبان": 8, "رمضان": 9, "شوال": 10,
    "ذو القعده": 11, "ذي القعده": 11, "ذو الحجه": 12, "ذي الحجه": 12}.items(), key=lambda kv: -len(kv[0]))
HIJRI_NOTE = "Hijri date"


def hijri_to_gregorian(y: int, mo: int, d: int) -> date | None:
    """Umm al-Qura conversion (official Saudi calendar). Returns None if unavailable or not a valid Hijri date."""
    try:
        from hijridate import Hijri
    except ImportError:
        return None
    try:
        g = Hijri(y, mo, d).to_gregorian()
    except (ValueError, OverflowError):
        return None
    return date(g.year, g.month, g.day)


def _mk(y, mo, d, raw, note):
    if 1300 <= y <= 1500:
        g = hijri_to_gregorian(y, mo, d)
        if g is None:
            return None, raw, ("Hijri date detected - could not be converted (invalid date or converter not installed); "
                               "enter the Gregorian date manually")
        return g, raw, (f"{HIJRI_NOTE} {d:02d}/{mo:02d}/{y} AH converted to {g.isoformat()} (Umm al-Qura calendar) - "
                        "auditor confirmation required")
    try:
        return date(y, mo, d), raw, note
    except ValueError:
        return None, raw, "Invalid calendar date"


def to_date(v) -> date | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        d, _, _ = parse_date(s)
        return d


def norm_invoice_no(v) -> str:
    if v in (None, ""):
        return ""
    s = unicodedata.normalize("NFKC", str(v)).translate(ARABIC_DIGITS).upper()
    if re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return re.sub(r"[^A-Z0-9]", "", s)


_LEGAL_SUFFIX = re.compile(r"\b(co|company|ltd|limited|llc|est|establishment|inc|corp|corporation|"
                           r"trading|group|plc|wll|شركه|مؤسسه|المحدوده|ذات|مسؤوليه|للتجاره|التجاريه)\b\.?")


def norm_party(v) -> str:
    s = norm_text(str(v or ""))
    s = re.sub(r"[^\w\s]", " ", s)
    s = _LEGAL_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def valid_saudi_vat(v: str | None) -> bool:
    """ZATCA VAT registration numbers are 15 digits, beginning and ending with 3."""
    return bool(v) and bool(re.fullmatch(r"3\d{13}3", str(v)))


# ------------------------------------------------------------------------- reconciliation helpers
def clean_exact(v) -> str:
    """Pass 1 key: exact alphanumeric reference (case and surrounding whitespace ignored only)."""
    if v in (None, ""):
        return ""
    s = unicodedata.normalize("NFKC", str(v)).translate(ARABIC_DIGITS).strip().upper()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


_DOC_PREFIX = re.compile(r"^(?:TAX\s*[-_/]?\s*INV(?:OICE)?|INV(?:OICE)?|TAX|NO\.?|#)+\s*[-_/:#.]*\s*")


def norm_doc_no(v) -> str:
    """Pass 2 key: strip common prefixes (INV-, TAX-, #), punctuation and leading zeros of each numeric group.
    'INV-2025-0018', 'INV2025-0018', '#2025/18' -> '2025-18'. Other prefixes (e.g. CN-) are kept, so a credit
    note and an invoice with the same digits do not collide."""
    if v in (None, ""):
        return ""
    s = clean_exact(v)
    prev = None
    while prev != s:
        prev, s = s, _DOC_PREFIX.sub("", s)
    groups = re.findall(r"[A-Z]+|\d+", s)
    if not groups:
        return ""
    return "-".join(g.lstrip("0") or "0" if g.isdigit() else g for g in groups)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def levenshtein_ratio(a, b) -> float:
    """1 - edit distance / longer length, on normalised party names (legal suffixes removed)."""
    na, nb = norm_party(a), norm_party(b)
    if not na or not nb:
        return 0.0
    return round(1 - levenshtein(na, nb) / max(len(na), len(nb)), 3)


# KSA weekend is Friday and Saturday (Python weekday 4 and 5). Configurable for other jurisdictions.
WEEKEND = (4, 5)


def business_days_between(d1: date, d2: date, weekend=WEEKEND) -> int:
    """Absolute number of business days between two dates (0 when the same day)."""
    if d1 > d2:
        d1, d2 = d2, d1
    n, cur = 0, d1
    from datetime import timedelta
    while cur < d2:
        cur += timedelta(days=1)
        if cur.weekday() not in weekend:
            n += 1
    return n
