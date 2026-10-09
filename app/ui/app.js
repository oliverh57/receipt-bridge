// Receipt Bridge UI.
//
// No framework and no build step: a few hundred lines that render from one
// state object. Everything shown comes from /api/state (an in-memory
// snapshot — never a network call to Gmail) and /api/receipts, so every
// interaction is local and instant.
//
// SECURITY: receipt text comes from arbitrary email senders and this page
// holds the API token. Every value interpolated into HTML goes through esc().

"use strict";

const TOKEN = window.RB_TOKEN;
const IDLE_POLL_MS = 4000;
const BUSY_POLL_MS = 900;

/** A remembered choice for this Mac's window (Bank Feed's Month / All and
 * how many). Only a convenience: a blocked localStorage just forgets. */
function pref(key, fallback) {
  try { return localStorage.getItem(`rb:${key}`) ?? fallback; } catch { return fallback; }
}
function setPref(key, value) {
  try { localStorage.setItem(`rb:${key}`, String(value)); } catch {}
}

const state = {
  view: "pending",
  month: new Date().toISOString().slice(0, 7),   // Statement month, YYYY-MM
  stView: pref("stView", "month") === "all" ? "all" : "month",   // Bank Feed: a month, or every month
  stLimit: Number(pref("stLimit", "100")) || 0,  // …and then the latest how many (0: all)
  statement: null,
  filedToday: [],
  snap: null,
  receipts: [],
  receiptsKey: "",        // view + version the list was loaded for
  query: "",              // search text for the current list
  selectedId: null,
  excluded: new Set(),    // pending receipts left out of the next export
  suppliers: null,
  shownOutcome: null,
  previewId: null,        // receipt whose PDF is currently in the iframe
  settingsTab: "general",    // Settings section: general | freeagent | email | about
  pollTimer: null,
};

// ---- helpers -----------------------------------------------------------

const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

/** Bank marks: [name pattern, text, background, text colour, logo key]. Specific names first.
 * The logo is the bank's own icon from data/bank-logos (fetched by tools/fetch_bank_logos.py,
 * not shipped); the coloured monogram shows until it loads, or when there is none. */
const BANK_MARKS = [
  [/mettle/i, "M", "#00a9a0", "#fff", "mettle"],
  [/royal bank of scotland|\brbs\b/i, "RBS", "#0a2240", "#fff", "rbs"],
  [/natwest/i, "NW", "#42145f", "#fff", "natwest"],
  [/ulster/i, "U", "#c8102e", "#fff", "ulster"],
  [/bank of scotland/i, "BoS", "#0072ce", "#fff", "bankofscotland"],
  [/bank of ireland/i, "BOI", "#0033a0", "#fff", "bankofireland"],
  [/barclay/i, "B", "#00aeef", "#fff", "barclays"],
  [/cater allen/i, "CA", "#e2231a", "#fff", "caterallen"],
  [/danske/i, "D", "#003755", "#fff", "danske"],
  [/first direct/i, "1d", "#111", "#fff", "firstdirect"],
  [/halifax/i, "H", "#005eb8", "#fff", "halifax"],
  [/hsbc|kinetic/i, "H", "#db0011", "#fff", "hsbc"],
  [/lloyds/i, "L", "#024731", "#fff", "lloyds"],
  [/metro/i, "M", "#e4002b", "#fff", "metro"],
  [/santander/i, "S", "#ec0000", "#fff", "santander"],
  [/starling/i, "S", "#6935d3", "#fff", "starling"],
  [/tide/i, "T", "#3d2bd1", "#fff", "tide"],
  [/\btsb\b/i, "TSB", "#00a1e0", "#fff", "tsb"],
  [/virgin/i, "V", "#e10a0a", "#fff", "virgin"],
  [/wise|transferwise/i, "W", "#9fe870", "#163300", "wise"],
  [/monzo/i, "M", "#ff4f40", "#fff", "monzo"],
  [/revolut/i, "R", "#191c1f", "#fff", "revolut"],
  [/capital on tap/i, "C", "#0a2f5c", "#fff", "capitalontap"],
  [/allica/i, "A", "#00828a", "#fff", "allica"],
];

/** The badge span for a bank account name; unknown banks keep the first-letter square. */
function bankBadge(name) {
  const hit = BANK_MARKS.find(([re]) => re.test(name || ""));
  if (!hit) return `<span class="badge">${esc((name || "?").charAt(0).toUpperCase())}</span>`;
  const [, text, bg, fg, key] = hit;
  return `<span class="badge bank${text.length > 2 ? " wide" : ""}" style="background:${bg};color:${fg}">${esc(text)}<img src="/bank-logo/${key}" alt="" onload="this.parentNode.classList.add('logo')" onerror="this.remove()"></span>`;
}

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "X-Receipt-Bridge": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return res.json();
}

// A receipt's amount. A photo whose currency couldn't be told shows the
// bare number and a "?", never a guessed £.
function amountOf(r) {
  if (r.total === null || r.total === undefined) return "—";
  if (r.source === "photo" && !r.currency) return `${Number(r.total).toFixed(2)} ?`;
  return money(r.total, r.currency);
}

function money(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  try {
    return new Intl.NumberFormat("en-GB", { style: "currency", currency: currency || "GBP" }).format(amount);
  } catch {
    return `${currency} ${Number(amount).toFixed(2)}`;
  }
}

function totalsText(totals) {
  const parts = Object.entries(totals || {}).filter(([, v]) => v).map(([c, v]) => money(v, c));
  return parts.join(" + ") || money(0, "GBP");
}

function shortDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  if (isNaN(d)) return iso;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }) });
}

function longDate(iso) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
  return isNaN(d) ? iso : d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "long", year: "numeric" });
}

function ago(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 45) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)} hr ago`;
  const days = Math.round(secs / 86400);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

let toastTimer = null;
function toast(html, ms = 6000) {
  const el = $("#toast");
  el.innerHTML = html;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}

const DOC_LABEL = {
  supplier: ["supplier", "Supplier receipt", "The supplier's own receipt document"],
  email: ["neutral", "Email receipt", "This supplier's receipt is the email itself — there's no separate document"],
  fallback: ["email", "Email copy", "The supplier's own receipt wasn't available, so this is a printout of the email"],
  none: ["none", "No document", "No PDF could be produced"],
  photo: ["neutral", "Photo", "Photographed on your phone and read on this Mac"],
};
const PAID_BY = { business: "Business account", personal: "Personal expense" };
function docTag(kind) {
  const [cls, label, title] = DOC_LABEL[kind] || DOC_LABEL.none;
  return `<span class="tag ${cls}" title="${esc(title)}">${esc(label)}</span>`;
}

// ---- data --------------------------------------------------------------

async function refresh(force = false) {
  let snap;
  try {
    snap = await api("/api/state");
  } catch (err) {
    $("#status").innerHTML = `<strong>Can't connect.</strong> Retrying…`;
    schedule(3000);
    return;
  }
  const versionChanged = !state.snap || snap.version !== state.snap.version;
  const reloaded = state.receiptsKey;
  state.snap = snap;
  if (!snap.setup?.done && !setup.open && !setup.dismissed) setup.open = true;

  if (state.view === "statement") {
    const all = state.stView === "all";
    const key = `statement:${all ? `all:${state.stLimit}` : state.month}:${snap.version}`;
    if (force || key !== state.receiptsKey) {
      // the payments, and the unlinked files that could be their receipts
      const query = all ? `month=all&limit=${state.stLimit}` : `month=${encodeURIComponent(state.month)}`;
      try { state.statement = await api(`/api/statement?${query}`); }
      catch (err) { state.statement = { error: err.message, rows: [], summary: {} }; }
      state.receipts = await api("/api/receipts?status=pending");
      state.receiptsKey = key;
    }
  } else if (state.view === "emails") {
    await emailsRefresh(force, versionChanged);
  } else if (state.view !== "settings") {
    const key = `${state.view}:${snap.version}`;
    if (force || key !== state.receiptsKey) {
      if (state.view === "archived") {
        // Ignored and couldn't-be-read: nothing to do, kept in case
        const [ignored, failed] = await Promise.all([api("/api/receipts?status=ignored"), api("/api/receipts?status=failed")]);
        state.receipts = [...ignored, ...failed];
      } else {
        const status = state.view === "expenses" ? "pending" : state.view;
        state.receipts = await api(`/api/receipts?status=${status}`);
      }
      if (state.view === "expenses") {
        // the claims already saved to FreeAgent, month by month
        state.filedToday = (await api("/api/receipts?status=exported"))
          .filter((r) => r.paid_by === "personal" && r.status === "filed");
      }
      state.receiptsKey = key;
      if (!["pending", "expenses", "archived"].includes(state.view) && !state.receipts.some((r) => r.id === state.selectedId)) {
        state.selectedId = state.receipts[0]?.id ?? null;
      }
    }
  } else if (force || versionChanged || !state.suppliers) {
    state.suppliers = await api("/api/suppliers");
  }

  if (snap.freeagent?.connected && snap.freeagent.categories && !state.categories?.length) {
    try { state.categories = await api("/api/freeagent/categories"); } catch {}
  }

  if (versionChanged && ["pending", "emails"].includes(state.view)) loadSupplierNames();
  announceOutcome(snap.outcome);
  // Redrawing replaces the preview's controls; never do it while one of
  // them is in use, or an open dropdown would snap shut.
  // Only when something changed (and once a minute, for "5 min ago"):
  // a redraw replaces the button under the pointer.
  const due = force || versionChanged || reloaded !== state.receiptsKey
    || Date.now() - (state.renderedAt || 0) > 60000;
  // …nor while a field or dropdown in the detail is in use (a button with
  // focus is not "in use": that would leave the screen stale after a click)
  const typing = document.activeElement?.matches?.("input:not([type=checkbox]), select, textarea")
    && document.activeElement.closest(".filing, .m-keep, .m-search");
  if (due && !typing) {
    render();
    state.renderedAt = Date.now();
  }
  schedule(snap.activity.busy || snap.queued.length || snap.connecting ? BUSY_POLL_MS : IDLE_POLL_MS);
}

/** The suppliers you've had before, offered as you type a supplier
 * (<datalist id="supplier-names">). Local; at most once every 20 seconds. */
async function loadSupplierNames() {
  if (Date.now() - (state.namesAt || 0) < 20000) return;
  state.namesAt = Date.now();
  try {
    const names = await api("/api/supplier-names");
    const html = names.map((n) => `<option value="${esc(n)}"></option>`).join("");
    const list = $("#supplier-names");
    if (list.innerHTML !== html) list.innerHTML = html;
  } catch {}
}

function schedule(ms) {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(() => refresh(), ms);
}

function announceOutcome(outcome) {
  if (!outcome || outcome.kind === "health") return;
  if (state.shownOutcome === null) { state.shownOutcome = outcome.finished_at; return; }
  if (outcome.finished_at === state.shownOutcome) return;
  state.shownOutcome = outcome.finished_at;
  const quiet = outcome.kind === "scan" && outcome.ok && !outcome.found;
  if (!quiet) toast(esc(outcome.message));
}

// ---- rendering ---------------------------------------------------------

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme;
  else delete root.dataset.theme;
  // The native window follows too, so the title bar matches the page.
  if (theme !== state.appliedTheme) {
    state.appliedTheme = theme;
    window.webkit?.messageHandlers?.rb?.postMessage({ theme });
  }
}

function render() {
  applyTheme(state.snap.theme);
  renderNav();
  renderStatus();
  renderBanner();
  if (state.view === "settings") {
    // Settings is redrawn on every poll; without this the page jumps back
    // to the top each time, which made it impossible to scroll.
    const old = document.querySelector(".settings");
    const top = old && old.dataset.tab === state.settingsTab ? old.scrollTop : 0;
    const open = [...document.querySelectorAll(".settings details[open] > summary")].map((s) => s.textContent);
    renderSettings();
    const fresh = document.querySelector(".settings");
    if (fresh) fresh.scrollTop = top;
    for (const s of document.querySelectorAll(".settings details > summary")) {
      if (open.includes(s.textContent)) s.parentElement.open = true;
    }
  } else if (state.view === "pending") renderFiles();
  else if (state.view === "expenses") renderExpenses();
  else if (state.view === "archived") renderArchived();
  else if (state.view === "statement") renderStatement();
  else if (state.view === "emails") renderEmails();
  else renderList();
  renderSetup();
}

function renderNav() {
  const { counts, needs_attention } = state.snap;
  for (const el of document.querySelectorAll("[data-count]")) {
    const n = counts[el.dataset.count] || 0;
    el.textContent = n ? n : "";
  }

  // Statement: payments missing a receipt this month.
  // Files: everything not saved to FreeAgent yet, expenses included
  document.querySelector('[data-count="files"]').textContent = counts.files || "";
  const missing = counts.missing;
  document.querySelector('[data-count="statement"]').textContent = missing ? `${missing} missing` : "";
  const fa = state.snap.freeagent;
  const badge = $("#sandbox-badge");
  badge.hidden = !(fa?.connected && fa.environment === "sandbox");
  $("#settings-dot").hidden = !needs_attention.length && !state.snap.update?.available;
  for (const el of document.querySelectorAll(".nav-item[data-view]")) {
    el.classList.toggle("active", el.dataset.view === state.view);
  }
}

