// Files, Statement and Expenses (PLAN.md §11, reworked 2026-10-07):
//
//   Files      every receipt that isn't linked yet: photos, emails, files
//              dropped here. Check it (supplier, date, total) and say who
//              paid. The starting point for everything.
//   Statement  where files get linked: each bank payment, its suggested file,
//              Link (and it's saved to FreeAgent), or pick another file.
//   Expenses   files paid personally become expense claims.
//
// Linked and saved to FreeAgent means done: it leaves every list (Undo stays
// on its Statement line). Loaded after app.js and built on its helpers
// (state, api, esc, money, amountOf, shortDate, longDate, ago, toast, act,
// render). The engine works out stage, issues and suggestions
// (app/review.py, docs/API.md); this file only lays them out.
//
// SECURITY: receipt text comes from arbitrary senders. Every value
// interpolated into HTML goes through esc().

"use strict";

const m = {
  sel: null,                         // Files: selected receipt id
  xsel: null,                        // Expenses: selected receipt id
  xedit: null,                       // Expenses: the claim being edited
  open: { waiting: false, filed: false },
  query: "",
  fileAll: null,                     // { phase: confirm|running|done, items, since, results }
  undo: null,                        // what the toast's Undo does
  docId: null,                       // receipt whose document is on screen
  docTidy: false,                    // …and whether it's the tidied copy
  reveal: false,                     // scroll the selected row into view on the next draw
  dragging: false,
  stSel: null,                       // Statement: selected payment URL
  stOther: false,                    // Statement: showing other files for a suggested payment
  stPick: null,                      // Statement: { url, sel } while picking from all files not yet linked
  stPending: null,                   // Statement: receipt whose payment to open once loaded
  curOpen: null,                     // receipt id whose currency list is open
  asel: null,                        // Archived: selected receipt id
  multi: [],                         // Files: ids picked with shift- or ⌘-click (2 or more = bulk)
};

const CURRENCY_WORDS = { EUR: "euros", USD: "dollars", AUD: "dollars", CAD: "dollars", NZD: "dollars",
  JPY: "yen", DKK: "kroner", SEK: "kronor", NOK: "kroner", CHF: "francs", ARS: "pesos", MXN: "pesos" };

const ICON = {
  check: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 2.2l6.2 11H1.8z"/><path d="M8 6.6v3"/><path d="M8 11.7v.01"/></svg>`,
  link: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M5 8h6M8.5 5.5L11 8l-2.5 2.5"/></svg>`,
  waiting: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M8 4.5V8l2.4 1.6"/></svg>`,
  filed: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M5.2 8.2l1.9 1.9 3.7-4"/></svg>`,
  tick: `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg>`,
  cross: `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>`,
  search: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5L14 14"/></svg>`,
  expense: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1.5" y="4" width="13" height="8" rx="1.2"/><circle cx="8" cy="8" r="1.8"/><path d="M4 6.2v.01M12 9.8v.01"/></svg>`,
  cash: `<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="1.5" y="4" width="13" height="8" rx="1.2"/><circle cx="8" cy="8" r="1.8"/><path d="M4 6.2v.01M12 9.8v.01"/></svg>`,
  info: `<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/><path d="M8 7.2v4"/><path d="M8 4.8v.01"/></svg>`,
  upload: `<svg width="40" height="40" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 10.5V2.5M5 5.5l3-3 3 3"/><path d="M2.5 10v2.5a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V10"/></svg>`,
};

const kbd = (k, primary) => `<span class="kbd ${primary ? "on" : ""}">${k}</span>`;

// ---- shared ------------------------------------------------------------------------

function findReceipt(id) {
  return [...(state.receipts || []), ...(state.filedToday || [])].find((r) => r.id === id) || null;
}

function matches(r) {
  const q = m.query.trim().toLowerCase();
  return !q || [r.supplier, String(r.total ?? ""), r.reason, shortDate(r.date)].join(" ").toLowerCase().includes(q);
}

function undoToast(text, undo, label = "Undo") {
  m.undo = undo;
  toast(`<span>${esc(text)}</span>${undo ? ` <button class="btn small quiet" data-action="m-undo">${esc(label)}</button>` : ""}`, 8000);
}

function accountName(url) {
  const accounts = (state.snap.freeagent || {}).bank_accounts || [];
  if (url) { const a = accounts.find((x) => x.url === url); if (a) return a.name; }
  const chosen = accounts.find((a) => a.chosen);
  return chosen ? chosen.name : "Bank";
}

function searchHtml(placeholder) {
  return `<label class="m-search">${ICON.search}<input type="search" placeholder="${esc(placeholder)}" aria-label="Search"
    data-action="m-search" value="${esc(m.query)}"></label>`;
}

function rowButton(r, key, sel, note, action) {
  const on = action === "m-pick" && m.multi.length > 1 ? m.multi.includes(r.id) : r.id === sel;
  return `<button type="button" class="m-row k-${key} ${on ? "selected" : ""}" data-action="${action}" data-id="${r.id}"
      aria-pressed="${on}">
      <span class="ic">${ICON[key] || ICON.link}</span>
      <span class="txt"><span class="top"><span class="v">${esc(r.supplier || "Unknown supplier")}</span>${
        r.source === "gmail" ? `<span class="src-badge">Email</span>` : ""}<span class="a">${esc(amountOf(r))}</span></span>
        <span class="m-sub">${esc(r.date ? shortDate(r.date) : "No date")} · ${esc(note)}</span></span>
    </button>`;
}

function groupHtml(key, title, rows, sel, note, action, collapsible, hint) {
  if (!rows.length) return "";
  const open = !collapsible || m.open[key];
  const label = `${esc(title)} <span class="n">${rows.length}</span>`;
  return `<div class="m-group-head">${hint
      ? `<span class="m-tip" tabindex="0">${label}${ICON.info}<span class="m-tip-pop" role="tooltip">${esc(hint)}</span></span>`
      : `<span>${label}</span>`}
      ${collapsible ? `<button class="link" data-action="m-toggle" data-group="${key}">${open ? "Hide" : "Show"}</button>` : ""}</div>
    ${open ? rows.map((r) => rowButton(r, key, sel, note(r), action)).join("") : ""}`;
}

function docFigure(r) {
  // a photo opens as it arrived, before any tidying
  const original = r.source === "photo" ? "original" : "pdf";
  return `<figure class="m-figure">
      <div class="m-doc">${r.has_pdf ? "" : `<div class="m-nodoc">No document</div>`}</div>
      ${r.has_pdf ? `<figcaption><a href="/api/receipts/${r.id}/${original}?t=${encodeURIComponent(TOKEN)}" target="_blank" rel="noopener">Open original</a>${tidyNote(r)}</figcaption>` : ""}
    </figure>`;
}

/** Under a photo: how it was tidied, and a switch back to the plain copy.
 * When it wasn't tidied, why, on hover. */
function tidyNote(r) {
  const t = r.tidy;
  if (!t) return "";
  const locked = r.status === "filed";
  const why = [...t.notes, ...(t.dropped.length ? [`Background text cut away: ${t.dropped.join(", ")}`] : [])].join("\n");
  if (t.on) {
    return `<span class="m-tidy" title="${esc(why)}"> · Tidied: ${esc(t.steps.join(", "))}${locked ? "" :
      ` · <button class="link" data-action="m-tidy" data-id="${r.id}" data-on="0">Show plain photo</button>`}</span>`;
  }
  if (t.available) {
    return `<span class="m-tidy" title="${esc(why)}"> · Plain photo${locked ? "" :
      ` · <button class="link" data-action="m-tidy" data-id="${r.id}" data-on="1">Show tidied</button>`}</span>`;
  }
  return `<span class="m-tidy" title="${esc(why)}"> · Not tidied: it might have lost something</span>`;
}

const HL_LABEL = { supplier: "Supplier", total: "Total", date: "Date", vat_number: "VAT no." };

/** Boxes over the photo around what was read (supplier, total, date, VAT
 * number), positioned as fractions of the image so they scale with it. */
function highlightsHtml(r, page = 0) {
  return (r.highlights || []).filter((h) => (h.page || 0) === page).map((h) => {
    const [x, y, w, hh] = h.box;
    const pad = 0.006;
    return `<div class="hl hl-${esc(h.field)}" title="${esc(h.label)} read here" style="left:${(x - pad) * 100}%;top:${(y - pad) * 100}%;width:${(w + 2 * pad) * 100}%;height:${(hh + 2 * pad) * 100}%"><span>${esc(HL_LABEL[h.field] || h.label)}</span></div>`;
  }).join("");
}

function placeDocument(r, oldDoc) {
  const slot = document.querySelector(".m-doc");
  if (!slot || !r || !r.has_pdf) { m.docId = null; return; }
  const tidyOn = Boolean(r.tidy?.on);
  if (oldDoc && m.docId === r.id && m.docTidy === tidyOn) {
    slot.appendChild(oldDoc);
    // a correction moves its box
    oldDoc.querySelectorAll?.(".hl-layer").forEach((layer) => { layer.innerHTML = highlightsHtml(r, Number(layer.dataset.page || 0)); });
    return;
  }
  m.docTidy = tidyOn;
  const src = `/api/receipts/${r.id}/pdf?t=${encodeURIComponent(TOKEN)}${r.tidy ? `&v=${tidyOn ? "tidied" : "plain"}` : ""}`;
  if (!r.is_image && r.page_count) {
    // a PDF shown as its pages, so what was read can be boxed on them
    const pages = document.createElement("div");
    pages.className = "doc hl-pages";
    pages.innerHTML = Array.from({ length: r.page_count }, (_, i) => `<div class="hl-wrap"><img class="photo page" draggable="false"
        alt="Page ${i + 1} of the ${esc(r.supplier || "")} receipt" src="/api/receipts/${r.id}/page/${i + 1}?t=${encodeURIComponent(TOKEN)}">
        <div class="hl-layer" data-page="${i}">${highlightsHtml(r, i)}</div></div>`).join("");
    slot.appendChild(pages);
    m.docId = r.id;
    return;
  }
  if (r.is_image) {
    const wrap = document.createElement("div");
    wrap.className = "doc hl-wrap";
    wrap.innerHTML = `<img class="photo" draggable="false" alt="Photo of the ${esc(r.supplier || "")} receipt" src="${src}"><div class="hl-layer">${highlightsHtml(r)}</div>`;
    slot.appendChild(wrap);
  } else {
    const el = document.createElement("iframe");
    el.className = "doc";
    el.title = `${r.supplier} receipt`;
    el.src = src;
    slot.appendChild(el);
  }
  m.docId = r.id;
}

/** A photo or page, full screen, with its boxes: they're placed as fractions
 * of the image, so a copy scales with it. Click or Esc to close. */
function openLightbox(wrap) {
  closeLightbox();
  const box = document.createElement("div");
  box.className = "lightbox";
  box.setAttribute("role", "dialog");
  box.setAttribute("aria-modal", "true");
  box.setAttribute("aria-label", "Receipt, full screen. Click or press Escape to close.");
  const copy = wrap.cloneNode(true);
  copy.className = "hl-wrap";
  box.appendChild(copy);
  box.addEventListener("click", closeLightbox);
  document.body.appendChild(box);
}

function closeLightbox() { document.querySelector(".lightbox")?.remove(); }

document.addEventListener("click", (e) => {
  const wrap = e.target.closest?.(".m-doc .hl-wrap");
  if (wrap && e.target.matches("img.photo")) openLightbox(wrap);
});

// While it's open, keys belong to it: an arrow or a shortcut would change
// the file behind it without being seen.
document.addEventListener("keydown", (e) => {
  if (!document.querySelector(".lightbox")) return;
  e.stopPropagation();
  if (e.key === "Escape" || e.key === " " || e.key === "Enter") { e.preventDefault(); closeLightbox(); }
}, true);

/** The detail pane, keeping the document and the scroll across redraws. */
function drawSplit(head, list, detail, r, foot = "") {
  const content = $("#content");
  const queueTop = content.querySelector(".m-queue")?.scrollTop || 0;
  const detailTop = content.querySelector(".m-detail")?.scrollTop || 0;
  const oldDoc = content.querySelector(".m-doc .doc");
  content.innerHTML = `<div class="m-wrap ${m.dragging ? "dragging" : ""}">${head}
    <div class="m-body">
      ${foot ? `<div class="m-side"><section class="m-queue" aria-label="Files">${list}</section>${foot}</div>`
        : `<section class="m-queue" aria-label="Files">${list}</section>`}
      <section class="m-detail" aria-label="Selected file">${detail}</section>
    </div>${dropHtml()}</div>`;
  content.querySelector(".m-queue").scrollTop = queueTop;
  if (m.reveal) { m.reveal = false; content.querySelector(".m-row.selected")?.scrollIntoView({ block: "center" }); }
  content.querySelector(".m-detail").scrollTop = r && r.id === m.docId ? detailTop : 0;
  placeDocument(r, oldDoc);
  renderMatchModal();
}

function dropHtml() {
  const where = state.view === "expenses" ? "as expenses (paid personally)" : "as business receipts";
  return `<div class="f-drop" aria-hidden="true"><div>${ICON.upload}<b>Drop receipts here</b><span>Photos and PDFs, added ${where}</span></div></div>`;
}

