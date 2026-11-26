# AI Audit Dashboard – Invoice Extraction & Sales Vouching

An audit-automation web application for external auditors. It extracts sales-invoice data from PDFs
(Arabic / English, text-based or scanned), matches invoices to the sales ledger, performs automated
audit checks, and populates the auditor's own Excel working paper **without changing its structure,
formulas or formatting**. Every value is traceable to its PDF page and location, and AI-extracted data is
kept distinct from auditor-confirmed data.

## Quick start

```bash
pip install -r requirements.txt
```

```bash
AUDIT_ADMIN_PASSWORD='choose-a-strong-password' python -m uvicorn audit_app.api:app --port 8000
```

Open http://localhost:8000 and sign in as `admin`. (If `AUDIT_ADMIN_PASSWORD` is not set, a random
password is printed to the console on first start.)

Generate a fictitious test pack (template, 50 invoices, ledger with planted exceptions, ground truth):

```bash
python samples/generate_samples.py samples/pack
```

Run the test suite (offline, ~45 s; includes a LibreOffice recalculation check when `soffice` is installed):

```bash
python -m pytest tests -q
```

GitHub Actions (`.github/workflows/ci.yml`) runs the same suite on Python 3.11 and 3.13 for every pull request and
push to `main`.


## AI extraction (Claude)

Text-layer PDFs are read by a deterministic, label-driven rule engine (works offline). Scanned PDFs and
low-confidence results are sent to Claude (`claude-opus-5-5`) for document understanding when an API key is
configured; where both engines run, their results are cross-checked and disagreements are flagged for the
auditor instead of being silently resolved.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Enables AI extraction. Without it, scanned invoices are flagged for manual entry (no values are guessed). |
| `AUDIT_EXTRACTION_MODE` | `auto` | `auto` (AI for scanned / low confidence), `ai_always` (AI + rule cross-check on every file), `rules_only` (never call AI). |
| `AUDIT_AI_MODEL` / `AUDIT_AI_EFFORT` | `claude-opus-5-5` / `high` | Model and effort level. |
| `AUDIT_DATA_DIR` | `./data` | SQLite database, preserved uploads (read-only) and generated outputs. |
| `AUDIT_WORKERS` | `4` | Parallel extraction workers. |
| `AUDIT_LOW_CONFIDENCE` | `0.85` | Below this, fields are highlighted and raise a `LOW_CONFIDENCE` finding until confirmed. |

Optional local OCR: install `pytesseract` and the `tesseract` binary with `ara` + `eng` language data.

**Data protection:** with AI enabled, invoice PDFs are sent to the Anthropic API. Confirm this is permitted
under your engagement terms and client confidentiality obligations before enabling it; use
`rules_only` otherwise.

## Workflow

1. **Engagement** – client, financial year (start/end), tolerance (default SAR 1), materiality, cut-off window.
2. **Uploads** – working-paper template (`.xlsx`), sales ledger (`.xlsx` / `.csv`; columns auto-detected),
   invoices (bulk PDF drag-and-drop). Files are validated (type, signature, size), hashed (SHA-256) and stored read-only.
3. **Extraction** – fields with value, confidence, method, page, bounding box and source text.
4. **Matching (3 passes)** – *Pass 1* exact invoice number and customer; *Pass 2* normalised number (prefixes
   `INV-`, `TAX-`, `#`, punctuation and leading zeros stripped; other series such as `CN-` kept apart); *Pass 3*
   heuristic proposals – customer Levenshtein similarity > 85 %, posting date within ±5 business days (KSA weekend
   Friday–Saturday) and amount within tolerance – which **require auditor confirmation** and are never linked
   automatically. Statuses: Matched, Not Found in Ledger, Possible Match, Duplicate Invoice, Amount Mismatch,
   Customer Mismatch. Reconciliation categories: `MATCHED_CLEAN`, `WITHIN_TOLERANCE`, `MATERIAL_DIFFERENCE`,
   `NOT_IN_LEDGER`, `POSSIBLE_MATCH`, `DUPLICATE_INVOICE`, `INCOMPLETE_DATA`, and `UNMATCHED_LEDGER_ROW` for
   ledger entries with no invoice.
