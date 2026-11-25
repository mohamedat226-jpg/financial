/* AI Audit Dashboard - single page client (no build step). All server text is escaped before rendering. */
"use strict";
const S = {
  token: sessionStorage.getItem("tok"), user: null, lang: localStore("lang") || "en", eid: null, eng: null, tab: "setup",
  rows: [], summary: null, findings: [], filters: {}, invoiceId: null, charts: [], poll: null, selField: null,
};
function localStore(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (v) => (v === null || v === undefined ? "" : String(v)).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const t = (k) => (I18N[S.lang] && I18N[S.lang][k]) || I18N.en[k] || k;
const fl = (k) => FIELD_LABELS[S.lang][k] || k;
const fmt = (v, d = 2) => (v === null || v === undefined || v === "" ? "" : Number(v).toLocaleString(S.lang === "ar" ? "ar-SA-u-nu-latn" : "en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
const pct = (v) => (v === null || v === undefined ? "" : Math.round(v * 100) + "%");

function toast(msg, err) {
  const el = $("#toast"); el.textContent = msg; el.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => el.classList.add("hidden"), err ? 7000 : 3500);
}
async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (S.token) opt.headers.Authorization = "Bearer " + S.token;
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401 && path !== "/api/login") { logout(); throw new Error("Session expired"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : r;
  if (!r.ok) {
    let d = data && data.detail !== undefined ? data.detail : r.statusText;
    if (d && d.blockers) d = d.blockers.join(" • ");
    if (Array.isArray(d)) d = d.map((x) => x.msg || JSON.stringify(x)).join("; ");
    throw new Error(typeof d === "string" ? d : JSON.stringify(d));
  }
  return data;
}
async function blobOf(method, path) {
  const r = await fetch(path, { method, headers: { Authorization: "Bearer " + S.token } });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail; } catch (e) { /* ignore */ } throw new Error(m); }
  const cd = r.headers.get("content-disposition") || "";
  const star = /filename\*=UTF-8''([^;]+)/i.exec(cd), m = /filename="?([^";]+)"?/.exec(cd);
  const name = star ? decodeURIComponent(star[1]) : m ? m[1] : "download";
  return { blob: await r.blob(), name, headers: r.headers };
}
async function download(method, path) {
  try {
    const { blob, name, headers } = await blobOf(method, path);
    const wh = headers.get("X-Workpaper-Warnings");
    if (wh) { const ws = JSON.parse(decodeURIComponent(wh)).filter((w) => !w.startsWith("DRAFT")); if (ws.length) toast("⚠ " + ws.join(" • "), true); }
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  } catch (e) { toast(e.message, true); }
}
const wrap = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };

function applyLang() {
  document.documentElement.lang = S.lang; document.documentElement.dir = S.lang === "ar" ? "rtl" : "ltr";
  $("#langBtn").textContent = S.lang === "ar" ? "English" : "عربي";
  $$("[data-t]").forEach((el) => (el.textContent = t(el.dataset.t)));
}
function logout() { sessionStorage.removeItem("tok"); S.token = null; S.user = null; clearInterval(S.poll); render(); }

/* ------------------------------------------------------------------------------------- badges */
function statusBadge(s) {
  const map = { "Matched": "good", "No Difference": "good", "approved": "good", "cleared": "good", "extracted": "neutral",
    "Possible Match": "warn", "Customer Mismatch": "warn", "Missing Ledger Amount": "warn", "pending": "neutral", "investigate": "warn",
    "Amount Mismatch": "crit", "Not Found in Ledger": "crit", "Duplicate Invoice": "crit", "Difference Identified": "crit",
    "Missing Invoice Data": "serious", "needs_manual": "serious", "failed": "crit", "rejected": "crit", "open": "warn", "escalated": "serious",
    "High": "crit", "Medium": "warn", "Low": "neutral", "Ledger Not Uploaded": "neutral", "queued": "neutral", "processing": "neutral" };
  const icon = { good: "✓", warn: "!", crit: "✕", serious: "▲", neutral: "•" };
  const c = map[s] || "neutral";
  return `<span class="badge b-${c}">${icon[c]} ${esc(s)}</span>`;
}
const confBar = (c) => (c === null || c === undefined ? "" : `<span class="conf ${c < 0.75 ? "low" : ""}" title="${pct(c)}"><i style="width:${Math.round(c * 100)}%"></i></span> <span class="small muted">${pct(c)}</span>`);

/* --------------------------------------------------------------------------------------- shell */
async function render() {
  applyLang();
  const app = $("#app");
  if (!S.token) { $("#topbar").classList.add("hidden"); return renderLogin(app); }
  if (!S.user) {
    try { S.user = await api("GET", "/api/me"); } catch (e) { return; }
  }
  $("#topbar").classList.remove("hidden");
  $("#userPill").textContent = `${S.user.full_name} · ${S.user.role}`;
  $("#aiPill").textContent = S.user.ai_available ? t("aiOn") : t("aiOff");
  const h = location.hash.slice(1).split("/");
  if (h[0] === "e" && h[1]) { S.eid = +h[1]; S.tab = h[2] || "setup"; S.invoiceId = h[3] ? +h[3] : S.invoiceId; return renderEngagement(app); }
  if (h[0] === "users") return renderUsers(app);
  S.eid = null; clearInterval(S.poll);
  return renderHome(app);
}
window.addEventListener("hashchange", () => render());

function renderLogin(app) {
  app.innerHTML = `<div class="card login"><h3>${t("appName")}</h3>
    <form id="lf" class="grid"><label class="f">${t("username")}<input name="u" autocomplete="username" required></label>
    <label class="f">${t("password")}<input name="p" type="password" autocomplete="current-password" required></label>
    <button class="primary" type="submit">${t("login")}</button></form></div>`;
  $("#lf").addEventListener("submit", wrap(async (e) => {
    e.preventDefault();
    const r = await api("POST", "/api/login", { username: e.target.u.value, password: e.target.p.value });
    S.token = r.token; sessionStorage.setItem("tok", r.token); S.user = null; render();
  }));
}