function renderStatus() {
  const { activity, last_scan_at, queued } = state.snap;
  const status = $("#status");
  const progress = $("#progress");
  const bar = $("#progress-bar");
  const scanBtn = $("#scan-btn");
  const scanning = activity.kind === "scan" || queued.includes("scan");

  status.className = "status";
  if (activity.busy && activity.kind !== "health") {
    const counter = activity.total ? ` · ${activity.current} of ${activity.total}` : "";
    status.innerHTML = `<span class="spinner"></span><strong>${esc(activity.label)}</strong>${esc(counter)}`;
    progress.hidden = false;
    if (activity.total) {
      progress.classList.remove("indeterminate");
      bar.style.width = `${Math.round((activity.current / activity.total) * 100)}%`;
    } else {
      progress.classList.add("indeterminate");
    }
  } else {
    // FreeAgent is what matters; email only when that tool is in use
    progress.hidden = true;
    const fa = state.snap.freeagent || {};
    const parts = [];
    if (fa.last_sync) parts.push(`FreeAgent synced <strong>${esc(ago(fa.last_sync))}</strong>`);
    if (state.snap.accounts.length) {
      parts.push(last_scan_at ? `email checked <strong>${esc(ago(last_scan_at))}</strong>` : "email not checked yet");
    }
    status.innerHTML = parts.length ? parts.map((x) => `<span>${x}</span>`).join("") : "<span>Not checked yet</span>";
  }

  const checking = scanning || queued.includes("freeagent-sync") || queued.includes("photos");
  scanBtn.disabled = checking;
  scanBtn.textContent = checking ? "Checking…" : "Check now";
}

function renderBanner() {
  // FreeAgent is what the app is for; email is a separate, optional tool
  // (Settings → Email), so it only speaks up when a connected
  // account needs reconnecting.
  const { needs_attention, connecting } = state.snap;
  const fa = state.snap.freeagent || {};
  const el = $("#banner");
  if (fa.has_credentials && !fa.connected) {
    // signed out but with data from before (Disconnect clears it): expired
    const expired = /expired|refused/i.test(fa.error || "") || !!fa.last_sync;
    el.innerHTML = `<div class="banner${expired ? "" : " info"}"><div class="text"><b>${expired
      ? "FreeAgent sign-in has expired."
      : "Connect FreeAgent to match and file receipts."}</b>${expired
      ? " Reconnect to file receipts. Showing the last synced data." : ""}</div>
      <button class="btn small" data-action="fa-connect">${expired ? "Reconnect" : "Connect FreeAgent"}</button></div>`;
  } else if (needs_attention.length) {
    const a = needs_attention[0];
    el.innerHTML = `<div class="banner"><div class="text"><b>${esc(a.email)} needs reconnecting.</b> Email receipts aren't being checked.</div>
      ${connecting ? `<button class="btn small" data-action="connect-stop">Cancel sign-in</button>` : `<button class="btn small" data-action="connect">Reconnect</button>`}</div>`;
  } else if (state.snap.update?.can_install) {
    const u = state.snap.update;
    el.innerHTML = `<div class="banner info"><div class="text"><b>Version ${esc(u.latest.replace(/^v/i, ""))} of Receipt Bridge is ${u.restart_needed ? "installed" : "available"}.</b>
      ${u.installing ? " Installing; the app will restart." : u.restart_needed ? " Restart to start using it." : " Takes a few seconds; your receipts and settings are kept."}</div>
      ${updateButton(u, "btn small")}</div>`;
  } else {
    el.innerHTML = "";
  }
}

const VIEW_COPY = {
  pending: { empty: "All caught up", sub: "New receipts appear here.", icon: "✓" },
  exported: { empty: "Nothing filed yet", sub: "Exported receipts appear here.", icon: "🗂" },
  ignored: { empty: "Nothing ignored", sub: "", icon: "🙈" },
  failed: { empty: "Nothing to fix", sub: "", icon: "✓" },
};

function renderList() {
  const content = $("#content");
  const rows = state.receipts;

  if (!rows.length) {
    const copy = VIEW_COPY[state.view];
    const last = state.snap.last_scan_at;
    content.innerHTML = `<div class="empty"><div class="big">${copy.icon}</div><h2>${esc(copy.empty)}</h2>
      ${copy.sub ? `<div>${esc(copy.sub)}</div>` : ""}
      ${state.view === "pending" && last ? `<div>Last checked ${esc(ago(last))}.</div>` : ""}</div>`;
    state.previewId = null;
    return;
  }

  // Keep the list's scroll position and the preview's iframe across redraws.
  const oldList = content.querySelector(".list");
  const scroll = oldList ? oldList.scrollTop : 0;
  const oldFrame = content.querySelector(".doc");

  content.innerHTML = `<div class="list-pane">${listHead()}<div class="list" tabindex="0">${listRows()}</div>
    <div class="hint"><kbd>↑</kbd> <kbd>↓</kbd> move · ${state.view === "pending" ? "<kbd>space</kbd> include · <kbd>⌫</kbd> ignore" : ""}</div></div>
    <div class="preview">${previewHtml()}</div>`;

  content.querySelector(".list").scrollTop = scroll;
  placeDocument(oldFrame);
}

function included() {
  return state.receipts.filter((r) => !state.excluded.has(r.id));
}

// Rows matching the search box. Matches supplier, description, reference,
// amount and date, so "32.40", "whitstable" and "sept" all work.
function visible() {
  const q = state.query.trim().toLowerCase();
  if (!q) return state.receipts;
  return state.receipts.filter((r) =>
    [r.supplier, r.description, r.reference, r.filename, String(r.total ?? ""), shortDate(r.date), r.export_folder]
      .join(" ").toLowerCase().includes(q));
}

const SEARCH_FROM = 12;

// Receipts safe to file without opening each one: exactly one bank payment
// fits (not "likely"), a category is set, and nothing is flagged.
function readyToFile() {
  return included().filter((r) => r.match?.status === "matched" && r.category && !r.flags.length && r.total != null);
}

function fileAllButton() {
  const fa = state.snap.freeagent;
  if (!fa?.connected) return "";
  const ready = readyToFile();
  const busy = state.snap.queued.some((q) => q.startsWith("file:"));
  const label = fa.dry_run ? `Dry run ${ready.length || ""} matched` : `File ${ready.length || ""} matched`;
  return `<button class="btn primary" data-action="file-all" ${ready.length && !busy ? "" : "disabled"}
    title="Receipts with exactly one matching bank payment, a category and nothing to check${fa.dry_run ? ". Dry run: nothing is sent" : ""}">${label}</button>`;
}

function listHead() {
  if (state.view === "pending") {
    const chosen = included();
    const totals = {};
    // Photos whose currency isn't known yet are left out rather than counted as £.
    for (const r of chosen) {
      if (r.source === "photo" && !r.currency) continue;
      totals[r.currency || "GBP"] = (totals[r.currency || "GBP"] || 0) + (r.total || 0);
    }
    const all = chosen.length === state.receipts.length;
    return `<div class="list-head">
      <input type="checkbox" data-action="toggle-all" ${all ? "checked" : ""} title="Include all">
      <div class="summary"><strong>${esc(totalsText(totals))}</strong> · ${chosen.length === state.receipts.length ? `all ${chosen.length}` : `${chosen.length} of ${state.receipts.length}`}</div>
      ${fileAllButton()}
      <button class="btn ${state.snap.freeagent?.connected ? "" : "primary"}" data-action="export" ${chosen.length ? "" : "disabled"}
        title="Copies the selected PDFs into a dated folder, ready to drag into FreeAgent Smart Capture">
        Export ${chosen.length || ""} for FreeAgent</button></div>`;
  }
  const label = { exported: "filed", ignored: "ignored", failed: "couldn't be read" }[state.view];
  const shown = visible().length;
  const count = state.query ? `<strong>${shown}</strong> of ${state.receipts.length}` : `<strong>${state.receipts.length}</strong>`;
  const search = state.receipts.length >= SEARCH_FROM
    ? `<input class="search" type="search" placeholder="Search" value="${esc(state.query)}" aria-label="Search receipts" data-action="search">`
    : "";
  return `<div class="list-head"><div class="summary">${count} ${esc(label)}</div>${search}</div>`;
}

function listRows() {
  const checkable = state.view === "pending";
  const row = (r) => `
    <div class="row ${checkable ? "" : "no-check"} ${r.id === state.selectedId ? "selected" : ""}" data-id="${r.id}">
      ${checkable ? `<input type="checkbox" data-action="include" data-id="${r.id}" ${state.excluded.has(r.id) ? "" : "checked"}
          aria-label="Include ${esc(r.supplier)} ${esc(money(r.total, r.currency))} on ${esc(shortDate(r.date))} in the export">` : ""}
      <div class="row-main">
        <div class="row-top"><span class="row-supplier">${esc(r.supplier)}</span><span class="row-date">${esc(shortDate(r.date))}</span></div>
        <div class="row-desc">${esc(state.view === "failed" ? r.error || r.description : r.description)}</div>
      </div>
      <div class="row-side"><span class="amount">${esc(amountOf(r))}</span>${state.view === "failed" ? "" : r.flags.length ? `<span class="tag email" title="${esc(r.flags.join("\n"))}">Check</span>` : docTag(r.document)}</div>
    </div>`;

  const rows = visible();
  if (!rows.length) return `<div class="empty" style="padding:30px">No receipts match “${esc(state.query)}”.</div>`;
  if (state.view !== "exported") return rows.map(row).join("");

  // Filed receipts read best grouped by the batch they went out in, each
  // batch collapsible so a long history stays scannable.
  return [...batches(rows)].map(([folder, items], index) => {
    const open = groupOpen(folder, index);
    return `
    <div class="group-head" data-action="toggle-group" data-folder="${esc(folder)}"
         role="button" tabindex="0" aria-expanded="${open}" title="⌥-click to open or close all">
      <span class="group-title"><svg class="chev ${open ? "open" : ""}" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5"/></svg>${esc(folder)} · ${items.length}</span>
      <button class="btn quiet small" data-action="reveal" data-folder="${esc(folder)}">Show in Finder</button></div>
    ${open ? items.map(row).join("") : ""}`;
  }).join("");
}

