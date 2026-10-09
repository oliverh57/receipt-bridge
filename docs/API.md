# Receipt Bridge local API (for the UI)

The engine serves the UI from `app/ui/` and this JSON API on
`http://127.0.0.1:8765` (`cli.py serve --port N` for development). The UI
is a client of these endpoints only; all logic lives in Python.

**Auth.** Every `/api/` request needs the per-launch token: header
`x-receipt-bridge: <token>` (the index page gets it substituted for
`__TOKEN__`). GETs may pass `?t=<token>` instead, which is how `<img>` and
`<iframe>` load documents. Requests whose Host isn't localhost get 403.

**Rule of thumb.** Every endpoint answers at once. Slow work is *queued*
on one background worker; poll `/api/state` and watch `version`,
`activity`, `queued` and `outcome` to see it run and finish. Errors are
`{"detail": "<message for a person>"}` with status 400.

---

## Polling

`GET /api/state`: the snapshot. Cheap; poll every ~1 s while
`activity.busy || queued.length`, every ~4 s otherwise. Redraw only when
`version` changes.

| Field | Meaning |
|---|---|
| `version` | bumps on every change |
| `activity` | `{busy, kind, label, current, total, started_at, log[]}`. `kind`: `scan`, `retry`, `connect`, `health`, `photos`, `freeagent`, `file` |
| `queued` | job keys waiting or running, e.g. `scan`, `photos`, `freeagent-sync`, `file:12,15`, `unfile:12`, `retry:7`, `auto-link` |
| `outcome` | last finished job: `{kind, ok, message, at, found}` |
| `counts` | `{pending, exported, ignored, failed, needs, missing, files}` (`files` = everything in Files, i.e. not saved to FreeAgent yet, expenses included; `missing` = payments this month without a receipt, first chosen account) |
| `totals.pending` | `{currency: sum}` |
| `accounts[]` | Gmail: `{email, state, detail, checked_at, read_only, receipts, last_scan_at}` |
| `freeagent` | see below |
| `photo_inbox` | `{path, exists, has_subfolders, is_default, archive, archive_is_default, waiting_bytes, too_big}` |
| `auto_scan`, `theme`, `export_dir`, `has_credentials`, `connecting`, `notifications`, `last_log` | as before |
| `notification_prefs` | `{enabled: bool, frequency: instant\|hourly\|daily, categories: {new, filed, problems: bool}, waiting}`; `waiting` = messages held for the next summary |

`freeagent`: `{has_credentials, environment ("sandbox"|"live"), connected,
error, vat: {registered, scheme, currency, company}, bank_accounts:
[{url, name, currency, type, status, is_personal, chosen}], categories (a
count), last_sync, transactions, projects}`. There is no app-wide auto-filing switch.

## Receipts

`GET /api/receipts?status=pending|exported|ignored|failed`. `exported` is
the "Filed" view: exported folders *and* receipts filed into FreeAgent.

Each receipt:

