"""Generic receipt rules, prototype for PLAN.md §5.2 / §6.4.

Reads OCR rows (one text file per receipt, from ocr_rows.swift) and returns
total, whether a payment line confirms it, currency, UK VAT number, printed
UK VAT and date. No rules for individual shops.

    python3 receipt_rules.py ocr/*.txt                 # print results
    python3 receipt_rules.py --score expected.yaml ocr/ # score against answers
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

# ---- amounts ---------------------------------------------------------------

# A trailing amount. ",dd"/".dd" is decimals, ",ddd"/".ddd" thousands; yen has
# none. Masked card digits ("xxxx6335", "****2685") are never amounts.
AMOUNT = re.compile(
    r'(-?)\s*(?:[£€$¥·]|EUR|GBP|DKK|AUD|USD|JPY)?\s*'
    r'(?<![xX*#\d])(\d{1,3}(?:[,.]\d{3})+|\d+)(?:[.,](\d{2}))?(?!\d)'
    r'\s*(?:€|EUR|DKK|円)?\s*(?:\d|[A-Z<])?\s*$'
)
MONEY_LOOKING = re.compile(r'[£€$¥·]|\d[.,]\d{2}\b')
ISO_TRAILING = re.compile(r'^\s*\b(EUR|GBP|DKK|AUD|USD|JPY|ARS|MXN|BRL|CAD)\b|\b(EUR|GBP|DKK|AUD|USD|JPY|ARS|MXN|BRL|CAD)\s*$')
LETTERS = re.compile('[A-Za-z぀-ヿ一-鿿]{3}')


def amount(row: str) -> str | None:
    row = row.replace('）', '').replace(')', '')
    row = re.sub(r'(\d)[.,]\s+(\d{3})\b', r'\1,\2', row)    # "15.   300"
    m = AMOUNT.search(row)
    if not m:
        return None
    whole = re.sub(r'[,.]', '', m.group(2))
    return f"{int(whole)}.{m.group(3) or '00'}"


# ---- labels: generic, several languages ------------------------------------

TOTAL = re.compile(
    r'BALANCE DUE|BALANCE TO PAY|\bTOTAL\b|\bTOTAAL\b|NOG TE BETALEN|AMOUNT DUE|\bTO PAY\b|ZU ZAHLEN|'
    r'\bSUMME\b|\bGESAMT|\bTOTALE\b|À PAYER|総合計|合計|金額', re.I)
NOT_TOTAL = re.compile(
    r'SUB\s*TOTAL|FOOD TOTAL|DRINK TOTAL|NET\s*TOTAL|POINTS|EX\.?\s*(TAX|VAT|GST|BTW|MWST)|TAXIMETER|'
    r'SAVING|BEFORE|本体合計|小計|NETTO', re.I)
PAYMENT = re.compile(
    r'^\s*(CARD|DEBIT MASTERCARD|MASTERCARD|VISA|AMEX|CREDIT CARD|AMOUNT|APPLE[ _]PAY|GOOGLE[ _]PAY|PAYMENT\b|DOJO\b|'
    r'KARTENZAHLUNG|EC-?KARTE|その他クレジット|クレジット|差引き現金支払い額|'
    r'現金|CASH|BAR\b|DINHEIRO|EFECTIVO|ESPÈCES|CONTANTI|CREDIT\b)', re.I)
NOT_PAYMENT = re.compile(
    r'お預かり|お釣り|つり銭|RÜCKGELD|CHANGE|GEGEBEN|TROCO|CAMBIO|MONNAIE|'
    r'SURCHARGE|GEBÜHR|CARD\s*(?:NO|NUMBER)|CARD:\s*[\dX*]{6,}', re.I)
POUND_MISREADS = '82EC€'   # "£6.99" seen as "86.99", "26.99", "E6.99", "C6.99"


def _neighbour(rows: list[str], i: int) -> str | None:
    """A label with no amount takes it from the row below or above, if that
    row looks like money and isn't another labelled line."""
    for j in (i + 1, i - 1):
        if not 0 <= j < len(rows):
            continue
        row = rows[j]
        a = amount(row)
        bare = ISO_TRAILING.sub('', row)
        if a and MONEY_LOOKING.search(row) and (not LETTERS.search(bare) or PAYMENT.search(row)):
            return a
    return None


