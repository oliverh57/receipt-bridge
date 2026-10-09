// Emails: browse the mailbox and turn any one email into a receipt or an
// expense. For the one-offs no supplier rule collects: a hotel, a ticket,
// a shop used once.
//
//   Signed out  the whole view is Gmail sign-in (the card from Settings).
//   Signed in   every email, newest first, a page at a time. The ones that
//               look like receipts are picked out (app/email_inbox.py
//               judges them). Pick one to read it; "Convert to receipt"
//               opens a dialog with the supplier, date and total read from
//               it, and files it into Files as a receipt or an expense.
//
// Unlike the other views the list comes from Gmail while you wait: there is
// nothing to show until it answers. Loaded after app.js and match.js, and
// built on their helpers (state, api, esc, money, shortDate, longDate,
// toast, act, render, accountName, bankBadge, kbd, ICON, CURRENCIES, m).
//
// SECURITY: everything here comes from arbitrary senders and this page holds
// the API token. Text goes through esc(). An email's own HTML is shown only
// inside a sandboxed srcdoc iframe: no scripts, an opaque origin (it can't
// reach this page or the token), and a CSP that blocks anything remote
// until you ask for images.

"use strict";

const em = {
  account: "",            // the mailbox chosen in the list ("" = the first)
  q: "",                  // the search, as Gmail search syntax
  receiptsOnly: false,    // "Likely receipts" instead of "All mail"
  key: "",                // what the list was loaded for (account, search, filter)
  list: null,             // {account, emails[], next}
  loading: false,
  more: false,            // loading the next page
  error: "",
  loadedAt: 0,
  seq: 0,                 // drops the answer to a search that's been replaced
  sel: null,              // selected message id
  mails: {},              // opened emails by id: {…, draft}
  opening: null,          // id being downloaded
  openError: "",
  forms: {},              // the "Convert to receipt" form, by message id
  dialog: null,           // the email whose "Convert to receipt" dialog is open
  dialogError: "",
  lastAdd: null,          // the email last added, whose failure its header shows
  images: false,          // show remote images in emails
  searchTimer: null,
  drawn: {},              // the HTML of each part on screen, so unchanged parts aren't redrawn
};

const STALE_MS = 5 * 60 * 1000;
const MAILS_KEPT = 15;
const PAPERCLIP = `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-label="Has an attachment"><path d="M13 7.5l-5.2 5.2a3.2 3.2 0 0 1-4.5-4.5l5.6-5.6a2.1 2.1 0 0 1 3 3L6.3 11.2a1 1 0 0 1-1.5-1.5L10 4.5"/></svg>`;
const REFRESH = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.5 2.5v3h-3"/></svg>`;

/** Signed in: an account that hasn't expired. Until then the view is sign-in. */
function emailsReady() {
  const s = state.snap;
  return Boolean(s?.has_credentials) && (s.accounts || []).some((a) => !["expired", "signed_out"].includes(a.state));
}

function listKey() { return JSON.stringify([em.account, em.q.trim(), em.receiptsOnly]); }

function findEmail(id) { return (em.list?.emails || []).find((x) => x.id === id) || null; }

/** Today: the time. Otherwise the day. */
function mailDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toDateString() === new Date().toDateString()
    ? d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : shortDate(iso);
}

// ---- data ------------------------------------------------------------------------

/** Called by refresh() on each poll while this view is open. Gmail is only
 * asked when the search changes (or the list is old); otherwise only which
 * emails are already receipts is looked up, which is local. */
async function emailsRefresh(force, versionChanged) {
  if (!emailsReady()) { em.key = ""; return; }
  const entered = state.receiptsKey !== "emails";
  state.receiptsKey = "emails";
  const stale = entered && Date.now() - em.loadedAt > STALE_MS;
  if (!em.loading && (em.key !== listKey() || stale)) { emailsLoad(); return; }
  if ((force || versionChanged) && em.list) await emailsKnown();
}