| Field | Meaning |
|---|---|
| `id`, `supplier`, `date`, `total`, `currency`, `description`, `reference`, `filename`, `status` | basics. `total` may be `null` (couldn't be read); `currency` may be `""` for a photo (unknown: show the number with "?", never £) |
| `source` | `gmail` or `photo` |
| `document` | `supplier`, `email`, `fallback`, `photo`, `none` (what the attached document is) |
| `has_pdf`, `is_image` | load it from `GET /api/receipts/{id}/pdf?t=…`: `<img>` when `is_image`, else `<iframe>` |
| `paid_by` | `business`, `personal`, or `null` (not asked yet) |
| `vat`, `vat_number`, `total_status` | printed VAT; UK VAT number; `confirmed` / `unconfirmed` / `missing` / `confirmed by you` |
| `flags[]` | plain-words things to check (photos). Empty = nothing to check |
| `category`, `category_name` | FreeAgent category URL and name (the receipt's, or the one remembered for the supplier) |
| `vat_treatment` | supplier setting: `printed` or `reverse_charge` |
| `auto_file` | you ticked "Link {supplier} automatically from now on" for this supplier |
| `match` | `null` (no account chosen) or `{status, reason, candidates, transaction}`. `status`: `matched` (a payment chosen; identical ones resolve to the nearest date), `choose` (no date on the receipt, several fit), `waiting` (none yet), `expense_but_found` (marked personal but an exact business payment exists), `not_applicable`. `overdue: true` when `waiting` longer than `freeagent.waiting_days`: show it as needing attention. `transaction`: `{date, amount, description, explained}`; `explained: true` means FreeAgent already explained it, and filing will only attach the receipt |
| `filing` | last filing state or `null`: `{state, kind, ...}`. `state`: `problem` (`message`), `filing`/`explained` (interrupted; filing again resumes), `filed`, `unfiled`. `kind`: `explanation`, `expense`, `attach` |
| `error`, `account`, `exported_at`, `export_folder`, `watcher` | as before |

| Call | Body | Does |
|---|---|---|
| `POST /api/files/upload?name=&paid_by=business|personal` | the file's bytes | a file dropped on Files or Expenses: into the receipt inbox, read at once. Photos (JPEG, PNG, HEIC, WebP) and PDFs, up to 30 MB |
| `POST /api/receipts/{id}/fields` | `checked: true` ("Looks right": settles the file's doubts, except a total, currency or payer that's still blank), or any of `vendor`, `purchased_on` (YYYY-MM-DD), `total`, `currency` (3 letters), `category` (URL), `paid_by`, `vat_treatment`, `auto_file` (bool: the supplier's "link automatically" approval), `rebill` | correct a receipt. Clears that field's flags; remembers category, VAT treatment and the auto-link approval per supplier, and supplier name per VAT number. 400 with a reason if invalid |
| `POST /api/receipts/file` | `{ids: [..]}` | queue filing. Read results from `filing` and `outcome` |
| `POST /api/receipts/{id}/unfile` | — | queue undo: deletes only what the app created (or removes only the attachment it added) |
| `POST /api/receipts/status` | `{ids, status: "pending"|"ignored"}` | ignore / restore |
| `POST /api/receipts/{id}/retry` | — | email: fetch the supplier's PDF again; photo: read the archived original again |
| `POST /api/export` | `{ids}` | export folder + manifest (as before) |

**What can be filed:** the button should be enabled when `total != null`
and either (a) `paid_by == "personal"` with a `category`, or (b) `match.transaction`
exists and either `match.transaction.explained` or there's a `category`.
"Link N ready" (Statement) = suggested files with `group == "ready"`.

## The Match screen (PLAN.md §11)

Every pending receipt (and every filed one) also carries what the Match
screen shows, worked out in `app/review.py`:

| Field | Meaning |
|---|---|
| `stage` | where it lives (2026-10-07 layout): `check` (Files: something about the file itself to check), `link` (Files → Statement: a payment is suggested, or close options), `waiting` (Files: no payment yet), `expense` (Expenses), `filed` |
| `issues[]` | the file-level problems behind `check`: "Not a receipt?", "No total", "Possible duplicate", "Check the date", "Who paid?"… ("Check the total" / "Check supplier" don't count when an exact payment names the supplier) |
| `group` | `needs` (Needs you), `waiting` (Waiting for bank), `ready` (nothing to check: link it in Statement), `filed` |
| `reason` | the list line, a few words: "Which day?", "No £68.85 payment", "Expense in euros", "Possible duplicate", "Check the date", "Choose a category", "Waiting for bank"… `reasons[]` has them all |
| `checks[]` | chips `{name, ok}`: Total / Check total, VAT, VAT number / No VAT number, Date / No date, No exact payment, Not GBP |
| `payment` | the chosen payment or `null`: `{description, date, amount, chips[], pinned, explained}`. `chips`: "Amount" (or "+£6.89" for a chosen bigger payment), "Name", "Date" (within the usual settling days), or "+21 days" for a payment you chose further away (`far: true`) |
| `options[]` | for "Which payment is this?" (`match.status == "choose"`) and near misses (`waiting`): `{url, date, amount, description, difference}`. "This one" / "Use this" → `POST /api/receipts/{id}/payment {url}` |
| `waiting_until` | Waiting for bank: the date it moves to Needs you |
| `will_file` | `{type, category, vat, attachment}`, e.g. `{"Bank explanation", "Travel", "£0.55 (20%)", "Photo"}`; `vat` may read "£0.52 (20%) + £0 (0%)", "Reverse charge", "£0 (foreign VAT)", "… + £6.89 tip (0%)" |
| `ai_guess.supplier` | the supplier name was guessed: tag it "Guess" |
| `ai_guess.category` | `category` is a suggestion made on this Mac (only ever one of the FreeAgent expense categories): tag it "Guess". Choosing a category replaces it; filing with it keeps it for the supplier. Never filed automatically |
| `photo_taken` | the day the photo was taken (photos), for "Use photo date" when the receipt shows none (→ `fields` `purchased_on`) |
| `page_count` | a PDF receipt rendered as page images (`GET /api/receipts/{id}/page/{n}?t=…`, n from 1), so its highlights can be drawn; 0 until the background read has run |
| `highlights[]` | photos and PDFs (`page` from 0): `{field: supplier|total|date|vat_number, label, page, box: [x, y, w, h]}` as fractions of the upright photo, worked out from the current values (a correction moves its box) |
| `rebill` | `null` or `{project, type: cost|markup|price, factor}`: re-bill to a client's project (`freeagent.projects[]`: `{url, name, client, currency}`, active projects read with each sync). `factor` is the markup in percent or the price in £; set via `fields` `rebill` (`null` to stop). Sent as `project`, `rebill_type`, `rebill_factor` (markup as a fraction) on explanations and expenses; a set price needs a receipt saved as one entry |
| `native_gross` | "£ charged" on a foreign expense (edit via `fields`) |
| `pinned_payment` | the payment URL you chose, if any |
| `filed_today`, `filed_automatically` | for "Filed today" |

`match.status` is now `matched`, `choose`, `waiting`, `expense_but_found` or
`not_applicable` (`likely` is gone: identical payments take the nearest date).

Auto-linking is only ever per supplier and only when you ask: `fields`
`auto_file: true` ("Link {supplier} automatically from now on", unticked by
default; `false` turns it off). Filing by hand never turns it on. Then a
receipt from that supplier links and saves itself when the payment matches
exactly with its name, nothing needs checking, the category isn't a guess,
and it isn't an expense.

| Call | Body | Does |
|---|---|---|
| `POST /api/receipts/{id}/payment` | `{url}` or `{url: null}` | pin a payment ("This one", "Use this", Change payment), or back to automatic |
| `POST /api/receipts/unfile` | `{ids}` | "Undo all" after File all |
| `GET /api/statement?account=&month=YYYY-MM` (or `month=all&limit=N&offset=M`) | — | the Statement screen: `{account, account_name, month, limit, offset, total_payments, last_sync, rows[], summary}`. `month=all`: every month, newest first, a page of `limit` payments from `offset` (`limit=0`: all); `total_payments` is how many there are in all. Rows that can't take a receipt here carry `blocked` (why). Row: `{url, date, description, amount, status: filed|in_match|missing|not_needed, detail, receipt: {id, vendor, date} | null, suggestion}`. `suggestion` (missing rows only, or `null`): `{kind: "email"|"ignored", label, …}`, e.g. label "Found an Amazon email for £23.99 on 27 Sep."; emails are looked for in Gmail for payments in the last 60 days, each once a day for two weeks. Summary: `{payments, with_receipt, filed, in_match, missing, not_needed, out, in}` ("19 of 22 payments have a receipt" = `with_receipt` of `payments`) |
| `POST /api/statement/no-receipt` | `{url, reason}` / `{url, reason: null}` | "No receipt needed" (reason e.g. "Tax payment"), or undo |
| `POST /api/statement/use-suggestion` | `{url}` | "Use that email" / "Use that receipt" on a `missing` row with a `suggestion` (queued; `outcome` says how it went). The email becomes a receipt (its PDF attachment, or the email printed) paired with that payment; an ignored receipt comes back to Match paired with it |
| `POST /api/statement/find-emails` | — | look in Gmail now (it also runs after each FreeAgent read and email check) |
| `POST /api/statement/add-file` | `{paid_by?}` | the Mac's file picker; the file goes into the receipt inbox (`{added: path | null}`) |

`state.file_results` after File / File all: `{at, automatic, message?,
results: [{id, vendor, total, ok, note}]}`. A skipped one's `note` says why,
e.g. "This payment was explained in FreeAgent since the last check, so it was
left alone. The receipt is back in Match."

`state.freeagent.web`: the company's FreeAgent home page ("View in
FreeAgent"); links to a single explanation aren't documented.

## FreeAgent

| Call | Body | Does |
|---|---|---|
| `POST /api/freeagent/connect` | — | opens FreeAgent sign-in in the browser; returns `{url}` |
| `GET /freeagent/callback` | (FreeAgent's redirect) | not for the UI |
| `POST /api/freeagent/disconnect` | — | forget the sign-in and the cached data |
| `POST /api/freeagent/accounts` | `{urls: [...]}` | choose business bank accounts (from `freeagent.bank_accounts`) |
| `GET /api/freeagent/categories` | — | `[{url, description, nominal_code, group}]`; `group` is `admin_expenses_categories`, `cost_of_sales_categories`, `general_categories` |
| `POST /api/freeagent/sync` | — | queue a read now |

## Receipt inbox (folders)

| Call | Body | Does |
|---|---|---|
| `POST /api/settings/choose-folder` | `{which: "inbox"|"archive"}` | the Mac's folder picker; returns `{path}` or `{path: null}` if cancelled. Doesn't save |
| `POST /api/settings/folders` | `{inbox?, archive?}` | save (validated: inbox must exist; neither inside the other) |
| `POST /api/settings/folders/reset` | `{which}` | back to the default |
| `POST /api/settings/folders/create-subfolders` | — | creates `Bank/` and `Expense/` in the inbox |
| `POST /api/settings/open-folder` | `{which}` | reveal in Finder |

Changing the inbox also rewrites `Receipt Bridge.txt` in iCloud Drive › Shortcuts
(the inbox's path inside iCloud Drive), which the iPhone Shortcut reads to know
where to save. Moving the inbox out of iCloud Drive removes it.

## First-run setup guide

`state.setup` is `{done, icloud, icloud_inbox, shortcut_saves_to, shortcut_url}`:
whether the guide has been finished, whether iCloud Drive is on, the inbox's path
inside iCloud Drive (or null), the fixed iCloud Drive folder the shared Shortcut
saves to, and its iCloud link (`SHORTCUT_URL` in app/setup_guide.py, or config
`iphone.shortcut_url`).

| Call | Body | Does |
|---|---|---|
| `POST /api/setup/inbox` | `{location: "icloud" \| path}` | makes the inbox (`Receipt Inbox` in iCloud Drive or inside the chosen folder, unless the folder already is one) with `Bank/` and `Expense/`, and uses it; returns `{path}` |
| `POST /api/setup/done` | `{done?: bool}` | the guide stops opening at launch (default true) |
| `GET /api/setup/shortcut-qr` | — | the Shortcut link as a QR code PNG; 404 with no link |
| `POST /api/setup/open-shortcut` | — | opens the Shortcut link in the Mac's browser |

## A payment's own FreeAgent settings (Statement)

| Endpoint | Body | Does |
|---|---|---|
| `POST /api/statement/payment` | `{url, changes: {category?, vat_rate?: "20.0"\|"5.0"\|"0.0", rebill?}}` | Saves the payment's category / VAT rate / re-billing; returns `{settings}` |
| `POST /api/statement/explain` | `{url, reason?}` | "No receipt needed" with a category: explains the whole payment in FreeAgent, nothing attached |
| `POST /api/statement/update-explanation` | `{url}` | A payment FreeAgent explained: sends what you changed (category, VAT rate, re-bill) to its explanation |
| `POST /api/statement/payment-reset` | `{url}` | "Keep FreeAgent's": forgets what you set on the payment |
| `POST /api/statement/unexplain` | `{url}` | Undo: deletes the explanation the app made, or puts FreeAgent's own one back as it was |

Statement rows carry `approved` (FreeAgent's explanation is approved there: `marked_for_review` false; status `approved` when it has no receipt, counted as done), `settings`, `freeagent: {explained, category, vat_rate, rebill, changes}` (FreeAgent's own explanation, and what you changed that isn't sent yet) and `explained_here` (`{state: filed|updated, body, url?}`).

## Emails (any one email into Files)

The **Emails** view, for one-off receipts no supplier rule collects. Unlike
the rest of the API, the list and an opened email are read from Gmail while
the request waits (like the supplier search): there is nothing to show until
Gmail answers. Adding one is queued. Signed out (no account, or all expired),
the UI shows the Gmail card from Settings instead.

| Call | Body | Does |
|---|---|---|
| `GET /api/emails?account=&q=&receipts=true|false&page=` | — | one page (50) of the mailbox, newest first: `{account, emails[], next}`. `q` is Gmail search syntax; `receipts=true` narrows to likely receipts (Gmail searches for receipt words); `page` is the last answer's `next` (`""` at the end). Chats, drafts, sent, spam and bin are left out. 400 "Connect a Gmail account first." when signed out |
| `GET /api/emails/{message_id}?account=` | — | the whole email: `{id, thread_id, account, subject, from_name, from_address, to, date, html, text, attachments: [{filename, content_type, size}], draft, in_files}`. The UI shows `html` only in a sandboxed `srcdoc` iframe |
| `POST /api/emails/known` | `{ids: [..]}` | which of these emails are already receipts: `{message_id: {id, status, vendor}}`. Local only, for redrawing the list after a change |
| `POST /api/emails/{message_id}/add` | `{account, supplier, date, total, currency, vat, vat_choice, paid_by, payment_url?, document?}` | queue "Add to Files" (`email-add:{id}`). The attached PDF, or the email printed, becomes the receipt; `paid_by: personal` makes it an expense. `payment_url` (Bank Feed's "Use that email") pairs it with that payment. `document`: what becomes the receipt, `email` (printed) or `att:N` (that attachment, a PDF or picture; see `receipt` on each attachment of `GET /api/emails/{id}`); default its first PDF, else the email. 400 with a reason for a bad field, a payment that can't take a receipt, or an email that's already a receipt; an ignored one comes back |

Each list email: `{id, thread_id, account, subject, from_name, from_address,
date, snippet, has_attachment, unread, amount: {amount, currency, as_total} | null,
receipt: "likely" | "maybe" | null, why[], in_files}`. `receipt` is judged
from the list metadata alone (subject, sender, snippet, Gmail's tabs) in
`app/email_inbox.py`; `why` says what made it look like one.

`draft` (what the email would become, read from the whole email and any PDF):
`{supplier, date, total, currency, vat, total_label, total_found_in, amounts[], pdf}`.
Anything not found is `null`, never guessed.

## Email receipts (the separate Gmail tool)

`POST /api/scan` (Check email), `POST /api/accounts/connect` (`{scan_from?: "YYYY-MM-DD"}`: how far back the new mailbox's first scan looks), `POST /api/accounts/disconnect|check`,
`GET /api/suppliers`, and the supplier builder/editor routes
(`/api/suppliers/search|analyse|preview`, `POST /api/suppliers`,
`GET|POST /api/suppliers/{id}…`, `/api/suppliers/restore`): unchanged.

## Other

`POST /api/check-now` ("Check now" in the top bar: queues the receipt inbox,
a FreeAgent read and, when Gmail is connected, an email check; returns
`{queued: [...]}`), `POST /api/settings` `{auto_scan?, theme?, waiting_days? (1–90), notify_enabled?, notify_frequency?, notify_categories? ({new?, filed?, problems?: bool})}`, `POST /api/reveal` `{name}`,
`POST /api/notifications/test`, `POST /api/notifications/open-settings`.

## Updates

`state.update` is `{current, latest, available, can_install, installing, checking, url, notes,
error, checked_at, git_checkout, restarts, repo}`: this copy's version (`app/updates.py`) and the
last answer from GitHub (`updates.repo` in `config.yaml`: the version on the default branch,
or a release of the same or higher version, for its notes). Checked a minute after launch and then daily; a new version is notified
once (category `updates`, never held for a summary).

| Call | Does |
|---|---|
| `POST /api/updates/check` | check now; `{started: false}` if a check is already running |
| `POST /api/updates/install` | queue the install on the worker; the app restarts when it's done (400 when there's nothing to install, or this is a git checkout) |
| `POST /api/updates/open` | open `url` (always a github.com page) in the browser |