function batches(rows) {
  const groups = new Map();
  for (const r of rows) {
    const key = r.export_folder || "Earlier";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  return groups;
}

// Open/closed per batch, remembered between launches. A batch you haven't
// touched follows the default: the newest open, older ones closed. While
// searching every batch with a match is open, so results can't hide.
const GROUPS_KEY = "rb.filedGroups";
let groupPrefs = {};
try { groupPrefs = JSON.parse(localStorage.getItem(GROUPS_KEY) || "{}"); } catch {}

function groupOpen(folder, index) {
  if (state.query.trim()) return true;
  return folder in groupPrefs ? groupPrefs[folder] : index === 0;
}

function setGroups(folders, open) {
  for (const f of folders) groupPrefs[f] = open;
  try { localStorage.setItem(GROUPS_KEY, JSON.stringify(groupPrefs)); } catch {}
}

function toggleGroup(folder, all) {
  const folders = [...batches(visible()).keys()];
  const index = folders.indexOf(folder);
  const next = !groupOpen(folder, index);
  setGroups(all ? folders : [folder], next);
  // Don't leave the selection inside a batch that just closed.
  const selectedRow = state.receipts.find((r) => r.id === state.selectedId);
  if (!next && selectedRow && (all || (selectedRow.export_folder || "Earlier") === folder)) {
    state.selectedId = navigableIds()[0] ?? state.selectedId;
    state.previewId = null;
  }
  render();
}

// Receipts the keyboard can reach: visible ones not inside a closed batch.
function navigableIds() {
  const rows = visible();
  if (state.view !== "exported") return rows.map((r) => r.id);
  const ids = [];
  [...batches(rows)].forEach(([folder, items], index) => {
    if (groupOpen(folder, index)) ids.push(...items.map((r) => r.id));
  });
  return ids;
}

function selected() {
  return state.receipts.find((r) => r.id === state.selectedId) || null;
}

function previewHtml() {
  const r = selected();
  if (!r) return `<div class="empty">Select a receipt to see it.</div>`;
  const retrying = state.snap.queued.includes(`retry:${r.id}`);
  const actions = [];
  if (state.view === "pending") {
    if (r.document === "fallback") {
      actions.push(`<button class="btn small" data-action="retry" data-id="${r.id}" ${retrying ? "disabled" : ""}>${retrying ? "Fetching…" : "Get the supplier's receipt"}</button>`);
    }
    actions.push(`<button class="btn small" data-action="ignore" data-id="${r.id}">Ignore</button>`);
  }
  if (state.view === "exported" && r.filing?.state === "filed") {
    actions.push(`<button class="btn small danger" data-action="unfile" data-id="${r.id}">Unfile</button>`);
  } else if (state.view === "ignored") {
    actions.push(`<button class="btn small" data-action="restore" data-id="${r.id}">Move back to To file</button>`);
  } else if (state.view === "failed") {
    actions.push(`<button class="btn small" data-action="retry" data-id="${r.id}" ${retrying ? "disabled" : ""}>${retrying ? "Reading…" : "Try reading it again"}</button>`);
  } else if (state.view === "exported" && r.export_folder) {
    actions.push(`<button class="btn small" data-action="reveal" data-folder="${esc(r.export_folder)}">Show in Finder</button>`);
  }

  let note = r.document === "fallback" && state.view === "pending"
    ? `<div class="note">Email copy — ${esc(r.supplier)}'s own receipt wasn't available.</div>`
    : state.view === "failed" ? `<div class="note">${esc(r.error)}</div>` : "";
  if (r.flags.length && state.view !== "failed") {
    note += `<ul class="note flags">${r.flags.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`;
  }

  return `<div class="preview-head">
      <div class="preview-title"><h2>${esc(r.supplier)}</h2>${docTag(r.document)}<span class="amount">${esc(amountOf(r))}</span></div>
      <div class="meta selectable">
        <span>${r.date ? esc(longDate(r.date)) : "No date"}</span>
        ${r.reference ? `<span>Ref <b>${esc(r.reference)}</b></span>` : ""}
        ${r.account ? `<span>${esc(r.account)}</span>` : ""}
        ${r.source === "photo" ? `<span>${esc(PAID_BY[r.paid_by] || "Who paid? Not asked")}</span>` : ""}
        ${r.vat ? `<span>VAT <b>${esc(money(r.vat, r.currency))}</b></span>` : ""}
        ${r.vat_number ? `<span>${esc(r.vat_number)}</span>` : ""}
      </div>
      <div class="meta selectable"><span>${esc(r.description)}</span></div>
      ${note}
      ${matchHtml(r)}
      ${state.view === "pending" ? detailsHtml(r) + filingHtml(r) : filedHtml(r)}
      <div class="preview-actions">${actions.join("")}</div>
    </div>
    <div class="doc-slot" style="flex:1;display:flex;min-height:0"></div>`;
}

const GROUP_LABEL = {
  admin_expenses_categories: "Admin expenses",
  cost_of_sales_categories: "Cost of sales",
  general_categories: "General",
};

// Corrections: open straight away for a photo with doubts, a click away otherwise.
function detailsHtml(r) {
  const open = r.source === "photo" && r.flags.length ? "open" : "";
  const input = (field, type, value, attrs = "") =>
    `<input class="edit-input" type="${type}" data-action="set-field" data-field="${field}" data-id="${r.id}" value="${esc(value ?? "")}" ${attrs}>`;
  return `<details class="filing details" ${open}><summary>Correct details</summary>
      <label class="field"><span>Supplier</span>${input("vendor", "text", r.supplier)}</label>
      <label class="field"><span>Date</span>${input("purchased_on", "date", (r.date || "").slice(0, 10))}</label>
      <label class="field"><span>Total</span><span class="pair">${input("total", "number", r.total, 'step="0.01" min="0"')}
        ${input("currency", "text", r.currency, 'maxlength="3" placeholder="GBP" style="width:5em;text-transform:uppercase"')}</span></label>
    </details>`;
}

function filingHtml(r) {
  const fa = state.snap.freeagent;
  if (!fa?.connected) return "";
  const groups = {};
  for (const c of state.categories || []) (groups[c.group] ||= []).push(c);
  const options = Object.entries(groups).map(([g, list]) =>
    `<optgroup label="${esc(GROUP_LABEL[g] || g)}">${list.map((c) =>
      `<option value="${esc(c.url)}" ${c.url === r.category ? "selected" : ""}>${esc(c.description)} (${esc(c.nominal_code)})</option>`).join("")}</optgroup>`).join("");
  const personal = r.paid_by === "personal";
  const tx = r.match?.transaction;
  let blocker = "";
  if (r.total == null) blocker = "Enter the total first";
  else if (!r.category) blocker = "Choose a category";
  else if (!personal && !tx) blocker = "No bank payment matched yet";
  const filing = state.snap.queued.some((q) => q.startsWith("file:"));
  const label = fa.dry_run ? "Dry run" : personal ? "File as expense" : "File to FreeAgent";
  const last = r.filing;
  let lastHtml = "";
  if (last?.state === "dry_run") {
    lastHtml = `<details class="dry"><summary>Last dry run: what would be sent</summary><pre class="selectable">${esc(JSON.stringify(last.parts && last.parts.length > 1 ? last.parts : last.body, null, 2))}${last.attachment ? `\n\nattachment: ${esc(last.attachment.file_name)} (${esc(last.attachment.content_type)})` : ""}</pre></details>`;
  } else if (last?.state === "problem") {
    lastHtml = `<div class="note">${esc(last.message)}</div>`;
  } else if (last?.state === "filing" || last?.state === "explained") {
    lastHtml = `<div class="note">A filing was interrupted. Filing again picks it up without creating a second entry.</div>`;
  }
  return `<div class="filing">
      <label class="field"><span>Category</span>
        <select data-action="set-category" data-id="${r.id}"><option value="">Choose…</option>${options}</select></label>
      <label class="field"><span>Paid</span>
        <select data-action="set-paid-by" data-id="${r.id}">
          <option value="business" ${personal ? "" : "selected"}>From the business account</option>
          <option value="personal" ${personal ? "selected" : ""}>Personally: claim as an expense</option></select></label>
      ${fa.vat?.registered ? `<label class="field"><span>VAT</span><span class="pair">${vatMenuHtml(r)}</span></label>` : ""}
      <div class="file-row">
        <button class="btn small ${fa.dry_run ? "" : "primary"}" data-action="file" data-id="${r.id}" ${blocker || filing ? "disabled" : ""}
          title="${esc(blocker || (fa.dry_run ? "Builds the request and shows it; nothing is sent" : "Creates the entry in FreeAgent and attaches this receipt"))}">${label}</button>
        <span class="sub">${esc(blocker || (fa.dry_run ? "Dry run is on: nothing is sent to FreeAgent." : ""))}</span>
      </div>
      ${lastHtml}
    </div>`;
}

function filedHtml(r) {
  if (r.filing?.state !== "filed") return "";
  const what = r.filing.kind === "expense" ? "an expense" : "an explanation of the bank payment";
  return `<div class="match"><span class="pill ok">In FreeAgent</span> Filed as ${what}${r.category_name ? `, ${esc(r.category_name)}` : ""}.</div>`;
}

const MATCH_LABEL = {
  matched: ["ok", "Matched"],
  likely: ["unknown", "Likely"],
  waiting: ["unknown", "Waiting for the bank"],
  expense_but_found: ["bad", "Check"],
};
function matchHtml(r) {
  const m = r.match;
  if (!m || m.status === "not_applicable") return "";
  const [cls, label] = MATCH_LABEL[m.status] || ["unknown", m.status];
  const t = m.transaction;
  const line = t ? `${esc(shortDate(t.date))} · ${esc(t.description)} · ${esc(money(t.amount, r.currency || "GBP"))}` : esc(m.reason);
  const why = t && m.reason ? `<div class="sub">${esc(m.reason)}</div>` : "";
  return `<div class="match"><span class="pill ${cls}">${label}</span> <span class="selectable">${line}</span>${why}</div>`;
}

function placeDocument(oldFrame) {
  const slot = $("#content .doc-slot");
  const r = selected();
  if (!slot || !r || !r.has_pdf) { state.previewId = null; return; }
  // Reuse the existing iframe when it already shows this receipt, so a
  // background refresh never reloads (and flashes) the document.
  if (oldFrame && state.previewId === r.id) {
    slot.appendChild(oldFrame);
    return;
  }
  const frame = document.createElement(r.is_image ? "img" : "iframe");
  frame.className = r.is_image ? "doc photo" : "doc";
  frame.title = `${r.supplier} receipt`;
  if (r.is_image) frame.alt = `Photo of the ${r.supplier} receipt`;
  frame.src = `/api/receipts/${r.id}/pdf?t=${encodeURIComponent(TOKEN)}`;
  slot.appendChild(frame);
  state.previewId = r.id;
}

const NOTIFY_FREQUENCIES = [["instant", "As they happen"], ["hourly", "Hourly"], ["daily", "Daily at 9am"]];
const NOTIFY_CATEGORIES = [
  ["new", "New receipts", "From Gmail or phone photos"],
  ["filed", "Auto-linked receipts", "Linked automatically, or an email copy replaced by the supplier's PDF"],
  ["problems", "Problems", "Gmail sign-in expired, or the photo inbox is stuck"],
  ["updates", "Updates", "A new version of Receipt Bridge is ready to install"],
];

// What macOS itself allows, shown only when it gets in the way.
function notificationPermission(status) {
  if (status === "denied") {
    return `<div class="card-row"><div class="grow"><span class="pill warn">Blocked by macOS</span>
      <div class="sub">Allow Receipt Bridge in System Settings › Notifications.</div></div>
      <button class="btn small" data-action="notifications-settings">Open System Settings…</button></div>`;
  }
  if (status !== "allowed") {
    return `<div class="card-row"><div class="grow">Not allowed yet
      <div class="sub">Send a test, then click Allow.</div></div>
      <button class="btn small" data-action="notifications-test">Send a test</button></div>`;
  }
  return "";
}

function notificationPrefsCard(status, prefs) {
  if (!status || status === "unavailable" || !prefs) return "";
  const master = `<label class="switch"><input type="checkbox" data-action="notify-enabled" ${prefs.enabled ? "checked" : ""} aria-label="Send notifications"><span></span></label>`;
  if (!prefs.enabled) {
    return `<div class="card">
      <div class="card-head"><h3>Notifications</h3>${master}</div>
      <div class="card-note">Notifications are off.</div>
    </div>`;
  }
  const kinds = NOTIFY_CATEGORIES.map(([key, label, sub]) => `<label class="card-row choice">
      <input type="checkbox" data-action="notify-category" data-category="${key}" ${prefs.categories[key] ? "checked" : ""}>
      <div class="grow">${label}<div class="sub">${sub}</div></div></label>`).join("");
  // Problems never wait for a summary, so timing only matters for the other two.
  const batchable = prefs.categories.new || prefs.categories.filed;
  const held = prefs.waiting ? ` ${prefs.waiting} waiting.` : "";
  const timing = !batchable ? "" : `<div class="card-row"><div class="grow">When to send
      <div class="sub">${prefs.frequency === "instant" ? "One per update." : "Sent as one summary." + held}
        ${prefs.categories.problems || prefs.categories.updates ? " Problems and updates are sent immediately." : ""}</div></div>
      <div class="segmented" role="group" aria-label="When to send">
        ${NOTIFY_FREQUENCIES.map(([v, label]) => `<button class="seg ${prefs.frequency === v ? "on" : ""}" data-action="notify-frequency" data-frequency="${v}" aria-pressed="${prefs.frequency === v}">${label}</button>`).join("")}
      </div></div>`;
  const test = status === "allowed"
    ? `<div class="card-row"><div class="grow sub-only">Check notifications work.</div>
        <button class="btn small" data-action="notifications-test">Send a test</button></div>`
    : "";
  return `<div class="card">
    <div class="card-head"><h3>Notifications</h3>${master}</div>
    ${notificationPermission(status)}
    <div class="card-note lead">Notify me about</div>
    ${kinds}
    ${timing}
    ${test}
  </div>`;
}

function loginRow(login, installing) {
  const sub = installing ? "Installing Receipt Bridge in your Applications folder…"
    : login.available ? "Keeps automatic checks running."
    : "Keeps automatic checks running. Turning this on also puts the app in your Applications folder.";
  return `<div class="card-row"><div class="grow">Open at login<div class="sub">${sub}</div></div>
      <label class="switch"><input type="checkbox" data-action="open-at-login" ${login.enabled || installing ? "checked" : ""}
        ${installing ? "disabled" : ""} aria-label="Open at login"><span></span></label></div>`;
}

function updateButton(u, cls = "btn primary small") {
  return u.installing
    ? `<button class="${cls}" disabled>Updating…</button>`
    : `<button class="${cls}" data-action="update-install">${u.restart_needed ? "Restart to update" : "Update now"}</button>`;
}

function updateCard(u) {
  if (!u) return "";
  const latest = esc(u.latest.replace(/^v/i, ""));
  const status = u.installing ? `<span class="pill unknown">Updating to ${latest}…</span>`
    : u.checking ? `<span class="pill unknown">Checking…</span>`
    : u.restart_needed ? `<span class="pill warn">Version ${latest} is installed</span>`
    : u.available ? `<span class="pill warn">Version ${latest} is available</span>`
    : u.error ? `<span class="pill bad">Couldn't check</span>`
    : u.latest ? `<span class="pill ok">Up to date</span>`
    : "";
  const restart = u.restarts ? "restarts the app" : "you then quit and reopen it";
  const detail = u.checking || u.installing ? ""
    : u.restart_needed ? `Restart to start using it. ${u.restarts ? "Takes a few seconds." : ""}`
    : u.error ? esc(u.error)
    : u.available && u.git_checkout ? `Updates this copy with <code>git pull</code> (never over edits not committed yet), then ${restart}.`
    : u.available ? `Downloads it from GitHub, then ${restart}. Receipts, sign-ins and settings are kept.`
    : u.checked_at ? `Checked ${ago(u.checked_at)}. Checks <code>${esc(u.repo)}</code> on GitHub every day.`
    : `Checks <code>${esc(u.repo)}</code> on GitHub every day.`;
  return `<div class="card">
      <div class="card-head"><h3>Updates</h3></div>
      <div class="card-row"><div class="grow">Version ${esc(u.current)}${status ? ` · ${status}` : ""}
          <div class="sub">${detail}</div></div>
        ${u.can_install ? updateButton(u) : ""}
        ${u.available && !u.can_install ? `<button class="btn small" data-action="update-open">View on GitHub</button>` : ""}
        ${u.installing ? "" : `<button class="btn small" data-action="update-check" ${u.checking ? "disabled" : ""}>Check for updates</button>`}</div>
      ${u.available && !u.restart_needed && u.notes ? `<details class="log"><summary>What's new</summary><pre>${esc(u.notes)}</pre></details>` : ""}
    </div>`;
}

function archiveCard(days) {
  const on = days > 0;
  return `<div class="card">
      <div class="card-head"><h3>Archived receipts</h3></div>
      <div class="card-row"><div class="grow">Auto-delete archived receipts
          <div class="sub">Permanently deletes ignored and unreadable receipts, including their files.</div></div>
        <label class="switch"><input type="checkbox" data-action="archive-auto-delete" ${on ? "checked" : ""} aria-label="Auto-delete archived receipts"><span></span></label></div>
      ${on ? `<div class="card-row"><div class="grow">Delete after</div>
        <input type="number" class="days-input" min="1" max="3650" value="${days}" data-action="archive-delete-days" aria-label="Days before deleting"> days</div>` : ""}
    </div>`;
}

function inboxCard(p) {
  const status = !p.exists
    ? `<b>Folder not found.</b> Choose another, or create it.`
    : p.has_subfolders
      ? "<code>Bank</code>: paid by the business. <code>Expense</code>: paid personally. Anything else: you'll be asked."
      : `Add <code>Bank</code> and <code>Expense</code> subfolders so the phone Shortcut can sort receipts.
         <button class="btn quiet small" data-action="create-subfolders">Create them</button>`;
  return `<div class="card">
      <div class="card-head"><h3>Receipt inbox</h3></div>
      <div class="card-row"><div class="grow">Inbox folder
          <div class="sub selectable"><code>${esc(p.path)}</code>${p.is_default ? " (default)" : ""}</div>
          <div class="sub">${status}</div></div>
        <button class="btn small" data-action="choose-folder" data-which="inbox">Change…</button>
        ${p.is_default ? "" : `<button class="btn quiet small" data-action="reset-folder" data-which="inbox">Default</button>`}</div>
      <div class="card-row"><div class="grow">Move processed receipts to
          <div class="sub selectable"><code>${esc(p.archive)}</code>${p.archive_is_default ? " (default)" : ""}</div>
          <div class="sub">Originals are kept here. Changing this doesn't move existing files.</div></div>
        <button class="btn small" data-action="open-folder" data-which="archive">Open</button>
        <button class="btn small" data-action="choose-folder" data-which="archive">Change…</button>
        ${p.archive_is_default ? "" : `<button class="btn quiet small" data-action="reset-folder" data-which="archive">Default</button>`}</div>
      <div class="card-note">Any synced folder works: iCloud Drive, Dropbox, Google Drive or OneDrive.</div>
    </div>`;
}

/** Where the licence file goes: it carries the Google and FreeAgent app
 * keys. Dropping it anywhere in the window works too (match.js). */
function licenceDrop() {
  return `<div class="licence-drop">
      <div><b>Drop your <code>.rbkey</code> licence file here</b></div>
      <button class="btn small" data-action="licence-choose">Choose file…</button>
    </div>`;
}

async function installLicence(file) {
  const res = await fetch("/api/licence", { method: "POST",
    headers: { "x-receipt-bridge": TOKEN, "content-type": "application/octet-stream" }, body: file });
  const body = await res.json().catch(() => ({}));
  if (res.ok) toast(`<b>Licence installed.</b> ${esc(body.installed.join(" and "))} can now be connected.`);
  else toast(`<b>Licence not installed.</b> ${esc(body.detail || res.statusText)}`, 9000);
  refresh(true);
}

function freeagentCard(fa) {
  if (!fa) return "";
  const env = fa.environment === "sandbox" ? ` <span class="pill unknown">Sandbox</span>` : "";
  if (!fa.has_credentials) {
    return `<div class="card"><div class="card-head"><h3>FreeAgent</h3></div>
      ${licenceDrop()}</div>`;
  }
  const error = fa.error ? `<div class="card-note"><span class="pill bad">Problem</span> ${esc(fa.error)}</div>` : "";
  if (!fa.connected) {
    return `<div class="card"><div class="card-head"><h3>FreeAgent${env}</h3>
        <button class="btn small" data-action="fa-connect">Connect FreeAgent</button></div>
      ${error}<div class="card-note">Opens in your browser.</div></div>`;
  }
  const vat = fa.vat ? esc(fa.vat.company || "Your company") : "Loading company…";
  const accounts = fa.bank_accounts.map((a) => `
      <label class="card-row choice"><input type="checkbox" data-action="fa-account" value="${esc(a.url)}" ${a.chosen ? "checked" : ""}>
        ${bankBadge(a.name)}<div class="grow">${esc(a.name)}<div class="sub">${esc(a.currency)}${a.type === "CreditCardAccount" ? " · credit card" : ""}${a.is_personal ? " · personal" : ""}</div></div></label>`).join("");
  const synced = fa.last_sync ? `Last synced ${esc(shortDate(fa.last_sync))} · ${fa.transactions} transaction${fa.transactions === 1 ? "" : "s"}` : "Not synced yet";
  return `<div class="card">
      <div class="card-head"><h3>FreeAgent${env}</h3>
        <button class="btn small" data-action="fa-sync" ${state.snap.queued.includes("freeagent-sync") ? "disabled" : ""}>Refresh</button>
        <button class="btn small danger" data-action="fa-disconnect">Disconnect</button></div>
      <div class="card-row"><div class="grow">${freeagentStatus(fa)}<div class="sub">${vat}</div>
        ${fa.error ? `<div class="sub">${esc(fa.error)}${fa.last_sync ? ` Showing what was synced ${esc(shortDate(fa.last_sync))}.` : ""}</div>` : ""}</div></div>
      ${vatSchemeRow(fa.vat)}
      <div class="card-note">Match receipts against:</div>
      ${accounts || `<div class="card-note">No bank accounts yet.</div>`}
      <div class="card-row"><div class="grow">Dry run<div class="sub">${fa.dry_run ? "On: nothing is sent to FreeAgent" : "<b>Off: changes are saved to FreeAgent</b>"}${fa.environment === "sandbox" ? " (sandbox)" : ""}</div></div>
        <label class="switch"><input type="checkbox" data-action="fa-dry-run" ${fa.dry_run ? "checked" : ""} aria-label="Dry run"><span></span></label></div>
      <div class="card-note">${synced}.</div>
    </div>`;
}

/** One status for the connection: never "Connected" beside a problem. */
function freeagentStatus(fa) {
  if (!fa.error) return `<span class="pill ok">Connected</span>`;
  if (fa.problem === "offline") return `<span class="pill warn">Can't reach FreeAgent</span>`;
  if (fa.problem === "unavailable") return `<span class="pill warn">FreeAgent isn't responding</span>`;
  return `<span class="pill bad">Problem</span>`;
}

const VAT_SCHEME_LABEL = {
  "standard": "Standard", "cash accounting": "Cash accounting",
  "flat rate": "Flat Rate Scheme", "not registered": "Not VAT registered",
};

/** FreeAgent only reports the scheme registered with, so it can be
 * corrected here. Only Receipt Bridge uses the choice. */
function vatSchemeRow(vat) {
  if (!vat) return "";
  const from = VAT_SCHEME_LABEL[vat.freeagent_scheme] || vat.freeagent_scheme;
  const note = vat.chosen
    ? `Overrides FreeAgent (${esc(from)}). FreeAgent isn't changed.`
    : "From FreeAgent. Change it if you've switched scheme.";
  const warn = vat.scheme === "flat rate"
    ? `<div class="sub warn">Flat Rate isn't supported yet. VAT is filed as printed on each receipt.</div>` : "";
  return `<div class="card-row"><div class="grow">VAT scheme<div class="sub">${note}</div>${warn}</div>
      <select class="setting-select" data-action="fa-vat-scheme" aria-label="VAT scheme">
        <option value="" ${vat.chosen ? "" : "selected"}>As FreeAgent says (${esc(from)})</option>
        ${Object.entries(VAT_SCHEME_LABEL).map(([value, label]) =>
          `<option value="${value}" ${vat.chosen && vat.scheme === value ? "selected" : ""}>${label}</option>`).join("")}
      </select></div>`;
}

/** Before Google sign-in: how far back to look for receipts in this mailbox. */
const LOOKBACK = [[1, "1 month"], [3, "3 months"], [6, "6 months"], [12, "1 year"], [24, "2 years"], [0, "From a date…"]];

function gmailFrom() {
  const ask = state.gmailAsk || {};
  if (ask.months === 0) return ask.date || "";
  const d = new Date();
  d.setMonth(d.getMonth() - (ask.months || 3));
  return d.toISOString().slice(0, 10);
}

function gmailAskHtml() {
  const ask = state.gmailAsk;
  const today = new Date().toISOString().slice(0, 10);
  const from = gmailFrom();
  return `<div class="gm-ask">
      <div class="gm-q">Import receipts from how far back?</div>
      <div class="seg2 gm-range" role="group" aria-label="How far back">${LOOKBACK.map(([n, label]) =>
        `<button type="button" class="${ask.months === n ? "on" : ""}" data-action="connect-months" data-months="${n}" aria-pressed="${ask.months === n}">${label}</button>`).join("")}</div>
      ${ask.months === 0 ? `<label class="gm-date">From <input type="date" data-action="connect-date" max="${today}" value="${esc(ask.date)}"></label>` : ""}
      <div class="card-note">${from ? `Emails from ${esc(longDate(from))} onwards. Older receipts link only if FreeAgent has their payments.` : "Choose a date."}</div>
      <div class="gm-actions"><button class="btn primary small" data-action="connect" ${from ? "" : "disabled"}>Continue to Google</button>
        <button class="btn small" data-action="connect-cancel">Cancel</button></div>
    </div>`;
}

/** Gmail sign-in and the connected accounts: in Settings → Email, and the
 * whole of the Emails view until an account is working. */
function gmailCard(s) {
  const accountRows = s.accounts.map((a) => {
    const pill = a.state === "ok" ? `<span class="pill ok">Working</span>`
      : a.state === "unknown" ? `<span class="pill unknown">Checking…</span>`
      : `<span class="pill bad">${a.state === "expired" ? "Needs reconnecting" : "Problem"}</span>`;
    const broken = a.state !== "ok" && a.state !== "unknown";
    return `<div class="card-row">
        <div class="grow"><div class="selectable">${esc(a.email)}</div>
          <div class="sub">${pill} · ${a.receipts} receipt${a.receipts === 1 ? "" : "s"}${a.read_only ? "" : " · <b>has write access</b>"}</div>
          ${a.state === "expired" ? `<div class="sub">Expires weekly? Set your Google Cloud consent screen to <b>In production</b>.</div>` : ""}</div>
        ${broken ? `<button class="btn small" data-action="connect" ${s.connecting ? "disabled" : ""}>Reconnect</button>` : ""}
        <button class="btn small danger" data-action="disconnect" data-email="${esc(a.email)}">Sign out</button>
      </div>`;
  }).join("");
  return `<div class="card">
        <div class="card-head"><h3>Gmail</h3>
          ${s.connecting
            ? `<button class="btn small" data-action="connect-stop">Cancel sign-in</button>`
            : `<button class="btn small" data-action="connect-ask" ${!s.has_credentials || state.gmailAsk ? "disabled" : ""}>
                ${s.accounts.length ? "+ Add account" : "Connect Gmail"}</button>`}</div>
        ${s.connecting ? `<div class="card-note">Waiting for you to finish signing in to Google in your browser. Gives up after 5 minutes.</div>` : ""}
        ${state.gmailAsk && !s.connecting ? gmailAskHtml() : ""}
        ${s.has_credentials ? "" : licenceDrop()}
        ${accountRows || `<div class="card-note">No account connected.</div>`}
        <div class="card-note">Opens in your browser. Read-only.</div>
      </div>`;
}

function renderSettings() {
  const s = state.snap;

  const suppliers = (state.suppliers || []).map((w) => w.problem
    ? `<div class="card-row"><div class="grow"><span class="pill bad">Couldn't load</span><div class="sub">${esc(w.problem)}</div></div></div>`
    : `<div class="card-row ${w.enabled ? "" : "off"}"><div class="grow"><div>${esc(w.name)}${w.enabled ? "" : ` <span class="pill unknown">Off</span>`}</div>
        <div class="sub">${w.receipts} receipt${w.receipts === 1 ? "" : "s"} · ${w.gets_supplier_document ? "supplier PDF" : "email"}</div></div>
        <button class="btn small" data-action="edit-supplier" data-id="${esc(w.id)}">Edit</button>
      </div>`).join("");

  const theme = s.theme || "system";
  const themeButton = (value, label) =>
    `<button class="seg ${theme === value ? "on" : ""}" data-action="theme" data-theme="${value}" aria-pressed="${theme === value}">${label}</button>`;
  const log = (s.activity.log.length ? s.activity.log : s.last_log || []).join("\n");

  const sections = {
    about: state.settingsTab === "about" ? aboutHtml() : "",
    freeagent: freeagentCard(s.freeagent),

    email: `
      ${gmailCard(s)}

      <div class="card">
        <div class="card-head"><h3>Recurring receipts</h3>
          <button class="btn small" data-action="add-supplier" ${s.accounts.length ? "" : "disabled"}>+ Add recurring receipt</button></div>
        <div class="card-note">Suppliers who email a receipt every time. Their receipts are collected by themselves.</div>
        ${suppliers || `<div class="card-note">Loading…</div>`}
      </div>

      <div class="card">
        <div class="card-row"><div class="grow">Check Gmail automatically<div class="sub">Every 6 hours</div></div>
          <label class="switch"><input type="checkbox" data-action="auto-scan" ${s.auto_scan ? "checked" : ""} aria-label="Check Gmail automatically"><span></span></label></div>
      </div>`,

    general: `
      <div class="card">
        ${loginRow(s.open_at_login || {}, s.queued.includes("install-app"))}
        <div class="card-row"><div class="grow">Appearance</div>
          <div class="segmented" role="group" aria-label="Appearance">
            ${themeButton("system", "System")}${themeButton("light", "Light")}${themeButton("dark", "Dark")}</div></div>
      </div>

      ${notificationPrefsCard(s.notifications, s.notification_prefs)}

      ${inboxCard(s.photo_inbox)}

      <div class="card">
        <div class="card-row"><div class="grow">Setup guide
            <div class="sub">FreeAgent, where receipt photos go, Gmail, and adding the iPhone Shortcut.</div></div>
          <button class="btn small" data-action="setup-open">Open</button></div>
      </div>

      ${archiveCard(s.archive_delete_days)}

      ${updateCard(s.update)}

      <div class="card">
        <details class="log"><summary>Activity log</summary><pre>${esc(log || "Nothing yet.")}</pre></details>
      </div>`,
  };

  // a dot where something needs doing: FreeAgent signed out or failing,
  // a Gmail account to reconnect
  const fa = s.freeagent || {};
  const attention = {
    freeagent: fa.has_credentials && (!fa.connected || (!!fa.error && fa.problem === "error")),
    email: s.needs_attention.length > 0,
    general: !!s.update?.available,
  };
  const tab = SETTINGS_TABS.some(([key]) => key === state.settingsTab) ? state.settingsTab : "general";
  const [, title, intro] = SETTINGS_TABS.find(([key]) => key === tab);

  $("#content").innerHTML = `<div class="settings" data-tab="${tab}">
    <nav class="settings-nav" aria-label="Settings sections">
      <div class="settings-title">Settings</div>
      ${SETTINGS_TABS.map(([key, label, , group], i) => {
        const starts = group && group !== (SETTINGS_TABS[i - 1] || [])[3];
        const before = !starts ? "" : group === "foot" ? `<div class="nav-sep" role="separator"></div>` : `<div class="settings-group">${group}</div>`;
        return `${before}<button class="nav-item ${key === tab ? "active" : ""}" data-action="settings-tab" data-tab="${key}"
          ${key === tab ? 'aria-current="page"' : ""}><span>${label}</span>${attention[key] ? `<span class="dot"></span>` : ""}</button>`;
      }).join("")}
    </nav>
    <div class="settings-inner">
      <h1>${title}</h1>
      <p class="settings-intro">${intro}</p>
      ${sections[tab]}
    </div></div>`;
}

// [key, label, intro, group]: General first, then what Receipt Bridge connects
// to under "Connections", then About on its own at the foot.
const SETTINGS_TABS = [
  ["general", "General", "Startup, appearance, notifications, receipt inbox, archive and updates.", ""],
  ["freeagent", "FreeAgent", "Where receipts are matched and filed.", "Connections"],
  ["email", "Email", "Optional. Finds receipts from these suppliers in Gmail and adds them to Files.", "Connections"],
  ["about", "About", "Version, licence, terms, and the software Receipt Bridge is built on.", "foot"],
];

// ---- Settings → About ----------------------------------------------------

async function loadAbout() {
  if (state.aboutLoading) return;
  state.aboutLoading = true;
  try { state.about = await api("/api/about"); } catch (err) { state.about = { error: err.message }; }
  state.aboutLoading = false;
  render();
}

/** The EULA (docs/EULA.md): a title line, "## " headings, "- " lists, paragraphs. */
function eulaHtml(text) {
  const blocks = String(text || "").trim().split(/\n\s*\n/);
  return blocks.map((b, i) => {
    if (i === 0) return `<p class="eula-title">${esc(b)}</p>`;
    if (b.startsWith("## ")) return `<h4>${esc(b.slice(3))}</h4>`;
    const lines = b.split("\n");
    if (lines.every((l) => l.startsWith("- "))) return `<ul>${lines.map((l) => `<li>${esc(l.slice(2))}</li>`).join("")}</ul>`;
    if (lines[0].startsWith("## ")) return `<h4>${esc(lines[0].slice(3))}</h4><p>${esc(lines.slice(1).join(" "))}</p>`;
    return `<p>${esc(b.replace(/\n/g, " "))}</p>`;
  }).join("");
}

function aboutHtml() {
  const a = state.about;
  if (!a) { loadAbout(); return `<div class="card"><div class="card-note">Loading…</div></div>`; }
  if (a.error) return `<div class="card"><div class="card-note">${esc(a.error)}</div></div>`;
  const u = state.snap.update;
  const status = !u ? "" : u.restart_needed ? `Version ${esc(u.latest.replace(/^v/i, ""))} is installed. Restart to use it.`
    : u.available ? `Version ${esc(u.latest.replace(/^v/i, ""))} is available.`
    : u.checking ? "Checking for updates…" : u.latest ? "Up to date." : "";
  const keys = a.licence.google && a.licence.freeagent ? "Licensed: the Google and FreeAgent keys are installed."
    : a.licence.google || a.licence.freeagent ? `Partly licensed: only the ${a.licence.google ? "Google" : "FreeAgent"} key is installed.`
    : "No licence file installed yet.";
  const oss = a.open_source.map((p) => {
    const text = (state.licences || {})[p.name];
    const body = !p.has_text ? (p.url ? `<p class="sub">Licence text: <a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a></p>` : "")
      : text === undefined ? `<p class="sub">Loading…</p>` : `<pre class="licence-text">${esc(text)}</pre>`;
    return `<details class="oss" data-licence="${esc(p.name)}" ${p.has_text ? 'data-has-text="1"' : ""}>
        <summary><span class="oss-name">${esc(p.name)}</span><span class="sub">${esc(p.version)}</span><span class="grow"></span><span class="oss-lic">${esc(p.licence)}</span></summary>
        ${body}</details>`;
  }).join("");
  return `
    <div class="card about-head">
      <div class="card-row"><span class="about-mark" aria-hidden="true">🧾</span>
        <div class="grow"><div class="about-name">${esc(a.name)}</div>
          <div class="sub">Version ${esc(a.version)} · ${esc(a.copyright)}</div>
          ${status ? `<div class="sub">${status}</div>` : ""}</div>
        ${u?.available && u.can_install ? updateButton(u) : `<button class="btn small" data-action="update-check" ${u?.checking ? "disabled" : ""}>Check for updates</button>`}</div>
    </div>

    <div class="card">
      <div class="card-head"><h3>Licence</h3></div>
      <div class="card-row"><div class="grow">This copy<div class="sub">${esc(keys)} The keys identify Receipt Bridge; you sign in to your own Gmail and FreeAgent.</div></div></div>
      ${a.licence.google && a.licence.freeagent ? "" : licenceDrop()}
    </div>

    <div class="card">
      <div class="card-head"><h3>Your data</h3></div>
      <div class="card-note">Your receipts, the emails kept as receipts and the database stay on this Mac. Receipt Bridge only
        talks to Google (to read email, read-only), FreeAgent (to read your bank feed and file what you approve), suppliers' websites
        (to download a receipt an email links to) and GitHub (for updates).</div>
      <div class="card-row"><div class="grow">Kept in<div class="sub selectable"><code>${esc(a.data_dir)}</code></div></div></div>
    </div>

    <div class="card">
      <details class="about-doc"><summary>End user licence agreement</summary><div class="eula">${eulaHtml(a.eula)}</div></details>
    </div>

    <div class="card">
      <div class="card-head"><h3>Open-source software</h3></div>
      <div class="card-note">Receipt Bridge is built on these, each used under its own licence. Click one to read it.</div>
      <div class="oss-list">${oss}</div>
    </div>`;
}

// A package's licence text is fetched the first time it's opened.
document.addEventListener("toggle", (e) => {
  const d = e.target;
  if (!d.matches?.("details.oss[data-has-text]") || !d.open) return;
  const name = d.dataset.licence;
  state.licences ||= {};
  if (name in state.licences) return;
  state.licences[name] = undefined;
  api(`/api/about/licence?name=${encodeURIComponent(name)}`)
    .then((r) => { state.licences[name] = r.text; })
    .catch(() => { state.licences[name] = "The licence text couldn't be read."; })
    .then(render);
}, true);

window.rbShowAbout = () => { state.settingsTab = "about"; if (state.view === "settings") render(); else setView("settings"); };

// ---- first-run setup guide ---------------------------------------------
//
// Shown over everything until finished: FreeAgent, where receipt photos are
// saved, Gmail (optional), the iPhone Shortcut. Every step reuses the same
// endpoints as Settings, so nothing here can drift from it. "Set up later"
// hides it until the next launch; Finish (or Settings → General) settles it.

const SETUP_STEPS = [["licence", "Licence"], ["freeagent", "FreeAgent"], ["folder", "Receipt folder"], ["email", "Email"], ["iphone", "iPhone"]];
/** A step's number as people see it: "step 3". */
function stepNumber(key) { return SETUP_STEPS.findIndex(([k]) => k === key) + 1; }
const setup = { open: false, dismissed: false, step: 0, faWaiting: false, busy: false };

function openSetup(step = 0) {
  Object.assign(setup, { open: true, step, faWaiting: false, busy: false });
  renderSetup();
}

function closeSetup(done) {
  setup.open = false;
  setup.dismissed = true;
  $("#setup").innerHTML = "";
  if (done) act(() => api("/api/setup/done", { method: "POST", body: { done: true } }));
}

function setupReady(key) {
  const s = state.snap;
  if (key === "licence") return !!s.has_credentials && !!s.freeagent?.has_credentials;
  if (key === "freeagent") return !!s.freeagent?.connected;
  if (key === "folder") return s.photo_inbox.exists && s.photo_inbox.has_subfolders;
  if (key === "email") return s.accounts.length > 0;
  return false;
}

/** Step 1: the licence file. Its keys let Receipt Bridge connect to
 * FreeAgent and Gmail, so it comes before either. Dropping it anywhere in
 * the window works too (match.js). */
function setupLicenceHtml(s) {
  const google = !!s.has_credentials, freeagent = !!s.freeagent?.has_credentials;
  const body = google && freeagent
    ? `<div class="setup-done"><span class="pill ok">Installed</span></div>`
    : `${licenceDrop()}${google || freeagent ? `<div class="setup-note warn">This file only has the ${google ? "Google" : "FreeAgent"} key. Ask for one with both.</div>` : ""}`;
  return `<h3>Add your licence file</h3>${body}`;
}

/** Instead of the licence drop again: back to step 1. */
function needsLicence() {
  return `<div class="setup-note warn">Add your licence file first.
    <button class="btn small" data-action="setup-step" data-step="${stepNumber("licence") - 1}">Go to step ${stepNumber("licence")}</button></div>`;
}

function setupFreeagentHtml(fa) {
  let body;
  if (!fa.has_credentials) {
    body = needsLicence();
  } else if (!fa.connected) {
    body = `<div class="setup-actions"><button class="btn primary" data-action="setup-fa-connect">Connect FreeAgent</button></div>
      ${setup.faWaiting ? `<div class="setup-note"><span class="spinner"></span>Approve it in your browser, then come back.</div>` : ""}
      ${fa.error ? `<div class="setup-note bad">${esc(fa.error)}</div>` : ""}`;
  } else {
    const accounts = fa.bank_accounts.map((a) => `
      <label class="card-row choice"><input type="checkbox" data-action="fa-account" value="${esc(a.url)}" ${a.chosen ? "checked" : ""}>
        ${bankBadge(a.name)}<div class="grow">${esc(a.name)}<div class="sub">${esc(a.currency)}${a.type === "CreditCardAccount" ? " · credit card" : ""}</div></div></label>`).join("");
    const none = fa.bank_accounts.length && !fa.bank_accounts.some((a) => a.chosen);
    body = `<div class="setup-done"><span class="pill ok">Connected</span> ${esc(fa.vat?.company || "Your company")}
        ${fa.environment === "sandbox" ? ` <span class="pill unknown">Sandbox</span>` : ""}</div>
      <div class="setup-q">Which account does the business pay from?</div>
      <div class="card">${accounts || `<div class="card-note"><span class="spinner"></span>Loading your bank accounts…</div>`}</div>
      ${none ? `<div class="setup-note warn">Tick at least one.</div>` : ""}
      <div class="setup-note">Dry run is on: nothing is sent to FreeAgent until you turn it off in Settings → FreeAgent.</div>`;
  }
  return `<h3>Connect FreeAgent</h3>
    <p>Receipts are matched to your bank payments and filed there.</p>${body}`;
}

function setupFolderHtml(s) {
  const p = s.photo_inbox, su = s.setup;
  const ready = setupReady("folder");
  const where = su.icloud_inbox ? `iCloud Drive › ${su.icloud_inbox.split("/").join(" › ")}` : p.path;
  return `<h3>Where should receipt photos go?</h3>
    <p>Your iPhone saves photos here. <b>Bank</b> and <b>Expense</b> folders are made inside.</p>
    <div class="setup-options">
      <button class="setup-option ${ready && su.icloud_inbox ? "on" : ""}" data-action="setup-inbox-icloud" ${su.icloud && !setup.busy ? "" : "disabled"}>
        <b>iCloud Drive</b> <span class="tag">Recommended</span>
        <span class="sub">${su.icloud ? "Works with the iPhone Shortcut as it is." : "iCloud Drive is off on this Mac."}</span>
      </button>
      <button class="setup-option ${ready && !su.icloud_inbox ? "on" : ""}" data-action="setup-inbox-choose" ${setup.busy ? "disabled" : ""}>
        <b>Choose a folder…</b>
        <span class="sub">Any folder your phone can save to. You'll point the Shortcut at it yourself.</span>
      </button>
    </div>
    ${ready ? `<div class="setup-done"><span class="pill ok">Ready</span> <span class="selectable">${esc(where)}</span>
        <button class="btn quiet small" data-action="open-folder" data-which="inbox">Show in Finder</button></div>`
      : p.exists ? `<div class="setup-note">Choose above to add the Bank and Expense folders.</div>` : ""}
    ${ready && su.icloud_inbox ? `<div class="setup-note">In Finder, right-click the folder and choose <b>Keep Downloaded</b>.</div>` : ""}`;
}

function setupEmailHtml(s) {
  let body;
  if (s.accounts.length) {
    body = `<div class="card">${s.accounts.map((a) => `<div class="card-row"><span class="pill ok"></span><div class="grow selectable">${esc(a.email)}</div></div>`).join("")}</div>
      <div class="setup-actions">${s.connecting ? "" : `<button class="btn small" data-action="connect-ask" ${state.gmailAsk ? "disabled" : ""}>+ Add another account</button>`}</div>`;
  } else if (!s.has_credentials) {
    body = needsLicence();
  } else if (!s.connecting && !state.gmailAsk) {
    body = `<div class="setup-actions"><button class="btn primary" data-action="connect-ask">Connect Gmail</button></div>`;
  } else body = "";
  if (s.connecting) body += `<div class="setup-note"><span class="spinner"></span>Finish signing in to Google in your browser.
      <button class="btn quiet small" data-action="connect-stop">Cancel</button></div>`;
  else if (state.gmailAsk) body += gmailAskHtml();
  return `<h3>Find receipts in Gmail <span class="optional">Optional</span></h3>
    <p>Read-only: Receipt Bridge can't send, change or delete email.</p>${body}`;
}

function setupIphoneHtml(s) {
  const su = s.setup;
  // The shared Shortcut saves to one fixed folder in iCloud Drive.
  const fixed = `iCloud Drive › ${su.shortcut_saves_to}`;
  const step = stepNumber("folder");
  const folder = su.icloud_inbox === su.shortcut_saves_to
    ? setupReady("folder") ? "" : `It saves to <b>${esc(fixed)}</b>. Set that up in step ${step} first.`
    : `It saves to <b>${esc(fixed)}</b>, but your folder is
       <span class="selectable"><code>${esc(su.icloud_inbox ? "iCloud Drive › " + su.icloud_inbox.split("/").join(" › ") : s.photo_inbox.path)}</code></span>.
       Change both <b>Save File</b> steps in the Shortcut, or choose iCloud Drive in step ${step}.`;
  const install = su.shortcut_url
    ? `<div class="setup-qr">
        <img src="/api/setup/shortcut-qr?t=${encodeURIComponent(TOKEN)}" alt="QR code for the Receipt Shortcut" width="164" height="164">
        <div><div class="setup-q">Scan with your iPhone's camera</div>
          <p>Then tap <b>Add Shortcut</b>.</p>
          <button class="btn quiet small" data-action="setup-open-shortcut">Open the link on this Mac instead</button></div>
      </div>`
    : `<div class="setup-note">Build it on your iPhone: the steps are in <b>SETUP-FOR-YOU.md</b>, section 2.</div>`;
  return `<h3>Add the iPhone Shortcut</h3>
    <p>Photograph a receipt and it's here a minute later.</p>
    ${install}
    ${folder ? `<div class="setup-note">${folder}</div>` : ""}
    <div class="setup-note">Tip: add it to the Home Screen or the Action button. Allow Location for Camera so photos taken abroad show their currency.</div>`;
}

function renderSetup() {
  const box = $("#setup");
  if (!setup.open || !state.snap) { if (box.innerHTML) box.innerHTML = ""; return; }
  const s = state.snap;
  const [key] = SETUP_STEPS[setup.step];
  const body = { licence: () => setupLicenceHtml(s), freeagent: () => setupFreeagentHtml(s.freeagent || {}), folder: () => setupFolderHtml(s),
                 email: () => setupEmailHtml(s), iphone: () => setupIphoneHtml(s) }[key]();
  const last = setup.step === SETUP_STEPS.length - 1;
  // FreeAgent and the folder are needed to file anything; skipping is
  // allowed, but the button says so.
  // Without the licence nothing after it can work: no skipping, just "Set up later".
  const blocked = key === "licence" && !setupReady(key);
  const next = last ? "Finish" : setupReady(key) || blocked ? "Continue" : key === "email" ? "Skip" : "Skip for now";
  const steps = SETUP_STEPS.map(([k, label], i) => `<li class="${i === setup.step ? "on" : setupReady(k) ? "done" : ""}">
      <button data-action="setup-step" data-step="${i}" ${i === setup.step ? 'aria-current="step"' : ""}>${label}</button></li>`).join("");
  box.innerHTML = `<div class="setup-backdrop"></div>
    <div class="setup" role="dialog" aria-modal="true" aria-labelledby="setup-title">
      <div class="setup-head"><span class="brand-mark" aria-hidden="true">🧾</span>
        <h2 id="setup-title">Set up Receipt Bridge</h2>
        <ol class="setup-steps">${steps}</ol></div>
      <div class="setup-body">${body}</div>
      <div class="setup-foot">
        <button class="btn quiet" data-action="setup-later">Set up later</button><span class="spacer"></span>
        ${setup.step ? `<button class="btn" data-action="setup-back">Back</button>` : ""}
        <button class="btn ${setupReady(key) || last || blocked ? "primary" : ""}" data-action="setup-next" ${blocked ? "disabled" : ""}>${next}</button>
      </div>
    </div>`;
}

// While the guide is open, the views underneath mustn't act on keys (⏎ files a receipt).
document.addEventListener("keydown", (e) => {
  if (!setup.open) return;
  e.stopImmediatePropagation();
}, true);

// ---- actions -----------------------------------------------------------

async function act(fn) {
  try { await fn(); } catch (err) { toast(`<b>Something went wrong.</b> ${esc(err.message)}`); }
  refresh(true);
}

function move(delta) {
  const ids = navigableIds();
  if (!ids.length) return;
  const i = Math.max(0, ids.indexOf(state.selectedId));
  state.selectedId = ids[Math.min(ids.length - 1, Math.max(0, i + delta))];
  render();
  document.querySelector(`.row[data-id="${state.selectedId}"]`)?.scrollIntoView({ block: "nearest" });
}

function setView(view) {
  if (view === state.view) return;
  if (state.view === "emails") closeEmailDialog();
  state.view = view;
  state.receiptsKey = "";
  state.selectedId = null;
  state.previewId = null;
  state.query = "";
  refresh(true);
}

document.addEventListener("click", (e) => {
  const nav = e.target.closest(".nav-item[data-view]");
  if (nav) return setView(nav.dataset.view);

  const target = e.target.closest("[data-action]");
  const action = target?.dataset.action;
  const id = target?.dataset.id ? Number(target.dataset.id) : null;

  switch (action) {
    case "setup-open":
      return openSetup();
    case "setup-later":
      return closeSetup(false);
    case "setup-back":
      setup.step = Math.max(0, setup.step - 1);
      return renderSetup();
    case "setup-step":
      setup.step = Number(target.dataset.step);
      return renderSetup();
    case "setup-next":
      if (setup.step === SETUP_STEPS.length - 1) return closeSetup(true);
      setup.step += 1;
      state.gmailAsk = null;
      return renderSetup();
    case "setup-fa-connect":
      setup.faWaiting = true;
      return act(() => api("/api/freeagent/connect", { method: "POST" }));
    case "setup-inbox-icloud":
    case "setup-inbox-choose":
      return act(async () => {
        let location = "icloud";
        if (action === "setup-inbox-choose") {
          ({ path: location } = await api("/api/settings/choose-folder", { method: "POST", body: { which: "setup" } }));
          if (!location) return;                         // cancelled
        }
        setup.busy = true;
        renderSetup();
        try { await api("/api/setup/inbox", { method: "POST", body: { location } }); }
        finally { setup.busy = false; }
      });
    case "setup-open-shortcut":
      return act(() => api("/api/setup/open-shortcut", { method: "POST" }));
    case "include":
      state.excluded.has(id) ? state.excluded.delete(id) : state.excluded.add(id);
      return render();
    case "toggle-all":
      if (included().length === state.receipts.length) state.receipts.forEach((r) => state.excluded.add(r.id));
      else state.excluded.clear();
      return render();
    case "export":
      return act(async () => {
        const ids = included().map((r) => r.id);
        const result = await api("/api/export", { method: "POST", body: { ids } });
        await api("/api/reveal", { method: "POST", body: { name: result.name } });
        toast(`Exported ${result.count} to <b>${esc(result.name)}</b>. Drag them into FreeAgent.`
          + (result.missing.length ? ` ${result.missing.length} had no PDF.` : ""), 9000);
      });
    case "ignore":
      return act(() => api("/api/receipts/status", { method: "POST", body: { ids: [id], status: "ignored" } }));
    case "restore":
      return act(() => api("/api/receipts/status", { method: "POST", body: { ids: [id], status: "pending" } }));
    case "retry":
      return act(() => api(`/api/receipts/${id}/retry`, { method: "POST" }));
    case "reveal":
      return act(() => api("/api/reveal", { method: "POST", body: { name: target.dataset.folder || "" } }));
    case "settings-tab":
      state.settingsTab = target.dataset.tab;
      return render();
    case "connect-ask":
      state.gmailAsk = { months: 3, date: "" };
      return render();
    case "connect-months":
      state.gmailAsk.months = Number(target.dataset.months);
      return render();
    case "connect-cancel":
      state.gmailAsk = null;
      return render();
    case "connect-stop":
      return act(() => api("/api/accounts/connect/cancel", { method: "POST" }));
    case "connect": {
      const from = gmailFrom();
      if (!from) return toast("Choose a start date.");
      state.gmailAsk = null;
      return act(async () => {
        await api("/api/accounts/connect", { method: "POST", body: { scan_from: from } });
        toast("Finish signing in in your browser.", 9000);
      });
    }
    case "theme":
      state.snap.theme = target.dataset.theme;
      render();
      return act(() => api("/api/settings", { method: "POST", body: { theme: target.dataset.theme } }));
    case "add-supplier":
      return openSupplierWizard();
    case "notify-frequency":
      state.snap.notification_prefs.frequency = target.dataset.frequency;
      render();
      return act(() => api("/api/settings", { method: "POST", body: { notify_frequency: target.dataset.frequency } }));
    case "notifications-test":
      return act(() => api("/api/notifications/test", { method: "POST" }));
    case "licence-choose":
      return act(async () => {
        const { installed } = await api("/api/licence/choose", { method: "POST" });
        if (installed) toast(`<b>Licence installed.</b> ${esc(installed.join(" and "))} can now be connected.`);
      });
    case "update-install":
      return act(() => api("/api/updates/install", { method: "POST" }));
    case "update-check":
      return act(() => api("/api/updates/check", { method: "POST" }));
    case "update-open":
      return act(() => api("/api/updates/open", { method: "POST" }));
    case "notifications-settings":
      return act(() => api("/api/notifications/open-settings", { method: "POST" }));
    case "toggle-group":
      return toggleGroup(target.dataset.folder, e.altKey);
    case "edit-supplier":
      return openSupplierEditor(target.dataset.id);
    case "fa-connect":
      return act(async () => {
        await api("/api/freeagent/connect", { method: "POST" });
        toast("Finish signing in to FreeAgent in your browser.", 9000);
      });
    case "file-all": {
      const ids = readyToFile().map((r) => r.id);
      const dry = state.snap.freeagent?.dry_run;
      if (!dry && !confirm(`File ${ids.length} receipt${ids.length === 1 ? "" : "s"} into FreeAgent?`)) return;
      return act(() => api("/api/receipts/file", { method: "POST", body: { ids } }));
    }
    case "file":
      return act(() => api("/api/receipts/file", { method: "POST", body: { ids: [Number(target.dataset.id)] } }));
    case "unfile":
      if (!confirm("Delete this entry from FreeAgent and put the receipt back in To file?")) return;
      return act(() => api(`/api/receipts/${target.dataset.id}/unfile`, { method: "POST" }));
    case "choose-folder":
      return act(async () => {
        const which = target.dataset.which;
        const { path } = await api("/api/settings/choose-folder", { method: "POST", body: { which } });
        if (path) await api("/api/settings/folders", { method: "POST", body: { [which]: path } });
      });
    case "reset-folder":
      return act(() => api("/api/settings/folders/reset", { method: "POST", body: { which: target.dataset.which } }));
    case "create-subfolders":
      return act(() => api("/api/settings/folders/create-subfolders", { method: "POST" }));
    case "open-folder":
      return act(() => api("/api/settings/open-folder", { method: "POST", body: { which: target.dataset.which } }));
    case "fa-sync":
      return act(() => api("/api/freeagent/sync", { method: "POST" }));
    case "fa-disconnect":
      if (!confirm("Disconnect FreeAgent? Nothing is deleted.")) return;
      return act(() => api("/api/freeagent/disconnect", { method: "POST" }));
    case "disconnect":
      if (!confirm(`Sign out ${target.dataset.email}? Receipts are kept.`)) return;
      return act(() => api("/api/accounts/disconnect", { method: "POST", body: { email: target.dataset.email } }));
  }

  const row = e.target.closest(".row");
  if (row && !e.target.matches("input")) {
    state.selectedId = Number(row.dataset.id);
    render();
  }
});

document.addEventListener("input", (e) => {
  if (e.target.dataset.action !== "search") return;
  state.query = e.target.value;
  const at = e.target.selectionStart;
  render();
  const box = document.querySelector('input[data-action="search"]');
  if (box) { box.focus(); box.setSelectionRange(at, at); }
});

/** FreeAgent's VAT menu for a receipt: Auto (as printed), Amount…, 20%, 5%,
 *  0%, Exempt, Out of Scope; and reverse charge, kept for the supplier. */
const VAT_MENU = [["auto", "Auto"], ["amount", "Amount…"], ["20.0", "20%"], ["5.0", "5%"], ["0.0", "0%"],
  ["EXEMPT", "Exempt"], ["OUT_OF_SCOPE", "Out of Scope"]];

function vatMenuHtml(r) {
  const reverse = r.vat_treatment === "reverse_charge";
  const choice = reverse ? "reverse_charge" : (r.vat_choice || "auto");
  const options = VAT_MENU.map(([v, l]) => `<option value="${v}" ${choice === v ? "selected" : ""}>${l}</option>`).join("")
    + `<option disabled>──────</option><option value="reverse_charge" ${reverse ? "selected" : ""}
        title="Remembered for ${esc(r.supplier)}">Reverse charge (${esc(r.supplier || "supplier")})</option>`;
  const amount = choice === "amount" ? `<label class="rb-factor">£<input type="text" inputmode="decimal" data-action="set-vat-amount"
      data-id="${r.id}" value="${r.vat_amount != null ? Number(r.vat_amount).toFixed(2) : ""}" placeholder="0.00" aria-label="VAT amount"></label>` : "";
  return `<select data-action="set-vat-choice" data-id="${r.id}" aria-label="VAT for ${esc(r.supplier)}">${options}</select>${amount}`;
}

function saveVatChoice(el) {
  const r = (state.receipts || []).find((x) => x.id === Number(el.dataset.id)) || findReceiptAny(Number(el.dataset.id));
  const v = el.value;
  const body = v === "reverse_charge" ? { vat_treatment: "reverse_charge" }
    : { vat_choice: v, ...(r?.vat_treatment === "reverse_charge" ? { vat_treatment: "printed" } : {}) };
  if (v === "amount" && r?.vat_amount == null && r?.vat) body.vat_amount = r.vat;   // start from what was read
  act(() => api(`/api/receipts/${el.dataset.id}/fields`, { method: "POST", body }));
}

function findReceiptAny(id) { return typeof findReceipt === "function" ? findReceipt(id) : null; }

document.addEventListener("change", (e) => {
  const field = { "set-category": "category", "set-paid-by": "paid_by", "set-vat-treatment": "vat_treatment" }[e.target.dataset.action]
    || (e.target.dataset.action === "set-field" ? e.target.dataset.field : null);
  if (e.target.dataset.action === "set-vat-choice") { saveVatChoice(e.target); e.target.blur(); return; }
  if (e.target.dataset.action === "set-vat-amount") {
    const raw = String(e.target.value).replace(/[£\s]/g, "").replace(",", ".");
    const v = raw === "" ? null : Number(raw);
    if (v !== null && !(Number.isFinite(v) && v >= 0)) { toast("<b>Type the VAT as an amount,</b> like 15.00"); return; }
    act(() => api(`/api/receipts/${e.target.dataset.id}/fields`, { method: "POST", body: { vat_choice: "amount", vat_amount: v } }));
    return;
  }
  if (field) {
    let value = e.target.value.trim() || null;
    if (field === "total" && value !== null) {
      // "4.59", "4,59", "£4.59": just the number
      value = Number(value.replace(/[£$€\s]/g, "").replace(/,(\d{1,2})$/, ".$1").replace(/,/g, ""));
      if (!Number.isFinite(value)) { toast("<b>That total isn't a number.</b> Try 4.59"); return; }
    }
    act(() => api(`/api/receipts/${e.target.dataset.id}/fields`, { method: "POST", body: { [field]: value } }));
    e.target.blur();
    return;
  }
  if (e.target.dataset.action === "connect-date") {
    state.gmailAsk.date = e.target.value;
    render();
    return;
  }
  if (e.target.dataset.action === "fa-dry-run") {
    act(() => api("/api/freeagent/dry-run", { method: "POST", body: { on: e.target.checked } }));
    return;
  }
  if (e.target.dataset.action === "fa-vat-scheme") {
    act(() => api("/api/freeagent/vat-scheme", { method: "POST", body: { scheme: e.target.value } }));
    return;
  }
  if (e.target.dataset.action === "fa-account") {
    const urls = [...document.querySelectorAll('input[data-action="fa-account"]:checked')].map((el) => el.value);
    act(() => api("/api/freeagent/accounts", { method: "POST", body: { urls } }));
  }
  if (e.target.dataset.action === "notify-enabled") {
    act(() => api("/api/settings", { method: "POST", body: { notify_enabled: e.target.checked } }));
    return;
  }
  if (e.target.dataset.action === "notify-category") {
    act(() => api("/api/settings", { method: "POST", body: { notify_categories: { [e.target.dataset.category]: e.target.checked } } }));
    return;
  }
  if (e.target.dataset.action === "open-at-login") {
    act(() => api("/api/settings", { method: "POST", body: { open_at_login: e.target.checked } }));
    return;
  }
  if (e.target.dataset.action === "archive-auto-delete") {
    act(() => api("/api/settings", { method: "POST", body: { archive_delete_days: e.target.checked ? 30 : 0 } }));
    return;
  }
  if (e.target.dataset.action === "archive-delete-days") {
    const days = Math.round(Number(e.target.value));
    if (!(days >= 1 && days <= 3650)) { toast("Choose between 1 and 3650 days."); return render(); }
    act(() => api("/api/settings", { method: "POST", body: { archive_delete_days: days } }));
    return;
  }
  if (e.target.dataset.action === "auto-scan") {
    act(() => api("/api/settings", { method: "POST", body: { auto_scan: e.target.checked } }));
  }
});

$("#scan-btn").addEventListener("click", () => act(() => api("/api/check-now", { method: "POST" })));

document.addEventListener("keydown", (e) => {
  if (wiz.open || ed.open || state.view === "settings" || state.view === "pending" || state.view === "statement" || state.view === "expenses" || state.view === "archived" || state.view === "emails" || e.metaKey || e.ctrlKey || e.target.matches("input:not([type=checkbox]), select, textarea")) return;
  const r = selected();
  if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); move(1); }
  else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); move(-1); }
  else if (e.key === " " && r && state.view === "pending") {
    e.preventDefault();
    state.excluded.has(r.id) ? state.excluded.delete(r.id) : state.excluded.add(r.id);
    render();
  } else if ((e.key === "Backspace" || e.key === "Delete") && r && state.view === "pending") {
    e.preventDefault();
    move(1);
    act(() => api("/api/receipts/status", { method: "POST", body: { ids: [r.id], status: "ignored" } }));
  }
});