ZERO_DECIMAL = {'JPY', 'KRW'}


CHANGE_GIVEN = re.compile(r'^\s*(CHANGE|RÜCKGELD|TROCO|CAMBIO|お釣り|つり銭)\b', re.I)


def find_total(rows: list[str], currency: str = '?') -> tuple[str | None, str]:
    totals, payments, raw_payments = [], [], []
    change = 0.0
    for i, row in enumerate(rows):
        if CHANGE_GIVEN.search(row) and (c := amount(row) or _neighbour(rows, i)):
            change += float(c)      # cash handed over minus change = amount paid
        if NOT_PAYMENT.search(row) and not TOTAL.search(row):
            continue
        a = amount(row) if re.search(r'\d', row) else None
        if a and currency not in ZERO_DECIMAL and not MONEY_LOOKING.search(row):
            a = None    # "A0000000041010" is a card chip ID, not €41,010
        a = a or _neighbour(rows, i)
        if not a or a == '0.00':
            continue
        if TOTAL.search(row) and not NOT_TOTAL.search(row):
            totals.append(a)
        elif PAYMENT.search(row):
            raw_payments.append(row)
            payments.append(a)
    candidates = sorted(set(totals), key=lambda x: -float(x))

    def unmisread(p: str) -> str:
        # One stray leading glyph where "£" was: accept only if it then equals a total.
        for t in candidates:
            if len(p) == len(t) + 1 and p.endswith(t) and p[0] in POUND_MISREADS:
                return t
        return p

    payments = [unmisread(p) for p in payments]
    paid = sum(float(p) for p in payments)
    if len(payments) > 1 and len(set(payments)) == 1:
        paid = float(payments[0])
    paid -= change
    for t in candidates:
        if payments and abs(float(t) - paid) < 0.005:
            how = 'payment line' if len(payments) == 1 else 'payments ' + ' + '.join(payments)
            return t, f'confirmed by {how}'
    if len(candidates) == 1:
        return candidates[0], 'unconfirmed'
    if not candidates and len(set(payments)) == 1:
        # card slip or ticket: no total line, only the amount charged
        return payments[0], 'unconfirmed (payment line only)'
    if not candidates and len(payments) > 1:
        # several payments and the TOTAL label lost: confirmed if a bare
        # amount row equals what was paid
        bare = {amount(r) for r in rows if amount(r) and MONEY_LOOKING.search(r) and not LETTERS.search(r)}
        t = f'{paid:.2f}'
        if t in bare:
            return t, 'confirmed: payments ' + ' + '.join(payments) + ' = unlabelled total'
        return t, 'unconfirmed (sum of payment lines)'
    return None, f'needs attention: totals {candidates}, payments {payments}'


# ---- currency ----------------------------------------------------------------

ISO = re.compile(r'\b(GBP|EUR|DKK|SEK|NOK|CHF|USD|AUD|NZD|CAD|PLN|CZK|HUF|JPY|CNY|HKD|SGD|KRW|THB|INR|AED|ZAR|MXN|BRL|ARS|CLP|COP|PEN|TRY|ISK)\b')
JAPANESE = re.compile('[぀-ヿ]')
UK_VAT_LABEL = re.compile(r'\bVAT\b', re.I)


def find_currency(text: str) -> str:
    if '£' in text or UK_VAT_LABEL.search(text):
        return 'GBP'
    m = ISO.search(text)
    if m:
        return m.group(1)
    if JAPANESE.search(text) or '¥' in text or '円' in text:
        return 'JPY'
    if '€' in text:
        return 'EUR'
    if '$' in text:
        return '$?'     # AUD / USD / NZD / CAD: photo location, or you
    return '?'


# ---- UK VAT number and printed VAT (GBP receipts only) -----------------------

VAT_NUMBER = re.compile(
    r'\b(?:VAT|Tax\s*ID)\s*(?:N[o0]\.?|Number|Reg(?:istration)?(?:\s*N[o0]\.?)?)?\s*[:.]?\s*'
    r'((?:GB|G8|C3|CB|GE)?\s*(?:\d\s?){9}(?:(?:\d\s?){3})?)(?!\d)', re.I)