function issuesHtml(r, extraTitle) {
  const flags = (r.flags || []);
  const notReceipt = (r.issues || []).includes("Not a receipt?");
  const title = notReceipt ? "This doesn't look like a receipt" : extraTitle || "Check this file";
  const body = notReceipt ? "No amount, date or VAT number found. Ignore it or enter the details." : "";
  const list = flags.filter((f) => !(notReceipt && f.startsWith("This doesn't look like a receipt")));
  return `<div class="m-issue"><div class="t">${esc(title)}</div>${body ? `<div class="b">${esc(body)}</div>` : ""}
    ${list.length ? `<ul class="m-flags">${list.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}</div>`;
}

function fieldsHtml(r) {
  const input = (field, type, value, attrs = "") =>
    `<input class="edit-input" type="${type}" data-action="set-field" data-field="${field}" data-id="${r.id}" value="${esc(value ?? "")}" ${attrs}>`;
  return `<div class="m-card"><div class="m-card-head"><span>The receipt</span>${r.ai_guess?.supplier && !(r.payment?.chips || []).includes("Name")
      ? `<span class="ai" title="Not confirmed yet. Check the name.">Supplier is a guess</span>` : ""}</div>
    <div class="m-kv">
      <span class="k">Supplier</span>${input("vendor", "text", r.supplier === "Unknown supplier" ? "" : r.supplier, 'placeholder="Who is it from?"')}
      <span class="k">Date</span>${undated(r) && r.photo_taken
        ? `<span class="pair">${input("purchased_on", "date", (r.date || "").slice(0, 10))}<button class="btn small" data-action="m-photo-date"
            data-id="${r.id}" data-day="${esc(r.photo_taken)}" title="The receipt shows no date">Use photo date (${esc(shortDate(r.photo_taken))})</button></span>`
        : input("purchased_on", "date", (r.date || "").slice(0, 10))}
      <span class="k">Total</span><span class="pair">${input("total", "text", r.total == null ? "" : Number(r.total).toFixed(2), 'inputmode="decimal" placeholder="0.00" autocomplete="off"')}
        ${currencyHtml(r)}</span>
    </div></div>`;
}

/** The receipt shows no date of its own (the date may be the photo's). */
function undated(r) {
  return !r.date || (r.flags || []).some((f) => /No date (on the receipt|found)/.test(f));
}

/** "Paid with": the business bank account (by its name) or an expense
 * claim. One click swaps; the file moves between Files and Expenses. */
// Currencies for the picker: the usual three first, then the rest by name.
const CURRENCIES = [["GBP", "British pound", "£"], ["EUR", "Euro", "€"], ["USD", "US dollar", "$"],
  ["AED", "UAE dirham"], ["ARS", "Argentine peso"], ["AUD", "Australian dollar", "$"], ["BRL", "Brazilian real"],
  ["CAD", "Canadian dollar", "$"], ["CHF", "Swiss franc"], ["CLP", "Chilean peso"], ["CNY", "Chinese yuan", "¥"],
  ["COP", "Colombian peso"], ["CZK", "Czech koruna"], ["DKK", "Danish krone", "kr"], ["HKD", "Hong Kong dollar", "$"],
  ["HUF", "Hungarian forint"], ["INR", "Indian rupee", "₹"], ["ISK", "Icelandic króna", "kr"], ["JPY", "Japanese yen", "¥"],
  ["KRW", "South Korean won", "₩"], ["MXN", "Mexican peso", "$"], ["NOK", "Norwegian krone", "kr"],
  ["NZD", "New Zealand dollar", "$"], ["PEN", "Peruvian sol"], ["PLN", "Polish złoty", "zł"], ["SEK", "Swedish krona", "kr"],
  ["SGD", "Singapore dollar", "$"], ["THB", "Thai baht", "฿"], ["TRY", "Turkish lira", "₺"], ["ZAR", "South African rand", "R"]];

/** Currency: a button that opens a searchable list. */
function currencyHtml(r) {
  const code = r.currency || "";
  const open = m.curOpen === r.id;
  const options = CURRENCIES.map(([c, name, sym]) => `<button type="button" class="cur-opt ${c === code ? "on" : ""}" data-action="cur-pick"
      data-id="${r.id}" data-code="${c}" data-search="${esc(`${c} ${name} ${sym || ""}`.toLowerCase())}">
      <b>${c}</b><span>${esc(name)}</span>${c === code ? ICON.tick : ""}</button>`).join("");
  return `<span class="cur">
      <button type="button" class="cur-btn ${code ? "" : "empty"}" data-action="cur-open" data-id="${r.id}" aria-haspopup="listbox"
        aria-expanded="${open}">${esc(code || "Currency")} <span aria-hidden="true">▾</span></button>
      ${open ? `<div class="cur-pop" role="listbox" aria-label="Currency"><input class="cur-q" type="search" placeholder="Search: euro, yen, USD…"
          data-action="cur-search" autocomplete="off"><div class="cur-list">${options}</div></div>` : ""}
    </span>`;
}

/** Who paid as saved: email receipts were always business unless you say
 * otherwise; a photo with no answer from the Shortcut is null (it asks). */
function savedPaid(r) {
  if (r.paid_by === "personal") return "personal";
  if (r.paid_by == null && r.source === "photo") return null;
  return "business";
}

function paidHtml(r) {
  const chosen = savedPaid(r);
  const personal = chosen === "personal";
  const unknown = chosen == null;
  const bank = accountName(r.bank_account);
  const option = (to, on, badge, cls, title, sub) => `<button type="button" class="pw-opt ${on ? "on" : ""}" data-action="m-paid"
      data-id="${r.id}" data-to="${to}" aria-pressed="${on}">
      ${badge.startsWith("<span") ? badge : `<span class="badge ${cls}">${badge.startsWith("<svg") ? badge : esc(badge)}</span>`}<span class="who"><b>${esc(title)}</b><span>${esc(sub)}</span></span>
      ${on ? `<span class="tick">${ICON.tick}</span>` : ""}</button>`;
  return `<div class="m-card"><div class="m-card-head"><span>Paid with</span>${unknown ? `<span class="ai">Choose one</span>`
    : `<span class="muted pw-key">${kbd("E")} to switch</span>`}</div>
    <div class="pw" role="group" aria-label="Paid with">
      ${option("business", !personal && !unknown, bankBadge(bank), "", bank, "Business account · linked in Bank Feed")}
      ${option("personal", personal, ICON.cash, "personal", "Expense", "Paid personally · claimed back")}
    </div></div>`;
}

function categoryOptions(selected) {
  const groups = {};
  for (const c of state.categories || []) (groups[c.group] ||= []).push(c);
  const label = { admin_expenses_categories: "Admin expenses", cost_of_sales_categories: "Cost of sales", general_categories: "General" };
  return `<option value="">Choose…</option>${Object.entries(groups).map(([gname, list]) => `<optgroup label="${esc(label[gname] || gname)}">${
    list.map((c) => `<option value="${esc(c.url)}" ${c.url === selected ? "selected" : ""}>${esc(c.description)}</option>`).join("")}</optgroup>`).join("")}`;
}

function categoryName(url) { return (state.categories || []).find((c) => c.url === url)?.description || ""; }

function categoryHtml(r, editable) {
  if (!editable) return esc(r.will_file?.category || "—");
  return `<span class="pair"><select data-action="set-category" data-id="${r.id}" aria-label="Category">${categoryOptions(r.category)}</select>${
    r.category_from_freeagent ? `<span class="fa-tag" title="Set in FreeAgent. Choosing another changes it there.">From FreeAgent</span>`
    : r.freeagent_category && r.payment?.explained ? `<span class="ai" title="Saving updates FreeAgent's category. Undo restores it.">Overrides FreeAgent</span>`
    : r.ai_guess?.category ? `<span class="ai" title="Suggested from the supplier and receipt. Change it if it's wrong.">Guess</span>` : ""}</span>`;
}

function willFileHtml(r, type) {
  const fa = state.snap.freeagent || {};
  const wf = r.will_file || {};
  // FreeAgent already explained the payment: its VAT stays as it is
  const explained = !type && r.payment && r.payment.explained;
  const vatRow = explained ? `<span class="muted">Kept as in FreeAgent</span>` : fa.vat?.registered
    ? `<span class="pair">${vatMenuHtml(r)}<span class="muted">${esc(wf.vat || "")}</span></span>`
    : esc(wf.vat || "—");
  return `<div class="m-kv">
      <span class="k">Saved as</span><span>${esc(type || wf.type || "—")}</span>
      <span class="k">Category</span>${categoryHtml(r, fa.connected)}
      <span class="k">VAT</span><span>${vatRow}</span>
      ${rebillHtml(r)}
    </div>`;
}

/** "Project": link to a client's project in FreeAgent. Once linked, choose
 * whether to re-bill it: not at all, at cost, with a markup, or at a set price. */
function rebillHtml(r) { return rebillFields(r.rebill || null, `data-id="${r.id}"`); }

/** `tag`: shown beside the project, e.g. "Changed" on a payment card. */
function rebillFields(rb, key, tag = "") {
  const fa = state.snap.freeagent || {};
  const projects = fa.projects || [];
  if (!fa.connected) return "";
  if (!projects.length) {
    return `<span class="k">Project</span><span class="muted">No active projects in FreeAgent.</span>`;
  }
  const select = `<select data-action="rb-project" ${key} aria-label="Link to project">
      <option value="">No project</option>${projects.map((p) => `<option value="${esc(p.url)}" ${rb?.project === p.url ? "selected" : ""}>${
        esc(p.client ? `${p.client}: ${p.name}` : p.name)}</option>`).join("")}</select>`;
  if (!rb) return `<span class="k">Project</span><span class="pair">${select}${tag}</span>`;
  const kind = rb.type || "cost";
  const kinds = [["none", "Don't re-bill"], ["cost", "Re-bill at cost"], ["markup", "Re-bill with markup"], ["price", "Re-bill at a set price"]];
  const how = `<select data-action="rb-kind" ${key} aria-label="How to re-bill">${kinds.map(([k, label]) =>
      `<option value="${k}" ${kind === k ? "selected" : ""}>${label}</option>`).join("")}</select>`;
  const factor = kind === "cost" || kind === "none" ? "" : `<label class="rb-factor">${kind === "price" ? "£" : ""}<input type="text" inputmode="decimal"
      data-action="rb-factor" ${key} value="${esc(rb.factor ?? "")}" placeholder="${kind === "price" ? "0.00" : "10"}"
      aria-label="${kind === "price" ? "Price to charge" : "Markup percent"}">${kind === "markup" ? "%" : ""}</label>`;
  return `<span class="k">Project</span><span class="pair">${select}${tag}</span>
    <span class="k">Re-bill</span><span class="pair">${how}${factor}</span>`;
}

/** Re-billing changed on a receipt (data-id) or on a payment (data-url). */
function saveRebill(el, change) {
  const url = el.dataset.url;
  const r = url ? null : findReceipt(Number(el.dataset.id));
  const now = (url ? stPayment(url)?.settings?.rebill : r?.rebill) || { type: "none", factor: null };
  const next = change.project === null ? null : { ...now, ...change };
  if (next && (next.type === "cost" || next.type === "none")) next.factor = null;
  if (url) return savePayment(url, { rebill: next });
  return r && act(() => api(`/api/receipts/${r.id}/fields`, { method: "POST", body: { rebill: next } }));
}

function stPayment(url) { return (state.statement?.rows || []).find((t) => t.url === url) || null; }

function savePayment(url, changes) {
  return act(() => api("/api/statement/payment", { method: "POST", body: { url, changes } }));
}

function chipsHtml(p) {
  const warn = (c) => c.startsWith("+£") || (p.far && /day/.test(c));
  return [...(p.chips || []).map((c) => `<span class="m-chip ${warn(c) ? "warn" : "good"}">${warn(c) ? "" : ICON.tick}${esc(c)}</span>`),
    ...(p.pinned ? [`<span class="m-chip">You chose this</span>`] : [])].join("");
}

function lastFilingHtml(r) {
  const f = r.filing;
  if (!f) return "";
  if (f.state === "problem") return `<div class="m-note warn">${esc(f.message)}</div>`;
  if (f.state === "filing" || f.state === "explained") {
    return `<div class="m-note warn">Saving to FreeAgent was interrupted. Retrying won't create a duplicate.</div>`;
  }
  if (f.state === "dry_run") {
    const parts = f.parts && f.parts.length > 1 ? f.parts : f.body;
    return `<details class="m-dry"><summary>Last dry run: what would be sent</summary><pre class="selectable">${
      esc(JSON.stringify(parts, null, 2))}${f.attachment ? `\n\nattachment: ${esc(f.attachment.file_name)}` : ""}</pre></details>`;
  }
  return "";
}

/** "Link {supplier} automatically from now on": your approval for this
 * supplier, saved as soon as it's ticked; unticked by default. */
function learnHtml(r) {
  if (r.paid_by === "personal" || !r.supplier || r.supplier === "Unknown supplier") return "";
  return `<label class="m-learn" title="Exact matches with nothing to check are linked and saved automatically">
    <input type="checkbox" data-action="m-learn" data-id="${r.id}" ${r.auto_file ? "checked" : ""}>
    <span>Auto-link ${esc(r.supplier)} from now on</span></label>`;
}

/** Why this receipt can't be saved to FreeAgent yet, or "". */
function fileBlocker(r) {
  if (r.total == null) return "Enter the total first";
  if (r.stage === "check" && r.paid_by !== "personal") return "Check the file first";
  if (r.paid_by !== "personal" && !r.payment) return "No payment yet";
  if (!r.category && !(r.payment && r.payment.explained)) return "Choose a category";
  if (r.rebill && ["markup", "price"].includes(r.rebill.type) && !r.rebill.factor) return r.rebill.type === "markup" ? "Enter the markup %" : "Enter the re-bill price";
  return "";
}