async function emailsLoad(more = false) {
  const seq = ++em.seq;
  if (more) em.more = true;
  else { em.key = listKey(); em.loading = true; em.error = ""; em.list = null; }
  render();
  try {
    const params = new URLSearchParams({ account: em.account, q: em.q.trim(), receipts: String(em.receiptsOnly),
      page: more ? em.list.next : "" });
    const res = await api(`/api/emails?${params}`);
    if (seq !== em.seq) return;
    if (more) {
      const have = new Set(em.list.emails.map((x) => x.id));
      em.list.emails.push(...res.emails.filter((x) => !have.has(x.id)));
      em.list.next = res.next;
    } else {
      em.list = res;
      em.loadedAt = Date.now();
    }
  } catch (err) {
    if (seq !== em.seq) return;
    if (more) toast(`<b>Couldn't load more.</b> ${esc(err.message)}`);
    else em.error = err.message;
  }
  em.loading = false;
  em.more = false;
  render();
}

/** Which emails on screen are already receipts. Local: no Gmail. */
async function emailsKnown() {
  const ids = [...new Set([...(em.list?.emails || []).map((x) => x.id), ...Object.keys(em.mails)])];
  if (!ids.length) return;
  try {
    const known = await api("/api/emails/known", { method: "POST", body: { ids } });
    for (const x of em.list?.emails || []) x.in_files = known[x.id] || null;
    for (const [id, mail] of Object.entries(em.mails)) mail.in_files = known[id] || null;
  } catch {}
}

async function openEmail(id) {
  const row = findEmail(id);
  if (em.mails[id] || em.opening === id) return;
  em.opening = id;
  em.openError = "";
  render();
  try {
    const params = new URLSearchParams({ account: row?.account || em.account });
    const mail = await api(`/api/emails/${encodeURIComponent(id)}?${params}`);
    em.mails[id] = mail;
    const d = mail.draft;
    em.forms[id] ||= { supplier: d.supplier || "", date: d.date || "", total: d.total == null ? "" : d.total.toFixed(2),
      currency: d.currency || "GBP", vat: d.vat == null ? "" : d.vat.toFixed(2), vat_choice: vatRateOf(d.vat, d.total),
      paid_by: "business" };
    const kept = Object.keys(em.mails);
    for (const old of kept.slice(0, Math.max(0, kept.length - MAILS_KEPT))) if (old !== em.sel) delete em.mails[old];
  } catch (err) {
    if (em.sel === id) em.openError = err.message;
  }
  if (em.opening === id) em.opening = null;
  render();
}

/** The VAT menu choice an email's VAT works out to: 20% or 5% of the total,
 * else the amount as typed; with no VAT read, Auto (as printed: none). */
function vatRateOf(vat, total) {
  if (vat == null) return "auto";
  for (const rate of [20, 5]) if (total && Math.abs(vat - total * rate / (100 + rate)) <= 0.02) return `${rate}.0`;
  return "amount";
}

/** What the chosen VAT comes to, beside the menu. */
function vatNote(f, d) {
  const total = Number(String(f.total).replace(/[£,\s]/g, ""));
  const rate = { "20.0": 20, "5.0": 5, "0.0": 0 }[f.vat_choice];
  if (rate != null) return Number.isFinite(total) && total > 0 ? money(total * rate / (100 + rate), f.currency) : "";
  if (f.vat_choice === "auto") return d.vat != null ? `${money(d.vat, f.currency)} as printed` : "None shown";
  return "";
}

function pick(id) {
  em.sel = id;
  em.openError = "";
  render();
  if (id) openEmail(id);
}

function adding(id) { return (state.snap?.queued || []).includes(`email-add:${id}`); }

function canAdd(mail) {
  const known = mail?.in_files;
  return mail && !adding(mail.id) && !(known && ["pending", "exported", "filed"].includes(known.status));
}

function openDialog(id) {
  if (!canAdd(em.mails[id])) return;
  em.dialog = id;
  em.dialogError = "";
  render();
  document.querySelector('.e-dialog input[data-field="supplier"]')?.focus();
}

