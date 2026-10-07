# Writing your own watcher

A watcher is one YAML file in this folder. Drop a file in, restart the app,
scan. No Python unless the supplier needs browser automation to get at a real
receipt document — and even then, the YAML is where you wire it up.

## The workflow that actually works

Don't write the YAML blind against a live inbox. Do this instead:

1. In Gmail, open the receipt email → ⋮ → **Download message**. You get a
   `.eml` file. Put it in `tests/fixtures/`.
2. See what your patterns will be matching against:

   ```bash
   python cli.py test-watcher trainline tests/fixtures/your-email.eml --dump-text
   ```

   The `--dump-text` output is the flattened email — HTML stripped, entities
   resolved, one value per line. **Your patterns run against exactly this
   text**, so write them by reading it, not by reading the raw HTML.
3. Write the YAML, rerun `test-watcher` until every field is right.
4. `python cli.py ingest <id> <file.eml>` to check the PDF comes out properly.
5. Only then run a real `scan`.

## Full reference

```yaml
id: acme                    # required, unique, used in filenames and logs
name: Acme Cloud            # shown in the UI
vendor: Acme Cloud Ltd      # goes in the filename and the PDF summary band
enabled: true

# Required. A Gmail search query — this is the server-side filter, so make it
# tight. The app adds an `after:` clause automatically for incremental scans.
gmail_query: 'from:(billing@acme.com) subject:("your receipt")'

# Optional second pass, run locally on the full message. Cheap insurance
# against a marketing blast with a similar subject getting filed as a receipt.
match:
  from_contains: "acme.com"
  subject_contains: "your receipt"
  body_contains: "Invoice number"
  subject_regex: 'Receipt #\d+'
  body_regex: 'Total\s+\$'
  exclude_if_contains:            # string or list; any hit rejects the email
    - "your subscription is ending"

fields:
  # Shorthand: a bare string or list is treated as `patterns`.
  reference: 'Invoice number:\s*([A-Z0-9-]+)'

  # Full form.
  total:
    patterns:                     # tried in order, first match wins
      - 'Total charged:\s*\$([0-9,]+\.[0-9]{2})'
      - 'Amount:\s*\$([0-9,]+\.[0-9]{2})'
    type: money                   # string (default) | money | date
    required: true                # a miss fails the receipt loudly
    source: text                  # text (default) | subject | html | plain | links | sender
    join: " "                     # how multiple capture groups are joined
    template: "{value}"           # reshape, see below
    default: email_date           # email_date | subject | any literal

  currency:
    value: USD                    # a constant, no regex

  purchased_on:
    patterns: ['Paid on\s+([A-Za-z]+ \d{1,2}, \d{4})']
    type: date
    date_formats: ["%B %d, %Y"]   # tried before the fuzzy day-first parser
    default: email_date

  description:
    template: "Acme hosting, {reference}"   # no patterns = pure template

# How to get the PDF. Tried top to bottom; first success wins.
pdf:
  - fetcher: trainline            # a Python plugin in app/fetchers/
    url_field: order_link         # which extracted field holds the link
  - render_email                  # built-in: print the email itself

# Any extracted field is available here, plus {vendor} and {date}.
filename: "{purchased_on} {vendor} {currency}{total} {reference}.pdf"
```

### Notes that will save you time

- **Patterns run with `re.IGNORECASE | re.DOTALL`.** `.` matches newlines, so
  use `[^\n]+` when you want to stop at the end of a line. This bites people
  writing `Card Type:\s*(.+)` and capturing the rest of the email.
- **Labels and values usually end up on separate lines.** Table cells become
  newlines, so write `Total:\s*£([0-9.]+)` — the `\s*` spans the break.
- **Fields are evaluated in the order you declare them**, and `template:` can
  reference any field above it. That is how Trainline builds
  `"Train travel, {origin} to {destination}"`.
- **`source: links`** gives you one `href` per line from the HTML body — the
  way to grab a "manage my booking" link for a fetcher to follow.
- **Currency symbols survive** the flattening, so `£` and `€` work in patterns.
- **`type: date` always outputs `YYYY-MM-DD`.** Bare dates are parsed
  day-first, so `03/09/2026` is 3 September, not 9 March.
- Prefer the **transaction/payment date** over any service or travel date.
  It is what the bank statement line shows, which is what you are reconciling.

## When you need a fetcher

Most suppliers put everything on the receipt email, and `render_email` is
enough. Write a fetcher only when the supplier has a real receipt document
behind a link — see `app/fetchers/trainline.py` for a worked example,
including how it handles the cookie dialog and verifies it got a real PDF
rather than an expired-token error page.

A fetcher is a class with an `id` and a `fetch(ctx) -> FetchResult | None`,
registered with `register()`. Returning `None` is a normal outcome: the
pipeline just falls through to the next `pdf:` step. **Never raise** — a
supplier redesigning their site should downgrade the receipt, not break the
scan.