async function fileOne(r, next) {
  if (fileBlocker(r)) return;
  const fa = state.snap.freeagent || {};
  // an expense still to check is checked by filing it: one press, straight to FreeAgent
  if (r.stage === "check") await api(`/api/receipts/${r.id}/fields`, { method: "POST", body: { checked: true } });
  await api("/api/receipts/file", { method: "POST", body: { ids: [r.id] } });
  if (next) next();
  const what = r.paid_by === "personal" ? "Expense" : "Linked";
  if (fa.dry_run) undoToast(`Dry run for ${r.supplier}: nothing sent`, null);
  else undoToast(`${what}: ${r.supplier} ${amountOf(r)} saved to FreeAgent`, () => api(`/api/receipts/${r.id}/unfile`, { method: "POST" }));
}

async function setPaid(r, to) {
  const from = r.paid_by || null;
  if (from === to) return;
  await api(`/api/receipts/${r.id}/fields`, { method: "POST", body: { paid_by: to } });
  undoToast(to === "personal" ? `${r.supplier} is now an expense` : `${r.supplier}: paid from ${accountName()}`,
    () => api(`/api/receipts/${r.id}/fields`, { method: "POST", body: { paid_by: from } }));
}

async function ignoreMany(ids) {
  const rows = ids.map(findReceipt).filter(Boolean);
  if (!rows.length) return;
  await api("/api/receipts/status", { method: "POST", body: { ids: rows.map((x) => x.id), status: "ignored" } });
  m.multi = [];
  m.sel = nextIn(fileRows(fileGroups()).filter((x) => !ids.includes(x.id)), -1);
  state.receipts = (state.receipts || []).filter((x) => !ids.includes(x.id));
  render();
  undoToast(`Ignored ${rows.length} files`,
    () => api("/api/receipts/status", { method: "POST", body: { ids: rows.map((x) => x.id), status: "pending" } }));
}

async function ignoreOne(r, next) {
  await api("/api/receipts/status", { method: "POST", body: { ids: [r.id], status: "ignored" } });
  if (next) next();
  state.receipts = (state.receipts || []).filter((x) => x.id !== r.id);   // gone at once, not after the reload
  render();
  undoToast(`Ignored ${r.supplier} ${amountOf(r)}`,
    () => api("/api/receipts/status", { method: "POST", body: { ids: [r.id], status: "pending" } }));
}

function nextIn(rows, id) {
  const i = rows.findIndex((r) => r.id === id);
  const rest = rows.filter((r) => r.id !== id);
  return (i < 0 ? rest[0] : rest[Math.min(i, rest.length - 1)])?.id ?? null;
}

// ---- Files ---------------------------------------------------------------------------

function fileGroups() {
  const all = (state.receipts || []).filter(matches);
  return {
    check: all.filter((r) => r.stage === "check"),
    link: all.filter((r) => r.stage === "link"),
    expense: all.filter((r) => r.stage === "expense"),
    waiting: all.filter((r) => r.stage === "waiting"),
  };
}

function fileRows(g) { return [...g.check, ...g.link, ...g.expense, ...(m.open.waiting ? g.waiting : [])]; }

function fileNote(r) {
  if (r.stage === "check") return (r.issues || [])[0] || "Check it";
  if (r.stage === "expense") return fileBlocker(r) || r.category_name || "Ready to claim";
  if (r.stage === "link") {
    if (r.payment) return `Suggested: ${r.payment.description} · ${shortDate(r.payment.date)}`;
    return r.match?.status === "choose" ? "Several matches. Pick in Bank Feed" : "Close match. Review in Bank Feed";
  }
  return "No payment yet";
}

function renderFiles() {
  const g = fileGroups();
  const all = [...g.check, ...g.link, ...g.expense, ...g.waiting];
  if (!all.some((r) => r.id === m.sel)) m.sel = (g.check[0] || g.link[0] || g.expense[0] || null)?.id ?? null;
  const sel = all.find((r) => r.id === m.sel) || null;
  if (sel?.stage === "waiting") m.open.waiting = true;

  const parts = [];
  if (g.check.length) parts.push(`${g.check.length} to check`);
  if (g.link.length) parts.push(`${g.link.length} ready to link`);
  if (g.expense.length) parts.push(`${g.expense.length} expense${g.expense.length === 1 ? "" : "s"} to claim`);
  if (g.waiting.length) parts.push(`${g.waiting.length} waiting for a payment`);
  const head = `<header class="m-head">
      <div class="m-title"><span>Files</span><span class="sub">${esc(parts.join(" · ") || "Receipts not yet in FreeAgent")}</span></div>
      ${searchHtml("Supplier or amount")}
      <button class="btn small" data-action="m-add" data-paid="business">Add files…</button>
      ${saveAllHtml(g)}
    </header>`;
  const list = groupHtml("check", "To check", g.check, m.sel, fileNote, "m-pick", false)
    + groupHtml("link", "Ready to link", g.link, m.sel, fileNote, "m-pick", false)
    + groupHtml("expense", "Expenses to claim", g.expense, m.sel, fileNote, "m-pick", false)
    + groupHtml("waiting", "Waiting for a payment", g.waiting, m.sel, fileNote, "m-pick", true,
      "Filed and waiting to be linked to a bank transaction in the feed")
    || `<div class="m-none">${m.query ? `No files match “${esc(m.query)}”.` : "No files waiting."}</div>`;
  // in a footer under the list, not in it: for the whole list, and always in view
  const dropZone = `<button class="m-dropzone" data-action="m-add" data-paid="business">${ICON.upload}
      <span><b>Drop receipts here</b><span>Photos and PDFs, or click to choose</span></span></button>`;
  m.multi = m.multi.filter((id) => all.some((r) => r.id === id));
  const picked = m.multi.length > 1 ? all.filter((r) => m.multi.includes(r.id)) : [];
  const detail = picked.length ? bulkHtml(picked) : sel ? fileDetailHtml(sel) : `<div class="m-empty">${ICON.upload}<div class="big">Nothing to check</div>
      <div>New receipts appear here.</div></div>`;
  drawSplit(head, list, detail, picked.length ? null : sel, `<div class="m-qfoot">${dropZone}</div>`);
}

/** Several files picked (shift-click a range, ⌘-click to add or remove one):
 * the same action for all of them. */
function bulkHtml(rows) {
  const gbp = rows.filter((r) => r.currency === "GBP").reduce((t, r) => t + Number(r.total || 0), 0);
  return `<div class="m-dhead"><h1>${rows.length} files selected</h1><span class="grow"></span>
      <span class="amt">${gbp ? esc(money(gbp, "GBP")) : ""}</span></div>
    <div class="m-dsub">Shift-click selects a range, ⌘-click adds or removes. Esc clears.</div>
    <div class="m-bulk">
      <div class="m-card"><div class="m-bulk-list">${rows.map((r) => `<div class="m-bulk-row">
          <span class="v">${esc(r.supplier || "Unknown supplier")}</span>
          <span class="muted">${esc(r.date ? shortDate(r.date) : "No date")}</span>
          <span class="a">${esc(amountOf(r))}</span></div>`).join("")}</div></div>
      <div class="m-card"><div class="m-card-head"><span>Paid with</span></div>
        <div class="m-actions">
          <button class="btn" data-action="m-bulk-paid" data-to="business">${esc(accountName())}</button>
          <button class="btn" data-action="m-bulk-paid" data-to="personal">Expense (paid personally)</button></div></div>
      <div class="m-actions">
        <button class="btn danger" data-action="m-bulk-ignore">Ignore ${rows.length} ${kbd("⌫")}</button>
        <button class="btn" data-action="m-bulk-clear">Clear selection</button></div>
    </div>`;
}

/** "Save N expenses": every expense in Files with nothing missing. */
function saveAllHtml(g) {
  const fa = state.snap.freeagent || {};
  const ready = g.expense.filter((r) => !fileBlocker(r));
  if (!fa.connected || ready.length < 2) return "";
  const busy = state.snap.queued.some((q) => q.startsWith("file:"));
  return `<button class="btn primary small" data-action="m-file-all" data-kind="expense" ${busy ? "disabled" : ""}>${fa.dry_run ? "Dry run" : "Save"} ${ready.length} expenses</button>`;
}

const STAGE_CHIP = { check: ["To check", "warn"], link: ["Ready to link", "info"], waiting: ["Waiting for a payment", "quiet"],
  expense: ["Expense to claim", "info"], filed: ["Saved to FreeAgent", "good"],
  ignored: ["Ignored", "quiet"], failed: ["Couldn't be read", "quiet"] };

function headHtml(r) {
  const [label, kind] = STAGE_CHIP[r.stage] || STAGE_CHIP.check;
  const source = { photo: "Photo", gmail: "Email" }[r.source] || "File";
  return `<div class="m-dhead">
      <h1>${esc(r.supplier || "Unknown supplier")}</h1>
      <span class="chip ${kind}">${esc(label)}</span>
      <span class="grow"></span>
      <span class="amt">${esc(amountOf(r))}</span>
    </div>
    <div class="m-dsub">${esc(r.date ? longDate(r.date) : "No date on receipt")} · ${esc(source)}</div>`;
}

function fileDetailHtml(r) {
  let payment = "";
  if (r.stage === "link" && r.payment) {
    const p = r.payment;
    payment = `<div class="m-card"><div class="m-card-head"><span>Suggested payment</span></div>
      <div class="m-pay">${bankBadge(accountName())}
        <div class="who"><div class="desc">${esc(p.description)}</div><div class="sub">${esc(accountName())} · ${esc(longDate(p.date))}</div></div>
        <span class="amt">${esc(money(p.amount, "GBP"))}</span></div>
      <div class="m-chips">${chipsHtml(p)}</div>
      ${willFileHtml(r)}
      ${fileBlocker(r) ? `<div class="m-note warn st-why">${esc(fileBlocker(r))} before linking.</div>` : ""}</div>
      ${learnHtml(r)}`;
  } else if (r.stage === "link") {
    payment = `<div class="m-wait"><div class="t">${r.match?.status === "choose" ? "Which day was this?" : `No payment of exactly ${esc(amountOf(r))}`}</div>
      <div class="b">${r.match?.status === "choose" ? `${(r.options || []).length} payments match. Pick one in Bank Feed.`
        : "A slightly larger payment came through (a tip?). Link it in Bank Feed if it's this one."}</div></div>`;
  } else if (r.stage === "waiting") {
    const until = r.waiting_until ? shortDate(r.waiting_until) : "";
    payment = `<div class="m-wait"><div class="t">No ${esc(amountOf(r))} payment yet</div>
      <div class="b">Checked on each FreeAgent sync.${until ? ` Overdue from ${esc(until)}.` : ""} Or link it manually in Bank Feed.</div></div>`;
  }
  const canOk = r.stage === "check" && r.total != null;
  const fa = state.snap.freeagent || {};
  const personal = savedPaid(r) === "personal";
  if (personal) payment = expenseExtrasHtml(r) + expenseCardHtml(r);
  const blocker = fileBlocker(r);
  const primary = personal ? (!fa.connected ? `<span class="m-note">Connect FreeAgent in Settings to claim.</span>`
      : blocker ? `<button class="btn" disabled>${esc(blocker)}</button>`
      : `<button class="btn primary" data-action="x-file" data-id="${r.id}">File to expenses${fa.dry_run ? " (dry run)" : ""} ${kbd("⏎", true)}</button>`)
    : canOk ? `<button class="btn primary" data-action="m-checked" data-id="${r.id}">Looks right ${kbd("⏎", true)}</button>`
    : r.stage === "link"
    ? `<button class="btn primary" data-action="m-to-statement" data-id="${r.id}">Link in Bank Feed ${kbd("⏎", true)}</button>`
    : r.stage === "waiting" ? `<button class="btn" data-action="m-to-statement" data-id="${r.id}">Find its payment</button>` : "";
  return `${headHtml(r)}
    <div class="m-dgrid">${docFigure(r)}
      <div class="m-inspector m-keep">
        ${r.stage === "check" ? issuesHtml(r) : ""}
        ${paidHtml(r)}
        ${fieldsHtml(r)}
        ${payment}
        ${lastFilingHtml(r)}
        <div class="m-actions">${primary}
          <button class="btn" data-action="m-ignore" data-id="${r.id}">Ignore ${kbd("⌫")}</button></div>
      </div>
    </div>`;
}

// ---- Archived: ignored, and emails that couldn't be read -----------------------------------

