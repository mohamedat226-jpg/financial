"""Sales ledger import (Excel / CSV) with header detection and column mapping."""
from __future__ import annotations

import math
from pathlib import Path

import pandas as pd

from .extraction.normalize import norm_text, parse_amount, to_date

SYNONYMS = {
    "invoice_number": ["invoice no", "invoice number", "invoice #", "inv no", "inv number", "document no", "document number",
                       "doc no", "bill no", "voucher no", "reference", "ref", "invoice", "رقم الفاتوره", "رقم المستند", "المرجع"],
    "customer": ["customer", "customer name", "client", "client name", "party", "account name", "buyer", "العميل",
                 "اسم العميل"],
    "date": ["invoice date", "date", "posting date", "document date", "transaction date", "التاريخ", "تاريخ الفاتوره"],
    "amount": ["amount (sar)", "amount sar", "net amount", "sales amount", "amount excl vat", "amount excluding vat",
               "taxable amount", "amount", "value", "credit", "المبلغ", "صافي المبلغ"],
    "currency": ["currency", "curr", "العمله"],
}


def _score_header(cells: list[str]) -> dict:
    mapping = {}
    for field, syns in SYNONYMS.items():
        best = None
        for idx, c in enumerate(cells):
            n = norm_text(c)
            for rank, s in enumerate(syns):
                if n == s or (len(s) > 3 and n.startswith(s)):
                    if best is None or rank < best[0]:
                        best = (rank, idx)
        if best:
            mapping[field] = best[1]
    return mapping


def read_table(path: str) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    if ext == ".csv":
        for enc in ("utf-8-sig", "cp1256", "latin-1"):
            try:
                return pd.read_csv(path, header=None, dtype=str, encoding=enc, keep_default_na=False)
            except UnicodeDecodeError:
                continue
        raise ValueError("Unable to decode CSV")
    return pd.read_excel(path, header=None, dtype=object, sheet_name=0)


def detect(df: pd.DataFrame) -> tuple[int, dict, list[str]]:
    best = (-1, {}, [])
    for r in range(min(25, len(df))):
        cells = ["" if (v is None or (isinstance(v, float) and math.isnan(v))) else str(v) for v in df.iloc[r].tolist()]
        m = _score_header(cells)
        if "invoice_number" in m and "amount" in m and len(m) > len(best[1]):
            best = (r, m, cells)
    if best[0] < 0:
        raise ValueError("Could not find a header row with at least an invoice number and an amount column. "
                         "Provide a column mapping.")
    return best


def parse(path: str, mapping: dict | None = None) -> tuple[list[dict], dict, list[str]]:
    """Returns (entries, mapping_used(by header name), warnings). Never fills missing values."""
    df = read_table(path)
    hdr_row, auto_map, headers = detect(df) if not mapping else (None, None, None)
    if mapping:
        # mapping: field -> header text ; find header row containing those headers
        for r in range(min(25, len(df))):
            cells = [str(v) for v in df.iloc[r].tolist()]
            if all(h in cells for h in mapping.values() if h):
                hdr_row, headers = r, cells
                auto_map = {f: cells.index(h) for f, h in mapping.items() if h}
                break
        else:
            raise ValueError("Mapped headers not found in file")
    warnings = []
    entries = []
    for r in range(hdr_row + 1, len(df)):
        row = df.iloc[r].tolist()
        def get(f):
            i = auto_map.get(f)
            if i is None or i >= len(row):
                return None
            v = row[i]
            if v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip()):
                return None
            return v
        raw = {headers[i] if i < len(headers) else f"col{i}": (None if row[i] is None or (isinstance(row[i], float) and math.isnan(row[i])) else str(row[i]))
               for i in range(len(row))}
        if all(v in (None, "") for v in raw.values()):
            continue
        inv = get("invoice_number")
        amt_raw = get("amount")
        amt = float(amt_raw) if isinstance(amt_raw, (int, float)) else parse_amount(amt_raw) if amt_raw is not None else None
        cust = get("customer")
        d = to_date(get("date"))
        if isinstance(inv, float) and inv.is_integer():
            inv = int(inv)
        label = norm_text(str(inv or "") + " " + str(cust or ""))
        if inv is None and amt is not None and ("total" in label or "الاجمالي" in label):
            continue  # skip subtotal / total lines
        if amt is None and inv is None:
            continue
        if amt is None:
            warnings.append(f"Row {r + 1}: amount missing")
        entries.append({"row_number": r + 1, "invoice_number": None if inv is None else str(inv).strip(),
                        "customer": None if cust is None else str(cust).strip(), "date": d.isoformat() if d else None,
                        "amount": amt, "currency": (str(get("currency")).strip().upper() if get("currency") else None),
                        "raw": raw})
    used = {f: headers[i] for f, i in auto_map.items()}
    for f in ("customer", "date"):
        if f not in auto_map:
            warnings.append(f"No '{f}' column detected - matching will rely on other keys")
    return entries, used, warnings