function closeEmailDialog() {
  em.dialog = null;
  em.dialogError = "";
  em.drawn.dialog = undefined;
  const host = $("#modal");
  if (host.querySelector(".e-dialog")) host.innerHTML = "";
}

/** "Add to Files" in the dialog: queued; the dialog closes once it's accepted,
 * and stays open with the reason when it isn't (a total that isn't a number). */
async function submitDialog() {
  const id = em.dialog;
  const mail = em.mails[id];
  if (!canAdd(mail)) return closeEmailDialog();
  const f = em.forms[id];
  try {
    await api(`/api/emails/${encodeURIComponent(id)}/add`, { method: "POST",
      body: { account: mail.account, supplier: f.supplier, date: f.date, total: f.total, currency: f.currency,
              vat: f.vat, vat_choice: f.vat_choice, paid_by: f.paid_by } });
  } catch (err) {
    em.dialogError = err.message;
    return render();
  }
  em.lastAdd = id;
  closeEmailDialog();
  toast(`Adding the ${esc(f.supplier || "email")} email to Files${f.paid_by === "personal" ? " as an expense" : ""}…`);
  refresh(true);
}

// ---- drawing ---------------------------------------------------------------------

function renderEmails() {
  if (!emailsReady()) { closeEmailDialog(); return renderEmailsSignIn(); }
  drawEmails(emailsHead(), emailsTools(), emailsList(), emailDetail());
  drawEmailDialog();
}

/** Signed out: the whole view is Gmail's sign-in, as in Settings. */
function renderEmailsSignIn() {
  em.drawn = {};
  const s = state.snap;
  const expired = (s.accounts || []).length > 0;
  $("#content").innerHTML = `<div class="e-signin"><div class="e-signin-inner">
      <h1>Emails</h1>
      <p class="settings-intro">${expired
        ? "Gmail needs signing in again before your emails can be shown."
        : "Sign in to Gmail to turn any email into a receipt or an expense: a hotel, a ticket, a shop you used once. Receipt Bridge only reads your mail; it can't send, change or delete anything."}</p>
      ${gmailCard(s)}
      <p class="e-signin-note">Receipts from the same supplier every month? Add a supplier in Settings → Email instead and they're collected by themselves.</p>
    </div></div>`;
}

function emailsHead() {
  const s = state.snap;
  const working = s.accounts.filter((a) => !["expired", "signed_out"].includes(a.state));
  const current = em.account || em.list?.account || working[0]?.email || "";
  const likely = (em.list?.emails || []).filter((x) => x.receipt === "likely").length;
  const sub = em.loading ? (em.q.trim() ? "Searching…" : "Loading your email…")
    : em.error ? "Couldn't load your email"
    : em.list ? (likely ? `${likely} likely receipt${likely === 1 ? "" : "s"} below. Pick an email to convert it to a receipt.`
      : "Pick an email to convert it to a receipt or an expense.")
    : "";
  const accounts = working.length > 1 ? `<select class="setting-select e-account" data-action="e-account" aria-label="Mailbox">${working.map((a) =>
      `<option value="${esc(a.email)}" ${a.email === current ? "selected" : ""}>${esc(a.email)}</option>`).join("")}</select>` : "";
  return `<header class="m-head">
      <div class="m-title"><span>Emails</span><span class="sub">${esc(sub)}</span></div>
      ${accounts}
      <button class="btn small icon" data-action="e-reload" title="Check for new email" aria-label="Check for new email" ${em.loading ? "disabled" : ""}>${REFRESH}</button>
    </header>`;
}

/** Above the list: the search, and All mail / Likely receipts. */
function emailsTools() {
  const filter = (on, label, value) => `<button type="button" class="${on ? "on" : ""}" data-action="e-filter" data-receipts="${value}"
      aria-pressed="${on}">${label}</button>`;
  return `<label class="m-search e-search">${ICON.search}<input type="search" placeholder="Search mail: supplier, amount, from:…" aria-label="Search mail"
        data-action="e-search" value="${esc(em.q)}" autocomplete="off" spellcheck="false"></label>
    <div class="seg2 e-filter" role="group" aria-label="Show">${filter(!em.receiptsOnly, "All mail", "0")}${filter(em.receiptsOnly, "Likely receipts", "1")}</div>`;
}

