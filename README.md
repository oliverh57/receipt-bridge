# Receipt Bridge

A Mac app that collects your receipts (photos from your phone, and supplier
emails from Gmail), reads them on this Mac, matches each to its bank payment
in FreeAgent, and files it there with the receipt attached. It replaces
FreeAgent Smart Capture. The design and its reasoning are in `PLAN.md`;
one-off setup steps are in `SETUP-FOR-YOU.md`.

## Install

Open **Terminal** (in Applications → Utilities), paste this line and press
Return:

```bash
curl -fsSL https://raw.githubusercontent.com/oliverh57/receipt-bridge/main/install.sh | bash
```

It downloads Receipt Bridge into a **Receipt Bridge** folder in your home
folder, sets it up, puts **Receipt Bridge** in Applications and opens it. A
setup guide then walks you through connecting FreeAgent, choosing where
receipt photos go, Gmail (optional) and the iPhone Shortcut. It takes about
five minutes and needs no admin password. If macOS asks to install its
developer tools first, click **Install**, wait, then paste the line again.
New versions arrive inside the app (**Update now**).

---

## Using it

1. **Photograph a receipt** with the iPhone "Receipt" Shortcut and answer
   *business account* or *personally*. It lands in the receipt inbox
   (iCloud Drive by default; any synced folder, set in Settings) and appears
   in **Match** within a minute. Email receipts (the optional email tool,
   Settings → Email) arrive every 6 hours. **Check now** in the top
   bar reads the inbox, FreeAgent and email straight away.
   Photos are tidied (cut to the receipt, squared up, grey with the
   contrast lifted) only when the tidied copy reads back with the same
   total, date, VAT and amounts; otherwise the plain photo is used.
   **Open original** always opens the photo exactly as it arrived.
2. **Match** lists only what needs you, each with its one problem in a few
   words: *Which day?*, *No £68.85 payment*, *Expense in
   euros*. Below are **Ready to file**, **Waiting for bank** and **Filed
   today**, folded away. The selected receipt shows its photo or PDF, the
   bank payment it pairs with, and what it will be filed as. A category
   tagged *Guess* was suggested on this Mac; change it if it's wrong.
   Keys: ↑↓ move, ⏎ file, E expense / business, ⌫ ignore, C change payment.
   Every action has **Undo**.
3. **Linking.** You approve every link: **Link** in Statement (⏎), or
   **Link N ready** for all the clean suggestions at once, with **Undo all**.
   The one exception is a supplier you've ticked *Link {supplier}
   automatically from now on* for: its exact matches link themselves. Dry
   run is on until you switch it off in Settings → FreeAgent: it shows the
   exact request and sends nothing.
4. **Statement** shows every payment in a month and whether it has a
   receipt: filed, in Match, or missing. For a missing one: **Use that
   email** (an email showing that amount was found in Gmail), **Add photo**,
   or **No receipt needed** (tax payment, transfer, bank fee…).

5. **Expenses paid personally** stay in **Files** like everything else:
   switch *Paid with* to Expense, set the category, VAT and re-billing by
   hand (there's no bank payment to take them from), then **Save expense**.
   **Expenses** lists only the claims already saved to FreeAgent, a month at
   a time, with **Undo claim**. It has no count: nothing there needs you.

6. **Emails** is for one-off email receipts no supplier rule collects: a
   hotel, a ticket, a shop used once. Until Gmail is connected it shows the
   Gmail sign-in. Then it lists your mail, newest first, with the emails that
   look like receipts marked in green (**Likely receipts** shows only those).
   The search above the list takes Gmail's own syntax (`from:`, `after:`,
   `has:attachment`). Pick one to read it, then **Convert to receipt** (⏎):
   a dialog shows the supplier, date, total and VAT read from it; choose
   **Bank** or **Expense** and **Add to Files**. The attached PDF, or else
   the email itself as a PDF, becomes the receipt.