function renderArchived() {
  const all = (state.receipts || []).filter(matches);
  const ignored = all.filter((r) => r.status === "ignored");
  const failed = all.filter((r) => r.status === "failed");
  if (!all.some((r) => r.id === m.asel)) m.asel = all[0]?.id ?? null;
  const sel = all.find((r) => r.id === m.asel) || null;
  const head = `<header class="m-head">
      <div class="m-title"><span>Archived</span><span class="sub">Ignored files and unreadable emails. Nothing to do here.</span></div>
      ${searchHtml("Supplier or amount")}
      ${(state.receipts || []).length ? `<button class="btn small danger" data-action="a-clear">Clear archive</button>` : ""}
    </header>`;
  const note = (r) => r.status === "failed" ? (r.error || "Couldn't be read") : "Ignored";
  const list = groupHtml("ignored", "Ignored", ignored, m.asel, note, "a-pick", false)
    + groupHtml("failed", "Couldn't be read", failed, m.asel, note, "a-pick", false)
    || `<div class="m-none">${m.query ? `Nothing matches “${esc(m.query)}”.` : "Nothing archived."}</div>`;
  const detail = sel ? `${headHtml({ ...sel, stage: sel.status })}
    <div class="m-dgrid">${docFigure(sel)}
      <div class="m-inspector">
        <div class="m-wait"><div class="t">${sel.status === "failed" ? (sel.source === "photo" ? "This photo couldn't be read" : "This email couldn't be read") : "You ignored this"}</div>
          <div class="b">${esc(sel.status === "failed" ? (sel.error || "") : "It won't be linked or claimed unless you restore it.")}</div></div>
        <div class="m-actions">
          <button class="btn primary" data-action="a-restore" data-id="${sel.id}">Back to Files</button>
          ${sel.status === "failed" ? `<button class="btn" data-action="a-retry" data-id="${sel.id}">Read again</button>` : ""}
          <button class="btn danger" data-action="a-delete" data-id="${sel.id}">Delete</button></div>
      </div></div>` : `<div class="m-empty"><div class="big">Nothing archived</div></div>`;
  drawSplit(head, list, detail, sel);
}

// ---- Expenses: the claims already saved to FreeAgent, a month at a time ---------------
// Nothing to do here: expenses are checked and claimed in Files.

function claimMonth(r) { return (r.date || r.photo_taken || new Date().toISOString()).slice(0, 7); }

function claimStatus(r) {
  if (r.status === "filed") return "saved";
  return (r.issues || []).length || fileBlocker(r) ? "check" : "ready";
}

/** Saved claims (loaded only on the Expenses page). */
function allClaims() {
  return (state.filedToday || []).filter(matches);
}

/** The month shown: the one chosen, else the latest with a claim. */
function xMonth() {
  if (state.xMonth) return state.xMonth;
  const months = allClaims().map(claimMonth).sort();
  return months.length ? months[months.length - 1] : new Date().toISOString().slice(0, 7);
}

function xRows() {
  const month = xMonth();
  return allClaims().filter((r) => claimMonth(r) === month)
    .sort((a, b) => (a.date || "9999").localeCompare(b.date || "9999") || a.id - b.id);
}

function gbpOf(r) {
  if (r.currency === "GBP") return Number(r.total || 0);
  return r.native_gross != null ? Number(r.native_gross) : 0;
}

function renderExpenses() {
  const content = $("#content");
  const top = content.querySelector(".st-wrap")?.scrollTop || 0;
  const panelTop = content.querySelector(".st-panel")?.scrollTop || 0;
  const oldDoc = content.querySelector(".m-doc .doc");
  const month = xMonth();
  const rows = xRows();
  if (!rows.some((r) => r.id === m.xsel)) m.xsel = null;
  const sel = rows.find((r) => r.id === m.xsel) || null;
  const claimed = rows.reduce((t, r) => t + gbpOf(r), 0);
  const unpriced = rows.filter((r) => r.currency !== "GBP" && r.native_gross == null).length;

  content.innerHTML = `<div class="m-wrap"><div class="st-split ${sel ? "with-panel" : ""}"><div class="st-wrap">
    <header class="m-head">
      <div class="m-title"><span>Expenses</span>
        <span class="sub">Claims saved to FreeAgent. Add new ones in Files.</span></div>
      ${searchHtml("Supplier or amount")}
      <div class="st-month">
        <button class="btn small" data-action="x-month" data-delta="-1" aria-label="Previous month">‹</button>
        <span>${esc(monthLabel(month))}</span>
        <button class="btn small" data-action="x-month" data-delta="1" aria-label="Next month">›</button></div>
    </header>
    <div class="st-body">
      <section class="st-summary">
        <div class="main"><div class="big">${rows.length ? `Claimed in ${esc(monthLabel(month))}` : `Nothing claimed in ${esc(monthLabel(month))}`}</div></div>
        <div class="money"><span class="muted">Total</span><b>${esc(money(claimed, "GBP"))}${unpriced ? `<span class="muted small"> + ${unpriced} not in pounds</span>` : ""}</b></div>
      </section>
      <div class="st-table-box"><table class="st-table">
        <thead><tr><th scope="col">Date</th><th scope="col">Supplier</th><th scope="col" class="r">Amount</th><th scope="col">Category</th></tr></thead>
        <tbody>${rows.map(claimRow).join("") || `<tr><td colspan="4" class="m-none">${m.query ? `No claims match “${esc(m.query)}”.`
          : `No expenses claimed in ${esc(monthLabel(month))}.`}</td></tr>`}</tbody>
      </table></div>
    </div></div>
    ${sel ? `<aside class="st-panel m-keep" aria-label="Selected expense">${claimPanelHtml(sel)}</aside>` : ""}</div></div>`;
  content.querySelector(".st-wrap").scrollTop = top;
  if (m.reveal) { m.reveal = false; content.querySelector("tr.selected")?.scrollIntoView({ block: "center" }); }
  const panel = content.querySelector(".st-panel");
  if (panel) panel.scrollTop = sel && sel.id === m.docId ? panelTop : 0;
  placeDocument(sel, oldDoc);
  renderMatchModal();
}

function claimRow(r) {
  let detail = r.category_name || "";
  if (r.currency && r.currency !== "GBP") detail += `${detail ? " · " : ""}${r.native_gross != null ? `£${Number(r.native_gross).toFixed(2)} charged` : `in ${CURRENCY_WORDS[r.currency] || r.currency}`}`;
  return `<tr class="filed ${r.id === m.xsel ? "selected" : ""}" data-action="x-sel" data-id="${r.id}">
    <td class="d">${esc(r.date ? statementDate(r.date.slice(0, 10)) : "No date")}</td>
    <td>${esc(r.supplier || "Unknown supplier")}</td>
    <td class="r amt">${esc(amountOf(r))}</td>
    <td><div class="st-rec"><span class="st-status filed">${ICON.filed}<span>Claimed</span></span>
      <span class="muted">${esc(detail)}</span></div></td></tr>`;
}

/** An expense has no bank payment to take its category from, so category,
 * VAT and re-billing are all set here, by hand. */
function expenseCardHtml(r) {
  return `<div class="m-card"><div class="m-card-head"><span>Expense claim</span></div>${willFileHtml(r, "Expense claim")}</div>`;
}

/** Above the claim: a business payment that matches exactly, and the £
 * charged for an expense in another currency. */
function expenseExtrasHtml(r) {
  const business = r.match?.status === "expense_but_found"
    ? `<div class="m-issue"><div class="t">Paid from ${esc(accountName())}?</div><div class="b">A ${esc(amountOf(r))} business payment matches exactly.</div>
        <div class="m-iss-ctl"><button class="btn small" data-action="m-paid" data-id="${r.id}" data-to="business">Yes: paid from ${esc(accountName())}</button></div></div>` : "";
  const gbp = r.currency && r.currency !== "GBP" ? `<div class="m-card"><div class="m-card-head"><span>Paid in ${esc(CURRENCY_WORDS[r.currency] || r.currency)}</span></div>
      <label class="m-gbp"><span>£ charged</span><input type="number" step="0.01" min="0" placeholder="Optional"
        data-action="set-field" data-field="native_gross" data-id="${r.id}" value="${esc(r.native_gross ?? "")}"></label>
      <div class="m-note">From your card statement. Leave blank to let FreeAgent convert it.</div></div>` : "";
  return business + gbp;
}

function claimPanelHtml(r) {
  const fa = state.snap.freeagent || {};
  return `<div class="st-phead"><div><div class="st-pdesc">${esc(r.supplier || "Unknown supplier")}</div>
      <div class="muted">${esc(r.date ? longDate(r.date) : "No date on receipt")} · ${esc({ photo: "Photo", gmail: "Email" }[r.source] || "File")}</div></div>
      <span class="amt">${esc(amountOf(r))}</span>
      <button class="link" data-action="x-close" aria-label="Close">✕</button></div>
    <div class="st-pbody">${m.xedit === r.id ? claimEditHtml(r) : `
      <div class="m-wait"><div class="t">Claimed</div><div class="b">Saved to FreeAgent as an expense${r.category_name ? ` (${esc(r.category_name)})` : ""}. Nothing more to do.</div></div>
      ${claimUpdateNote(r)}
      ${docFigure(r)}
      <div class="m-actions">${fa.connected ? `<button class="btn" data-action="x-edit" data-id="${r.id}">Edit claim</button>` : ""}
        <button class="btn" data-action="m-unfile" data-id="${r.id}">Undo claim</button>
        ${fa.web ? `<a class="btn" href="${esc(fa.web)}" target="_blank" rel="noopener">View in FreeAgent</a>` : ""}</div>`}</div>`;
}

/** Editing a claim: the same details as in Files, saved here as you go;
 * "Update FreeAgent" changes the expense already there, in place. */
function claimEditHtml(r) {
  const fa = state.snap.freeagent || {};
  return `<div class="m-note">Changes save here as you go. Click <b>Update FreeAgent</b> to send them.</div>
    ${fieldsHtml(r)}
    ${expenseCardHtml(r)}
    ${claimUpdateNote(r)}
    <div class="m-actions"><button class="btn primary" data-action="x-update" data-id="${r.id}">Update FreeAgent${fa.dry_run ? " (dry run)" : ""}</button>
      <button class="btn" data-action="x-edit" data-id="">Done</button></div>
    ${docFigure(r)}`;
}

function claimUpdateNote(r) {
  const u = r.filing?.update;
  if (!u) return "";
  return `<div class="m-note">${u.state === "dry_run" ? "Dry run: nothing sent." : `Updated in FreeAgent ${esc(shortDate(u.at))}.`}</div>`;
}

// ---- Statement: where files get linked ---------------------------------------------------

function monthLabel(month) {
  return new Date(month + "-15T12:00:00").toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}

