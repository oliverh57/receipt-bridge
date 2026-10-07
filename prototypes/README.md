# Receipt-reading prototypes (2026-10-06)

Throwaway code from testing PLAN.md §5 against the photos in
`tests/fixtures/photos/`. Not used by the app. **Superseded** by
`native/receipt-reader.swift`, `app/receipt_reader.py` and
`app/receipt_text.py`, tested by `tests/test_receipt_text.py` (rules, all 47
photos, zero-wrong-values gate) and `tests/test_receipt_reader.py` (OCR end
to end).

- `ocr_rows.swift` — Vision OCR (auto language detection: English, Japanese,
  German, French, Italian, Spanish), rows rebuilt by vertical position.
  `swiftc -O ocr_rows.swift -o ocr_rows && ./ocr_rows photo.jpg > photo.txt`
- `receipt_rules.py` — the full generic rule set: total (with payment-line
  confirmation), currency, UK VAT number, printed UK VAT, date. Score it:
  `../.venv/bin/python receipt_rules.py --score ../tests/fixtures/photos/expected.yaml ../tests/fixtures/photos/ocr` (also `expected-holdout-1.yaml`, `expected-holdout-2.yaml`). OCR output for every photo is saved in `tests/fixtures/photos/ocr/`, so scoring needs no re-OCR.
- `duplicates.py` — same-purchase check (amount + card auth code, or amount +
  date + VAT number) and DUPLICATE / RE-PRINTED flags.
  `../.venv/bin/python duplicates.py ../tests/fixtures/photos/ocr`
- `receipt_totals.py` — earlier totals-only version, superseded.
- `model_extract.swift` — Apple on-device model extraction. Kept to show why
  it isn't used for money: it fabricated totals, VAT and VAT numbers, and
  refuses Japanese, Danish and some English receipts ("Unsupported language").

Result on 47 photos (20 development + 27 held out in four blind batches):
0 wrong values, 5 blanks after fixes. Blind accuracy on UK receipts rose
66% → 84% → 96%; the foreign batch (set 4) scored 83% with 2 linked wrong
values, now fixed. Model: 3 of the first 15 totals right,
several refused. See PLAN.md §5.2 and §5.5.