function emailsList() {
  if (em.error) {
    return `<div class="m-none"><div>${esc(em.error)}</div>
      <div class="e-retry"><button class="btn small" data-action="e-reload">Try again</button></div></div>`;
  }
  if (!em.list) return `<div class="m-none"><span class="spinner" aria-hidden="true"></span> ${em.q.trim() ? "Searching…" : "Loading…"}</div>`;
  const rows = em.list.emails;
  if (!rows.length) {
    return `<div class="m-none">${em.q.trim() ? `No emails match “${esc(em.q.trim())}”.` : em.receiptsOnly ? "No likely receipts found." : "No emails."}
      ${em.receiptsOnly ? `<div class="e-retry"><button class="btn small" data-action="e-filter" data-receipts="0">Show all mail</button></div>` : ""}</div>`;
  }
  const more = em.list.next
    ? `<div class="e-more"><button class="btn small" data-action="e-more" ${em.more ? "disabled" : ""}>${em.more ? "Loading…" : "Load more"}</button></div>` : "";
  return rows.map(emailRow).join("") + more;
}

const IN_FILES = { pending: "In Files", exported: "Saved", filed: "Saved", ignored: "Ignored", failed: "Ignored" };

function emailRow(x) {
  const on = x.id === em.sel;
  const known = x.in_files && IN_FILES[x.in_files.status];
  const badges = [
    x.receipt === "likely" ? `<span class="e-badge likely">Receipt</span>` : x.receipt === "maybe" ? `<span class="e-badge maybe">Receipt?</span>` : "",
    known ? `<span class="e-badge done">${esc(known)}</span>` : "",
    x.has_attachment ? `<span class="e-clip">${PAPERCLIP}</span>` : "",
    x.amount ? `<span class="e-amt">${esc(money(x.amount.amount, x.amount.currency))}</span>` : "",
  ].join("");
  const why = x.why?.length ? `Looks like a receipt: ${x.why.join(", ").toLowerCase()}.` : "";
  return `<button type="button" class="m-row e-row ${x.receipt ? `e-${x.receipt}` : ""} ${x.unread ? "unread" : ""} ${known ? "e-known" : ""} ${on ? "selected" : ""}"
      data-action="e-pick" data-id="${esc(x.id)}" aria-pressed="${on}" ${why ? `title="${esc(why)}"` : ""}>
      <span class="txt">
        <span class="top"><span class="v">${esc(x.from_name || x.from_address || "Unknown sender")}</span><span class="e-date">${esc(mailDate(x.date))}</span></span>
        <span class="e-subj">${esc(x.subject || "(no subject)")}</span>
        <span class="m-sub">${esc(x.snippet)}</span>
        ${badges ? `<span class="e-badges">${badges}</span>` : ""}
      </span>
    </button>`;
}

/** The selected email: {head, frame, caption}, or {empty} when there's
 * nothing to show yet. Kept as parts so a redraw never reloads the email. */
