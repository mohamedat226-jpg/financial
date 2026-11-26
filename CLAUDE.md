# CLAUDE.md – AI Audit Dashboard & Sales Vouching

FastAPI backend + single-page UI (vanilla JS, EN/AR with RTL) in `audit_app/`. There is no separate
`frontend/` or `backend/` folder; do not scaffold one without the owner's approval.

## Core operating principles
1. **Inspect first.** Read the relevant module and its tests before changing code.
2. **No destructive actions without explicit approval:** never overwrite templates or uploaded files, delete
   directories, drop tables or wipe `data/`. Ask first: `⚠️ [DESTRUCTIVE ACTION WARNING]: <target>. Impact: <impact>. Confirm (Y/N)?`
3. **Zero fabrication.** Never invent invoice numbers, dates, customers or amounts. Column D stays blank unless
   a ledger row is linked to the invoice. QR data is evidence only and never fills working-paper fields.
   Pass 3 (heuristic) matches are proposals; only an auditor can confirm them.
4. **Net vs gross.** Column E is the taxable amount **excluding VAT**. `|taxable + VAT − total| > 0.05` raises
   `VAT_ARITHMETIC_ERROR`.
5. **Preserve formulas.** Never replace a template formula with a value. Template formulas (e.g. J, K, and the
   P status formula in the firm's Arabic template) are kept verbatim and replicated into inserted rows; where a
   row has none, the app writes `=IF(OR(E="",I=""),"",E*I)` and `=IF(OR(D="",J=""),"",D-J)` (guarded so blank
   rows never show `#VALUE!`). An auditor explanation for a formula cell in P goes into a cell comment.
6. **Manual columns.** L–O are written only from auditor-entered values; P only from an auditor-saved explanation.
7. **Hijri dates** are converted (Umm al-Qura) but capped at confidence 0.6 and flagged `HIJRI_DATE` until confirmed.
8. **Reports state facts only.** The executive report never writes an audit conclusion; AI explanations are labelled unconfirmed.
9. **Low confidence.** Fields below `AUDIT_LOW_CONFIDENCE` (default 0.85) raise `LOW_CONFIDENCE` until confirmed.
10. **OCR.** Scanned PDFs are read by Tesseract (`AUDIT_OCR=auto`) when installed; OCR words are converted to the text
    layer's visual Arabic order before the rule engine sees them, each field's confidence is capped at the OCR word
    confidence, and `OCR_READ` stays open until key values are confirmed. Tests run the shared pack with `AUDIT_OCR=off`.
11. **PDFium is not thread-safe.** Wrap every `pypdfium2` call in `extraction.pdf_text.PDFIUM_LOCK`.
12. **Bilingual UI.** Every new string needs both `en` and `ar` entries in `static/i18n.js` (labels in `STATUS_LABELS`,
    messages in `MSG`, engine notes in `NOTE_AR`). A new finding needs Arabic wording: `PROC_AR` (and `STATIC_AR`, or an
    `ar=(observation, reason)` argument to `_f`) in `audit_app/findings_ar.py`. The English text stays the audit record.
13. **Report after each phase:** commands run with exit codes, files changed, errors and fixes, and acceptance
    criteria with evidence (pass/fail).

## Commands
- Install: `pip install -r requirements.txt`
- Run: `AUDIT_ADMIN_PASSWORD='…' python -m uvicorn audit_app.api:app --port 8000` (or `./run.sh`)
- Sample pack: `python samples/generate_samples.py samples/pack`
- All tests: `python -m pytest tests -v` (offline, ~45 s)
- Excel formula integrity: `python -m pytest tests/test_excel_formulas.py -v`
- ZATCA QR: `python -m pytest tests/test_zatca_qr.py -v`
- Full integration: `python -m pytest tests/test_e2e.py -v`
- CI: `.github/workflows/ci.yml` runs the suite on Python 3.11 and 3.13 for every pull request and push to `main`,
  with OpenCV and LibreOffice installed so the QR and Excel recalculation tests cannot silently skip.