// ---- add supplier ------------------------------------------------------
//
// Pick one example receipt; the server proposes the rule from it. The user
// only confirms: who it's from, which figure is the total, which is the
// reference. A live preview shows the file the rule would produce.

const wiz = { open: false, step: "search", q: "", results: [], busy: false, error: "",
              pick: null, analysis: null, choices: {}, preview: null, previewTimer: null };

function openSupplierWizard() {
  Object.assign(wiz, { open: true, step: "search", q: "", results: [], busy: false, error: "",
                       pick: null, analysis: null, choices: {}, preview: null });
  renderWizard();
  setTimeout(() => $("#wiz-q")?.focus(), 0);
}

/** "Turn into recurring receipt" in Emails: the same steps, starting from
 * this email as the example (no search). Back goes to a search for its sender. */
function openSupplierWizardFor(email) {
  Object.assign(wiz, { open: true, step: "search", q: email.from_address || "", busy: false, error: "",
                       results: [{ id: email.id, account: email.account, subject: email.subject,
                                   sender: email.from_name, date: email.date }],
                       pick: null, analysis: null, choices: {}, preview: null });
  wizardPick(0);
}

function closeWizard() {
  wiz.open = false;
  $("#modal").innerHTML = "";
}

async function wizardSearch() {
  wiz.q = $("#wiz-q").value.trim();
  if (wiz.q.length < 2) return;
  Object.assign(wiz, { busy: true, error: "", results: [] });
  renderWizard();
  try { wiz.results = await api("/api/suppliers/search", { method: "POST", body: { q: wiz.q } }); }
  catch (err) { wiz.error = err.message; }
  wiz.busy = false;
  renderWizard();
}