function emailDetail() {
  if (!em.sel) {
    return { empty: `<div class="m-empty">${ICON.search}<div class="big">Pick an email</div>
      <div>Emails that look like receipts are marked <span class="e-badge likely">Receipt</span>.<br>Convert any email to a receipt or an expense.</div></div>` };
  }
  const mail = em.mails[em.sel];
  if (!mail) {
    return { empty: em.openError
      ? `<div class="m-empty"><div class="big">Couldn't open this email</div><div>${esc(em.openError)}</div>
          <div><button class="btn small" data-action="e-pick" data-id="${esc(em.sel)}">Try again</button></div></div>`
      : `<div class="m-empty"><span class="spinner" aria-hidden="true"></span><div>Opening…</div></div>` };
  }
  const row = findEmail(mail.id);
  const looks = row?.receipt === "likely" ? `<span class="chip good">Looks like a receipt</span>`
    : row?.receipt === "maybe" ? `<span class="chip quiet">Might be a receipt</span>` : "";
  const d = mail.draft;
  const head = `<div class="m-dhead"><h1 class="e-h1">${esc(mail.subject || "(no subject)")}</h1>${looks}<span class="grow"></span>
      <span class="amt">${d.total != null ? esc(money(d.total, d.currency)) : ""}</span></div>
    <div class="e-subline"><div class="m-dsub">${esc(mail.from_name)}${mail.from_address && mail.from_address !== mail.from_name ? ` &lt;${esc(mail.from_address)}&gt;` : ""}
      · ${esc(mail.date ? `${longDate(mail.date)}, ${new Date(mail.date).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : "No date")}</div>
      <div class="e-actions">${emailActions(mail)}</div></div>
    ${emailNote(mail)}`;
  return { head, frame: emailFrame(mail), caption: emailCaption(mail) };
}

/** Beside the sender: "Convert to receipt", or where the receipt already is. */
function emailActions(mail) {
  const known = mail.in_files;
  if (adding(mail.id)) return `<button class="btn primary" disabled><span class="spinner" aria-hidden="true"></span> Adding…</button>`;
  if (known?.status === "pending") {
    return `<span class="chip info">In Files</span><button class="btn" data-action="e-show" data-rid="${known.id}">Show in Files</button>`;
  }
  if (known && ["exported", "filed"].includes(known.status)) return `<span class="chip good">Saved to FreeAgent</span>`;
  return `<button class="btn primary" data-action="e-convert">Convert to receipt ${kbd("⏎", true)}</button>`;
}

function emailNote(mail) {
  const outcome = state.snap.outcome;
  if (!adding(mail.id) && em.lastAdd === mail.id && outcome?.kind === "email-add" && !outcome.ok) {
    return `<div class="m-note warn e-note">${esc(outcome.message)}</div>`;
  }
  return "";
}

/** The email as its sender laid it out, in a sandbox. */
function emailFrame(mail) {
  const remote = em.images ? " https: http:" : "";
  const csp = `default-src 'none'; style-src 'unsafe-inline'${remote}; img-src data:${remote}; font-src data:${remote}`;
  const body = mail.html
    ? mail.html.replace(/<script\b[\s\S]*?<\/script\s*>/gi, "").replace(/<(base|meta\b[^>]*http-equiv)\b[^>]*>/gi, "")
    : `<pre style="white-space:pre-wrap;font:13px/1.5 -apple-system,system-ui,sans-serif;margin:0">${esc(mail.text)}</pre>`;
  const doc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}">`
    + `<base target="_blank"><style>html{background:#fff;color:#1d1d1f}body{margin:16px;font:14px/1.45 -apple-system,system-ui,sans-serif;overflow-wrap:anywhere}`
    + `img{max-width:100%;height:auto}table{max-width:100%}</style></head><body>${body}</body></html>`;
  // links open in the browser (a popup leaves the sandbox; the app sends it to the Mac's browser)
  return `<iframe class="doc e-frame" title="${esc(mail.subject || "Email")}" sandbox="allow-popups allow-popups-to-escape-sandbox"
    referrerpolicy="no-referrer" srcdoc="${esc(doc)}"></iframe>`;
}

function emailCaption(mail) {
  const files = mail.attachments.map((a) => `<span class="e-file">${PAPERCLIP} ${esc(a.filename)}</span>`).join("");
  const gmail = `https://mail.google.com/mail/?authuser=${encodeURIComponent(mail.account)}#all/${encodeURIComponent(mail.thread_id || mail.id)}`;
  return `${files ? `<div class="e-files">${files}</div>` : ""}
    <div><button class="link" data-action="e-images">${em.images ? "Hide images" : "Show images"}</button>
      · <a href="${esc(gmail)}" target="_blank" rel="noopener">Open in Gmail</a></div>`;
}

/** "Convert to receipt": what it will be filed as, read from the email and
 * changeable here, and who paid. */