5. **Calculations** – J = E × I; K = D − J; difference status against the configurable tolerance.
6. **ZATCA QR** – the QR code on each invoice is located (last / first page, also on scanned images) and its
   Base64 TLV decoded: tags 1–5 (seller, VAT number, timestamp, total incl. VAT, VAT total) and Phase 2 tags 6–9.
   Seller VAT, date, total and VAT are compared with the printed invoice (`ZATCA_QR_MISMATCH`, `ZATCA_QR_INVALID`,
   `ZATCA_QR_MISSING`). QR data is evidence only – it never fills working-paper fields – and the Phase 2
   cryptographic signature is **not** verified (that needs the signed XML and ZATCA's certificate chain).
7. **Audit checks** – 30+ checks (completeness of key fields, VAT recalculation, arithmetic (`VAT_ARITHMETIC_ERROR` when |taxable + VAT − total| > 0.05), line-item totals,
   VAT number format, duplicates, ledger entries without invoices, differences, credit notes, unusual amounts,
   period / cut-off, low confidence, auditor-recorded evidence failures), each with observation, risk,
   reason, supporting document, recommended procedure and review status.
8. **Review** – page image with highlighted source locations, edit / confirm fields (reason required and logged),
   ledger record and match evidence, manual evidence (columns L–O), AI-suggested explanation for P (written only
   after the auditor saves it), approve / investigate / reject, comments, change history.
9. **Dashboard** – 11 KPIs, 10 charts and 10 filters (incl. reconciliation category and risk). Clicking a bar in the
   reconciliation, risk or matching chart filters the whole dashboard; export buttons give the working paper, the
   executive report and a CSV of exactly the rows in the current view, all computed from the same rows as the working paper.
10. **Export** – completed working paper (copy of the template), PDF findings report, bilingual (Arabic / English)
   executive summary PDF (scope, reconciliation, findings by risk, differences, ZATCA QR results, outstanding
   procedures; the audit conclusion is left blank for the engagement team), extraction data as JSON in the
   specification's contract with per-field provenance (page, bounding box, raw text, method, confidence), CSV extraction report,
   Excel exception report, extraction log + audit trail. **Finalise** (manager/partner) requires every invoice
   approved or rejected and no open high-risk findings; the engagement is then locked.

## Working-paper integration

* The `Sales Vouching` sheet and header row are located by heading text (English or Arabic, e.g. «اسم العميل»،
  «المبلغ كما في الفاتورة»، «رقم الفاتورة»), so column positions are taken from the template. Legend / sub-heading /
  section rows between the header and the first data row (e.g. «A = سجل الإيرادات», "Target Testing") are detected and left untouched.
* On upload the template is inspected and the auditor is warned about: existing sample rows, a header that does not name
  the engagement client or year, and a tolerance input cell (e.g. «حد التسامح» in B13) that differs from the engagement.
  On export the engagement tolerance is written to that input cell (logged) so the template's own formulas agree with the app.
* Template row formulas in any column (e.g. a status formula in P) are preserved; when rows are inserted they are
  replicated (translated) into the new rows. An auditor explanation for a column that holds a formula is attached as a cell comment.
* Yes / No evidence is written in the form the template's data-validation list accepts (e.g. «نعم» / «لا»).
* The export lists warnings (missing exchange rates, missing amounts, unconfirmed AI values, draft status) in the
  `AI Audit Summary` sheet and in the UI.
* Data rows are written between the header and the totals row. If more rows are needed, rows are inserted above
  the totals row and **all references are shifted**: formulas in every sheet (SUM ranges extended), merged cells,
  data validations, conditional formats, defined names and print area.
* Existing formulas (e.g. in J and K) are kept verbatim; where a row has none, the equivalent formula is written
  (`=IF(OR(D="",J=""),"",D-J)` – the spec's `IF(D="","",D-J)` with a guard for a blank J, which would otherwise return `#VALUE!`).
* Column D is filled only from a ledger row that is linked to the invoice; otherwise blank.
* Columns L–O are filled only with values an auditor entered; P only with an auditor-saved explanation.
* Unconfirmed AI values carry a cell comment with method, confidence and source page. Two sheets are added:
  `AI Audit Summary` and `AI Extraction Log`. Existing sheets are not renamed or removed.
* Constant values found in the template's data rows (e.g. sample data) are cleared in the output copy; the count is logged.

## Roles

| Role | Can |
|---|---|
| viewer | read everything |
| auditor | + upload, edit / confirm data, manual evidence, match decisions, comment, approve invoices |
| manager / partner | + tolerance, materiality, FX rates, finalise / re-open |
| admin | + user management |

## Architecture

```
audit_app/
  api.py            FastAPI endpoints, upload security, background extraction, review workflow
  extraction/       pdf_text (positioned words, OCR), rules (label engine + arithmetic reconciliation),
                    llm (Claude structured extraction), pipeline (orchestration + cross-validation)
  engine.py         canonical rows (single source for dashboard / working paper / reports), matching, checks
  ledger.py         ledger import with header detection
  workpaper.py      template population with reference-preserving row insertion
  reports.py        PDF / CSV / Excel exports
  modules.py        module registry (Purchase / Expense vouching, Bank reconciliation, AR/AP confirmations,
                    Fixed assets, Payroll, Inventory planned)
  static/           single-page UI (EN / AR with RTL, light / dark)
```

## Known limitations

* Rule-engine accuracy was measured on the generated test layouts; real client invoice layouts vary. Use AI mode
  for production volumes and always review low-confidence fields.
* Hijri dates are converted to Gregorian with the Umm al-Qura calendar (`hijridate`), but are always flagged
  (`HIJRI_DATE`, confidence 0.6) until an auditor confirms them; an invalid Hijri date is left blank, never guessed.
* Exchange rates are never assumed (except SAR = 1); the auditor enters the authorised rate and its source.
* Single-node deployment (SQLite). For multi-user production use, run behind HTTPS with a reverse proxy and back up `AUDIT_DATA_DIR`.