async function wizardPick(index) {
  wiz.pick = wiz.results[index];
  Object.assign(wiz, { step: "confirm", busy: true, error: "", analysis: null, preview: null });
  renderWizard();
  try {
    const a = await api("/api/suppliers/analyse", { method: "POST",
      body: { message_id: wiz.pick.id, account: wiz.pick.account } });
    wiz.analysis = a;
    const amount = a.amounts[0], ref = a.references[0];
    wiz.choices = {
      name: a.name, domain: a.domain, subject: a.subject_hint, mentions: "",
      amount: amount ? 0 : -1, reference: ref ? 0 : -1, use_attachment: a.has_pdf,
      vat: a.vat_amounts.length ? 0 : VAT_NONE, paid_with: "business",
    };
  } catch (err) { wiz.error = err.message; }
  wiz.busy = false;
  renderWizard();
  wizardPreview();
}

// The VAT question's two answers that aren't a figure from the email.
const VAT_NONE = -1, VAT_REVERSE = -2;

// Always asked, registered or not: a rule made before registering kept no
// VAT, and its invoices were filed at 0% once registered (an accountant).
function vatRegistered() { return (state.snap.freeagent || {}).vat?.registered !== false; }

function wizardSpec() {
  const a = wiz.analysis, c = wiz.choices;
  const amount = a.amounts[c.amount], ref = a.references[c.reference];
  const vat = c.vat >= 0 ? a.vat_amounts[c.vat] : null;
  return {
    name: c.name, domain: c.domain, subject: c.subject, mentions: c.mentions,
    amount_pattern: amount?.pattern || "", amount_source: amount?.source || "text",
    currency: amount?.currency || "GBP",
    reference_pattern: ref?.pattern || "", reference_source: ref?.source || "text",
    vat_pattern: vat?.pattern || "", vat_source: vat?.source || "text",
    vat_treatment: c.vat === VAT_REVERSE ? "reverse_charge" : "printed",
    use_attachment: !!c.use_attachment, paid_with: c.paid_with,
  };
}