**Settings → About** (also the app menu's *About Receipt Bridge*) shows the
version, whether the licence file is installed, where your data is kept, the
end user licence agreement (`docs/EULA.md`) and the licence of every
open-source package this copy runs on, read from what's installed.

**Export for FreeAgent** still writes a dated folder of PDFs plus
`manifest.csv`, for an accountant or a manual upload.

Each receipt is tagged by what its PDF is:

| Tag | Meaning |
|---|---|
| **Supplier receipt** | The supplier's own document |
| **Email receipt** | The email *is* the receipt (Yesim) |
| **Email copy** | The supplier's document wasn't available; retried automatically (3 times, 12 hours apart), or click *Get the supplier's receipt* |

### Adding a recurring receipt

A recurring receipt is a supplier that emails a receipt every time; once added,
its receipts are collected by themselves. In **Emails**, open one of its
receipts and click **Turn into recurring receipt**. Or **Settings → Email →
Recurring receipts → + Add recurring receipt**, search your mail and pick one
example. Either way, confirm what it found: sender, total, reference, attached
PDF. A preview shows the filename the rule produces and how many emails it
matches.

For senders that serve many shops — payment processors such as ecommpay —
fill in **Must mention** with the shop's name.

**Edit** changes a supplier's name, on/off, and which emails it matches. Edits
change only those lines in its rule file; comments and everything else stay.

**Delete** is in the Edit sheet, with **Undo** straight after. Deleted rules
go to `data/deleted-suppliers/`, not oblivion, and receipts already collected
are kept.

---

## Setup (once)

New Mac: use the **Install** line above. With the folder already here,
double-click **Install Receipt Bridge.command** instead. Either way it sets
up Python (using the Mac's own if it's 3.10 or newer, otherwise downloading
one into the folder's `.python/` with [uv](https://docs.astral.sh/uv/)),
downloads what the app needs, and puts **Receipt Bridge** in Applications.
Run it again any time to rebuild the app; receipts are kept.

**Licence file.** The Google and FreeAgent app keys aren't on GitHub: they
come in a licence file, `Receipt Bridge.rbkey`, that you give to people you
trust. The setup guide asks for it; drop it on the app (anywhere in the
window) or click **Choose file…**. The keys only identify Receipt Bridge:
everyone still signs in to their own Gmail and FreeAgent. To make a licence
from this folder's `credentials.json` and `freeagent_credentials.json` (after
replacing a key, say):

```bash
.venv/bin/python -m app.licence make "Receipt Bridge.rbkey"
```

> **Gmail: set the Google Cloud consent screen to "In production."** In
> *Testing*, only Google accounts listed as test users can sign in, and Google
> expires the sign-in every 7 days. In production each person clicks past an
> "unverified app" warning once (Google allows 100 users without a review).

Open **Receipt Bridge** from Applications. The first time, a **setup guide**
walks through it: add your **licence file**, connect **FreeAgent** (and tick your business bank
account), choose where receipt photos are saved (it makes `Receipt Inbox`
with `Bank` and `Expense` inside: iCloud Drive, or any folder you pick),
optionally **Connect Gmail**, then scan a QR code to add the iPhone
Shortcut (its link is `SHORTCUT_URL` in `app/setup_guide.py`).
**Set up later** brings it back next launch; **Settings → General → Setup
guide** opens it any time. Gmail access is read-only: the app cannot send,
delete or change mail.

The app runs this folder's code, so after changing code just quit and reopen.
Re-run the installer only if you move the folder. To start the app when
you log in, turn on **Settings → General → Open at login**.

---

## How it's built

```
app/
  mac_app.py        native window (WKWebView), menus, Dock badge
  service.py        background worker: scans, retries, Gmail health, backups
  api.py            local JSON API (127.0.0.1, per-launch token)
  ui/               the interface — HTML, CSS, JS, no build step
  pipeline.py       scan: search → match → extract → PDF → stage
  watchers.py       the rule engine for supplier YAML files
  fetchers/         supplier-site downloaders (Trainline)
  supplier_builder.py / supplier_editor.py   Add and Edit supplier
  email_inbox.py    the Emails view: spotting receipts in the list, reading one email
  photo_inbox.py    receipt inbox: archive, read, flag, stage
  setup_guide.py    first-run guide: inbox folders, the Shortcut's settings file, QR code
  receipt_reader.py / receipt_text.py   on-device OCR (Swift helper) and the reading rules
  freeagent.py      FreeAgent OAuth and API (writes only when the filer allows)
  matcher.py        receipt ↔ bank transaction
  filer.py          filing, VAT splits, recovery, undo
native/             receipt-reader.swift, compiled on first use into data/bin/
watchers/           one YAML rule per supplier (email receipts)
prototypes/         the throwaway code the reading rules were tested with
tests/fixtures/photos/   47 receipt photos with hand-checked answers
build_app.py        builds the .app with a compiled launcher
```

Design rules that hold it together:

- **The UI never waits on the network.** Pages read an in-memory snapshot; slow
  work (scans, retries, Gmail checks) runs on one background worker.
- **The window is never destroyed**, only hidden. Destroying it left a dangling
  pointer that crashed the app.
- **The app is a real executable.** A compiled launcher embeds Python, so macOS
  sees *Receipt Bridge*, not *Python* — which also makes single-instance work.
- **The local server is locked down.** A per-launch token plus a Host check stop
  other web pages, or DNS rebinding, from driving it.

### Trainline

The confirmation email holds a passwordless link with the token in the URL
fragment, so only a real browser can open it. The fetcher declines cookies,
opens *Manage booking*, downloads the *Expense receipt*, checks it really is a
PDF **and that it names this booking's reference**, then files it. About 3
seconds per booking. UK rail fares are zero-rated, so only booking fees carry
VAT.

---

## Commands

```bash
.venv/bin/python cli.py scan          # scan from the terminal
.venv/bin/python cli.py serve         # engine + UI in a browser, for development
.venv/bin/python cli.py test-watcher <id> <file.eml>   # try a rule on a saved email
.venv/bin/python cli.py reset         # clear staged receipts (keeps sign-in)
.venv/bin/python cli.py freeagent-check <statement.csv>   # compare FreeAgent's transactions with a statement
```

### Shipping an update

Raise `VERSION` in `app/updates.py` and push to `updates.repo` in
`config.yaml`. The commit message shows as *What's new*; publish a GitHub
release of that version instead if you want longer notes. Every copy checks GitHub a minute after launch and daily,
posts one notification, and shows **Update now**: it downloads that version,
copies it over the code (never `data/`, `.venv/`, `config.yaml`, the
credential files, or supplier rules already in `watchers/`), installs new
requirements if `requirements.txt` changed, and restarts. The replaced files
are kept in `data/updates/before-<version>.tar.gz` and put back if the
install fails part-way. A git checkout is never overwritten. The repository
must be public: GitHub hides private ones from the check.
`.venv/bin/python -m app.updates` shows what a copy would see.

Tests: `for t in tests/test_*.py; do .venv/bin/python "$t"; done`. The
receipt-reading suite fails on **any** wrong value across the 47 photos; a
blank (left for review) is allowed, a wrong figure is not.

## Files and safety

- `data/accounts/` — one Gmail token per account, owner-only. Signing out
  revokes access with Google.
- `data/receipts.sqlite3` — what has been collected and filed. Backed up daily
  to `data/backups/` (last 7 kept).
- `data/app.log` — rotated at 2 MB.
- `data/freeagent/token.json` — the FreeAgent sign-in, owner-only.
- `data/photos/` (or the archive folder chosen in Settings) — every original
  receipt photo. **Not** in the daily backup: choose a synced folder in
  Settings → Receipt inbox if you want an off-Mac copy.
- `data/`, `backups/`, the app keys and `*.rbkey` licence files are
  gitignored.

## Where this could go

See `PLAN.md` §13 for progress and what's next.