# A VAT *amount* line: "VAT 4.99", "VAT 20.0% £0.67", "T2 VAT @ 20% £2.58 £0.52",
# "20% VAT included". Never a VAT-number line.
VAT_LINE = re.compile(r'\bVAT\b', re.I)
VAT_ID_LINE = re.compile(r'\bVAT\s*(?:N[o0]|Number|Reg)|\bVAT\s*[:.]?\s*(?:GB|G8|C3)?\s*\d{3}\s*\d{3,4}|ex\s*VAT|inc\s*VAT', re.I)
NOT_VAT_RECEIPT = re.compile(r'NOT\s+A\s+(VAT|TAX)\s+(RECEIPT|INVOICE)', re.I)
MAX_UK_VAT_SHARE = 20 / 120     # VAT can't exceed a sixth of a VAT-inclusive total


def find_vat_number(text: str, currency: str) -> str | None:
    if currency != 'GBP':
        return None     # foreign tax numbers are never UK VAT evidence
    if NOT_VAT_RECEIPT.search(text):
        return None     # the receipt says so itself (Wetherspoon e-receipts)
    m = VAT_NUMBER.search(text)
    if not m:
        return None
    digits = re.sub(r'\D', '', m.group(1))
    return 'GB' + digits if len(digits) in (9, 12) else None


def find_printed_vat(rows: list[str], currency: str, total: str | None) -> str | None:
    """Sum of printed VAT amounts (one per rate in a VAT table). Anything
    larger than a sixth of the total is not VAT, e.g. a VAT number misread."""
    if currency != 'GBP':
        return None
    found = []
    for i, row in enumerate(rows):
        if not VAT_LINE.search(row) or VAT_ID_LINE.search(row):
            continue
        rate_m = re.search(r'(\d+(?:\.\d+)?)\s*%', row)
        tail = re.sub(r'\d+(?:\.\d+)?\s*%', '', row)          # drop "20.0%"
        if not re.search(r'\d[.,]\d{2}', tail):
            for j in (i + 1, i + 2):                             # table numbers below the header
                if j < len(rows) and re.search(r'\d[.,]\d{2}', rows[j]) and not LETTERS.search(rows[j]):
                    tail = rows[j]
                    break
        money = [float(x.replace(',', '.')) for x in re.findall(r'\d+[.,]\d{2}', tail)]
        a = None
        if len(money) >= 2:
            rates = [float(rate_m.group(1))] if rate_m else [20.0, 5.0]   # UK standard / reduced
            # "0.52 VAT 20% 3.10" or "£2.58 £0.52": VAT is the amount that is
            # rate/(100+rate) of a gross, or rate/100 of a net, on the same line
            for r in rates:
                for v in money:
                    for base in money:
                        if v != base and (abs(v - base * r / (100 + r)) <= 0.011 or abs(v - base * r / 100) <= 0.011):
                            a = f'{v:.2f}'
        elif len(money) == 1:
            a = f'{money[0]:.2f}'
        if a:
            found.append(float(a))
    if not found:
        return None
    vat = round(sum(found), 2)
    if total is not None and vat > float(total) * MAX_UK_VAT_SHARE + 0.01:
        return None
    return f'{vat:.2f}'


# ---- dates -------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(
    'jan feb mar apr may jun jul aug sep oct nov dec'.split(), 1)}