function wizardPreview() {
  clearTimeout(wiz.previewTimer);
  wiz.previewTimer = setTimeout(async () => {
    if (!wiz.analysis) return;
    wiz.preview = { loading: true };
    renderWizardPreview();
    try {
      wiz.preview = await api("/api/suppliers/preview", { method: "POST",
        body: { message_id: wiz.pick.id, account: wiz.pick.account, choices: wizardSpec() } });
    } catch (err) { wiz.preview = { ok: false, problem: err.message }; }
    renderWizardPreview();
  }, 350);
}

async function wizardSave() {
  wiz.busy = true;
  renderWizard();
  try {
    const saved = await api("/api/suppliers", { method: "POST",
      body: { message_id: wiz.pick.id, account: wiz.pick.account, choices: wizardSpec() } });
    closeWizard();
    toast(`Added <b>${esc(saved.name)}</b>. Collecting its receipts now…`, 6000);
    refresh(true);
  } catch (err) {
    wiz.busy = false;
    wiz.error = err.message;
    renderWizard();
  }
}

// Email Date headers are RFC 2822 and occasionally malformed; never throw.
function headerDate(raw) {
  const d = new Date(raw);
  return isNaN(d) ? raw : shortDate(d.toISOString());
}

function radio(name, value, checked, label, sub) {
  return `<label class="choice"><input type="radio" name="${name}" value="${value}" ${checked ? "checked" : ""} data-wiz="${name}">
    <span><b>${label}</b>${sub ? ` <span class="sub">${sub}</span>` : ""}</span></label>`;
}