function dialogHtml(mail) {
  const f = em.forms[mail.id];
  const d = mail.draft;
  const input = (field, type, attrs = "") =>
    `<input class="edit-input" type="${type}" data-action="e-field" data-field="${field}" value="${esc(f[field])}" ${attrs}>`;
  const codes = CURRENCIES.map(([c]) => c);
  if (!codes.includes(f.currency)) codes.unshift(f.currency);
  const currency = `<select class="e-cur" data-action="e-currency" aria-label="Currency">${codes.map((c) =>
      `<option ${c === f.currency ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  const others = d.amounts.filter((a) => !(a.amount.toFixed(2) === f.total && a.currency === f.currency)).slice(0, 4);
  const found = d.total != null ? `Total read from ${esc(d.total_found_in)}.` : `<span class="ai">No total found</span>`;
  const pw = (to, badge, cls, title, sub) => {
    const on = f.paid_by === to;
    return `<button type="button" class="pw-opt ${on ? "on" : ""}" data-action="e-paid" data-to="${to}" aria-pressed="${on}">
      ${badge.startsWith("<span") ? badge : `<span class="badge ${cls}">${badge}</span>`}<span class="who"><b>${esc(title)}</b><span>${esc(sub)}</span></span>
      ${on ? `<span class="tick">${ICON.tick}</span>` : ""}</button>`;
  };
  const bank = accountName();
  const known = mail.in_files;
  const before = known && ["ignored", "failed", "deleted"].includes(known.status)
    ? `<div class="m-note e-dnote">This email is in Archived. Adding it moves it back to Files.</div>` : "";
  return `<div class="m-overlay e-overlay"><section class="m-dialog wide e-dialog m-keep" role="dialog" aria-modal="true" aria-labelledby="e-dialog-title">
      <h2 id="e-dialog-title">Convert to receipt</h2>
      <p class="sub">${esc(mail.subject || "(no subject)")} · ${esc(mail.from_name)}. ${found}</p>
      <div class="m-kv">
        <span class="k">Supplier</span>${input("supplier", "text", 'placeholder="Who is it from?" autocomplete="off"')}
        <span class="k">Date</span>${input("date", "date")}
        <span class="k">Total</span><span class="pair">${input("total", "text", 'inputmode="decimal" placeholder="0.00" autocomplete="off"')}${currency}</span>
        <span class="k">VAT</span><span class="pair"><select class="e-vat" data-action="e-vat-choice" aria-label="VAT">${VAT_MENU.map(([v, l]) =>
          `<option value="${v}" ${f.vat_choice === v ? "selected" : ""}>${l}</option>`).join("")}</select>${f.vat_choice === "amount"
          ? input("vat", "text", 'inputmode="decimal" placeholder="0.00" autocomplete="off" aria-label="VAT amount"')
          : `<span class="e-vat-note muted">${esc(vatNote(f, d))}</span>`}</span>
        <span class="k">Receipt</span><span class="e-doc-name">${d.pdf ? `${PAPERCLIP} ${esc(d.pdf)}` : "This email, saved as a PDF"}</span>
      </div>
      ${others.length ? `<div class="e-others"><span class="muted">Other amounts:</span>${others.map((a) =>
        `<button type="button" class="e-other" data-action="e-amount" data-amount="${a.amount.toFixed(2)}" data-currency="${esc(a.currency)}"
          title="Use this as the total">${esc(money(a.amount, a.currency))}${a.label ? ` <span>${esc(a.label)}</span>` : ""}</button>`).join("")}</div>` : ""}
      <div class="e-dlabel">Paid with</div>
      <div class="pw" role="group" aria-label="Paid with">
        ${pw("business", bankBadge(bank), "", bank, "Business account · linked in Bank Feed")}
        ${pw("personal", ICON.cash, "personal", "Expense", "Paid personally · claimed back")}
      </div>
      ${before}
      ${em.dialogError ? `<div class="m-note warn">${esc(em.dialogError)}</div>` : ""}
      <div class="m-dialog-foot">
        <button class="btn" data-action="e-cancel">Cancel</button>
        <button class="btn primary" data-action="e-add">${f.paid_by === "personal" ? "Add as an expense" : "Add to Files"} ${kbd("⏎", true)}</button>
      </div>
    </section></div>`;
}

/** The dialog lives in #modal, over the whole window. Redrawn only when
 * something in it changed, keeping the field being typed in. */
function drawEmailDialog() {
  const host = $("#modal");
  const mail = em.dialog && em.mails[em.dialog];
  if (!mail || !canAdd(mail)) {
    if (em.dialog) closeEmailDialog();
    return;
  }
  const html = dialogHtml(mail);
  if (em.drawn.dialog === html && host.querySelector(".e-dialog")) return;
  const focus = document.activeElement;
  const field = host.contains(focus) ? focus.dataset.field || focus.dataset.action : null;
  const at = host.contains(focus) ? focus.selectionStart : null;
  host.innerHTML = html;
  em.drawn.dialog = html;
  if (field) {
    const again = host.querySelector(`[data-field="${field}"]`) || host.querySelector(`[data-action="${field}"]`);
    again?.focus();
    try { if (at != null) again.setSelectionRange(at, at); } catch {}
  }
}

/** Each part is only replaced when its HTML changed: the email's frame
 * would reload (and lose its scroll) on every poll otherwise. */
function drawEmails(head, tools, list, detail) {
  const content = $("#content");
  const focus = document.activeElement;
  const keepFocus = focus?.dataset?.action && content.contains(focus)
    ? { action: focus.dataset.action, at: focus.selectionStart } : null;
  let wrap = content.querySelector(".e-wrap");
  if (!wrap) {
    content.innerHTML = `<div class="m-wrap e-wrap"><div class="e-top"></div><div class="m-body">
        <div class="e-col"><div class="e-tools"></div><section class="m-queue e-queue" aria-label="Emails"></section></div>
        <section class="m-detail e-detail" aria-label="Selected email"></section></div></div>`;
    wrap = content.querySelector(".e-wrap");
    em.drawn = { dialog: em.drawn.dialog };
  }
  const put = (el, key, html) => { if (em.drawn[key] !== html) { el.innerHTML = html; em.drawn[key] = html; } };
  put(wrap.querySelector(".e-top"), "head", head);
  put(wrap.querySelector(".e-tools"), "tools", tools);
  put(wrap.querySelector(".e-queue"), "list", list);
  const pane = wrap.querySelector(".e-detail");
  if (detail.empty !== undefined) {
    put(pane, "detail", detail.empty);
    em.drawn.frame = em.drawn.dhead = em.drawn.caption = undefined;
  } else {
    if (em.drawn.detail !== "parts") {
      pane.innerHTML = `<div class="e-dhead"></div>
        <figure class="m-figure e-figure"><div class="m-doc e-doc"></div><figcaption class="e-caption"></figcaption></figure>`;
      em.drawn.detail = "parts";
      em.drawn.frame = em.drawn.dhead = em.drawn.caption = undefined;
      pane.scrollTop = 0;
    }
    put(pane.querySelector(".e-dhead"), "dhead", detail.head);
    put(pane.querySelector(".e-doc"), "frame", detail.frame);
    put(pane.querySelector(".e-caption"), "caption", detail.caption);
  }
  if (keepFocus && !content.contains(focus)) {
    const again = content.querySelector(`[data-action="${keepFocus.action}"]`);
    if (again) {
      again.focus();
      try { if (keepFocus.at != null) again.setSelectionRange(keepFocus.at, keepFocus.at); } catch {}
    }
  }
}

// ---- events ----------------------------------------------------------------------

document.addEventListener("click", (e) => {
  if (state.view !== "emails") return;
  if (e.target.classList?.contains("e-overlay")) return closeEmailDialog();     // outside the dialog
  const target = e.target.closest("[data-action]");
  const action = target?.dataset.action || "";
  if (!action.startsWith("e-")) return;
  const form = em.forms[em.dialog];
  switch (action) {
    case "e-pick":
      if (target.dataset.id === em.sel && em.mails[em.sel]) return;
      return pick(target.dataset.id);
    case "e-filter":
      em.receiptsOnly = target.dataset.receipts === "1";
      return emailsLoad();
    case "e-reload":
      return emailsLoad();
    case "e-more":
      return emailsLoad(true);
    case "e-images":
      em.images = !em.images;
      return render();
    case "e-convert":
      return openDialog(em.sel);
    case "e-cancel":
      return closeEmailDialog();
    case "e-paid":
      if (!form) return;
      form.paid_by = target.dataset.to;
      return render();
    case "e-amount":
      if (!form) return;
      Object.assign(form, { total: target.dataset.amount, currency: target.dataset.currency });
      return render();
    case "e-add":
      return submitDialog();
    case "e-show":
      m.sel = Number(target.dataset.rid);
      m.reveal = true;
      return setView("pending");
  }
});

document.addEventListener("input", (e) => {
  if (state.view !== "emails") return;
  const a = e.target.dataset.action;
  if (a === "e-field" && em.forms[em.dialog]) {
    em.forms[em.dialog][e.target.dataset.field] = e.target.value;
    const note = document.querySelector(".e-dialog .e-vat-note");
    if (note) note.textContent = vatNote(em.forms[em.dialog], em.mails[em.dialog].draft);
  } else if (a === "e-search") {
    em.q = e.target.value;
    clearTimeout(em.searchTimer);
    // Gmail is asked once typing pauses, not on every key
    em.searchTimer = setTimeout(() => { if (em.key !== listKey()) emailsLoad(); }, 500);
  }
});

document.addEventListener("change", (e) => {
  if (state.view !== "emails") return;
  const a = e.target.dataset.action;
  if (a === "e-currency" && em.forms[em.dialog]) {
    em.forms[em.dialog].currency = e.target.value;
    render();
  } else if (a === "e-vat-choice" && em.forms[em.dialog]) {
    em.forms[em.dialog].vat_choice = e.target.value;
    render();
    if (e.target.value === "amount") document.querySelector('.e-dialog input[data-field="vat"]')?.focus();
  } else if (a === "e-account") {
    em.account = e.target.value;
    em.sel = null;
    emailsLoad();
  }
});

document.addEventListener("keydown", (e) => {
  if (state.view !== "emails" || !emailsReady() || wiz.open || ed.open || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (em.dialog) {
    // the dialog has the keyboard: Esc closes it, ⏎ adds (a focused button does its own thing)
    if (e.key === "Escape") { e.preventDefault(); closeEmailDialog(); }
    else if (e.key === "Enter" && !t.closest?.("button, select")) { e.preventDefault(); submitDialog(); }
    return;
  }
  if (t.matches?.('input[data-action="e-search"]')) {
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(em.searchTimer); em.q = t.value; emailsLoad(); }
    else if (e.key === "Escape" && t.value) { e.preventDefault(); em.q = ""; t.value = ""; emailsLoad(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); t.blur(); pick(em.list?.emails[0]?.id || null); }
    return;
  }
  if (t.matches?.("input, select, textarea")) return;
  if (e.key === "Enter" && t.closest?.("button, a") && !t.closest(".e-row")) return;   // a focused button does its own thing
  const rows = em.list?.emails || [];
  const i = rows.findIndex((x) => x.id === em.sel);
  if (e.key === "ArrowDown" || e.key === "j" || e.key === "ArrowUp" || e.key === "k") {
    if (!rows.length) return;
    e.preventDefault();
    const down = e.key === "ArrowDown" || e.key === "j";
    const next = rows[i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + (down ? 1 : -1)))];
    pick(next.id);
    document.querySelector(".e-row.selected")?.scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && canAdd(em.mails[em.sel])) {
    e.preventDefault();
    openDialog(em.sel);
  } else if (e.key === "/") {
    e.preventDefault();
    document.querySelector('input[data-action="e-search"]')?.focus();
  }
});