async function renderHome(app) {
  $("#crumb").innerHTML = `<b>${t("engagements")}</b>`;
  const [engs, mods] = await Promise.all([api("GET", "/api/engagements"), api("GET", "/api/modules")]);
  const y = new Date().getFullYear() - 1;
  app.innerHTML = `
  <div class="grid g3">
    <div class="card" style="grid-column: span 2"><div class="row"><h3>${t("engagements")}</h3><span class="spacer"></span>
      ${S.user.role === "admin" ? `<a class="btn" href="#users">${t("users")}</a>` : ""}</div>
      <div class="table-wrap"><table class="t"><thead><tr><th>${t("client")}</th><th>${t("fy")}</th><th>${t("status")}</th><th class="num">Invoices</th><th>Created</th><th></th></tr></thead>
      <tbody>${engs.map((e) => `<tr class="click" data-id="${e.id}"><td><b>${esc(e.client_name)}</b></td><td>${esc(e.financial_year)} <span class="muted small">${esc(e.fy_start)} → ${esc(e.fy_end)}</span></td>
        <td>${e.status === "finalized" ? statusBadge("approved").replace("approved", t("finalized")) : `<span class="badge b-neutral">${t("draft")}</span>`}</td>
        <td class="num">${e.invoice_count}</td><td class="small muted">${esc(e.created_by)} · ${esc(e.created_at.slice(0, 10))}</td><td><button class="sm">${t("open")}</button></td></tr>`).join("") || `<tr><td colspan="6" class="muted">${t("noData")}</td></tr>`}</tbody></table></div></div>
    <div class="card"><h3>${t("newEngagement")}</h3>
      <form id="nf" class="grid">
        <label class="f">${t("client")}<input name="client_name" required maxlength="200"></label>
        <div class="grid g2"><label class="f">${t("fy")}<input name="financial_year" required value="${y}"></label>
        <label class="f">${t("tolerance")}<input name="tolerance" type="number" step="0.01" min="0" value="1"></label></div>
        <div class="grid g2"><label class="f">${t("fyStart")}<input name="fy_start" type="date" required value="${y}-01-01"></label>
        <label class="f">${t("fyEnd")}<input name="fy_end" type="date" required value="${y}-12-31"></label></div>
        <div class="grid g2"><label class="f">${t("materiality")}<input name="materiality" type="number" step="0.01" min="0"></label>
        <label class="f">${t("cutoffDays")}<input name="cutoff_days" type="number" min="0" max="90" value="7"></label></div>
        <button class="primary" type="submit" ${S.user.permissions.edit ? "" : "disabled"}>${t("create")}</button></form></div>
  </div>
  <div class="card" style="margin-top:14px"><h3>${t("modules")}</h3><div class="grid g4">
    ${mods.map((m) => `<div class="card module ${m.status}"><b>${esc(S.lang === "ar" ? m.name_ar : m.name)}</b><div class="small muted">${esc(m.evidence)} ↔ ${esc(m.population)}</div>
      <div style="margin-top:6px">${m.status === "available" ? `<span class="badge b-good">✓ ${t("available")}</span>` : `<span class="badge b-neutral">${t("planned")}</span>`}</div></div>`).join("")}
  </div></div>`;
  $$("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = `e/${tr.dataset.id}/setup`)));
  $("#nf").addEventListener("submit", wrap(async (e) => {
    e.preventDefault(); const f = e.target;
    const r = await api("POST", "/api/engagements", { client_name: f.client_name.value, financial_year: f.financial_year.value, fy_start: f.fy_start.value,
      fy_end: f.fy_end.value, tolerance: +f.tolerance.value || 0, materiality: f.materiality.value ? +f.materiality.value : null, cutoff_days: +f.cutoff_days.value || 0 });
    location.hash = `e/${r.id}/setup`;
  }));
}

/* ---------------------------------------------------------------------------------- engagement */
const TABS = ["setup", "extraction", "dashboard", "findings", "review", "exports", "trail"];
async function loadEng() {
  S.eng = await api("GET", `/api/engagements/${S.eid}`);
  const d = await api("GET", `/api/engagements/${S.eid}/rows`);
  S.rows = d.rows; S.summary = d.summary; S.progress = d.progress;
}
async function renderEngagement(app) {
  await loadEng();
  const e = S.eng;
  $("#crumb").innerHTML = `<a href="#">${t("engagements")}</a> / <b>${esc(e.client_name)}</b> · FY ${esc(e.financial_year)} ${e.status === "finalized" ? statusBadge("approved").replace("approved", t("finalized")) : ""}`;
  const openF = S.summary.exceptions;
  app.innerHTML = `<nav class="tabs" role="tablist">${TABS.map((k) => `<button class="tab ${S.tab === k ? "active" : ""}" data-tab="${k}">${t(k)}${k === "findings" && openF ? ` <span class="badge b-crit">${openF}</span>` : ""}</button>`).join("")}</nav><div id="view"></div>`;
  $$(".tab").forEach((b) => b.addEventListener("click", () => (location.hash = `e/${S.eid}/${b.dataset.tab}${b.dataset.tab === "review" && S.invoiceId ? "/" + S.invoiceId : ""}`)));
  destroyCharts();
  const v = $("#view");
  ({ setup: viewSetup, extraction: viewExtraction, dashboard: viewDashboard, findings: viewFindings, review: viewReview, exports: viewExports, trail: viewTrail }[S.tab] || viewSetup)(v);
  clearInterval(S.poll);
  if (S.progress.queued || S.progress.processing) S.poll = setInterval(pollProgress, 1500);
}
async function pollProgress() {
  const p = await api("GET", `/api/engagements/${S.eid}/progress`);
  const done = p.total - (p.queued || 0) - (p.processing || 0);
  const bar = $("#extractBar"); if (bar) { bar.style.width = (p.total ? (100 * done) / p.total : 0) + "%"; $("#extractTxt").textContent = `${done} / ${p.total}`; }
  if (!p.queued && !p.processing) { clearInterval(S.poll); render(); }
}

function viewSetup(v) {
  const e = S.eng, files = e.files, can = S.user.permissions, locked = e.status === "finalized";
  const tf = files.find((f) => f.kind === "template"), lf = files.find((f) => f.kind === "ledger");
  const p = S.progress, done = p.total - (p.queued || 0) - (p.processing || 0);
  v.innerHTML = `<div class="grid g3">
    ${uploadCard("template", ".xlsx,.xlsm", false, tf ? `<div class="small"><b>${esc(tf.original_name)}</b><br>Sheet “${esc(tf.meta.sheet)}”, header row ${esc(tf.meta.header_row)}, data rows ${esc(tf.meta.first_data_row)}–${tf.meta.totals_row ? tf.meta.totals_row - 1 : "…"} (${tf.meta.capacity ?? "∞"})${tf.meta.tolerance_cell ? `, tolerance cell ${esc(tf.meta.tolerance_cell)}` : ""}${tf.meta.totals_row ? `, totals row ${esc(tf.meta.totals_row)}` : ""}<br><span class="mono">SHA-256 ${esc(tf.sha256.slice(0, 16))}…</span>${(tf.meta.warnings || []).length ? `<div class="notice warn" style="margin-top:6px">${tf.meta.warnings.map(esc).join("<br>")}</div>` : ""}</div>` : "")}
    ${uploadCard("ledger", ".xlsx,.xlsm,.xls,.csv", false, lf ? `<div class="small"><b>${esc(lf.original_name)}</b> – ${esc(lf.meta.rows)} rows<br>${Object.entries(lf.meta.mapping || {}).map(([k, h]) => `${esc(k)} ← “${esc(h)}”`).join(", ")}${(lf.meta.warnings || []).length ? `<div class="notice warn" style="margin-top:6px">${lf.meta.warnings.map(esc).join("<br>")}</div>` : ""}</div>` : `<div class="small muted">Without a ledger, column D stays blank (never estimated).</div>`)}
    ${uploadCard("invoices", ".pdf", true, `<div class="small">${t("progress")}: <span id="extractTxt">${done} / ${p.total}</span></div><div class="progress"><i id="extractBar" style="width:${p.total ? (100 * done) / p.total : 0}%"></i></div>
      <div class="row small" style="margin-top:6px">${Object.entries(p).filter(([k]) => k !== "total").map(([k, n]) => statusBadge(k) + " " + n).join(" ")}</div>`)}
  </div>
  <div class="grid g2" style="margin-top:14px">
    <div class="card"><h3>${t("settings")}</h3><form id="sf" class="grid g3">
      <label class="f">${t("tolerance")}<input name="tolerance" type="number" step="0.01" min="0" value="${esc(e.tolerance)}"></label>
      <label class="f">${t("materiality")}<input name="materiality" type="number" step="0.01" min="0" value="${esc(e.materiality ?? "")}"></label>
      <label class="f">${t("cutoffDays")}<input name="cutoff_days" type="number" min="0" max="90" value="${esc(e.cutoff_days)}"></label>
      <div><button class="primary" ${can.configure && !locked ? "" : "disabled"}>${t("save")}</button></div></form>
      <p class="small muted">${t("fy")}: ${esc(e.fy_start)} → ${esc(e.fy_end)}. Difference status uses |Ledger − SAR amount| ≤ tolerance.</p></div>
    <div class="card"><h3>${t("fxRates")}</h3>
      <table class="t"><thead><tr><th>${t("currency")}</th><th class="num">${t("rate")}</th><th>${t("source")}</th><th>By</th></tr></thead><tbody>
      ${e.fx_rates.map((r) => `<tr><td>${esc(r.currency)}</td><td class="num">${esc(r.rate)}</td><td>${esc(r.source)}</td><td class="small muted">${esc(r.entered_by)}</td></tr>`).join("")}</tbody></table>
      ${missingCurrencies().length ? `<div class="notice warn" style="margin:8px 0">Rates required for: <b>${missingCurrencies().map(esc).join(", ")}</b></div>` : ""}
      <form id="fxf" class="row" style="margin-top:8px"><input name="currency" placeholder="USD" maxlength="3" size="4" required pattern="[A-Za-z]{3}">
      <input name="rate" type="number" step="0.000001" min="0" placeholder="3.75" required style="width:110px">
      <input name="source" placeholder="e.g. SAMA rate at 31 Dec" required style="flex:1">
      <button ${can.configure && !locked ? "" : "disabled"}>${t("add")}</button></form>
      <p class="small muted">Rates are never assumed (except SAR = 1). Enter the authorised rate and its source.</p></div>
  </div>`;
  $$(".drop").forEach((d) => bindDrop(d, locked || !can.edit));
  $("#sf").addEventListener("submit", wrap(async (ev) => {
    ev.preventDefault(); const f = ev.target;
    await api("PATCH", `/api/engagements/${S.eid}/settings`, { tolerance: +f.tolerance.value, materiality: f.materiality.value ? +f.materiality.value : null, cutoff_days: +f.cutoff_days.value });
    toast("Saved – matching and checks recalculated"); render();
  }));
  $("#fxf").addEventListener("submit", wrap(async (ev) => {
    ev.preventDefault(); const f = ev.target;
    await api("PUT", `/api/engagements/${S.eid}/fx`, { currency: f.currency.value.toUpperCase(), rate: +f.rate.value, source: f.source.value });
    toast("Rate saved"); render();
  }));
}
function missingCurrencies() {
  const have = new Set(S.eng.fx_rates.map((r) => r.currency));
  return [...new Set(S.rows.map((r) => r.currency).filter((c) => c && !have.has(c)))];
}
function uploadCard(kind, accept, multiple, extra) {
  return `<div class="card"><h3>${t(kind)}</h3><div class="drop" data-kind="${kind}" tabindex="0" role="button">${t("dropHere")}
    <input type="file" accept="${accept}" ${multiple ? "multiple" : ""} class="hidden"></div><div style="margin-top:10px">${extra}</div></div>`;
}
function bindDrop(d, disabled) {
  const input = $("input", d);
  if (disabled) { d.style.opacity = .5; return; }
  d.addEventListener("click", () => input.click());
  d.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") input.click(); });
  d.addEventListener("dragover", (e) => { e.preventDefault(); d.classList.add("over"); });
  d.addEventListener("dragleave", () => d.classList.remove("over"));
  d.addEventListener("drop", (e) => { e.preventDefault(); d.classList.remove("over"); upload(d.dataset.kind, e.dataTransfer.files); });
  input.addEventListener("change", () => upload(d.dataset.kind, input.files));
}
const upload = wrap(async (kind, files) => {
  if (!files.length) return;
  const fd = new FormData();
  if (kind === "invoices") [...files].forEach((f) => fd.append("files", f));
  else fd.append("file", files[0]);
  toast(`Uploading ${files.length} file(s)…`);
  const r = await api("POST", `/api/engagements/${S.eid}/${kind === "invoices" ? "invoices" : kind}`, fd);
  if (kind === "invoices") toast(`${r.accepted.length} accepted${r.rejected.length ? `, ${r.rejected.length} rejected: ` + r.rejected.map((x) => x.name + " – " + x.error).join("; ") : ""}`, r.rejected.length > 0);
  else toast(kind === "ledger" ? `Ledger loaded: ${r.rows} rows` : "Template accepted");
  render();
});

/* ------------------------------------------------------------------------------- extraction view */
function viewExtraction(v) {
  const low = 0.75;
  v.innerHTML = `<div class="card"><div class="row"><h3>${t("extraction")}</h3><span class="spacer"></span>
    <input id="q" placeholder="Search invoice / customer / file" style="min-width:240px">
    <div class="legend"><span><i style="background:var(--warn-soft);border:1px solid var(--warn)"></i>Low confidence</span><span><i style="background:var(--crit-soft);border:1px solid var(--crit)"></i>Missing</span><span><span class="badge b-aud">✓</span> Auditor-confirmed</span></div></div>
    <div class="table-wrap" style="margin-top:10px"><table class="t" id="xt"><thead><tr><th>#</th><th>${t("file")}</th><th>${t("status")}</th><th>${t("invNo")}</th><th>${t("invDate")}</th><th>${t("customer")}</th>
      <th>${t("currency")}</th><th class="num">${t("amountExVat")}</th><th class="num">${t("vat")}</th><th class="num">${t("gross")}</th><th class="num">${t("fx")}</th><th class="num">${t("amtSar")}</th>
      <th class="num">${t("ledgerAmt")}</th><th class="num">${t("diff")}</th><th>${t("match")}</th><th>${t("diffStatus")}</th><th>${t("confidence")}</th><th>${t("reviewStatus")}</th></tr></thead><tbody></tbody></table></div></div>`;
  const draw = () => {
    const q = $("#q").value.toLowerCase();
    $("#xt tbody").innerHTML = S.rows.filter((r) => !q || [r.invoice_number, r.customer, r.file].join(" ").toLowerCase().includes(q)).map((r) => {
      const cls = (val, k) => (val === null || val === undefined || val === "" ? "missing" : r.confidence !== null && r.confidence < low && ["invoice_number", "customer", "invoice_amount"].includes(k) ? "lowconf" : "");
      return `<tr class="click" data-id="${r.invoice_id}"><td>${r.sno}</td><td class="small">${esc(r.file)}</td><td>${statusBadge(r.status)}</td>
      <td class="${cls(r.invoice_number, "invoice_number")}">${esc(r.invoice_number)}</td><td class="${cls(r.invoice_date)}">${esc(r.invoice_date)}</td>
      <td class="${cls(r.customer, "customer")}" dir="auto">${esc(r.customer)}</td><td class="${cls(r.currency)}">${esc(r.currency)}</td>
      <td class="num ${cls(r.invoice_amount, "invoice_amount")}">${fmt(r.invoice_amount)}</td><td class="num">${fmt(r.vat_amount)}</td><td class="num">${fmt(r.gross_amount)}</td>
      <td class="num ${r.currency && r.exchange_rate === null ? "missing" : ""}">${r.exchange_rate ?? ""}</td><td class="num">${fmt(r.amount_sar)}</td><td class="num">${fmt(r.ledger_amount)}</td>
      <td class="num">${fmt(r.difference)}</td><td>${statusBadge(r.match_status)}</td><td>${statusBadge(r.diff_status)}</td><td>${confBar(r.confidence)} ${r.auditor_confirmed ? '<span class="badge b-aud">✓</span>' : ""}</td><td>${statusBadge(r.review_status)}</td></tr>`;
    }).join("") || `<tr><td colspan="18" class="muted">${t("noData")}</td></tr>`;
    $$("#xt tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = `e/${S.eid}/review/${tr.dataset.id}`)));
  };
  $("#q").addEventListener("input", draw); draw();
}

/* -------------------------------------------------------------------------------- dashboard view */
function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
function destroyCharts() { S.charts.forEach((c) => c.destroy()); S.charts = []; }
const SERIES = () => ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"].map(css);

function filtered() {
  const f = S.filters;
  return S.rows.filter((r) => (!f.from || (r.invoice_date && r.invoice_date >= f.from)) && (!f.to || (r.invoice_date && r.invoice_date <= f.to))
    && (!f.customer || r.customer === f.customer) && (!f.invno || (r.invoice_number || "").toLowerCase().includes(f.invno.toLowerCase()))
    && (!f.currency || r.currency === f.currency) && (!f.match || r.match_status === f.match) && (!f.diff || r.diff_status === f.diff)
    && (!f.review || r.review_status === f.review));
}
function viewDashboard(v) {
  const uniq = (k) => [...new Set(S.rows.map((r) => r[k]).filter(Boolean))].sort();
  const sel = (key, label, opts) => `<label class="f">${label}<select data-f="${key}"><option value="">${t("all")}</option>${opts.map((o) => `<option ${S.filters[key] === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>`;
  v.innerHTML = `<div class="filters card">
    <label class="f">${t("fy")}<select disabled><option>${esc(S.eng.financial_year)}</option></select></label>
    <label class="f">${t("from")}<input type="date" data-f="from" value="${esc(S.filters.from || "")}"></label>
    <label class="f">${t("to")}<input type="date" data-f="to" value="${esc(S.filters.to || "")}"></label>
    ${sel("customer", t("customer"), uniq("customer"))}
    <label class="f">${t("invNo")}<input data-f="invno" value="${esc(S.filters.invno || "")}"></label>
    ${sel("currency", t("currency"), uniq("currency"))}
    ${sel("match", t("match"), ["Matched", "Not Found in Ledger", "Possible Match", "Duplicate Invoice", "Amount Mismatch", "Customer Mismatch", "Ledger Not Uploaded"])}
    ${sel("diff", t("diffStatus"), ["No Difference", "Difference Identified", "Missing Ledger Amount", "Missing Invoice Data"])}
    ${sel("review", t("reviewStatus"), ["pending", "approved", "investigate", "rejected"])}
    <button id="fr">${t("reset")}</button></div>
    <div class="kpis" id="kpis"></div>
    <div class="grid g3" id="charts"></div>
    <div class="card" style="margin-top:14px"><h3>Data table (reconciles to KPIs and charts)</h3><div class="table-wrap" style="max-height:340px"><table class="t" id="dt"></table></div></div>`;
  $$("[data-f]").forEach((el) => el.addEventListener(el.tagName === "INPUT" && el.type !== "date" ? "input" : "change", () => { S.filters[el.dataset.f] = el.value || undefined; drawDash(); }));
  $("#fr").addEventListener("click", () => { S.filters = {}; viewDashboard(v); });
  drawDash();
}
function drawDash() {
  destroyCharts();
  const R = filtered();
  const sum = (a) => a.reduce((s, x) => s + (x || 0), 0);
  const k = [
    ["kTotal", R.length, ""], ["kExtracted", R.filter((r) => r.status === "extracted").length, `${R.filter((r) => r.status === "needs_manual").length} need manual entry`],
    ["kPending", R.filter((r) => r.review_status === "pending").length, ""], ["kMatched", R.filter((r) => r.match_status === "Matched").length, ""],
    ["kUnmatched", R.filter((r) => ["Not Found in Ledger", "Possible Match"].includes(r.match_status)).length, "Not found + possible match"],
    ["kDup", R.filter((r) => r.match_status === "Duplicate Invoice").length, ""], ["kDiff", R.filter((r) => r.diff_status === "Difference Identified").length, `Tolerance SAR ${fmt(S.eng.tolerance)}`],
    ["kInvTotal", fmt(sum(R.map((r) => r.amount_sar))), `${R.filter((r) => r.amount_sar === null).length} without SAR amount`],
    ["kLedger", fmt(sum(R.map((r) => r.ledger_amount))), "Linked ledger rows only"], ["kNetDiff", fmt(sum(R.map((r) => r.difference))), "Ledger − invoice (SAR)"],
  ];
  $("#kpis").innerHTML = k.map(([l, val, s]) => `<div class="kpi"><div class="k">${t(l)}</div><div class="v">${esc(val)}</div><div class="s">${esc(s)}</div></div>`).join("");
  const ids = ["cCountMonth", "cAmtMonth", "cAmtCust", "cMatch", "cDiff", "cCur", "cConf"];
  $("#charts").innerHTML = ids.map((id) => `<div class="card chart-card" ${id === "cAmtCust" || id === "cDiff" ? 'style="grid-column: span 1"' : ""}><h3>${t(id)}</h3><div class="chart-box"><canvas id="${id}" role="img" aria-label="${esc(t(id))}"></canvas></div></div>`).join("");
  if (!window.Chart) return;
  const [c1, c2, c3, c4] = SERIES();
  Chart.defaults.color = css("--text-2"); Chart.defaults.font.family = css("--font"); Chart.defaults.borderColor = css("--grid");
  const months = [...new Set(R.map((r) => r.month).filter(Boolean))].sort();
  const base = (extra = {}) => ({ responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false }, tooltip: { callbacks: {} } },
    scales: { x: { grid: { display: false } }, y: { beginAtZero: true, grid: { color: css("--grid") } } }, ...extra });
  const bar = { borderRadius: 4, borderSkipped: "start", maxBarThickness: 34 };
  const mk = (id, cfg) => S.charts.push(new Chart($("#" + id), cfg));
  mk("cCountMonth", { type: "bar", data: { labels: months, datasets: [{ label: "Invoices", data: months.map((m) => R.filter((r) => r.month === m).length), backgroundColor: c1, ...bar }] }, options: base() });
  mk("cAmtMonth", { type: "bar", data: { labels: months, datasets: [{ label: "SAR", data: months.map((m) => sum(R.filter((r) => r.month === m).map((r) => r.amount_sar))), backgroundColor: c1, ...bar }] },
    options: base({ plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => "SAR " + fmt(c.raw) } } } }) });
  const byCust = {}; R.forEach((r) => { if (r.customer) byCust[r.customer] = (byCust[r.customer] || 0) + (r.amount_sar || 0); });
  const top = Object.entries(byCust).sort((a, b) => b[1] - a[1]).slice(0, 10);
  mk("cAmtCust", { type: "bar", data: { labels: top.map((x) => x[0].length > 28 ? x[0].slice(0, 27) + "…" : x[0]), datasets: [{ label: "SAR", data: top.map((x) => x[1]), backgroundColor: c1, ...bar, borderSkipped: "start" }] },
    options: base({ indexAxis: "y", plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => "SAR " + fmt(c.raw) } } }, scales: { x: { beginAtZero: true, grid: { color: css("--grid") } }, y: { grid: { display: false }, reverse: S.lang === "ar" && false } } }) });
  const mStat = ["Matched", "Amount Mismatch", "Customer Mismatch", "Possible Match", "Duplicate Invoice", "Not Found in Ledger", "Ledger Not Uploaded"];
  const statusCol = { "Matched": css("--good"), "Amount Mismatch": css("--crit"), "Customer Mismatch": css("--warn"), "Possible Match": css("--serious"), "Duplicate Invoice": css("--s7"), "Not Found in Ledger": css("--s8"), "Ledger Not Uploaded": css("--text-3") };
  const present = mStat.filter((s) => R.some((r) => r.match_status === s));
  mk("cMatch", { type: "bar", data: { labels: present, datasets: [{ label: "Invoices", data: present.map((s) => R.filter((r) => r.match_status === s).length), backgroundColor: present.map((s) => statusCol[s]), ...bar }] }, options: base() });
  const dif = R.filter((r) => r.difference !== null && r.diff_status === "Difference Identified").sort((a, b) => Math.abs(b.difference) - Math.abs(a.difference)).slice(0, 12);
  mk("cDiff", { type: "bar", data: { labels: dif.map((r) => r.invoice_number || r.file), datasets: [{ label: "Difference SAR", data: dif.map((r) => r.difference), backgroundColor: dif.map((r) => (r.difference > 0 ? c2 : c1)), ...bar, borderSkipped: false }] },
    options: base({ plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => `SAR ${fmt(c.raw)} (${c.raw > 0 ? "ledger higher" : "ledger lower"})` } } }, scales: { x: { grid: { display: false }, ticks: { maxRotation: 60, autoSkip: false, font: { size: 10 } } }, y: { grid: { color: css("--grid") } } } }) });
  const curs = [...new Set(R.map((r) => r.currency || "Not identified"))];
  mk("cCur", { type: "doughnut", data: { labels: curs, datasets: [{ data: curs.map((c) => R.filter((r) => (r.currency || "Not identified") === c).length), backgroundColor: SERIES().slice(0, curs.length), borderColor: css("--surface"), borderWidth: 2 }] },
    options: { responsive: true, maintainAspectRatio: false, animation: false, cutout: "62%", plugins: { legend: { position: "bottom" } } } });
  const bins = ["<50%", "50–75%", "75–90%", "≥90%", "Not extracted"];
  const binOf = (c) => (c === null || c === 0 ? 4 : c < 0.5 ? 0 : c < 0.75 ? 1 : c < 0.9 ? 2 : 3);
  mk("cConf", { type: "bar", data: { labels: bins, datasets: [{ label: "Invoices", data: bins.map((_, i) => R.filter((r) => binOf(r.confidence) === i).length), backgroundColor: [css("--crit"), css("--warn"), c1, css("--good"), css("--text-3")], ...bar }] }, options: base() });
  $("#dt").innerHTML = `<thead><tr><th>#</th><th>${t("invNo")}</th><th>${t("invDate")}</th><th>${t("customer")}</th><th>${t("currency")}</th><th class="num">${t("amtSar")}</th><th class="num">${t("ledgerAmt")}</th><th class="num">${t("diff")}</th><th>${t("match")}</th><th>${t("diffStatus")}</th><th>${t("reviewStatus")}</th></tr></thead><tbody>${R.map((r) => `<tr class="click" data-id="${r.invoice_id}"><td>${r.sno}</td><td>${esc(r.invoice_number)}</td><td>${esc(r.invoice_date)}</td><td dir="auto">${esc(r.customer)}</td><td>${esc(r.currency)}</td><td class="num">${fmt(r.amount_sar)}</td><td class="num">${fmt(r.ledger_amount)}</td><td class="num">${fmt(r.difference)}</td><td>${statusBadge(r.match_status)}</td><td>${statusBadge(r.diff_status)}</td><td>${statusBadge(r.review_status)}</td></tr>`).join("")}
    <tr><td></td><td colspan="4"><b>Total (${R.length})</b></td><td class="num"><b>${fmt(sum(R.map((r) => r.amount_sar)))}</b></td><td class="num"><b>${fmt(sum(R.map((r) => r.ledger_amount)))}</b></td><td class="num"><b>${fmt(sum(R.map((r) => r.difference)))}</b></td><td colspan="3"></td></tr></tbody>`;
  $$("#dt tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = `e/${S.eid}/review/${tr.dataset.id}`)));
}

/* --------------------------------------------------------------------------------- findings view */
async function viewFindings(v) {
  S.findings = await api("GET", `/api/engagements/${S.eid}/findings`);
  const byInv = Object.fromEntries(S.rows.map((r) => [r.invoice_id, r]));
  v.innerHTML = `<div class="card"><div class="row"><h3>${t("findings")}</h3><span class="spacer"></span>
    <select id="fRisk"><option value="">${t("risk")}: ${t("all")}</option><option>High</option><option>Medium</option><option>Low</option></select>
    <select id="fSt"><option value="">${t("auditorStatus")}: ${t("all")}</option><option>open</option><option>cleared</option><option>escalated</option><option>adjustment_proposed</option></select></div>
    <div class="table-wrap" style="margin-top:10px;max-height:75vh"><table class="t" id="ft"><thead><tr><th>${t("risk")}</th><th>${t("invNo")}</th><th>${t("observation")}</th><th>${t("reason")}</th><th>${t("document")}</th><th>${t("procedure")}</th><th>${t("auditorStatus")}</th></tr></thead><tbody></tbody></table></div></div>`;
  const draw = () => {
    const r = $("#fRisk").value, s = $("#fSt").value;
    $("#ft tbody").innerHTML = S.findings.filter((f) => (!r || f.risk === r) && (!s || f.review_status === s)).map((f) => {
      const row = byInv[f.invoice_id];
      return `<tr><td>${statusBadge(f.risk)}<div class="small muted mono">${esc(f.code)}</div></td>
      <td>${row ? `<a href="#e/${S.eid}/review/${f.invoice_id}">${esc(row.invoice_number || row.file)}</a>` : `<span class="muted">Ledger</span>`}</td>
      <td dir="auto">${esc(f.observation)}</td><td class="small">${esc(f.reason)}</td><td class="small">${esc(f.document)}</td><td class="small">${esc(f.procedure)}</td>
      <td>${statusBadge(f.review_status)}${f.reviewer_note ? `<div class="small muted">${esc(f.reviewed_by)}: ${esc(f.reviewer_note)}</div>` : ""}
        ${S.user.permissions.edit && S.eng.status !== "finalized" ? `<div class="row" style="margin-top:4px"><button class="sm" data-fid="${f.id}" data-s="cleared">Clear</button><button class="sm" data-fid="${f.id}" data-s="escalated">Escalate</button><button class="sm" data-fid="${f.id}" data-s="adjustment_proposed">Adjust</button></div>` : ""}</td></tr>`;
    }).join("") || `<tr><td colspan="7" class="muted">${t("noData")}</td></tr>`;
    $$("#ft button[data-fid]").forEach((b) => b.addEventListener("click", wrap(async () => {
      const note = prompt("Reviewer note (required to clear):"); if (note === null) return;
      await api("PATCH", `/api/findings/${b.dataset.fid}`, { review_status: b.dataset.s, reviewer_note: note });
      S.findings = await api("GET", `/api/engagements/${S.eid}/findings`); draw();
    })));
  };
  $("#fRisk").addEventListener("change", draw); $("#fSt").addEventListener("change", draw); draw();
}

/* ----------------------------------------------------------------------------------- review view */
async function viewReview(v) {
  if (!S.invoiceId) {
    v.innerHTML = `<div class="card"><h3>${t("select")}</h3><div class="table-wrap"><table class="t"><tbody>${S.rows.map((r) => `<tr class="click" data-id="${r.invoice_id}"><td>${r.sno}</td><td>${esc(r.invoice_number || r.file)}</td><td dir="auto">${esc(r.customer)}</td><td>${statusBadge(r.review_status)}</td></tr>`).join("")}</tbody></table></div></div>`;
    $$("tr[data-id]").forEach((tr) => tr.addEventListener("click", () => (location.hash = `e/${S.eid}/review/${tr.dataset.id}`)));
    return;
  }
  const d = await api("GET", `/api/invoices/${S.invoiceId}`);
  const inv = d.invoice, row = d.row || {}, F = inv.fields || {}, AI = inv.ai_fields || {}, m = inv.match || {};
  const can = S.user.permissions.edit && d.engagement_status !== "finalized";
  const order = ["invoice_number", "invoice_date", "buyer_name", "buyer_vat", "seller_name", "seller_vat", "currency", "description", "quantity", "unit_price", "discount", "taxable_amount", "vat_rate", "vat_amount", "gross_amount", "credit_note_ref", "purchase_order"];
  const man = inv.manual || {};
  const yn = (k, label) => `<label class="f">${label}<select data-man="${k}" ${can ? "" : "disabled"}><option value=""></option>${["Yes", "No", "N/A"].map((o) => `<option ${man[k] === o ? "selected" : ""}>${o}</option>`).join("")}</select></label>`;
  v.innerHTML = `<div class="row" style="margin-bottom:10px">
      <button ${d.prev_id ? "" : "disabled"} id="pv">‹ ${t("prev")}</button><button ${d.next_id ? "" : "disabled"} id="nx">${t("next")} ›</button>
      <b dir="ltr">${esc(inv.original_name)}</b> ${statusBadge(inv.status)} ${statusBadge(inv.review_status)} <span class="badge b-neutral">${esc(inv.doc_type || "unclassified")}</span>
      <span class="badge b-neutral">${esc(inv.language || "")}</span><span class="small muted">${esc(inv.extraction_method || "")} · ${inv.page_count || "?"} page(s) · SHA-256 ${esc(inv.sha256.slice(0, 12))}…</span>
      <span class="spacer"></span><button id="openPdf">${t("openPdf")}</button></div>
    ${(inv.extraction_notes || []).length ? `<div class="notice warn" style="margin-bottom:10px">${inv.extraction_notes.map(esc).join("<br>")}</div>` : ""}
    <div class="review">
      <div class="viewer card"><div class="row" style="margin-bottom:8px"><span class="small muted">${t("page")}</span><select id="pg">${Array.from({ length: inv.page_count || 1 }, (_, i) => `<option>${i + 1}</option>`).join("")}</select>
        <span class="small muted" id="srcTxt" dir="auto"></span></div><div class="page-wrap" id="pw"><img id="pimg" alt="Invoice page"></div></div>
      <div class="grid">
        <div class="card"><div class="row"><h3>${t("fields")}</h3><span class="spacer"></span>${can ? `<button class="sm" id="cAll">${t("confirmAll")}</button>` : ""}</div>
          <div class="table-wrap" style="max-height:none"><table class="t fields"><thead><tr><th>${t("field")}</th><th>${t("value")}</th><th>${t("origin")}</th><th>${t("confidence")}</th><th>${t("page")}</th><th></th></tr></thead><tbody>
          ${order.map((k) => { const f = F[k] || {}; const ai = AI[k] || {}; const low = f.value !== null && f.value !== undefined && f.confidence < 0.75 && !f.confirmed;
            return `<tr data-k="${k}"><td>${esc(fl(k))}</td><td class="${f.value === null || f.value === undefined ? (["invoice_number", "invoice_date", "buyer_name", "taxable_amount", "currency"].includes(k) ? "missing" : "") : low ? "lowconf" : ""}">
              <input data-k="${k}" value="${esc(f.value ?? "")}" dir="auto" ${can ? "" : "disabled"}>
              ${f.method === "auditor" && ai.value !== f.value ? `<div class="small muted">AI: ${esc(ai.value ?? "—")} → ${esc(f.edited_by)}</div>` : ""}
              ${f.note ? `<div class="small muted">${esc(f.note)}</div>` : ""}</td>
              <td>${f.method === "auditor" ? '<span class="badge b-aud">Auditor</span>' : f.value === null || f.value === undefined ? "" : `<span class="badge b-ai">AI · ${esc(f.method)}</span>`}${f.confirmed && f.method !== "auditor" ? ' <span class="badge b-aud">✓</span>' : ""}</td>
              <td>${f.value === null || f.value === undefined ? "" : confBar(f.confidence)}</td><td>${f.page ?? ""}</td>
              <td>${can && f.value !== null && f.value !== undefined && !f.confirmed ? `<button class="sm" data-confirm="${k}">${t("confirm")}</button>` : ""}</td></tr>`; }).join("")}
          </tbody></table></div>
          ${(inv.line_items || []).length ? `<h4>${t("lineItems")} (${inv.line_items.length})</h4><div class="table-wrap" style="max-height:180px"><table class="t"><tbody>${inv.line_items.map((li) => `<tr><td dir="auto">${esc(li.description)}</td><td class="num">${esc(li.quantity ?? "")}</td><td class="num">${fmt(li.unit_price)}</td><td class="num">${fmt(li.amount)}</td></tr>`).join("")}</tbody></table></div>` : ""}
        </div>
        <div class="card"><h3>${t("calculations")}</h3><div class="calc">
          <span>E · ${t("amountExVat")}</span><b class="num">${fmt(row.invoice_amount)} ${esc(row.currency || "")}</b>
          <span>I · ${t("fx")}</span><b class="num">${row.exchange_rate ?? "—"} ${inv.fx_override ? '<span class="badge b-aud">override</span>' : ""}</b>
          <span>J · ${t("amtSar")} = E × I</span><b class="num">${fmt(row.amount_sar)}</b>
          <span>D · ${t("ledgerAmt")}</span><b class="num">${fmt(row.ledger_amount)}</b>
          <span>K · ${t("diff")} = D − J</span><b class="num">${fmt(row.difference)}</b>
          <span>${t("diffStatus")}</span><span>${statusBadge(row.diff_status || "")}</span></div>
          ${can && row.currency && row.currency !== "SAR" ? `<div class="row" style="margin-top:8px"><button class="sm" id="fxo">Invoice-specific rate…</button></div>` : ""}</div>
        <div class="card"><h3>${t("ledgerRecord")}</h3><div>${statusBadge(m.status || "Ledger Not Uploaded")} ${m.method ? `<span class="small muted">${esc(m.method)}</span>` : ""}</div>
          ${d.ledger ? `<div class="calc" style="margin-top:8px"><span>Row</span><b>${d.ledger.row_number}</b><span>${t("invNo")}</span><b>${esc(d.ledger.invoice_number)}</b><span>${t("customer")}</span><b dir="auto">${esc(d.ledger.customer)}</b><span>${t("invDate")}</span><b>${esc(d.ledger.date)}</b><span>Amount</span><b class="num">${fmt(d.ledger.amount)}</b></div>` : ""}
          <ul class="evidence small">${(m.evidence || []).map((e) => `<li dir="auto">${esc(e)}</li>`).join("")}</ul>
          ${m.status === "Customer Mismatch" && d.ledger && can ? `<button class="sm" id="alias">${t("confirmAlias")}</button>` : ""}
          ${(m.candidates || []).length ? `<h4>${t("candidates")}</h4><div class="table-wrap"><table class="t"><thead><tr><th>Row</th><th>${t("invNo")}</th><th>${t("customer")}</th><th>Date</th><th class="num">Amount</th><th>Score / evidence</th><th></th></tr></thead><tbody>
            ${m.candidates.map((c) => `<tr><td>${c.row_number}</td><td>${esc(c.invoice_number)}</td><td dir="auto">${esc(c.customer)}</td><td>${esc(c.date)}</td><td class="num">${fmt(c.amount)}</td><td class="small">${Math.round(c.score * 100)}% – ${c.reasons.map(esc).join("; ")}</td><td>${can ? `<button class="sm" data-cand="${c.ledger_id}">${t("confirmMatch")}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>
            ${can ? `<button class="sm danger" id="rejM" style="margin-top:6px">${t("rejectMatches")}</button>` : ""}` : ""}
          ${can && (m.method === "auditor_confirmed" || m.method === "auditor_rejected") ? `<button class="sm" id="resetM">Reset match</button>` : ""}</div>
        <div class="card"><h3>${t("observations")}</h3>${d.findings.map((f) => `<div class="finding ${f.risk}"><div class="row">${statusBadge(f.risk)} <b dir="auto">${esc(f.observation)}</b></div><div class="small muted">${esc(f.reason)} · ${esc(f.document)}</div><div class="small">→ ${esc(f.procedure)}</div><div class="small">${statusBadge(f.review_status)} ${esc(f.reviewer_note || "")}</div></div>`).join("") || `<span class="badge b-good">✓ No findings</span>`}</div>
        <div class="card"><h3>${t("manualEvidence")}</h3><div class="grid g2">${yn("gdn_issued", t("gdnIssued"))}
          <label class="f">${t("gdnDate")}<input type="date" data-man="gdn_date" value="${esc(man.gdn_date || "")}" ${can ? "" : "disabled"}></label>
          ${yn("price_list_matched", t("priceMatched"))}${yn("invoice_approved", t("invApproved"))}</div>
          <label class="f" style="margin-top:8px">${t("explanation")}<textarea data-man="explanation" rows="2" ${can ? "" : "disabled"}>${esc(man.explanation || "")}</textarea></label>
          ${row.suggestion ? `<div class="notice" style="margin-top:8px"><b>${t("aiSuggestion")}:</b> <span dir="auto">${esc(row.suggestion)}</span> ${can ? `<button class="sm" id="useSug">${t("useSuggestion")}</button>` : ""}</div>` : ""}
          ${can ? `<div class="row end" style="margin-top:8px"><button class="primary" id="saveMan">${t("save")}</button></div>` : ""}</div>
        <div class="card"><h3>${t("reviewStatus")}: ${statusBadge(inv.review_status)} <span class="small muted">${esc(inv.reviewer || "")} ${esc((inv.reviewed_at || "").slice(0, 16))}</span></h3>
          <textarea id="revC" rows="2" placeholder="${t("note")}" style="width:100%"></textarea>
          ${S.user.permissions.approve && d.engagement_status !== "finalized" ? `<div class="row" style="margin-top:8px"><button class="good" data-rev="approved">✓ ${t("approve")}</button><button data-rev="investigate">! ${t("investigate")}</button><button class="danger" data-rev="rejected">✕ ${t("reject")}</button><button class="ghost" data-rev="pending">↺</button></div>` : ""}
          <h4>${t("comments")}</h4>${d.comments.map((c) => `<div class="small"><b>${esc(c.user)}</b> <span class="muted">${esc(c.ts.slice(0, 16))}</span><div dir="auto">${esc(c.text)}</div></div>`).join("") || '<div class="small muted">—</div>'}
          ${can ? `<div class="row" style="margin-top:6px"><input id="cmt" style="flex:1" placeholder="${t("addComment")}"><button class="sm" id="addC">+</button></div>` : ""}
          <h4>${t("history")}</h4><div class="history">${d.history.map((h) => `<div><span class="muted">${esc(h.ts.replace("T", " ").slice(0, 16))}</span> <b>${esc(h.user)}</b> ${esc(h.action)} ${esc(h.field || "")} ${h.old_value !== null ? esc(h.old_value) + " → " : ""}${esc(h.new_value ?? "")} ${h.note ? "· " + esc(h.note) : ""}</div>`).join("")}</div>
          ${S.user.permissions.configure && can ? `<div class="row end" style="margin-top:8px"><button class="sm" id="reex">Re-extract</button><button class="sm danger" id="excl">${t("exclude")}</button></div>` : ""}</div>
      </div></div>`;
  // page viewer with source highlights
  const showPage = wrap(async (pg) => {
    const { blob, headers } = await blobOf("GET", `/api/invoices/${inv.id}/page/${pg}`);
    const img = $("#pimg"); img.src = URL.createObjectURL(blob);
    const pw = +headers.get("X-Page-Width"), ph = +headers.get("X-Page-Height");
    img.onload = () => drawHL(pg, pw, ph);
  });
  function drawHL(pg, pw, ph) {
    $$(".hl", $("#pw")).forEach((x) => x.remove());
    const img = $("#pimg"), sx = img.clientWidth / pw, sy = img.clientHeight / ph;
    Object.entries(F).forEach(([k, f]) => {
      if (!f.bbox || f.page !== pg) return;
      const el = document.createElement("div"); el.className = "hl" + (S.selField === k ? " active" : "");
      el.style.left = f.bbox[0] * sx - 2 + "px"; el.style.top = f.bbox[1] * sy - 2 + "px";
      el.style.width = (f.bbox[2] - f.bbox[0]) * sx + 4 + "px"; el.style.height = (f.bbox[3] - f.bbox[1]) * sy + 4 + "px";
      el.title = fl(k); $("#pw").appendChild(el);
    });
  }
  $("#pg").addEventListener("change", (e) => showPage(+e.target.value));
  showPage(1);
  $$("tr[data-k]").forEach((tr) => tr.addEventListener("click", () => {
    const k = tr.dataset.k, f = F[k] || {}; S.selField = k;
    $$("tr[data-k]").forEach((x) => x.classList.toggle("sel", x === tr));
    $("#srcTxt").textContent = f.source_text ? `${fl(k)}: “${f.source_text}”` : "";
    if (f.page && +$("#pg").value !== f.page) { $("#pg").value = f.page; showPage(f.page); }
    else { const img = $("#pimg"); if (img.naturalWidth) { $$(".hl").forEach((x) => x.remove()); showPage(+$("#pg").value); } }
  }));
  const go = (id) => (location.hash = `e/${S.eid}/review/${id}`);
  $("#pv").addEventListener("click", () => d.prev_id && go(d.prev_id));
  $("#nx").addEventListener("click", () => d.next_id && go(d.next_id));
  $("#openPdf").addEventListener("click", wrap(async () => { const { blob } = await blobOf("GET", `/api/invoices/${inv.id}/pdf`); window.open(URL.createObjectURL(blob), "_blank", "noopener"); }));
  const reload = () => renderEngagement($("#app"));
  $$("input[data-k]").forEach((inp) => inp.addEventListener("change", wrap(async () => {
    const reason = prompt(t("reasonPrompt"), "Corrected to agree with source document");
    if (reason === null) { inp.value = (F[inp.dataset.k] || {}).value ?? ""; return; }
    await api("PATCH", `/api/invoices/${inv.id}/fields`, { field: inp.dataset.k, value: inp.value, reason }); toast("Saved – recalculated"); reload();
  })));
  $$("button[data-confirm]").forEach((b) => b.addEventListener("click", wrap(async (e) => { e.stopPropagation(); await api("POST", `/api/invoices/${inv.id}/confirm`, { fields: [b.dataset.confirm] }); reload(); })));
  $("#cAll")?.addEventListener("click", wrap(async () => { await api("POST", `/api/invoices/${inv.id}/confirm`, { fields: order }); toast("Values confirmed"); reload(); }));
  $$("button[data-cand]").forEach((b) => b.addEventListener("click", wrap(async () => { const r = prompt("Reason / evidence for confirming this match:"); if (r === null) return; await api("POST", `/api/invoices/${inv.id}/match`, { action: "confirm", ledger_id: +b.dataset.cand, reason: r }); reload(); })));
  $("#rejM")?.addEventListener("click", wrap(async () => { const r = prompt("Reason for rejecting proposed matches:"); if (r === null) return; await api("POST", `/api/invoices/${inv.id}/match`, { action: "reject", reason: r }); reload(); }));
  $("#resetM")?.addEventListener("click", wrap(async () => { await api("POST", `/api/invoices/${inv.id}/match`, { action: "reset", reason: "Reset by auditor" }); reload(); }));
  $("#alias")?.addEventListener("click", wrap(async () => { await api("POST", `/api/engagements/${S.eid}/aliases`, { invoice_name: F.buyer_name.value, ledger_name: d.ledger.customer }); toast("Alias confirmed for all invoices of this customer"); reload(); }));
  $("#fxo")?.addEventListener("click", wrap(async () => { const r = prompt("Invoice-specific exchange rate (blank to remove):", inv.fx_override || ""); if (r === null) return; const why = prompt("Reason / source:"); if (!why) return; await api("POST", `/api/invoices/${inv.id}/fx`, { rate: r ? +r : null, reason: why }); reload(); }));
  $("#useSug")?.addEventListener("click", () => { $("textarea[data-man=explanation]").value = row.suggestion; toast("Review the text, then Save to confirm it"); });
  $("#saveMan")?.addEventListener("click", wrap(async () => { const body = {}; $$("[data-man]").forEach((el) => (body[el.dataset.man] = el.value || null)); await api("PATCH", `/api/invoices/${inv.id}/manual`, body); toast("Saved"); reload(); }));
  $$("button[data-rev]").forEach((b) => b.addEventListener("click", wrap(async () => { await api("POST", `/api/invoices/${inv.id}/review`, { status: b.dataset.rev, comment: $("#revC").value || null }); toast("Review status updated"); if (b.dataset.rev === "approved" && d.next_id) go(d.next_id); else reload(); })));
  $("#addC")?.addEventListener("click", wrap(async () => { const v2 = $("#cmt").value.trim(); if (!v2) return; await api("POST", `/api/invoices/${inv.id}/comments`, { text: v2 }); reload(); }));
  $("#reex")?.addEventListener("click", wrap(async () => { await api("POST", `/api/invoices/${inv.id}/reextract`); toast("Queued for re-extraction (auditor values are kept)"); }));
  $("#excl")?.addEventListener("click", wrap(async () => { const r = prompt("Reason for excluding from the population (file is retained):"); if (!r) return; await api("DELETE", `/api/invoices/${inv.id}?reason=${encodeURIComponent(r)}`); reload(); }));
}

/* ---------------------------------------------------------------------------------- exports view */
function viewExports(v) {
  const s = S.summary, e = S.eng;
  v.innerHTML = `<div class="grid g2"><div class="card"><h3>${t("exports")}</h3><div class="grid">
    <button class="primary" id="xWp">⬇ ${t("exportWp")}</button><button data-x="findings.pdf">⬇ ${t("exportPdf")}</button>
    <button data-x="extraction.csv">⬇ ${t("exportCsv")}</button><button data-x="exceptions.xlsx">⬇ ${t("exportExc")}</button><button data-x="log.xlsx">⬇ ${t("exportLog")}</button></div>
    <p class="small muted">The working paper is written to a copy of the uploaded template; the original is never modified. Until finalised it is marked DRAFT.</p>
    <h4>${t("finalize")}</h4>${e.status === "finalized" ? `<div class="notice">✓ ${t("finalized")} – ${esc(e.finalized_by)} · ${esc(e.finalized_at)}</div>${S.user.permissions.finalize ? `<button class="sm" id="reopen" style="margin-top:8px">Re-open…</button>` : ""}`
      : `<p class="small">Requires every invoice approved or rejected, no open high-risk findings, and manager/partner role.</p><button class="good" id="fin" ${S.user.permissions.finalize ? "" : "disabled"}>${t("finalize")}</button>`}
    <h4>Generated files</h4><div class="small">${e.files.filter((f) => f.kind === "output").map((f) => `<div><a href="#" data-fid="${f.id}">${esc(f.original_name)}</a> <span class="muted">${esc(f.uploaded_by)} · ${esc(f.uploaded_at.slice(0, 16))}</span></div>`).join("") || "—"}</div></div>
    <div class="card"><h3>${t("summary")}</h3><div class="calc">
      <span>Invoices tested</span><b class="num">${s.invoices_tested}</b><span>Total value tested (SAR, excl. VAT)</span><b class="num">${fmt(s.total_value_tested_sar)}</b>
      <span>Matched</span><b class="num">${s.matched}</b><span>Unmatched</span><b class="num">${s.unmatched}</b>
      <span>Open exceptions</span><b class="num">${s.exceptions}</b><span>High / Medium / Low</span><b class="num">${s.findings_by_risk.High} / ${s.findings_by_risk.Medium} / ${s.findings_by_risk.Low}</b>
      <span>Total identified differences (SAR)</span><b class="num">${fmt(s.total_differences)}</b><span>Approved by auditor</span><b class="num">${s.approved}</b>
      <span>Ledger rows / total</span><b class="num">${s.ledger_rows} / ${fmt(s.ledger_total)}</b></div>
      <h4>${t("outstanding")} (${s.outstanding_procedures.length})</h4><div class="table-wrap" style="max-height:320px"><table class="t"><tbody>${s.outstanding_procedures.map((o) => `<tr><td><a href="#e/${S.eid}/review/${o.invoice_id}">${esc(o.invoice_number || o.file)}</a></td><td class="small">${o.items.map(esc).join(", ")}</td></tr>`).join("")}</tbody></table></div></div></div>`;
  $("#xWp").addEventListener("click", () => download("POST", `/api/engagements/${S.eid}/export/workpaper`).then(() => render()));
  $$("button[data-x]").forEach((b) => b.addEventListener("click", () => download("GET", `/api/engagements/${S.eid}/export/${b.dataset.x}`)));
  $$("a[data-fid]").forEach((a) => a.addEventListener("click", (ev) => { ev.preventDefault(); download("GET", `/api/files/${a.dataset.fid}/download`); }));
  $("#fin")?.addEventListener("click", wrap(async () => { if (!confirm("Finalise and lock this working paper?")) return; await api("POST", `/api/engagements/${S.eid}/finalize`); toast("Finalised"); render(); }));
  $("#reopen")?.addEventListener("click", wrap(async () => { const r = prompt("Reason for re-opening:"); if (!r) return; await api("POST", `/api/engagements/${S.eid}/reopen?reason=${encodeURIComponent(r)}`); render(); }));
}

async function viewTrail(v) {
  const log = await api("GET", `/api/engagements/${S.eid}/audit-log?limit=2000`);
  v.innerHTML = `<div class="card"><h3>${t("trail")}</h3><div class="table-wrap" style="max-height:75vh"><table class="t"><thead><tr><th>Time (UTC)</th><th>User</th><th>Entity</th><th>Action</th><th>Field</th><th>Old</th><th>New</th><th>Note</th></tr></thead><tbody>
    ${log.map((l) => `<tr><td class="small mono">${esc(l.ts.replace("T", " ").slice(0, 19))}</td><td>${esc(l.user)}</td><td>${esc(l.entity)} ${l.entity_id ? (l.entity === "invoice" ? `<a href="#e/${S.eid}/review/${l.entity_id}">#${l.entity_id}</a>` : `#${l.entity_id}`) : ""}</td>
    <td>${esc(l.action)}</td><td>${esc(l.field || "")}</td><td class="small" dir="auto">${esc(l.old_value || "")}</td><td class="small" dir="auto">${esc(l.new_value || "")}</td><td class="small">${esc(l.note || "")}</td></tr>`).join("")}</tbody></table></div></div>`;
}

async function renderUsers(app) {
  $("#crumb").innerHTML = `<a href="#">${t("engagements")}</a> / <b>${t("users")}</b>`;
  const users = await api("GET", "/api/users");
  app.innerHTML = `<div class="grid g2"><div class="card"><h3>${t("users")}</h3><table class="t"><thead><tr><th>${t("username")}</th><th>${t("fullName")}</th><th>${t("role")}</th><th>${t("active")}</th></tr></thead><tbody>
    ${users.map((u) => `<tr><td>${esc(u.username)}</td><td>${esc(u.full_name)}</td><td><select data-uid="${u.id}" data-a="role">${["viewer", "auditor", "manager", "partner", "admin"].map((r) => `<option ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}</select></td>
    <td><input type="checkbox" data-uid="${u.id}" data-a="active" ${u.active ? "checked" : ""}></td></tr>`).join("")}</tbody></table>
    <p class="small muted">viewer: read only · auditor: upload, edit, review, approve invoices · manager/partner: + tolerance, FX rates, finalise · admin: + users</p></div>
    <div class="card"><h3>Add user</h3><form id="uf" class="grid"><label class="f">${t("username")}<input name="username" required></label><label class="f">${t("fullName")}<input name="full_name" required></label>
    <label class="f">${t("password")}<input name="password" type="password" minlength="8" required></label><label class="f">${t("role")}<select name="role">${["auditor", "viewer", "manager", "partner", "admin"].map((r) => `<option>${r}</option>`).join("")}</select></label>
    <button class="primary">${t("add")}</button></form></div></div>`;
  $$("[data-uid]").forEach((el) => el.addEventListener("change", wrap(async () => {
    await api("PATCH", `/api/users/${el.dataset.uid}`, el.dataset.a === "role" ? { role: el.value } : { active: el.checked }); toast("Saved");
  })));
  $("#uf").addEventListener("submit", wrap(async (e) => { e.preventDefault(); const f = e.target; await api("POST", "/api/users", { username: f.username.value, full_name: f.full_name.value, password: f.password.value, role: f.role.value }); renderUsers(app); }));
}

/* ------------------------------------------------------------------------------------------ boot */
document.addEventListener("DOMContentLoaded", () => {
  const th = localStore("theme"); if (th) document.documentElement.dataset.theme = th;
  $("#langBtn").addEventListener("click", () => { S.lang = S.lang === "ar" ? "en" : "ar"; localStore("lang", S.lang); render(); });
  $("#themeBtn").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const nx = cur === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = nx; localStore("theme", nx); render();
  });
  $("#logoutBtn").addEventListener("click", logout);
  render();
});