function renderWizard() {
  if (!wiz.open) return;
  let body = "";
  if (wiz.step === "search") {
    const rows = wiz.results.map((r, i) => `
      <button class="result" data-action="wiz-pick" data-index="${i}">
        <span class="result-subject">${esc(r.subject || "(no subject)")}</span>
        <span class="sub">${esc(r.sender)} · ${esc(headerDate(r.date))}</span>
      </button>`).join("");
    body = `
      <p class="sub">Find one receipt from the supplier.</p>
      <form class="search-row" data-action="wiz-search">
        <input id="wiz-q" class="search" type="search" placeholder="e.g. uber, receipts@apple.com" value="${esc(wiz.q)}" aria-label="Search your email">
        <button class="btn primary" type="submit" ${wiz.busy ? "disabled" : ""}>${wiz.busy ? "Searching…" : "Search"}</button>
      </form>
      <div class="results">${rows || (wiz.q && !wiz.busy && !wiz.error ? `<div class="sub" style="padding:12px 2px">No emails found.</div>` : "")}</div>`;
  } else if (wiz.busy && !wiz.analysis) {
    body = `<div class="empty" style="padding:40px"><span class="spinner"></span>Reading the email…</div>`;
  } else if (wiz.analysis) {
    const a = wiz.analysis, c = wiz.choices;
    const amounts = a.amounts.map((x, i) => radio("amount", i, c.amount === i,
      esc(money(Number(x.value.replace(/,/g, "")), x.currency)), esc(x.label || "unlabelled"))).join("");
    const refs = a.references.map((x, i) => radio("reference", i, c.reference === i, esc(x.value), esc(x.label))).join("")
      + radio("reference", -1, c.reference === -1, "None", "");
    const vats = a.vat_amounts.map((x, i) => radio("vat", i, c.vat === i,
      esc(money(Number(x.value.replace(/,/g, "")), x.currency)), esc(x.label))).join("")
      + radio("vat", VAT_NONE, c.vat === VAT_NONE, "No VAT", "Zero-rated, exempt, or the supplier isn't VAT registered")
      + radio("vat", VAT_REVERSE, c.vat === VAT_REVERSE, "Reverse charge", "An overseas supplier of services");
    const paidWith = paidWithOptions().map(([value, label, sub]) =>
      radio("paid_with", esc(value), c.paid_with === value, esc(label), sub)).join("");
    body = `
      <div class="form-grid">
        <label>Name<input data-wiz="name" value="${esc(c.name)}"></label>
        <label>From<input data-wiz="domain" value="${esc(c.domain)}"></label>
        <label>Subject contains<input data-wiz="subject" value="${esc(c.subject)}" placeholder="Optional"></label>
        <label>Must mention<input data-wiz="mentions" value="${esc(c.mentions)}" placeholder="Optional, e.g. Yesim"></label>
      </div>
      <fieldset><legend>Total</legend>${amounts || `<div class="sub">No amounts found in this email.</div>`}</fieldset>
      <fieldset><legend>VAT</legend>${vatRegistered() ? "" : `<div class="sub">You're not VAT registered, so no VAT is claimed. Set it anyway in case you register.</div>`}${vats}</fieldset>
      <fieldset><legend>Reference</legend>${refs}</fieldset>
      ${a.has_pdf ? `<fieldset><legend>Document</legend>
        ${radio("use_attachment", 1, c.use_attachment, "Attached PDF", esc(a.pdf_name))}
        ${radio("use_attachment", 0, !c.use_attachment, "The email", "")}</fieldset>` : ""}
      <fieldset><legend>Paid with</legend>${paidWith}</fieldset>
      <div class="wiz-preview" id="wiz-preview"></div>`;
  }

  const footer = wiz.step === "confirm"
    ? `<button class="btn" data-action="wiz-back">Back</button><span class="spacer"></span>
       <button class="btn" data-action="wiz-close">Cancel</button>
       <button class="btn primary" data-action="wiz-save" id="wiz-save" ${wiz.busy || !wiz.preview?.ok ? "disabled" : ""}>Add recurring receipt</button>`
    : `<span class="spacer"></span><button class="btn" data-action="wiz-close">Cancel</button>`;

  $("#modal").innerHTML = `<div class="backdrop" data-action="wiz-close"></div>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Add recurring receipt">
      <div class="modal-head"><h2>Add recurring receipt</h2></div>
      <div class="modal-body">${wiz.error ? `<div class="banner" style="margin:0 0 12px">${esc(wiz.error)}</div>` : ""}${body}</div>
      <div class="modal-foot">${footer}</div>
    </div>`;
  renderWizardPreview();
}

function renderWizardPreview() {
  const box = $("#wiz-preview");
  if (!box) return;
  const p = wiz.preview;
  const save = $("#wiz-save");
  if (save) save.disabled = wiz.busy || !p?.ok;
  if (!p || p.loading) { box.innerHTML = `<div class="sub"><span class="spinner"></span> Checking…</div>`; return; }
  if (!p.ok) { box.innerHTML = `<div class="note">${esc(p.problem)}</div>`; return; }
  const matches = p.matches == null ? "" : `<div class="sub">Matches ${p.matches} email${p.matches === 1 ? "" : "s"} in the past year</div>`;
  const vatNumber = p.vat_number ? `VAT number ${esc(p.vat_number)}` : "no VAT number found";
  const vat = wiz.choices.vat === VAT_REVERSE ? `<div class="sub">VAT: reverse charge</div>`
    : p.vat != null ? `<div class="sub">VAT ${esc(money(p.vat, p.currency))} (${esc(p.vat_rate)}) · ${vatNumber}</div>`
    : `<div class="sub">No VAT claimed</div>`;
  box.innerHTML = `<div class="sub">Saved as</div><div class="filename selectable">${esc(p.filename)}</div>${vat}${matches}`;
}

document.addEventListener("submit", (e) => {
  if (e.target.dataset.action === "wiz-search") { e.preventDefault(); wizardSearch(); }
});

document.addEventListener("input", (e) => {
  const key = e.target.dataset.wiz;
  if (!key || !wiz.open) return;
  const value = e.target.type === "radio" && key !== "paid_with" ? Number(e.target.value) : e.target.value;
  wiz.choices[key] = key === "use_attachment" ? value === 1 : value;
  if (key !== "paid_with") wizardPreview();   // who pays doesn't change what's read
});

document.addEventListener("click", (e) => {
  const target = e.target.closest("[data-action]");
  switch (target?.dataset.action) {
    case "wiz-pick": return wizardPick(Number(target.dataset.index));
    case "wiz-back": wiz.step = "search"; wiz.analysis = null; return renderWizard();
    case "wiz-close": return closeWizard();
    case "wiz-save": return wizardSave();
  }
});

document.addEventListener("keydown", (e) => {
  if (wiz.open && e.key === "Escape") closeWizard();
}, true);


// ---- edit supplier -----------------------------------------------------
//
// Name, on/off and which emails it matches — the settings that apply to
// every supplier. A live check shows what the edited rule would find.

const ed = { open: false, id: "", original: null, values: null, check: null, timer: null, busy: false, error: "" };

async function openSupplierEditor(id) {
  Object.assign(ed, { open: true, id, original: null, values: null, check: null, busy: false, error: "" });
  renderEditor();
  try {
    ed.original = await api(`/api/suppliers/${encodeURIComponent(id)}`);
    ed.values = { ...ed.original };
  } catch (err) { ed.error = err.message; }
  renderEditor();
  editorCheck();
}

function closeEditor() { ed.open = false; $("#modal").innerHTML = ""; }

function editorChanges() {
  const out = {};
  for (const k of ["name", "enabled", "domain", "subject", "mentions", "paid_with", "vat_treatment"]) {
    if (ed.values[k] !== ed.original[k]) out[k] = ed.values[k];
  }
  return out;
}

function editorCheck() {
  clearTimeout(ed.timer);
  ed.timer = setTimeout(async () => {
    if (!ed.values) return;
    ed.check = { loading: true };
    renderEditorCheck();
    try {
      ed.check = await api(`/api/suppliers/${encodeURIComponent(ed.id)}/check`, { method: "POST", body: editorChanges() });
    } catch (err) { ed.check = { ok: false, problem: err.message }; }
    renderEditorCheck();
  }, 350);
}

async function editorSave() {
  ed.busy = true; renderEditor();
  try {
    await api(`/api/suppliers/${encodeURIComponent(ed.id)}/edit`, { method: "POST", body: editorChanges() });
    closeEditor();
    toast(`Saved ${esc(ed.values.name)}.`);
    refresh(true);
  } catch (err) { ed.busy = false; ed.error = err.message; renderEditor(); }
}

function renderEditor() {
  if (!ed.open) return;
  const v = ed.values;
  const body = !v
    ? (ed.error ? "" : `<div class="empty" style="padding:30px"><span class="spinner"></span>Loading…</div>`)
    : `<div class="form-grid">
        <label>Name<input data-ed="name" value="${esc(v.name)}"></label>
        <label class="toggle-field">On
          <span class="switch"><input type="checkbox" data-ed="enabled" ${v.enabled ? "checked" : ""} aria-label="Recurring receipt on"><span></span></span></label>
        <label>From<input data-ed="domain" value="${esc(v.domain)}"></label>
        <label>Subject contains<input data-ed="subject" value="${esc(v.subject)}" placeholder="Optional"></label>
        <label>Must mention<input data-ed="mentions" value="${esc(v.mentions)}" placeholder="Optional"></label>
        <label>Paid with${paidWithSelect(v.paid_with)}</label>
        <label>VAT<select data-ed="vat_treatment">
          <option value="printed" ${v.vat_treatment === "reverse_charge" ? "" : "selected"}>As printed on each invoice</option>
          <option value="reverse_charge" ${v.vat_treatment === "reverse_charge" ? "selected" : ""}>Reverse charge (overseas supplier)</option></select></label>
      </div>
      <p class="sub" style="margin:4px 0 0">New receipts from this supplier use these settings.</p>
      <div class="wiz-preview" id="ed-check"></div>`;
  const changed = v && Object.keys(editorChanges()).length;
  $("#modal").innerHTML = `<div class="backdrop" data-action="ed-close"></div>
    <div class="modal" role="dialog" aria-modal="true" aria-label="Edit recurring receipt">
      <div class="modal-head"><h2>Edit ${esc(ed.original?.name || "supplier")}</h2></div>
      <div class="modal-body">${ed.error ? `<div class="banner" style="margin:0 0 12px">${esc(ed.error)}</div>` : ""}${body}</div>
      <div class="modal-foot">
        ${ed.original ? `<button class="btn danger" data-action="ed-delete">Delete</button>` : ""}
        <span class="spacer"></span>
        <button class="btn" data-action="ed-close">Cancel</button>
        <button class="btn primary" data-action="ed-save" id="ed-save" ${ed.busy || !changed ? "disabled" : ""}>Save</button>
      </div>
    </div>`;
  renderEditorCheck();
}

/** The bank accounts you chose in Settings, plus "any" and an expense:
 *  [value, label, what it means]. */
function paidWithOptions() {
  const accounts = ((state.snap.freeagent || {}).bank_accounts || []).filter((a) => a.chosen);
  const linked = "Linked to its payment in Bank Feed";
  return [
    ["business", accounts.length === 1 ? `${accounts[0].name} (business account)` : "Any business account", linked],
    ...(accounts.length > 1 ? accounts.map((a) => [a.url, a.name, linked]) : []),
    ["personal", "Expense (paid personally)", "Claimed back from the business"],
  ];
}

function paidWithSelect(value) {
  const option = ([v, label]) => `<option value="${esc(v)}" ${v === (value || "business") ? "selected" : ""}>${esc(label)}</option>`;
  return `<select data-ed="paid_with">${paidWithOptions().map(option).join("")}</select>`;
}

function renderEditorCheck() {
  const box = $("#ed-check");
  if (!box) return;
  const c = ed.check;
  if (!c || c.loading) { box.innerHTML = `<div class="sub"><span class="spinner"></span> Checking…</div>`; return; }
  if (!c.ok) { box.innerHTML = `<div class="note">${esc(c.problem)}</div>`; return; }
  const latest = c.latest
    ? `<div class="sub">Latest saved as</div><div class="filename selectable">${esc(c.latest)}</div>`
    : c.latest_problem ? `<div class="note">${esc(c.latest_problem)}</div>` : "";
  const vat = !c.latest ? "" : ed.values?.vat_treatment === "reverse_charge" ? `<div class="sub">VAT: reverse charge</div>`
    : c.vat_problem ? `<div class="note">${esc(c.vat_problem)}</div>`
    : c.vat ? `<div class="sub">VAT ${esc(money(c.vat, "GBP"))}${c.vat_rate ? ` (${esc(c.vat_rate)})` : ""}</div>`
    : `<div class="sub">No VAT shown on it</div>`;
  box.innerHTML = `${latest}${vat}<div class="sub">Matches ${c.matches} email${c.matches === 1 ? "" : "s"} in the past year</div>`;
}

document.addEventListener("input", (e) => {
  const key = e.target.dataset.ed;
  if (!key || !ed.open) return;
  ed.values[key] = e.target.type === "checkbox" ? e.target.checked : e.target.value;
  const save = $("#ed-save");
  if (save) save.disabled = ed.busy || !Object.keys(editorChanges()).length;
  if (key === "vat_treatment") renderEditorCheck();
  else if (!["name", "enabled", "paid_with"].includes(key)) editorCheck();
});

document.addEventListener("click", (e) => {
  const target = e.target.closest("[data-action]");
  switch (target?.dataset.action) {
    case "ed-close": return closeEditor();
    case "ed-save": return editorSave();
    case "ed-delete":
      if (!confirm(`Delete ${ed.original.name}? Receipts already collected are kept.`)) return;
      closeEditor();
      return act(async () => {
        const gone = await api(`/api/suppliers/${encodeURIComponent(ed.id)}/delete`, { method: "POST" });
        toast(`Deleted ${esc(gone.name)}. <button class="btn quiet small" data-action="undo-delete" data-undo="${esc(gone.undo)}">Undo</button>`, 10000);
      });
    case "undo-delete":
      $("#toast").hidden = true;
      return act(async () => {
        const back = await api("/api/suppliers/restore", { method: "POST", body: { undo: target.dataset.undo } });
        toast(`Restored ${esc(back.name)}.`);
      });
  }
});

document.addEventListener("keydown", (e) => {
  if (ed.open && e.key === "Escape") closeEditor();
}, true);

// Batch headers are focusable; Enter/Space toggles like a click.
document.addEventListener("keydown", (e) => {
  const head = e.target.closest?.('[data-action="toggle-group"]');
  if (head && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    e.stopPropagation();
    toggleGroup(head.dataset.folder, e.altKey);
  }
}, true);

// Native menu items call these.
window.rbCheckNow = () => act(() => api("/api/scan", { method: "POST" }));
window.rbShowSettings = () => setView("settings");
window.rbShowView = (view) => setView(view);

refresh(true);