function statementDate(iso) {
  return new Date(iso + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

function suggestedFile(t) {
  return t.status === "in_match" && t.receipt ? findReceipt(t.receipt.id) : null;
}

/** What each Statement line (all outgoing) counts as: still to do (missing
 * a receipt, or a file suggested), or done (linked, or no receipt needed). */
function stKind(t) {
  // approved in FreeAgent counts as done, with or without a receipt
  if (t.status === "filed" || t.status === "not_needed" || t.status === "approved" || t.approved) return "done";
  return t.status;                       // "missing" | "in_match"
}

const ST_FILTERS = ["all", "missing", "in_match", "done"];

function stRows() {
  const st = state.statement || {};
  if (!ST_FILTERS.includes(state.stFilter)) state.stFilter = "all";
  const filter = state.stFilter;
  return (st.rows || []).filter((r) => filter === "all" || stKind(r) === filter);
}

/** "Which bank feed is your business account?": FreeAgent's bank accounts,
 * business ones first; one click chooses it (more can be added in Settings). */
function chooseFeedHtml(fa) {
  const accounts = [...(fa.bank_accounts || [])].sort((a, b) => Number(a.is_personal) - Number(b.is_personal));
  if (!accounts.length) {
    return `<div class="m-empty"><div class="big">${fa.last_sync ? "No bank accounts in FreeAgent" : "Reading your bank accounts…"}</div>
      <div>${fa.last_sync ? "Add your bank feed in FreeAgent, then press Check now." : "From FreeAgent. This takes a few seconds."}</div></div>`;
  }
  const kind = (a) => [a.currency, a.type === "CreditCardAccount" ? "credit card" : a.type === "PaypalAccount" ? "PayPal" : "",
    a.is_personal ? "personal" : ""].filter(Boolean).join(" · ");
  return `<div class="feed-ask"><section class="feed-card" aria-labelledby="feed-q">
      <h2 id="feed-q">Which bank feed is your business account?</h2>
      <p class="sub">Receipts are matched to its payments. FreeAgent isn't changed.</p>
      <div class="feed-list">${accounts.map((a) => `<button type="button" class="feed-opt" data-action="st-feed" data-url="${esc(a.url)}">
          ${bankBadge(a.name)}
          <span class="who"><b>${esc(a.name || "Bank account")}</b><span>${esc(kind(a))}</span></span>
          <span class="go">Use this</span></button>`).join("")}</div>
      <p class="sub small">More than one? Pick one now and add the others in Settings → FreeAgent.</p>
    </section></div>`;
}

function renderStatement() {
  const content = $("#content");
  const st = state.statement;
  const fa = state.snap.freeagent || {};
  const top = content.querySelector(".st-wrap")?.scrollTop || 0;
  const panelTop = content.querySelector(".st-panel")?.scrollTop || 0;
  const oldDoc = content.querySelector(".m-doc .doc");
  // Connected but no bank feed chosen: ask which one, right here.
  if (fa.connected && !(fa.bank_accounts || []).some((a) => a.chosen)) {
    content.innerHTML = chooseFeedHtml(fa);
    return;
  }
  // Signed out (an expired sign-in) still shows the statement as last read.
  if ((!fa.connected && !st?.account) || (st && !st.account)) {
    content.innerHTML = `<div class="m-empty"><div class="big">No statement yet</div>
      <div>Connect FreeAgent and tick a business bank account in Settings.</div></div>`;
    return;
  }
  if (!st) { content.innerHTML = `<div class="m-empty"><div>Reading the statement…</div></div>`; return; }
  const s = st.summary || {};
  const rows = stRows();
  const filter = state.stFilter;
  if (m.stPending) {                       // came from Files: open that file's payment
    const t = (st.rows || []).find((x) => x.receipt?.id === m.stPending);
    if (t) m.stSel = t.url;
    m.stPending = null;
  }
  if (m.stSel && !(st.rows || []).some((t) => t.url === m.stSel)) m.stSel = null;
  const sel = (st.rows || []).find((t) => t.url === m.stSel) || null;
  const count = (k) => (st.rows || []).filter((r) => stKind(r) === k).length;
  const missing = count("missing"), suggested = count("in_match"), done = count("done");
  const payments = missing + suggested + done;
  const todo = missing + suggested;
  // one row that is both the key to the bar and the filter
  const pill = (k, label, n, dot) => `<button class="st-pill ${filter === k ? "on" : ""}" data-action="st-filter" data-filter="${k}"
      aria-pressed="${filter === k}">${dot ? `<i class="${dot}"></i>` : ""}<span>${label}</span><b>${n}</b></button>`;
  const bar = payments
    ? `<div class="st-bar" role="img" aria-label="${done} done, ${suggested} with a suggested file, ${missing} missing a receipt">
        <span class="seg-done" style="flex:${done}"></span><span class="seg-sugg" style="flex:${suggested}"></span><span class="seg-miss" style="flex:${missing}"></span></div>`
    : "";
  const ready = (st.rows || []).map(suggestedFile).filter((r) => r && !fileBlocker(r) && r.group === "ready");
  const busy = state.snap.queued.some((q) => q.startsWith("file:"));
  const linkAll = fa.connected && ready.length
    ? `<button class="btn primary small" data-action="m-file-all" data-kind="link" ${busy ? "disabled" : ""}>${fa.dry_run ? "Dry run" : "Link"} ${ready.length} ready</button>` : "";

  content.innerHTML = `<div class="st-split ${sel ? "with-panel" : ""}"><div class="st-wrap">
    <header class="m-head">
      <div class="m-title"><span>${esc(st.account_name || "Bank")} statement</span>
        <span class="sub">Match each payment to a receipt · synced ${esc(ago(st.last_sync))}</span></div>
      ${linkAll}
      <div class="st-month"><button class="btn small" data-action="st-month" data-delta="-1" aria-label="Previous month">‹</button>
        <span>${esc(monthLabel(st.month))}</span>
        <button class="btn small" data-action="st-month" data-delta="1" aria-label="Next month">›</button></div>
    </header>
    <div class="st-body">
      <section class="st-summary">
        <div class="main">
          <div class="big">${todo ? `<b>${todo}</b> payment${todo === 1 ? "" : "s"} still need${todo === 1 ? "s" : ""} a receipt` : payments ? "<b>All done</b> for this month" : "<b>No payments</b> this month"}</div>
          <div class="muted st-done">${done} of ${payments} done</div>
          ${bar}
        </div>
        <div class="money"><span class="muted">Out this month</span><b>${esc(money(s.out || 0, "GBP"))}</b></div>
      </section>
      <div class="st-pills" role="group" aria-label="Show">
        ${pill("all", "All", (st.rows || []).length)}
        ${pill("missing", "Needs approval", missing, "seg-miss")}
        ${pill("in_match", "File suggested", suggested, "seg-sugg")}
        ${pill("done", "Done", done, "seg-done")}</div>
      <div class="st-table-box"><table class="st-table">
        <thead><tr><th scope="col">Date</th><th scope="col">On the statement</th><th scope="col" class="r">Amount</th><th scope="col">Receipt</th></tr></thead>
        <tbody>${rows.map(statementRow).join("") || `<tr><td colspan="4" class="m-none">Nothing to show for ${esc(monthLabel(st.month))}.</td></tr>`}</tbody>
      </table></div>
    </div></div>
    ${sel ? `<aside class="st-panel m-keep" aria-label="Selected payment">${panelHtml(sel)}</aside>` : ""}</div>`;
  content.querySelector(".st-wrap").scrollTop = top;
  if (m.reveal) { m.reveal = false; content.querySelector("tr.selected")?.scrollIntoView({ block: "center" }); }
  const panel = content.querySelector(".st-panel");
  const shown = sel ? (suggestedFile(sel) || filedFile(sel)) : null;
  if (panel) panel.scrollTop = shown && shown.id === m.docId ? panelTop : 0;
  placeDocument(shown, oldDoc);
  renderMatchModal();
}


function statementRow(t) {
  let look = t.status;                                   // the colour and icon
  let icon = { filed: ICON.filed, in_match: ICON.link, missing: ICON.check, not_needed: ICON.waiting, approved: ICON.filed }[t.status];
  let label = { filed: "Linked", in_match: "Suggested", missing: "No receipt", not_needed: "No receipt needed", approved: "Approved" }[t.status];
  let detail = t.receipt ? `${t.receipt.vendor} · ${shortDate(t.receipt.date)}` : "";
  let action = "";
  const r = suggestedFile(t);
  if (t.approved) {
    // approved in FreeAgent: green, whether or not a receipt is attached yet
    look = "filed"; icon = ICON.filed;
    label = t.status === "filed" ? "Approved and linked" : "Approved";
    if (t.status === "in_match" && r) {
      detail = `${r.supplier} file to attach`;
      const fa = state.snap.freeagent || {};
      if (fa.connected && !approveBlocker(r)) {
        action = `<button class="btn small" data-action="st-approve" data-url="${esc(t.url)}" data-id="${r.id}">Attach</button>`;
      }
    }
  } else if (t.status === "not_needed") {
    detail = t.detail && !/^(No receipt needed|Other)$/.test(t.detail) ? t.detail : "";
  } else if (t.status === "in_match" && r) {
    if (r.stage === "check") { label = "Suggested · check file"; }
    const fa = state.snap.freeagent || {};
    if (fa.connected && !approveBlocker(r)) {
      action = `<button class="btn small primary" data-action="st-approve" data-url="${esc(t.url)}" data-id="${r.id}">Approve</button>`;
    }
  } else if (t.status === "missing" && t.suggestion) {
    detail = t.suggestion.kind === "email" ? "An email may be the receipt" : "An ignored file may be the receipt";
  }
  // a suggested file shows its receipt on hover, as in the candidate list
  const thumb = t.status === "in_match" && r ? thumbUrl(r) : "";
  return `<tr class="${look} ${t.url === m.stSel ? "selected" : ""}" data-action="st-sel" data-url="${esc(t.url)}">
    <td class="d">${esc(statementDate(t.date))}</td>
    <td class="mono">${esc(t.description)}</td>
    <td class="r amt ${t.amount > 0 ? "in" : ""}">${t.amount > 0 ? "+" : "−"}${esc(money(Math.abs(t.amount), "GBP"))}</td>
    <td><div class="st-rec"><span class="st-rec-what" ${thumb ? `data-peek="${esc(thumb)}"` : ""}><span class="st-status ${look}">${icon}<span>${esc(label)}</span></span>
      <span class="muted">${esc(detail)}</span></span><span class="grow"></span>${action}</div></td></tr>`;
}

function filedFile(t) {
  if (t.status !== "filed" || !t.receipt) return null;
  const { id, vendor, is_image, page_count, source, tidy } = t.receipt;
  return { id, supplier: vendor, status: "filed", has_pdf: true, is_image: Boolean(is_image), page_count: page_count || 0, source, tidy };
}

/** Files that could be this payment: unlinked business files, the engine's
 * own options for it first, then the closest amount and date. */
function candidatesFor(t) {
  const want = -Number(t.amount);
  const day = (iso) => (iso ? new Date(iso.slice(0, 10) + "T12:00:00").getTime() : 0);
  const paid = day(t.date);
  const current = t.receipt?.id;
  return (state.receipts || [])
    .filter((r) => r.paid_by !== "personal" && r.id !== current && r.stage !== "filed")
    .map((r) => {
      const option = (r.options || []).find((o) => o.url === t.url);
      const diff = r.total == null ? 9e9 : Math.abs(want - Number(r.total));
      return { r, option, diff, gap: Math.abs(day(r.date) - paid) / 864e5 };
    })
    // close in amount (a tip, a rounding) and in time, or the engine's own option
    .filter((c) => c.option || ((c.r.total == null || c.diff <= Math.max(1, want * 0.3)) && (!c.r.date || c.gap <= 45)))
    .sort((a, b) => (b.option ? 1 : 0) - (a.option ? 1 : 0) || a.diff - b.diff || a.gap - b.gap)
    .slice(0, 8);
}

/** A picture of the file for the candidate list: the photo, or a PDF's
 * first page. "" when there's nothing to show. */
function thumbUrl(r) {
  if (!r.has_pdf) return "";
  const t = encodeURIComponent(TOKEN);
  if (r.is_image) return `/api/receipts/${r.id}/pdf?t=${t}`;
  return r.page_count ? `/api/receipts/${r.id}/page/1?t=${t}` : "";
}

/** Unlinked business files not yet on any payment, for "Pick from all
 * files": the closest amount first. */
function unallocatedFor(t) {
  const want = -Number(t.amount);
  return (state.receipts || [])
    .filter((r) => r.paid_by !== "personal" && r.id !== t.receipt?.id && r.stage !== "filed" && !r.payment)
    .map((r) => ({ r, diff: r.total == null ? 9e9 : Math.abs(want - Number(r.total)) }))
    .sort((a, b) => a.diff - b.diff || String(b.r.date || "").localeCompare(String(a.r.date || "")));
}

function candRow(t, { r, option, diff }) {
  const note = option ? (option.difference ? `+${money(option.difference, "GBP")} tip?` : "Could be this")
    : r.payment ? `Suggested for ${shortDate(r.payment.date)}`
    : r.total != null && diff > 0.004 ? `${money(diff, "GBP")} ${Number(r.total) < -t.amount ? "less" : "more"}` : "";
  const warn = option || r.payment;
  const thumb = thumbUrl(r);
  const search = `${r.supplier || ""} ${r.total ?? ""} ${r.date || ""}`.toLowerCase();
  return `<div class="st-cand" ${thumb ? `data-peek="${esc(thumb)}"` : ""} data-search="${esc(search)}">
    <span class="st-thumb">${thumb ? `<img src="${esc(thumb)}" alt="" loading="lazy" draggable="false">` : ICON.expense.replace(/16/g, "18")}</span>
    <span class="st-cand-txt">
      <span class="top"><b>${esc(r.supplier || "Unknown supplier")}</b><span class="amt">${esc(amountOf(r))}</span></span>
      <span class="sub">${esc(r.date ? shortDate(r.date) : "No date")} · ${esc({ photo: "Photo", gmail: "Email" }[r.source] || "File")}${
        note ? ` · <span class="${warn ? "warn" : ""}" ${r.payment && !option ? 'title="Choosing it here moves it off that payment"' : ""}>${esc(note)}</span>` : ""}</span>
    </span>
    <button class="btn small" data-action="st-use-file" data-id="${r.id}" data-url="${esc(t.url)}">Use this</button></div>`;
}

/** The files that could be this payment. When one alone has the exact
 * amount it's shown on its own. "Pick from all
 * files" opens a dialog of every file not yet on a payment. */
function candidatesHtml(t, title) {
  const list = candidatesFor(t);
  const exact = list.filter((c) => c.r.total != null && c.diff <= 0.004);
  const sure = exact.length === 1 && exact[0] === list[0];
  const shown = sure ? [list[0]] : list;
  const body = shown.length ? shown.map((c) => candRow(t, c)).join("")
    : `<div class="m-none">No files near this amount.</div>`;
  return `<div class="m-card"><div class="m-card-head"><span>${esc(sure ? "File that matches this payment" : title)}</span></div>
    <div class="st-cands">${body}</div>
    <div class="st-cand-more"><button class="link" data-action="st-pick" data-url="${esc(t.url)}">Pick from all files…</button></div></div>`;
}

/** "Pick from all files": every file not yet on a payment, listed on the
 * left, the chosen one shown large on the right with what was read. */
function pickHtml() {
  const t = (state.statement?.rows || []).find((x) => x.url === m.stPick.url);
  if (!t) return "";
  const all = unallocatedFor(t);
  if (!all.some((c) => c.r.id === m.stPick.sel)) m.stPick.sel = all[0]?.r.id ?? null;
  const rows = all.map(({ r, diff }) => {
    const note = r.total != null && diff > 0.004 ? `${money(diff, "GBP")} ${Number(r.total) < -t.amount ? "less" : "more"}` : r.total != null ? "Same amount" : "";
    const search = `${r.supplier || ""} ${r.total ?? ""} ${r.date || ""} ${r.date ? shortDate(r.date) : ""}`.toLowerCase();
    const thumb = thumbUrl(r);
    return `<button class="pk-row ${r.id === m.stPick.sel ? "on" : ""}" data-action="st-pick-sel" data-id="${r.id}" data-search="${esc(search)}">
      <span class="st-thumb">${thumb ? `<img src="${esc(thumb)}" alt="" loading="lazy" draggable="false">` : ICON.expense.replace(/16/g, "18")}</span>
      <span class="st-cand-txt"><span class="top"><b>${esc(r.supplier || "Unknown supplier")}</b><span class="amt">${esc(amountOf(r))}</span></span>
        <span class="sub">${esc(r.date ? shortDate(r.date) : "No date")} · ${esc({ photo: "Photo", gmail: "Email" }[r.source] || "File")}${note ? ` · ${esc(note)}` : ""}</span></span></button>`;
  }).join("");
  const sel = all.find((c) => c.r.id === m.stPick.sel)?.r;
  return `<div class="m-overlay" data-action="st-pick-close"><section class="m-dialog pk-dialog" role="dialog" aria-label="Pick a file">
    <h2>Pick a file for ${esc(t.description)} · ${esc(money(Math.abs(t.amount), "GBP"))}</h2>
    <p class="sub">${esc(longDate(t.date))} · files not yet linked to a payment, closest amount first</p>
    ${all.length ? `<div class="pk-body">
      <div class="pk-side"><input class="pk-search" type="search" placeholder="Search supplier, amount or date" data-action="st-pick-search" aria-label="Search files">
        <div class="pk-list">${rows}</div></div>
      <div class="pk-preview">${sel ? pickPreviewHtml(sel) : ""}</div></div>`
      : `<div class="m-none">All files are linked.</div>`}
    <div class="m-dialog-foot"><button class="btn" data-action="st-pick-close">Cancel</button>
      <button class="btn primary" data-action="st-use-file" data-url="${esc(t.url)}" data-id="${sel?.id ?? ""}" ${sel ? "" : "disabled"}>Use this file</button></div></section></div>`;
}

function pickPreviewHtml(r) {
  const src = r.has_pdf ? (r.is_image ? `/api/receipts/${r.id}/pdf?t=${encodeURIComponent(TOKEN)}` : thumbUrl(r)) : "";
  const kv = [
    ["Supplier", r.supplier || "Unknown"],
    ["Amount", amountOf(r)],
    ["VAT", r.vat != null ? money(r.vat, "GBP") : ""],
    ["Date", r.date ? longDate(r.date) : "No date"],
    ["From", { photo: "Photo", gmail: "Email" }[r.source] || "File"],
    ["Category", r.will_file?.category || ""],
  ].filter(([, v]) => v);
  return `<div class="pk-img">${src ? `<img src="${esc(src)}" alt="Receipt">` : `<div class="m-nodoc">No picture</div>`}</div>
    <div class="m-kv pk-kv">${kv.map(([k, v]) => `<span class="k">${esc(k)}</span><span>${esc(v)}</span>`).join("")}</div>
    ${r.has_pdf ? `<a class="link" href="/api/receipts/${r.id}/${r.source === "photo" ? "original" : "pdf"}?t=${encodeURIComponent(TOKEN)}" target="_blank" rel="noopener">Open original</a>` : ""}`;
}

// Hovering a candidate shows its receipt, large, beside the panel.
const peek = document.createElement("div");
peek.className = "st-peek";
peek.hidden = true;
document.body.appendChild(peek);
document.addEventListener("mouseover", (e) => {
  const row = e.target.closest?.("[data-peek]");
  if (!row) { peek.hidden = true; return; }
  if (peek.dataset.src !== row.dataset.peek) {
    peek.dataset.src = row.dataset.peek;
    peek.innerHTML = `<img src="${esc(row.dataset.peek)}" alt="Receipt preview">`;
  }
  const box = row.getBoundingClientRect();
  const width = Math.min(360, Math.max(200, box.left - 24));
  peek.style.width = `${width}px`;
  peek.style.left = `${Math.max(8, box.left - width - 12)}px`;
  peek.style.top = `${Math.max(8, Math.min(box.top - 40, window.innerHeight - 520))}px`;
  peek.hidden = false;
});
document.addEventListener("scroll", () => { peek.hidden = true; }, true);

const VAT_RATES = [["20.0", "20% (standard)"], ["5.0", "5% (reduced)"], ["0.0", "0% (zero-rated or none)"]];

/** The payment's own FreeAgent details: category, VAT and re-billing.
 * A payment FreeAgent already explained starts from its explanation
 * (tagged "From FreeAgent"); change any of them and "Approve" (at the
 * foot of the panel) sends it. Otherwise set them and "No receipt needed" explains it. */
function paymentCardHtml(t) {
  const fa = state.snap.freeagent || {};
  if (!fa.connected || t.amount >= 0) return "";
  const st = t.settings || {};
  const own = t.freeagent?.explained ? t.freeagent : null;      // FreeAgent's explanation
  const pick = (k) => (k in st && st[k] != null) || (k === "rebill" && k in st) ? st[k] : own ? own[k] : null;
  const category = pick("category"), vatRate = pick("vat_rate"), rebill = pick("rebill");
  // only what you changed is tagged; the rest is FreeAgent's (said once, in the header)
  const tag = (k, value) => !own || JSON.stringify(value ?? null) === JSON.stringify(own[k] ?? null) ? ""
    : `<span class="ai" title="Sent to FreeAgent when you approve">Changed</span>`;
  const rates = [...VAT_RATES];
  if (vatRate && !rates.some(([v]) => v === vatRate)) rates.push([vatRate, `${parseFloat(vatRate)}%`]);
  const vat = fa.vat?.registered ? `<span class="k">VAT</span><span class="pair"><select data-action="pay-vat" data-url="${esc(t.url)}" aria-label="VAT rate">
      <option value="">${own ? "Keep FreeAgent's" : "Choose…"}</option>${rates.map(([v, l]) =>
        `<option value="${v}" ${vatRate === v ? "selected" : ""}>${l}</option>`).join("")}</select>${own && vatRate ? tag("vat_rate", vatRate) : ""}</span>` : "";
  const changed = own && Object.keys(own.changes || {}).length;
  const updated = t.explained_here?.state === "updated";
  return `<div class="m-card"><div class="m-card-head"><span>In FreeAgent</span>${own ? `<span class="fa-tag" title="Already explained in FreeAgent. Changes here update it.">From FreeAgent</span>` : ""}</div>
    <div class="m-kv">
      <span class="k">Category</span><span class="pair"><select data-action="pay-category" data-url="${esc(t.url)}" aria-label="Category">${categoryOptions(category)}</select>${own ? tag("category", category) : ""}</span>
      ${vat}
      ${rebillFields(rebill || null, `data-url="${esc(t.url)}"`, tag("rebill", rebill || null))}
    </div>
    ${changed ? `<div class="m-note">Your changes are sent when you approve. <button class="link" data-action="st-revert" data-url="${esc(t.url)}">Keep FreeAgent's</button></div>` : ""}
    ${updated ? `<div class="m-note">You changed FreeAgent's explanation here. <button class="link" data-action="st-unexplain" data-url="${esc(t.url)}">Undo</button></div>` : ""}
    ${lastExplainHtml(t)}</div>`;
}

function lastExplainHtml(t) {
  const x = t.explained_here;
  if (!x || x.state !== "dry_run") return "";
  return `<details class="m-dry"><summary>Last dry run: what would be sent</summary><pre class="selectable">${esc(JSON.stringify(x.body, null, 2))}</pre></details>`;
}

/** "Approve" with no receipt: explained and approved in FreeAgent, with
 * the category you set or FreeAgent's own (its guess, replaced). On a
 * payment already approved it sends the changes you made. Always sent,
 * never a dry run. */
function noReceiptButton(t) {
  const fa = state.snap.freeagent || {};
  const changed = t.freeagent?.explained && Object.keys(t.freeagent.changes || {}).length;
  if (t.status !== "missing") {
    return fa.connected && changed && t.amount < 0
      ? `<button class="btn primary" data-action="st-approve" data-url="${esc(t.url)}" data-changes="1">Approve changes</button>` : "";
  }
  if (!fa.connected) return `<button class="btn" data-action="st-none" data-url="${esc(t.url)}">Approve</button>`;
  if (!(t.settings?.category || t.freeagent?.category)) return `<button class="btn" disabled>Choose a category to approve</button>`;
  return `<button class="btn primary" data-action="st-approve" data-url="${esc(t.url)}">Approve</button>`;
}

/** Take FreeAgent's own explanation off a payment (not one you linked or
 * explained here: those have their own Undo). Asks first. */
function removeExplanationButton(t) {
  const fa = state.snap.freeagent || {};
  if (!fa.connected || !t.freeagent?.explained || t.status === "filed" || t.explained_here?.state === "filed") return "";
  return `<button class="btn quiet" data-action="st-remove-explanation" data-url="${esc(t.url)}">Remove explanation</button>`;
}

/** Why "Approve" can't send this file yet, or "". Unlike filing from Files,
 * a file still to check doesn't stop it: you're looking at it here. */
function approveBlocker(r) {
  return fileBlocker({ ...r, stage: r.stage === "check" ? "link" : r.stage });
}

function panelHtml(t) {
  const fa = state.snap.freeagent || {};
  const head = `<div class="st-phead"><div><div class="mono st-pdesc">${esc(t.description)}</div>
      <div class="muted">${esc(accountName())} · ${esc(longDate(t.date))}</div></div>
      <span class="amt">${t.amount > 0 ? "+" : "−"}${esc(money(Math.abs(t.amount), "GBP"))}</span>
      <button class="link" data-action="st-close" aria-label="Close">✕</button></div>`;
  if (t.status === "filed") {
    return `${head}<div class="st-pbody">
      <div class="m-wait"><div class="t">${t.approved ? "Approved and linked" : "Linked"} to the ${esc(t.receipt?.vendor || "")} receipt</div><div class="b">Saved to FreeAgent${t.approved ? " and approved there" : ""}. Nothing more to do.</div></div>
      ${t.receipt ? docFigure(filedFile(t)) : ""}
      <div class="m-actions">${t.receipt ? `<button class="btn" data-action="m-unfile" data-id="${t.receipt.id}">Undo link</button>` : ""}
        ${fa.web ? `<a class="btn" href="${esc(fa.web)}" target="_blank" rel="noopener">View in FreeAgent</a>` : ""}</div></div>`;
  }
  if (t.status === "not_needed" && t.explained_here?.state === "filed") {
    const b = t.explained_here.body || {};
    return `${head}<div class="st-pbody"><div class="m-wait"><div class="t">Explained in FreeAgent, no receipt</div>
      <div class="b">${esc(categoryName(b.category) || "")}${b.sales_tax_rate ? ` · VAT ${esc(String(parseFloat(b.sales_tax_rate)))}%` : ""}${b.project ? (b.rebill_type ? " · re-billed" : " · project") : ""}</div></div>
      <div class="m-actions"><button class="btn" data-action="st-unexplain" data-url="${esc(t.url)}">Remove from FreeAgent</button>
        ${fa.web ? `<a class="btn" href="${esc(fa.web)}" target="_blank" rel="noopener">View in FreeAgent</a>` : ""}</div></div>`;
  }
  if (t.status === "not_needed") {
    return `${head}<div class="st-pbody"><div class="m-wait"><div class="t">No receipt needed</div>
      <div class="b">${esc(t.detail && t.detail !== "Other" ? `You marked this: ${t.detail.toLowerCase()}.` : "You marked this.")}</div></div>
      <div class="m-actions"><button class="btn" data-action="st-need" data-url="${esc(t.url)}">Undo</button></div></div>`;
  }
  const r = suggestedFile(t);
  if (r && !m.stOther) {
    const explained = r.payment && r.payment.explained;
    const blocked = approveBlocker(r);
    const link = !fa.connected ? `<span class="m-note">Connect FreeAgent in Settings to link.</span>`
      : blocked ? `<button class="btn" disabled>${esc(blocked)}</button>`
      : `<button class="btn primary" data-action="st-approve" data-url="${esc(t.url)}" data-id="${r.id}">${explained && t.approved ? "Attach receipt" : "Approve"} ${kbd("⏎", true)}</button>`;
    return `${head}<div class="st-pbody">
      ${t.approved ? `<div class="m-wait"><div class="t">Approved in FreeAgent</div><div class="b">This file may be its receipt. Attach it if so.</div></div>` : ""}
      ${r.stage === "check" ? issuesHtml(r, "Worth a look") : ""}
      <div class="m-card m-filecard"><div class="m-card-head"><span>Suggested file</span><button class="link" data-action="st-open" data-id="${r.id}">Open in Files</button></div>
        <div class="m-pay"><div class="who"><div class="desc plain">${esc(r.supplier)}</div><div class="sub">${esc(r.date ? longDate(r.date) : "No date")}</div></div>
          <span class="amt">${esc(amountOf(r))}</span></div>
        ${r.payment ? `<div class="m-chips">${chipsHtml(r.payment)}</div>` : ""}
        ${willFileHtml(r)}
        ${docFigure(r)}
        <div class="m-card-foot"><button class="btn small" data-action="st-remove-file" data-id="${r.id}" data-url="${esc(t.url)}">Remove file</button></div></div>
      ${learnHtml(r)}
      ${lastFilingHtml(r)}
      <div class="m-actions">${link}<button class="btn" data-action="st-other">Another file</button>${removeExplanationButton(t)}</div></div>`;
  }
  // missing (or choosing another file)
  const s = t.suggestion;
  const hint = s && t.status === "missing" ? `<div class="m-issue"><div class="t">${s.kind === "email" ? "An email may be the receipt" : "An ignored file may be the receipt"}</div>
      <div class="b">${esc(s.label)}</div>
      <div class="m-iss-ctl"><button class="btn small primary" data-action="st-use" data-url="${esc(t.url)}">${s.kind === "email" ? "Use that email" : "Use that file"}</button></div></div>` : "";
  const approvedNote = t.approved && t.status !== "filed"
    ? `<div class="m-wait"><div class="t">Approved in FreeAgent</div><div class="b">Nothing to do. You can still attach a receipt below.</div></div>` : "";
  return `${head}<div class="st-pbody">
    ${approvedNote}
    ${hint}
    ${paymentCardHtml(t)}
    ${candidatesHtml(t, r ? "Other files that could be this payment" : "Files that could be this payment")}
    <div class="m-actions end">
      ${removeExplanationButton(t)}
      ${r ? `<button class="btn" data-action="st-other">Back to the suggestion</button>` : ""}
      <button class="btn" data-action="m-add" data-paid="business">Add a file…</button>
      ${noReceiptButton(t)}</div></div>`;
}

// ---- dialogs: "Link all" / "Save all" ---------------------------------------------------

function renderMatchModal() {
  const host = $("#modal");
  if (m.fileAll) { host.innerHTML = fileAllHtml(); return; }
  if (m.stPick) {
    // built once per payment, so a search typed in it survives the app's refreshes
    const open = host.querySelector(".pk-dialog");
    if (!open || open.dataset.url !== m.stPick.url) {
      host.innerHTML = pickHtml();
      const d = host.querySelector(".pk-dialog");
      if (d) { d.dataset.url = m.stPick.url; host.querySelector(".pk-search")?.focus(); }
    }
    return;
  }
  if (host.querySelector(".m-overlay")) host.innerHTML = "";      // never touch the supplier dialogs
}

function fileAllHtml() {
  const fa = state.snap.freeagent || {};
  const f = m.fileAll;
  if (f.phase === "running") {
    const res = state.snap.file_results;
    if (res && res.at !== f.since) { f.phase = "done"; f.results = res.results; f.message = res.message || ""; }
  }
  const expense = f.kind === "expense";
  const sandbox = fa.environment === "sandbox" && !fa.dry_run
    ? `<div class="m-sandbox"><b>Sandbox</b><span>These go to your practice company, not your real books.</span></div>` : "";
  if (f.phase === "confirm" || f.phase === "running") {
    const total = f.items.reduce((s, r) => s + Number(r.total || 0), 0);
    const vat = f.items.reduce((s, r) => s + Number(r.vat || 0), 0);
    const verb = fa.dry_run ? "Dry run" : expense ? "Save" : "Link";
    const noun = expense ? "expense" : "payment";
    return `<div class="m-overlay"><section class="m-dialog wide" role="dialog" aria-label="Confirm">
      <h2>${verb} ${f.items.length} ${noun}${f.items.length === 1 ? "" : "s"}${fa.dry_run ? "" : " to FreeAgent"}?</h2>
      <p class="sub">${fa.dry_run ? "Dry run: nothing is sent."
        : expense ? "Each is saved as an expense claim with its receipt. You can undo them later."
        : `Attaches a receipt to each ${esc(accountName())} payment. You can undo each one in Bank Feed.`}</p>
      <div class="m-fa-list">${f.items.map((r) => `<div class="m-fa-row">
          <span class="v">${esc(r.supplier)}</span>
          <span class="p">${r.payment ? `<span class="mono">${esc(r.payment.description)}</span> · ${esc(shortDate(r.payment.date))}` : "Expense claim"}</span>
          <span class="c">${esc(r.will_file?.category || "—")} · ${esc(r.will_file?.vat || "")}</span>
          <span class="a">${esc(amountOf(r))}</span></div>`).join("")}
        <div class="m-fa-total"><span>${f.items.length} ${noun}${f.items.length === 1 ? "" : "s"}${vat ? ` · VAT reclaimed ${esc(money(vat, "GBP"))}` : ""}</span>
          <span>${esc(money(total, "GBP"))}</span></div></div>
      ${sandbox}
      <div class="m-dialog-foot">
        <button class="btn" data-action="m-fa-cancel" ${f.phase === "running" ? "disabled" : ""}>Cancel</button>
        <button class="btn primary" data-action="m-fa-confirm" ${f.phase === "running" ? "disabled" : ""}>
          ${f.phase === "running" ? "Saving…" : `${verb} ${f.items.length}`} <span class="kbd on">⏎</span></button></div></section></div>`;
  }
  const ok = (f.results || []).filter((x) => x.ok);
  const skipped = (f.results || []).length - ok.length;
  return `<div class="m-overlay"><section class="m-dialog wide" role="dialog" aria-label="Finished">
    <h2>${f.message ? "Nothing was saved" : fa.dry_run ? "Dry run done" : `Saved ${ok.length} of ${(f.results || []).length}`}</h2>
    <p class="sub">${f.message ? esc(f.message)
      : skipped ? `${skipped} skipped to avoid overwriting anything.`
      : fa.dry_run ? "Nothing was sent." : "All done."}</p>
    <div class="m-fa-list">${(f.results || []).map((x) => `<div class="m-fa-res ${x.ok ? "" : "skip"}">
        <span class="ic">${x.ok ? ICON.filed : ICON.check}</span>
        <div class="txt"><div class="top"><b>${esc(x.vendor || "Receipt")}</b> <span class="muted">${esc(money(x.total, "GBP"))}</span>
            <span class="grow"></span>${!x.ok ? `<button class="link" data-action="m-fa-open" data-id="${x.id}">Open in Files</button>`
              : fa.web && !fa.dry_run ? `<a class="link" href="${esc(fa.web)}" target="_blank" rel="noopener">View in FreeAgent</a>` : ""}</div>
          <div class="m-sub">${esc(x.note)}</div></div></div>`).join("")}</div>
    <div class="m-dialog-foot">
      ${ok.length && !fa.dry_run ? `<button class="btn" data-action="m-fa-undo">Undo all ${ok.length}</button>` : ""}
      <button class="btn primary" data-action="m-fa-cancel">Done</button></div></section></div>`;
}

// ---- drag and drop -----------------------------------------------------------------------

const DROP_VIEWS = ["pending"];

async function uploadFiles(files, paidBy) {
  const list = [...files];
  if (!list.length) return;
  let added = 0;
  const problems = [];
  for (const f of list) {
    const res = await fetch(`/api/files/upload?name=${encodeURIComponent(f.name)}&paid_by=${paidBy}`, {
      method: "POST", headers: { "x-receipt-bridge": TOKEN, "content-type": "application/octet-stream" }, body: f });
    if (res.ok) added += 1;
    else problems.push(`${f.name}: ${((await res.json().catch(() => ({}))).detail) || res.statusText}`);
  }
  if (added) toast(`Added ${added} file${added === 1 ? "" : "s"}. Reading ${added === 1 ? "it" : "them"} now…`);
  if (problems.length) toast(`<b>Not added.</b> ${esc(problems.join(" · "))}`, 9000);
  refresh(true);
}

function setDragging(on) {
  if (m.dragging === on) return;
  m.dragging = on;
  document.querySelector(".m-wrap")?.classList.toggle("dragging", on);
}

// A drag that starts inside the app (a receipt photo, a link) is never a
// new file: dropping it back in would add a copy of a receipt already here.
let dragFromPage = false;
document.addEventListener("dragstart", () => { dragFromPage = true; });
document.addEventListener("dragend", () => { dragFromPage = false; });

function outsideFiles(e) {
  return !dragFromPage && [...(e.dataTransfer?.types || [])].includes("Files");
}

// Anywhere in the window: never let a dropped file navigate the page away.
document.addEventListener("dragover", (e) => {
  if (dragFromPage) { e.preventDefault(); e.dataTransfer.dropEffect = "none"; return; }
  if (!outsideFiles(e)) return;
  e.preventDefault();
  // a licence file can be dropped on any screen while the keys are missing
  const ok = DROP_VIEWS.includes(state.view) || !!state.snap?.needs_licence;
  e.dataTransfer.dropEffect = ok ? "copy" : "none";
  setDragging(ok);
});
document.addEventListener("dragleave", (e) => { if (!e.relatedTarget) setDragging(false); });
document.addEventListener("drop", (e) => {
  if (dragFromPage) { e.preventDefault(); dragFromPage = false; return; }
  if (!outsideFiles(e)) return;
  e.preventDefault();
  setDragging(false);
  const files = [...e.dataTransfer.files];
  const licences = files.filter((f) => f.name.toLowerCase().endsWith(".rbkey"));
  if (licences.length) installLicence(licences[0]);         // on any screen
  const rest = files.filter((f) => !licences.includes(f));
  if (rest.length && DROP_VIEWS.includes(state.view)) uploadFiles(rest, "business");
});

// ---- events ------------------------------------------------------------------------------

function goToStatement(r) {
  const month = (r.payment?.date || r.options?.[0]?.date || r.date || "").slice(0, 7);
  if (month) { state.month = month; state.statement = null; }
  m.stSel = r.options?.[0]?.url || null;
  m.stPending = r.payment ? r.id : null;             // select its row once the statement is loaded
  m.reveal = true;
  return setView("statement");
}

document.addEventListener("click", async (e) => {
  const target = e.target.closest("[data-action]");
  const action = target?.dataset.action || "";
  // a click anywhere else closes the currency list
  if (m.curOpen != null && !e.target.closest(".cur")) { m.curOpen = null; render(); }
  if (!["m-", "st-", "x-", "cur-", "rb-", "a-"].some((p) => action.startsWith(p))) return;
  if ((action === "st-sel" || action === "x-sel") && e.target.closest("button, a, select, input")) return;    // a button in the row
  const id = target.dataset.id ? Number(target.dataset.id) : null;
  const r = id !== null ? findReceipt(id) : null;
  switch (action) {
    case "m-pick": {
      if (e.shiftKey && m.sel != null) {
        // a range, in list order, from the last plain click
        const rows = fileRows(fileGroups()).map((x) => x.id);
        const a = rows.indexOf(m.anchor ?? m.sel), b = rows.indexOf(id);
        if (a >= 0 && b >= 0) m.multi = rows.slice(Math.min(a, b), Math.max(a, b) + 1);
      } else if (e.metaKey || e.ctrlKey) {
        const base = m.multi.length ? m.multi : (m.sel != null ? [m.sel] : []);
        m.multi = base.includes(id) ? base.filter((x) => x !== id) : [...base, id];
        if (m.multi.length === 1) { m.sel = m.multi[0]; m.multi = []; }
      } else {
        m.multi = []; m.sel = id; m.anchor = id;
      }
      return render();
    }
    case "m-bulk-clear": m.multi = []; return render();
    case "m-bulk-ignore": return act(() => ignoreMany(m.multi));
    case "m-bulk-paid": {
      const rows = m.multi.map(findReceipt).filter((x) => x && savedPaid(x) !== target.dataset.to);
      return act(async () => {
        for (const x of rows) await api(`/api/receipts/${x.id}/fields`, { method: "POST", body: { paid_by: target.dataset.to } });
        toast(`${rows.length} file${rows.length === 1 ? "" : "s"}: ${target.dataset.to === "personal" ? "now expenses" : `paid from ${esc(accountName())}`}`);
      });
    }
    case "m-toggle": m.open[target.dataset.group] = !m.open[target.dataset.group]; return render();
    case "m-add":
      return act(async () => {
        const { added } = await api("/api/statement/add-file", { method: "POST", body: { paid_by: target.dataset.paid || "business" } });
        if (added) toast("Added. It appears in a moment.");
      });
    case "m-paid":
      // saved at once: the file stays where it is, in Files
      return r && target.dataset.to && act(() => setPaid(r, target.dataset.to));
    case "a-pick": m.asel = id; return render();
    case "a-restore":
      return act(async () => {
        await api("/api/receipts/status", { method: "POST", body: { ids: [id], status: "pending" } });
        toast("Back in Files.");
      });
    case "a-retry": return act(() => api(`/api/receipts/${id}/retry`, { method: "POST" }));
    case "a-delete":
      if (!confirm("Permanently delete this receipt and its file? This can't be undone.")) return;
      return act(async () => {
        await api("/api/archived/delete", { method: "POST", body: { ids: [id] } });
        toast("Deleted.");
      });
    case "a-clear": {
      const n = (state.receipts || []).length;
      if (!confirm(`Permanently delete all ${n} archived receipt${n === 1 ? "" : "s"} and their files? This can't be undone.`)) return;
      return act(async () => {
        const { deleted } = await api("/api/archived/delete", { method: "POST", body: { all: true } });
        toast(`${deleted} archived receipt${deleted === 1 ? "" : "s"} deleted.`);
      });
    }
    case "cur-open":
      m.curOpen = m.curOpen === id ? null : id;
      render();
      document.querySelector(".cur-q")?.focus();
      return;
    case "cur-pick":
      m.curOpen = null;
      return act(() => api(`/api/receipts/${id}/fields`, { method: "POST", body: { currency: target.dataset.code } }));
    case "m-photo-date":
      return r && act(() => api(`/api/receipts/${r.id}/fields`, { method: "POST", body: { purchased_on: target.dataset.day } }));
    case "m-checked":
      return r && act(() => api(`/api/receipts/${r.id}/fields`, { method: "POST", body: { checked: true } }));
    case "m-tidy":
      return r && act(() => api(`/api/receipts/${r.id}/tidy`, { method: "POST", body: { on: target.dataset.on === "1" } }));
    case "m-to-statement": return r && goToStatement(r);
    case "m-ignore": {
      if (!r) return;
      const next = nextIn(fileRows(fileGroups()), r.id);
      return act(() => ignoreOne(r, () => { m.sel = next; }));
    }
    case "x-file": {
      if (!r) return;
      const next = nextIn(fileRows(fileGroups()), r.id);
      return act(() => fileOne(r, () => { if (m.sel === r.id) m.sel = next; }));
    }
    case "x-sel": m.xsel = m.xsel === id ? null : id; m.xedit = null; return render();
    case "x-edit": m.xedit = id || null; return render();
    case "x-update":
      m.xedit = null;
      return act(() => api(`/api/receipts/${id}/update-claim`, { method: "POST" }));
    case "x-close": m.xsel = null; m.xedit = null; return render();
    case "x-month": {
      const d = new Date(xMonth() + "-15T12:00:00");
      d.setMonth(d.getMonth() + Number(target.dataset.delta));
      state.xMonth = d.toISOString().slice(0, 7); m.xsel = null;
      return render();
    }
    case "m-unfile":
      if (!confirm("Remove this from FreeAgent? The receipt goes back to Files.")) return;
      return act(async () => { await api(`/api/receipts/${id}/unfile`, { method: "POST" }); undoToast("Removed from FreeAgent", null); });
    case "m-undo": {
      const undo = m.undo; m.undo = null; $("#toast").hidden = true;
      return undo && act(undo);
    }
    case "m-file-all": {
      const items = target.dataset.kind === "expense"
        ? fileGroups().expense.filter((x) => !fileBlocker(x))
        : (state.statement?.rows || []).map(suggestedFile).filter((x) => x && !fileBlocker(x) && x.group === "ready");
      m.fileAll = { phase: "confirm", kind: target.dataset.kind, items };
      return renderMatchModal();
    }
    case "m-fa-confirm":
      m.fileAll.phase = "running";
      m.fileAll.since = state.snap.file_results?.at || null;
      renderMatchModal();
      return act(() => api("/api/receipts/file", { method: "POST", body: { ids: m.fileAll.items.map((x) => x.id) } }));
    case "m-fa-undo": {
      const ids = (m.fileAll?.results || []).filter((x) => x.ok).map((x) => x.id);
      m.fileAll = null; $("#modal").innerHTML = "";
      return act(async () => { await api("/api/receipts/unfile", { method: "POST", body: { ids } }); undoToast(`Undoing ${ids.length}`, null); });
    }
    case "m-fa-cancel": {
      // after a real save, Undo all stays on offer for a few seconds
      const fa = state.snap.freeagent || {};
      const ids = m.fileAll?.phase === "done" && !fa.dry_run ? (m.fileAll.results || []).filter((x) => x.ok).map((x) => x.id) : [];
      m.fileAll = null; $("#modal").innerHTML = "";
      if (ids.length) {
        undoToast(`Saved ${ids.length} to FreeAgent`, () => api("/api/receipts/unfile", { method: "POST", body: { ids } }), `Undo all ${ids.length}`);
      }
      return;
    }
    case "m-fa-open":
      m.fileAll = null; $("#modal").innerHTML = "";
      m.sel = id; m.reveal = true;
      return setView("pending");
    // Statement
    case "st-sel":
      m.stSel = target.dataset.url === m.stSel ? null : target.dataset.url;
      m.stOther = false; m.stPick = null;
      return render();
    case "st-close": m.stSel = null; m.stOther = false; m.stPick = null; return render();
    case "st-other": m.stOther = !m.stOther; m.stPick = null; return render();
    case "st-pick": m.stPick = { url: target.dataset.url, sel: null }; return renderMatchModal();
    case "st-pick-close":
      if (e.target !== target) return;              // a click inside the dialog, not on the backdrop
      m.stPick = null; $("#modal").innerHTML = ""; return;
    case "st-pick-sel": {
      m.stPick.sel = Number(id);
      for (const b of document.querySelectorAll(".pk-row")) b.classList.toggle("on", b === target);
      const pr = (state.receipts || []).find((x) => x.id === Number(id));
      if (pr) document.querySelector(".pk-preview").innerHTML = pickPreviewHtml(pr);
      const use = document.querySelector('.pk-dialog [data-action="st-use-file"]');
      if (use) { use.dataset.id = id; use.disabled = false; }
      return;
    }
    case "st-link": {
      if (!r) return;
      return act(() => fileOne(r, null));
    }
    case "st-approve":
      if (!id && !target.dataset.changes && !confirm("Approve without a receipt?")) return;
      return act(() => api("/api/statement/approve", { method: "POST",
        body: { url: target.dataset.url, receipt_id: id } }));
    case "st-remove-explanation":
      if (!confirm("Remove FreeAgent's explanation? The payment becomes unexplained.")) return;
      return act(() => api("/api/statement/remove-explanation", { method: "POST", body: { url: target.dataset.url } }));
    case "st-remove-file":
      return act(async () => {
        await api(`/api/receipts/${id}/remove-from-payment`, { method: "POST", body: { url: target.dataset.url } });
        toast("Removed. It won't be suggested for this payment again.");
      });
    case "st-use-file":
      if (m.stPick) $("#modal").innerHTML = "";
      m.stOther = false; m.stPick = null;
      return act(async () => {
        await api(`/api/receipts/${id}/payment`, { method: "POST", body: { url: target.dataset.url } });
        toast(`${r ? r.supplier : "That file"} suggested for this payment. Check it, then Approve.`);
      });
    case "st-filter": state.stFilter = target.dataset.filter; return render();
    case "st-month": {
      const d = new Date(state.month + "-15T12:00:00");
      d.setMonth(d.getMonth() + Number(target.dataset.delta));
      state.month = d.toISOString().slice(0, 7);
      state.statement = null; m.stSel = null;
      return refresh(true);
    }
    case "st-none":
      if (!confirm("Approve without a receipt?")) return;
      return act(() => api("/api/statement/no-receipt", { method: "POST", body: { url: target.dataset.url, reason: "No receipt needed" } }));
    case "st-explain":
      return act(async () => {
        await api("/api/statement/explain", { method: "POST", body: { url: target.dataset.url } });
        if (!(state.snap.freeagent || {}).dry_run) undoToast("Explaining it in FreeAgent…",
          () => api("/api/statement/unexplain", { method: "POST", body: { url: target.dataset.url } }));
      });
    case "st-revert":
      return act(() => api("/api/statement/payment-reset", { method: "POST", body: { url: target.dataset.url } }));
    case "st-unexplain":
      if (stPayment(target.dataset.url)?.explained_here?.state === "updated") {
        if (!confirm("Restore FreeAgent's original explanation?")) return;
      } else if (!confirm("Remove this explanation from FreeAgent? The payment will need a receipt again.")) return;
      return act(() => api("/api/statement/unexplain", { method: "POST", body: { url: target.dataset.url } }));
    case "st-need": return act(() => api("/api/statement/no-receipt", { method: "POST", body: { url: target.dataset.url, reason: null } }));
    case "st-feed":
      state.statement = null;
      return act(async () => {
        await api("/api/freeagent/accounts", { method: "POST", body: { urls: [target.dataset.url] } });
        toast("Reading its payments from FreeAgent…");
      });
    case "st-use":
      // the status line shows the fetch; the outcome toast says how it went
      return act(() => api("/api/statement/use-suggestion", { method: "POST", body: { url: target.dataset.url } }));
    case "st-open":
      m.sel = id;
      m.reveal = true;
      return setView("pending");
  }
});