D_YMD = re.compile(r'(?<![\w-])(20\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})(?!\d)日?')
D_NUMERIC = re.compile(r'(?<![\d-])(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})(?![\d-])')
D_MONTH_NAME = re.compile(r'\b([A-Za-z]{3})[A-Za-z]*\.? (\d{1,2}),? (20\d{2})\b')       # Jul 29, 2025
D_COMPACT = re.compile(r'\b(\d{1,2})([A-Z]{3})(20\d{2})\b')                                      # 29MAY2025
D_DAY_MONTH = re.compile(r"\b(\d{1,2}) ([A-Za-z]{3})[A-Za-z]*\.?,?\s*(?:(20\d{2})|'(\d{2}))\b")  # 24 Jul 2025, 11 Jun'25


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, currency: str) -> list[date]:
    """All plausible dates, four-digit-year ones first. Day-first unless the
    receipt is in dollars (US style) or day-first is impossible."""
    four, two = [], []
    pairs = [(int(a), int(b)) for a, b, _ in D_NUMERIC.findall(text)]
    if any(a > 12 >= b for a, b in pairs):
        month_first = False         # "29/04/2025" on the same receipt: day-first
    elif any(b > 12 >= a for a, b in pairs):
        month_first = True          # "11/18/24": month-first
    else:
        month_first = currency in ('$?', 'USD')
    for y, m, d in D_YMD.findall(text):
        if (v := _valid(int(y), int(m), int(d))):
            four.append(v)
    for a, b, y in D_NUMERIC.findall(text):
        a, b = int(a), int(b)
        year = int(y) if len(y) == 4 else 2000 + int(y)
        # (month, day): US style month-first for dollars, otherwise day-first;
        # fall back to the other order when the preferred one is impossible
        orders = [(a, b), (b, a)] if month_first else [(b, a), (a, b)]
        for month, day in orders:
            if (v := _valid(year, month, day)):
                (four if len(y) == 4 else two).append(v)
                break
    named = (D_MONTH_NAME.findall(text)
             + [(m, d, y or '20' + y2) for d, m, y, y2 in D_DAY_MONTH.findall(text)]
             + [(m, d, y) for d, m, y in D_COMPACT.findall(text)])
    for mon, d, y in named:
        if mon.lower() in MONTHS and (v := _valid(int(y), MONTHS[mon.lower()], int(d))):
            four.append(v)
    seen, out = set(), []
    for v in four + two:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


# ---- one receipt -------------------------------------------------------------

def read(path: Path) -> dict:
    rows = path.read_text().splitlines()
    text = '\n'.join(rows)
    currency = find_currency(text)
    total, how = find_total(rows, currency)
    dates = find_dates(text, currency)
    return {
        'total': total, 'how': how, 'currency': currency,
        'vat_number': find_vat_number(text, currency),
        'vat': find_printed_vat(rows, currency, total),
        'date': dates[0] if dates else None,
        'other_dates': dates[1:],
    }


# ---- scoring against tests/fixtures/photos/expected.yaml ---------------------

def score(expected_path: Path, ocr_dir: Path) -> None:
    import yaml
    expected = yaml.safe_load(expected_path.read_text())
    fields = ['total', 'currency', 'vat_number', 'vat', 'date']
    tally = {f: [0, 0] for f in fields}
    wrong = []
    for name, want in expected.items():
        got = read(ocr_dir / (Path(name).stem + '.txt'))
        cells = []
        for f in fields:
            w, g = want.get(f), got[f]
            if f == 'total':
                ok = w is not None and g is not None and abs(float(w) - float(g)) < 0.005
            elif f == 'vat':
                ok = (float(w or 0) == float(g or 0))         # "not printed" ≡ £0 filed
            elif f == 'vat_number':
                wd = re.sub(r'\D', '', str(w)) if w else None
                gd = re.sub(r'\D', '', str(g)) if g else None
                ok = wd == gd
            elif f == 'currency':
                ok = g == w or (g == '$?' and w in ('USD', 'AUD', 'NZD', 'CAD'))
            else:
                ok = (str(w) if w else None) == (str(g) if g else None)
            tally[f][0] += ok
            if not ok and g is not None and g not in ('$?', '?'):
                wrong.append(f'{Path(name).stem}: {f} = {g} (expected {w})')
            tally[f][1] += 1
            cells.append(('✓ ' if ok else '✗ ') + (str(g) if g is not None else '—'))
        print(f"{Path(name).stem[:30]:31} " + ' | '.join(f'{c:16}' for c in cells) + f"  [{got['how']}]")
    print()
    for f, (ok, n) in tally.items():
        print(f'{f:11} {ok}/{n}')
    print(f'WRONG VALUES (dangerous): {len(wrong)}')
    for line in wrong:
        print('  ' + line)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--score']:
        score(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        for p in sys.argv[1:]:
            print(Path(p).stem, read(Path(p)))
