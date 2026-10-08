// Emails: browse the mailbox and turn any one email into a receipt or an
// expense. For the one-offs no supplier rule collects: a hotel, a ticket,
// a shop used once.
//
//   Signed out  the whole view is Gmail sign-in (the card from Settings).
//   Signed in   every email, newest first, a page at a time. The ones that
//               look like receipts are picked out (app/email_inbox.py
//               judges them). Pick one to read it; "Add to Files" makes it
//               a receipt, with the supplier, date and total read from it.
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
  forms: {},              // the "Add to Files" form, by message id
  lastAdd: null,          // the email last added, whose failure the form shows
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
      currency: d.currency || "GBP", vat: d.vat == null ? "" : d.vat.toFixed(2), paid_by: "business" };
    const kept = Object.keys(em.mails);
    for (const old of kept.slice(0, Math.max(0, kept.length - MAILS_KEPT))) if (old !== em.sel) delete em.mails[old];
  } catch (err) {
    if (em.sel === id) em.openError = err.message;
  }
  if (em.opening === id) em.opening = null;
  render();
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

async function addEmail(id) {
  const mail = em.mails[id];
  if (!canAdd(mail)) return;
  const f = em.forms[id];
  em.lastAdd = id;
  await api(`/api/emails/${encodeURIComponent(id)}/add`, { method: "POST",
    body: { account: mail.account, supplier: f.supplier, date: f.date, total: f.total, currency: f.currency,
            vat: f.vat, paid_by: f.paid_by } });
  toast(`Adding the ${esc(f.supplier || "email")} email to Files…`);
}

// ---- drawing ---------------------------------------------------------------------