document.addEventListener("input", (e) => {
  if (e.target.dataset.action === "st-pick-search") {
    const q = e.target.value.trim().toLowerCase();
    for (const o of document.querySelectorAll(".pk-row")) o.hidden = !!q && !o.dataset.search.includes(q);
    return;
  }
  if (e.target.dataset.action === "cur-search") {
    const q = e.target.value.trim().toLowerCase();
    for (const o of document.querySelectorAll(".cur-opt")) o.hidden = !!q && !o.dataset.search.includes(q);
    return;
  }
  if (e.target.dataset.action !== "m-search") return;
  m.query = e.target.value;
  const at = e.target.selectionStart;
  render();
  const box = document.querySelector('input[data-action="m-search"]');
  if (box) { box.focus(); box.setSelectionRange(at, at); }
});

document.addEventListener("change", (e) => {
  const a = e.target.dataset.action;
  if (a === "m-learn") {
    const on = e.target.checked;
    const r = findReceipt(Number(e.target.dataset.id));
    return act(async () => {
      await api(`/api/receipts/${e.target.dataset.id}/fields`, { method: "POST", body: { auto_file: on } });
      toast(on ? `${esc(r?.supplier || "This supplier")} will link automatically when a payment matches exactly.`
        : `${esc(r?.supplier || "This supplier")} won't link automatically.`);
    });
  }
  if (a === "rb-project") { saveRebill(e.target, { project: e.target.value || null }); e.target.blur(); return; }
  if (a === "rb-kind") { saveRebill(e.target, { type: e.target.value, factor: null }); e.target.blur(); return; }
  if (a === "rb-factor") {
    const v = Number(String(e.target.value).replace(/[£%\s]/g, "").replace(",", "."));
    saveRebill(e.target, { factor: Number.isFinite(v) && v > 0 ? v : null });
    return;
  }
  if (a === "pay-category") { savePayment(e.target.dataset.url, { category: e.target.value || null }); e.target.blur(); return; }
  if (a === "pay-vat") { savePayment(e.target.dataset.url, { vat_rate: e.target.value || null }); e.target.blur(); return; }

});

