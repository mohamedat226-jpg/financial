"""Audit module registry. Each module plugs its own extraction schema, matching rules, checks and
working-paper mapping into the shared engagement / evidence / review / audit-trail platform."""

MODULES = [
    {"key": "sales_vouching", "name": "Sales Vouching", "name_ar": "فحص المبيعات", "status": "available",
     "evidence": "Sales invoices (PDF)", "population": "Sales ledger"},
    {"key": "purchase_vouching", "name": "Purchase Vouching", "name_ar": "فحص المشتريات", "status": "planned",
     "evidence": "Supplier invoices", "population": "Purchases ledger"},
    {"key": "expense_vouching", "name": "Expense Vouching", "name_ar": "فحص المصروفات", "status": "planned",
     "evidence": "Expense invoices / receipts", "population": "Expense ledger"},
    {"key": "bank_reconciliation", "name": "Bank Reconciliation", "name_ar": "التسوية البنكية", "status": "planned",
     "evidence": "Bank statements", "population": "Cash book"},
    {"key": "ar_confirmation", "name": "Accounts Receivable Confirmation", "name_ar": "مصادقات العملاء", "status": "planned",
     "evidence": "Confirmation replies", "population": "AR ageing"},
    {"key": "ap_confirmation", "name": "Accounts Payable Confirmation", "name_ar": "مصادقات الموردين", "status": "planned",
     "evidence": "Confirmation replies", "population": "AP ageing"},
    {"key": "fixed_assets", "name": "Fixed Asset Testing", "name_ar": "فحص الأصول الثابتة", "status": "planned",
     "evidence": "Purchase invoices / title deeds", "population": "Fixed asset register"},
    {"key": "payroll", "name": "Payroll Testing", "name_ar": "فحص الرواتب", "status": "planned",
     "evidence": "Contracts, WPS files, GOSI", "population": "Payroll register"},
    {"key": "inventory", "name": "Inventory Testing", "name_ar": "فحص المخزون", "status": "planned",
     "evidence": "Count sheets, GRNs", "population": "Inventory listing"},
]