function renderEmails() {
  if (!emailsReady()) return renderEmailsSignIn();
  const head = emailsHead();
  const list = emailsList();
  const detail = emailDetail();
  drawEmails(head, list, detail);
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
    : em.list ? (likely ? `${likely} likely receipt${likely === 1 ? "" : "s"} below. Pick an email to add it to Files.`
      : "Pick an email to add it to Files as a receipt or an expense.")
    : "";
  const accounts = working.length > 1 ? `<select class="setting-select e-account" data-action="e-account" aria-label="Mailbox">${working.map((a) =>
      `<option value="${esc(a.email)}" ${a.email === current ? "selected" : ""}>${esc(a.email)}</option>`).join("")}</select>` : "";
  const filter = (on, label, value) => `<button type="button" class="${on ? "on" : ""}" data-action="e-filter" data-receipts="${value}"
      aria-pressed="${on}">${label}</button>`;
  return `<header class="m-head">
      <div class="m-title"><span>Emails</span><span class="sub">${esc(sub)}</span></div>
      ${accounts}
      <div class="seg2" role="group" aria-label="Show">${filter(!em.receiptsOnly, "All mail", "0")}${filter(em.receiptsOnly, "Likely receipts", "1")}</div>
      <label class="m-search e-search">${ICON.search}<input type="search" placeholder="Search mail: supplier, amount, from:…" aria-label="Search mail"
        data-action="e-search" value="${esc(em.q)}" autocomplete="off" spellcheck="false"></label>
      <button class="btn small icon" data-action="e-reload" title="Check for new email" aria-label="Check for new email" ${em.loading ? "disabled" : ""}>${REFRESH}</button>
    </header>`;
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

/** The selected email: {head, frame, side}, or {empty} when there's nothing
 * to show yet. Kept as parts so typing in the form never reloads the email. */
function emailDetail() {
  if (!em.sel) {
    return { empty: `<div class="m-empty">${ICON.search}<div class="big">Pick an email</div>
      <div>Emails that look like receipts are marked <span class="e-badge likely">Receipt</span>.<br>Add any email to Files as a receipt or an expense.</div></div>` };
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
    <div class="m-dsub">${esc(mail.from_name)}${mail.from_address && mail.from_address !== mail.from_name ? ` &lt;${esc(mail.from_address)}&gt;` : ""}
      · ${esc(mail.date ? `${longDate(mail.date)}, ${new Date(mail.date).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : "No date")}</div>`;
  return { head, frame: emailFrame(mail), caption: emailCaption(mail), side: emailSide(mail) };
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

function emailSide(mail) {
  const known = mail.in_files;
  if (known && known.status === "pending") {
    return `<div class="m-wait"><div class="t">Already in Files</div>
        <div class="b">Added as ${esc(known.vendor || "a receipt")}. Check it, then link it or claim it there.</div></div>
      <div class="m-actions"><button class="btn primary" data-action="e-show" data-rid="${known.id}">Show in Files</button></div>`;
  }
  if (known && ["exported", "filed"].includes(known.status)) {
    return `<div class="m-wait"><div class="t">Already saved</div>
        <div class="b">This email is a receipt that's been saved to FreeAgent${known.vendor ? ` (${esc(known.vendor)})` : ""}.</div></div>`;
  }
  const f = em.forms[mail.id];
  const d = mail.draft;
  const busy = adding(mail.id);
  const input = (field, type, attrs = "") =>
    `<input class="edit-input" type="${type}" data-action="e-field" data-field="${field}" value="${esc(f[field])}" ${busy ? "disabled" : ""} ${attrs}>`;
  const codes = CURRENCIES.map(([c]) => c);
  if (!codes.includes(f.currency)) codes.unshift(f.currency);
  const currency = `<select class="e-cur" data-action="e-currency" aria-label="Currency" ${busy ? "disabled" : ""}>${codes.map((c) =>
      `<option ${c === f.currency ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  const others = d.amounts.filter((a) => !(a.amount.toFixed(2) === f.total && a.currency === f.currency)).slice(0, 4);
  const found = d.total != null
    ? `<span class="muted">Read from ${esc(d.total_found_in)}</span>` : `<span class="ai">No total found</span>`;
  const pw = (to, badge, cls, title, sub) => {
    const on = f.paid_by === to;
    return `<button type="button" class="pw-opt ${on ? "on" : ""}" data-action="e-paid" data-to="${to}" aria-pressed="${on}" ${busy ? "disabled" : ""}>
      ${badge.startsWith("<span") ? badge : `<span class="badge ${cls}">${badge}</span>`}<span class="who"><b>${esc(title)}</b><span>${esc(sub)}</span></span>
      ${on ? `<span class="tick">${ICON.tick}</span>` : ""}</button>`;
  };
  const bank = accountName();
  const before = known && ["ignored", "failed", "deleted"].includes(known.status)
    ? `<div class="m-note">You ignored this email before. Adding it brings it back to Files.</div>` : "";
  const outcome = state.snap.outcome;
  const failed = !busy && em.lastAdd === mail.id && outcome?.kind === "email-add" && !outcome.ok
    ? `<div class="m-note warn">${esc(outcome.message)}</div>` : "";
  return `<div class="m-card"><div class="m-card-head"><span>Add to Files as</span>${found}</div>
      <div class="m-kv">
        <span class="k">Supplier</span>${input("supplier", "text", 'placeholder="Who is it from?" autocomplete="off"')}
        <span class="k">Date</span>${input("date", "date")}
        <span class="k">Total</span><span class="pair">${input("total", "text", 'inputmode="decimal" placeholder="0.00" autocomplete="off"')}${currency}</span>
        <span class="k">VAT</span>${input("vat", "text", 'inputmode="decimal" placeholder="None shown" autocomplete="off"')}
      </div>
      ${others.length && !busy ? `<div class="e-others"><span class="muted">Other amounts:</span>${others.map((a) =>
        `<button type="button" class="e-other" data-action="e-amount" data-amount="${a.amount.toFixed(2)}" data-currency="${esc(a.currency)}"
          title="Use this as the total">${esc(money(a.amount, a.currency))}${a.label ? ` <span>${esc(a.label)}</span>` : ""}</button>`).join("")}</div>` : ""}
    </div>
    <div class="m-card"><div class="m-card-head"><span>Paid with</span></div>
      <div class="pw" role="group" aria-label="Paid with">
        ${pw("business", bankBadge(bank), "", bank, "Business account · linked in Bank Feed")}
        ${pw("personal", ICON.cash, "personal", "Expense", "Paid personally · claimed back")}
      </div></div>
    <div class="m-note">${d.pdf ? `The attached ${esc(d.pdf)} is kept as the receipt.` : "The email is saved as a PDF and kept as the receipt."}
      You can change anything later in Files.</div>
    ${before}${failed}
    <div class="m-actions">${busy ? `<button class="btn primary" disabled><span class="spinner" aria-hidden="true"></span> Adding…</button>`
      : `<button class="btn primary" data-action="e-add">${f.paid_by === "personal" ? "Add as an expense" : "Add to Files"} ${kbd("⏎", true)}</button>`}</div>`;
}

/** Each part is only replaced when its HTML changed: the email's frame
 * would reload (and lose its scroll) on every poll otherwise. */
function drawEmails(head, list, detail) {
  const content = $("#content");
  const focus = document.activeElement;
  const keepFocus = focus?.dataset?.action && content.contains(focus)
    ? { action: focus.dataset.action, field: focus.dataset.field, at: focus.selectionStart } : null;
  let wrap = content.querySelector(".e-wrap");
  if (!wrap) {
    content.innerHTML = `<div class="m-wrap e-wrap"><div class="e-top"></div><div class="m-body">
        <section class="m-queue e-queue" aria-label="Emails"></section>
        <section class="m-detail e-detail" aria-label="Selected email"></section></div></div>`;
    wrap = content.querySelector(".e-wrap");
    em.drawn = {};
  }
  const put = (el, key, html) => { if (em.drawn[key] !== html) { el.innerHTML = html; em.drawn[key] = html; } };
  put(wrap.querySelector(".e-top"), "head", head);
  put(wrap.querySelector(".e-queue"), "list", list);
  const pane = wrap.querySelector(".e-detail");
  if (detail.empty !== undefined) {
    put(pane, "detail", detail.empty);
    em.drawn.frame = em.drawn.side = em.drawn.dhead = em.drawn.caption = undefined;
  } else {
    if (em.drawn.detail !== "parts") {
      // the form first: beside the email when there's room, above it when not
      pane.innerHTML = `<div class="e-dhead"></div><div class="m-dgrid e-grid">
          <div class="m-inspector m-keep e-side"></div>
          <figure class="m-figure e-figure"><div class="m-doc e-doc"></div><figcaption class="e-caption"></figcaption></figure></div>`;
      em.drawn = { head: em.drawn.head, list: em.drawn.list, detail: "parts" };
      pane.scrollTop = 0;
    }
    put(pane.querySelector(".e-dhead"), "dhead", detail.head);
    put(pane.querySelector(".e-doc"), "frame", detail.frame);
    put(pane.querySelector(".e-caption"), "caption", detail.caption);
    put(pane.querySelector(".e-side"), "side", detail.side);
  }
  if (keepFocus && !content.contains(focus)) {
    const sel = `[data-action="${keepFocus.action}"]${keepFocus.field ? `[data-field="${keepFocus.field}"]` : ""}`;
    const again = content.querySelector(sel);
    if (again) {
      again.focus();
      try { if (keepFocus.at != null) again.setSelectionRange(keepFocus.at, keepFocus.at); } catch {}
    }
  }
}

// ---- events ----------------------------------------------------------------------

document.addEventListener("click", (e) => {
  if (state.view !== "emails") return;
  const target = e.target.closest("[data-action]");
  const action = target?.dataset.action || "";
  if (!action.startsWith("e-")) return;
  const mail = em.mails[em.sel];
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
    case "e-paid":
      if (!mail) return;
      em.forms[mail.id].paid_by = target.dataset.to;
      return render();
    case "e-amount":
      if (!mail) return;
      Object.assign(em.forms[mail.id], { total: target.dataset.amount, currency: target.dataset.currency });
      return render();
    case "e-add":
      return act(() => addEmail(em.sel));
    case "e-show":
      m.sel = Number(target.dataset.rid);
      m.reveal = true;
      return setView("pending");
  }
});

document.addEventListener("input", (e) => {
  if (state.view !== "emails") return;
  const a = e.target.dataset.action;
  if (a === "e-field" && em.forms[em.sel]) {
    em.forms[em.sel][e.target.dataset.field] = e.target.value;
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
  if (a === "e-currency" && em.forms[em.sel]) {
    em.forms[em.sel].currency = e.target.value;
    render();
  } else if (a === "e-account") {
    em.account = e.target.value;
    em.sel = null;
    emailsLoad();
  }
});

document.addEventListener("keydown", (e) => {
  if (state.view !== "emails" || !emailsReady() || wiz.open || ed.open || e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t.matches?.('input[data-action="e-search"]')) {
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(em.searchTimer); em.q = t.value; emailsLoad(); }
    else if (e.key === "Escape" && t.value) { e.preventDefault(); em.q = ""; t.value = ""; emailsLoad(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); t.blur(); pick(em.list?.emails[0]?.id || null); }
    return;
  }
  if (t.matches?.(".e-side input")) {
    if (e.key === "Enter") { e.preventDefault(); act(() => addEmail(em.sel)); }
    else if (e.key === "Escape") t.blur();
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
    act(() => addEmail(em.sel));
  } else if (e.key === "/") {
    e.preventDefault();
    document.querySelector('input[data-action="e-search"]')?.focus();
  }
});