document.addEventListener("keydown", (e) => {
  if (!e.target.matches?.(".cur-q")) return;
  if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); m.curOpen = null; render(); }
  else if (e.key === "Enter") {
    e.preventDefault(); e.stopPropagation();
    document.querySelector(".cur-opt:not([hidden])")?.click();
  }
}, true);

// A picker left focused would take E or ⌫ as typing and silently change the
// category. In these views letters are shortcuts: the picker lets go of
// focus (arrow keys and the mouse still choose).
const KEY_VIEWS = ["pending", "expenses", "statement"];
document.addEventListener("keydown", (e) => {
  const t = e.target;
  if (KEY_VIEWS.includes(state.view) && t.matches?.(".m-inspector select, .st-panel select") && !e.metaKey && !e.ctrlKey && !e.altKey
      && (e.key.length === 1 || e.key === "Backspace" || e.key === "Delete") && e.key !== " ") {
    e.preventDefault();
    t.blur();
  }
}, true);

document.addEventListener("keydown", (e) => {
  if (!KEY_VIEWS.includes(state.view)) return;
  if (e.key === "Escape" && m.fileAll) {
    if (m.fileAll.phase === "running") return;
    m.fileAll = null; $("#modal").innerHTML = ""; return;
  }
  if (m.stPick) {
    if (e.key === "Escape") { e.preventDefault(); m.stPick = null; $("#modal").innerHTML = ""; }
    return;
  }
  if (m.fileAll?.phase === "confirm" && e.key === "Enter") { e.preventDefault(); document.querySelector('[data-action="m-fa-confirm"]')?.click(); return; }
  if (m.fileAll || wiz.open || ed.open || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.activeElement?.matches("input:not([type=checkbox]), select, textarea")) return;
  const move = (rows, key, get, set) => {
    const i = rows.findIndex((x) => get(x) === key);
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); set(get(rows[Math.min(rows.length - 1, i + 1)] || rows[0])); }
    else if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); set(get(rows[Math.max(0, i - 1)] || rows[0])); }
    else return false;
    render();
    document.querySelector(".m-row.selected, tr.selected")?.scrollIntoView({ block: "nearest" });
    return true;
  };
  if (state.view === "statement") {
    if (move(stRows(), m.stSel, (t) => t?.url, (u) => { m.stSel = u; m.stOther = false; m.stPick = null; })) return;
    const t = (state.statement?.rows || []).find((x) => x.url === m.stSel);
    if (e.key === "Escape" && m.stSel) { m.stSel = null; render(); return; }
    const r = t && suggestedFile(t);
    if (e.key === "Enter" && r && !approveBlocker(r) && (state.snap.freeagent || {}).connected) {
      e.preventDefault();
      act(() => api("/api/statement/approve", { method: "POST", body: { url: t.url, receipt_id: r.id } }));
    }
    return;
  }
  if (state.view === "expenses") {
    if (e.key === "Escape" && m.xsel) { m.xsel = null; render(); return; }
    move(xRows(), m.xsel, (x) => x?.id, (v) => { m.xsel = v; });
    return;
  }
  if (m.multi.length > 1) {
    if (e.key === "Escape") { m.multi = []; render(); }
    else if (e.key === "Backspace" || e.key === "Delete") { e.preventDefault(); act(() => ignoreMany(m.multi)); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") { m.multi = []; }
    if (m.multi.length > 1 || e.key === "Escape") return;
  }
  const rows = fileRows(fileGroups());
  if (move(rows, m.sel, (x) => x?.id, (v) => { m.sel = v; m.anchor = v; })) return;
  const r = rows.find((x) => x.id === m.sel);
  if (!r) return;
  const next = nextIn(rows, r.id);
  const setNext = () => { m.sel = next; };
  if (e.key === "e" || e.key === "E") {
    e.preventDefault();
    act(() => setPaid(r, savedPaid(r) === "personal" ? "business" : "personal"));
  } else if (e.key === "Backspace" || e.key === "Delete") {
    e.preventDefault(); act(() => ignoreOne(r, setNext));
  } else if (e.key === "Enter") {
    e.preventDefault();
    if (savedPaid(r) === "personal") { if (!fileBlocker(r) && (state.snap.freeagent || {}).connected) act(() => fileOne(r, setNext)); }
    else if (r.stage === "link") goToStatement(r);
    else if (r.stage === "check" && r.total != null) act(() => api(`/api/receipts/${r.id}/fields`, { method: "POST", body: { checked: true } }));
  }
});
