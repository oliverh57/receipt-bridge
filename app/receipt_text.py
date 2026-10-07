"""Read the money, VAT and date off a receipt's OCR text.

Input is the receipt as rows of text: Vision's OCR output regrouped into
lines by `receipt-reader` (see PLAN.md §5.1). Output is a `ReceiptText`
with the total, how sure we are of it, currency, UK VAT number, printed UK
VAT and date.

These are generic rules, not rules for individual shops. They lean on
conventions set by till and card-terminal software (a TOTAL line, a card
line carrying the same amount, "VAT No" followed by digits) in several
languages, so a shop redesigning its receipt doesn't break them.

The rules were developed against the photos in tests/fixtures/photos and
checked blind against four held-out batches (PLAN.md §5.2). The design
principle throughout: **a field we can't read is left blank, never guessed**.
A blank goes to Needs attention for a person; a wrong value goes into the
books. Several checks below exist only because a blind batch produced a
wrong value; each says which receipt.

The on-device language model is deliberately not used here. On real photos
it fabricated totals, VAT and VAT numbers, and refused Japanese and Danish
text outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# ---- amounts -----------------------------------------------------------------

# A trailing amount on a row. ",dd"/".dd" is decimals, ",ddd"/".ddd" is
# thousands (`11,60`, `15,300`); yen has no decimals. Masked card digits
# ("xxxx6335", "****2685") are never amounts.
_AMOUNT = re.compile(
    r'(-?)\s*(?:[£€$¥·]|EUR|GBP|DKK|AUD|USD|JPY)?\s*'
    r'(?<![xX*#\d])(\d{1,3}(?:[,.]\d{3})+|\d+)(?:[.,](\d{2}))?(?!\d)'
    r'\s*(?:€|EUR|DKK|円)?\s*(?:\d|[A-Z<])?\s*$'
)
# Something that looks like money rather than an ID: a currency symbol or
# two decimals. "A0000000041010" (a card chip ID) is not €41,010.
_MONEY_LOOKING = re.compile(r'[£€$¥·]|\d[.,]\d{2}\b')
_ISO_AT_EDGE = re.compile(
    r'^\s*\b(EUR|GBP|DKK|AUD|USD|JPY|ARS|MXN|BRL|CAD)\b|\b(EUR|GBP|DKK|AUD|USD|JPY|ARS|MXN|BRL|CAD)\s*$')
_LETTERS = re.compile('[A-Za-z぀-ヿ一-鿿]{3}')
_ZERO_DECIMAL = {'JPY', 'KRW'}


def amount(row: str) -> Decimal | None:
    """The amount at the end of a row, or None."""
    row = row.replace('）', '').replace(')', '')
    row = re.sub(r'(\d)[.,]\s+(\d{3})\b', r'\1,\2', row)    # OCR spacing: "15.   300"
    m = _AMOUNT.search(row)
    if not m:
        return None
    whole = re.sub(r'[,.]', '', m.group(2))
    return Decimal(f"{int(whole)}.{m.group(3) or '00'}")


# ---- labels: generic, several languages --------------------------------------

_TOTAL = re.compile(
    r'BALANCE DUE|BALANCE TO PAY|\bTOTAL\b|\bTOTAAL\b|NOG TE BETALEN|AMOUNT DUE|\bTO PAY\b|'
    r'ZU ZAHLEN|\bSUMME\b|\bGESAMT|\bTOTALE\b|À PAYER|総合計|合計|金額', re.I)
# Lines that say "total" but aren't the amount paid. "Net Total" was read as
# Pret's total (£2.58 instead of £14.25) in held-out set 2.
_NOT_TOTAL = re.compile(
    r'SUB\s*TOTAL|FOOD TOTAL|DRINK TOTAL|NET\s*TOTAL|POINTS|EX\.?\s*(TAX|VAT|GST|BTW|MWST)|'
    r'TAXIMETER|SAVING|BEFORE|本体合計|小計|NETTO', re.I)
_PAYMENT = re.compile(
    r'^\s*(CARD|DEBIT MASTERCARD|MASTERCARD|VISA|AMEX|CREDIT CARD|AMOUNT|APPLE[ _]PAY|'
    r'GOOGLE[ _]PAY|PAYMENT\b|DOJO\b|KARTENZAHLUNG|EC-?KARTE|その他クレジット|クレジット|'
    r'差引き現金支払い額|現金|CASH|BAR\b|DINHEIRO|EFECTIVO|ESPÈCES|CONTANTI|CREDIT\b)', re.I)
# Not payments: cash handed over, change, surcharges, and card-number lines
# ("CARD: 559806XXXXXX2685") — but "Card: £4.30" is a payment (Costa).
_NOT_PAYMENT = re.compile(
    r'お預かり|お釣り|つり銭|RÜCKGELD|CHANGE|GEGEBEN|TROCO|CAMBIO|MONNAIE|'
    r'SURCHARGE|GEBÜHR|CARD\s*(?:NO|NUMBER)|CARD:\s*[\dX*]{6,}', re.I)
_CHANGE_GIVEN = re.compile(r'^\s*(CHANGE|RÜCKGELD|TROCO|CAMBIO|お釣り|つり銭)\b', re.I)
# OCR reads "£" as one of these: "£6.99" → "86.99", "26.99", "E6.99", "C6.99".
_POUND_MISREADS = '82EC€'


def _neighbour(rows: list[str], i: int) -> Decimal | None:
    """A label with no amount takes it from the row below or above, when that
    row looks like money and isn't another labelled line. Angled photos split
    labels from their amounts (Tesco Highbury, The Black Cat, Castellum)."""
    for j in (i + 1, i - 1):
        if not 0 <= j < len(rows):
            continue
        row = rows[j]
        a = amount(row)
        if a is not None and _MONEY_LOOKING.search(row) and (
                not _LETTERS.search(_ISO_AT_EDGE.sub('', row)) or _PAYMENT.search(row)):
            return a
    return None


# ---- the result ----------------------------------------------------------------

CONFIRMED = 'confirmed'        # a payment line (or lines) carries the same amount
UNCONFIRMED = 'unconfirmed'    # read, but nothing on the receipt agrees with it
MISSING = 'missing'            # not read: Needs attention


@dataclass
class ReceiptText:
    total: Decimal | None = None
    total_status: str = MISSING
    total_note: str = ''
    # ISO code, or None when it can't be told from the text. `currency_hint`
    # then says why: "$" (AUD, USD, NZD, CAD…) or "" (no symbol at all). The
    # photo's location or the person settles it (PLAN.md §6.4).
    currency: str | None = None
    currency_hint: str = ''
    vat_number: str | None = None       # UK only, normalised "GB123456789"
    vat: Decimal | None = None          # printed UK VAT, summed across rates
    # Each printed VAT line: (rate %, VAT, gross for that rate). Rate or
    # gross is None when the line didn't say. Used to split a mixed-rate
    # receipt into one FreeAgent explanation per rate.
    vat_lines: list[tuple[Decimal | None, Decimal, Decimal | None]] = field(default_factory=list)
    date: date | None = None
    other_dates: list[date] = field(default_factory=list)
    auth_code: str | None = None        # card authorisation code: duplicate key
    reprint: bool = False               # "DUPLICATE" / "RE-PRINTED" printed on it
    not_vat_receipt: bool = False       # "This is not a VAT receipt / tax invoice"
    candidate_totals: list[Decimal] = field(default_factory=list)


# ---- total ---------------------------------------------------------------------

def find_total(rows: list[str], currency: str | None) -> tuple[Decimal | None, str, str, list[Decimal]]:
    """(total, status, note, candidate totals)."""
    totals: list[Decimal] = []
    payments: list[Decimal] = []
    change = Decimal(0)
    for i, row in enumerate(rows):
        if _CHANGE_GIVEN.search(row):
            c = amount(row)
            c = c if c is not None else _neighbour(rows, i)   # "Change £0.00" is 0, not missing
            if c:
                change += c      # cash tendered minus change = amount paid (Nomad)
        if _NOT_PAYMENT.search(row) and not _TOTAL.search(row):
            continue
        a = amount(row) if re.search(r'\d', row) else None
        if a is not None and currency not in _ZERO_DECIMAL and not _MONEY_LOOKING.search(row):
            a = None             # an ID, not money (Christiani's card chip ID)
        a = a if a is not None else _neighbour(rows, i)
        if not a:
            continue
        if _TOTAL.search(row) and not _NOT_TOTAL.search(row):
            totals.append(a)
        elif _PAYMENT.search(row):
            payments.append(a)
    candidates = sorted(set(totals), reverse=True)

    def unmisread(p: Decimal) -> Decimal:
        # One stray leading glyph where "£" was, accepted only if it then
        # equals a total: "84.49" → 4.49 (Sainsbury's), "26.99" → 6.99 (M&S).
        s = f'{p:.2f}'
        for t in candidates:
            ts = f'{t:.2f}'
            if len(s) == len(ts) + 1 and s.endswith(ts) and s[0] in _POUND_MISREADS:
                return t
        return p

    payments = [unmisread(p) for p in payments]
    if len(payments) > 1 and len(set(payments)) == 1:
        paid = payments[0]       # one payment printed twice ("MasterCard", "Payment")
    else:
        paid = sum(payments, Decimal(0))
    paid -= change

    for t in candidates:
        if payments and t == paid:
            how = 'payment line' if len(set(payments)) == 1 else 'payments ' + ' + '.join(map(str, payments))
            return t, CONFIRMED, f'confirmed by {how}', candidates
    if len(candidates) == 1:
        return candidates[0], UNCONFIRMED, 'no payment line agrees', candidates
    if not candidates and len(set(payments)) == 1:
        # card slip or ticket: no total line, only the amount charged
        return payments[0], UNCONFIRMED, 'payment line only', candidates
    if not candidates and len(payments) > 1:
        # several payments and OCR lost the TOTAL label (Taco bar): confirmed
        # if a bare amount row equals what was paid
        bare = {amount(r) for r in rows
                if amount(r) is not None and _MONEY_LOOKING.search(r) and not _LETTERS.search(r)}
        if paid in bare:
            return paid, CONFIRMED, 'payments add up to an unlabelled total', candidates
        return paid, UNCONFIRMED, 'sum of payment lines', candidates
    if len(candidates) > 1:
        # e.g. two receipts in one photo, a split bill (Christiani)
        return None, MISSING, 'several totals: ' + ', '.join(map(str, candidates)), candidates
    return None, MISSING, 'no total found', candidates


# ---- currency --------------------------------------------------------------------

_ISO = re.compile(
    r'\b(GBP|EUR|DKK|SEK|NOK|CHF|USD|AUD|NZD|CAD|PLN|CZK|HUF|JPY|CNY|HKD|SGD|KRW|THB|INR|'
    r'AED|ZAR|MXN|BRL|ARS|CLP|COP|PEN|TRY|ISK)\b')
_JAPANESE = re.compile('[぀-ヿ]')
_UK_VAT_LABEL = re.compile(r'\bVAT\b', re.I)


def find_currency(text: str) -> tuple[str | None, str]:
    """(ISO code or None, hint)."""
    if '£' in text or _UK_VAT_LABEL.search(text):
        return 'GBP', ''
    m = _ISO.search(text)       # the first code printed: ARS before "USD 21.37" (Libertad)
    if m:
        return m.group(1), ''
    if _JAPANESE.search(text) or '¥' in text or '円' in text:
        return 'JPY', ''
    if '€' in text:
        return 'EUR', ''
    if '$' in text:
        return None, '$'
    return None, ''


# ---- UK VAT number and printed VAT -------------------------------------------------

_VAT_NUMBER = re.compile(
    r'\b(?:VAT|Tax\s*ID)\s*(?:N[o0]\.?|Number|Reg(?:istration)?(?:\s*N[o0]\.?)?)?\s*[:.]?\s*'
    r'((?:GB|G8|C3|CB|GE)?\s*(?:\d\s?){9}(?:(?:\d\s?){3})?)(?!\d)', re.I)
_VAT_LINE = re.compile(r'\bVAT\b', re.I)
# A VAT-number line is never a VAT-amount line: "VAT: GB659880474" was read as
# £659,880,474 of VAT at Greggs (held-out set 1).
_VAT_ID_LINE = re.compile(
    r'\bVAT\s*(?:N[o0]|Number|Reg)|\bVAT\s*[:.]?\s*(?:GB|G8|C3)?\s*\d{3}\s*\d{3,4}|ex\s*VAT|inc\s*VAT', re.I)
# "VAT TOTAL", "Total VAT": the sum of the VAT lines, when those are printed too
_VAT_TOTAL_LINE = re.compile(r'\bVAT\s*total\b|\btotal\s*VAT\b', re.I)
_NOT_VAT_RECEIPT = re.compile(r'NOT\s+A\s+(VAT|TAX)\s+(RECEIPT|INVOICE)', re.I)
# UK VAT can't exceed a sixth of a VAT-inclusive total (20% of the net).
MAX_UK_VAT_SHARE = Decimal(20) / Decimal(120)


def find_vat_number(text: str, currency: str | None) -> str | None:
    """A UK VAT number, only when it's labelled as one. Foreign tax numbers
    (ABN, CVR, USt-ID, 登録番号) are never UK VAT evidence. A bare number with
    its label lost is not accepted: that costs a reclaim, never invents one."""
    if currency != 'GBP' or _NOT_VAT_RECEIPT.search(text):
        return None
    m = _VAT_NUMBER.search(text)
    if not m:
        return None
    digits = re.sub(r'\D', '', m.group(1))
    return 'GB' + digits if len(digits) in (9, 12) else None


def find_printed_vat(rows: list[str], currency: str | None, total: Decimal | None) -> Decimal | None:
    """VAT as printed, summed across rates. Never worked out from items."""
    return find_vat_lines(rows, currency, total)[0]


def find_vat_lines(rows: list[str], currency: str | None, total: Decimal | None
                   ) -> tuple[Decimal | None, list[tuple[Decimal | None, Decimal, Decimal | None]]]:
    """(VAT summed, each VAT line as (rate, VAT, gross))."""
    if currency != 'GBP':
        return None, []
    found: list[Decimal] = []
    lines: list[tuple[Decimal | None, Decimal, Decimal | None]] = []
    totals: list[bool] = []
    for i, row in enumerate(rows):
        if not _VAT_LINE.search(row) or _VAT_ID_LINE.search(row):
            continue
        rate_m = re.search(r'(\d+(?:\.\d+)?)\s*%', row)
        tail = re.sub(r'\d+(?:\.\d+)?\s*%', '', row)              # drop "20.0%"
        if not re.search(r'\d[.,]\d{2}', tail):
            for j in (i + 1, i + 2):                                # table numbers below the header
                if j < len(rows) and re.search(r'\d[.,]\d{2}', rows[j]) and not _LETTERS.search(rows[j]):
                    tail = rows[j]
                    break
        elif rate_m and len(re.findall(r'\d+[.,]\d{2}', tail)) == 1 and i + 1 < len(rows) \
                and re.search(r'\d[.,]\d{2}', rows[i + 1]) and not _LETTERS.search(rows[i + 1]):
            # a VAT table row split across two lines: "T2 VAT @ 20% £2.58" / "£0.52" (Sandwi)
            tail += '   ' + rows[i + 1]
        money = [Decimal(x.replace(',', '.')) for x in re.findall(r'\d+[.,]\d{2}', tail)]
        vat = None
        line_rate: Decimal | None = Decimal(rate_m.group(1)) if rate_m else None
        line_gross: Decimal | None = None
        if len(money) >= 2:
            # "0.52 VAT 20% 3.10" (Pret) or "£2.58 £0.52" (Sandwi): VAT is the
            # amount that is the rate's share of another amount on the line.
            # Costa's OCR dropped the rate, so try the UK standard and reduced.
            rates = [Decimal(rate_m.group(1))] if rate_m else [Decimal(20), Decimal(5)]
            for r in rates:
                for v in money:
                    for base in money:
                        if v == base:
                            continue
                        if abs(v - base * r / (100 + r)) <= Decimal('0.011'):
                            vat, line_rate, line_gross = v, r, base            # base is the gross
                        elif abs(v - base * r / 100) <= Decimal('0.011'):
                            vat, line_rate, line_gross = v, r, base + v        # base is the net
        elif len(money) == 1:
            vat = money[0]
        if vat is not None:
            found.append(vat)
            lines.append((line_rate, vat, line_gross))
            totals.append(bool(_VAT_TOTAL_LINE.search(row)))
    if not found:
        return None, []
    breakdown = [line for line, is_total in zip(lines, totals) if not is_total]
    summed = [v for v, is_total in zip(found, totals) if is_total]
    if summed and breakdown and all(v == sum((b[1] for b in breakdown), Decimal(0)) for v in summed):
        # "VAT TOTAL 15.00" and a summary "VAT @ 20% 15.00 75.00" (an accountant):
        # the same VAT twice, so count the breakdown once
        found, lines = [b[1] for b in breakdown], breakdown
    vat = sum(found, Decimal(0))
    if total is not None and vat > total * MAX_UK_VAT_SHARE + Decimal('0.01'):
        return None, []          # impossible as VAT: not a VAT amount at all
    return vat, lines


# ---- dates ---------------------------------------------------------------------------

_MONTHS = {m: i for i, m in enumerate('jan feb mar apr may jun jul aug sep oct nov dec'.split(), 1)}
# Y-M-D must stand alone: Lime's document number "GB-2025-07-2044986992"
# gave 20 Jul 2025 (held-out set 1).
_D_YMD = re.compile(r'(?<![\w-])(20\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})(?!\d)日?')
_D_NUMERIC = re.compile(r'(?<![\d-])(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})(?![\d-])')
_D_MONTH_DAY = re.compile(r'\b([A-Za-z]{3})[A-Za-z]*\.? (\d{1,2}),? (20\d{2})\b')                  # Jul 29, 2025
# 29MAY2025; the camera reads the O of OCT and NOV as a zero ("050CT2026")
_D_COMPACT = re.compile(r'\b(\d{1,2})([A-Z0]{3})(20\d{2})\b')
_D_DAY_MONTH = re.compile(r"\b(\d{1,2}) ([A-Za-z]{3})[A-Za-z]*\.?,?\s*(?:(20\d{2})|'(\d{2}))\b")   # 24 Jul 2025, 11 Jun'25


def _valid(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def find_dates(text: str, currency: str | None, currency_hint: str = '') -> list[date]:
    """Every date on the receipt, four-digit-year ones first.

    Day-first or month-first is decided by the receipt itself: one
    unambiguous date ("29/04/2025", "11/18/24") settles every date on it.
    Only when none is unambiguous does the currency decide (dollars →
    month-first). Deciding by currency alone read Libertad's 04/05/2025 as
    5 April after mistaking its currency (held-out set 4).
    A torn or partial date ("23 J… 2025") matches nothing and stays blank.
    """
    pairs = [(int(a), int(b)) for a, b, _ in _D_NUMERIC.findall(text)]
    if any(a > 12 >= b for a, b in pairs):
        month_first = False
    elif any(b > 12 >= a for a, b in pairs):
        month_first = True
    else:
        month_first = currency == 'USD' or currency_hint == '$'

    four: list[date] = []
    two: list[date] = []
    for y, m, d in _D_YMD.findall(text):
        if (v := _valid(int(y), int(m), int(d))):
            four.append(v)
    for a, b, y in _D_NUMERIC.findall(text):
        a, b = int(a), int(b)
        year = int(y) if len(y) == 4 else 2000 + int(y)
        for month, day in ([(a, b), (b, a)] if month_first else [(b, a), (a, b)]):
            if (v := _valid(year, month, day)):
                (four if len(y) == 4 else two).append(v)
                break
    named = (_D_MONTH_DAY.findall(text)
             + [(m, d, y or '20' + y2) for d, m, y, y2 in _D_DAY_MONTH.findall(text)]
             + [(m.replace('0', 'O'), d, y) for d, m, y in _D_COMPACT.findall(text)])
    for mon, d, y in named:
        if mon.lower() in _MONTHS and (v := _valid(int(y), _MONTHS[mon.lower()], int(d))):
            four.append(v)

    out: list[date] = []
    for v in four + two:
        if v not in out:
            out.append(v)
    return out


# ---- duplicates ------------------------------------------------------------------------

_AUTH = re.compile(r'AUTH(?:ORI[SZ]ATION)?\.?\s*(?:CODE|NO)?\.?\s*[:.]?\s*([A-Z0-9]{6})\b', re.I)
_REPRINT = re.compile(r'DUPLICATE|RE-?PRINTED|REPRINT|\bCOPY OF\b', re.I)


def same_purchase(a: ReceiptText, b: ReceiptText) -> str | None:
    """Why two receipts are the same purchase, or None. The card authorisation
    code is the strongest key: it caught the same Sainsbury's receipt
    photographed twice, once without its date. Look-alikes (two M&S
    receipts a day apart) differ in amount, date and auth code."""
    if a.total is None or a.total != b.total:
        return None
    if a.auth_code and a.auth_code == b.auth_code:
        return f'same amount and card authorisation code {a.auth_code}'
    if a.date and a.date == b.date and a.vat_number and a.vat_number == b.vat_number:
        return 'same amount, date and supplier VAT number'
    return None


# ---- one receipt ---------------------------------------------------------------------

def read_rows(rows: list[str]) -> ReceiptText:
    text = '\n'.join(rows)
    currency, hint = find_currency(text)
    total, status, note, candidates = find_total(rows, currency)
    dates = find_dates(text, currency, hint)
    auth = _AUTH.search(text)
    vat, vat_lines = find_vat_lines(rows, currency, total)
    return ReceiptText(
        total=total,
        total_status=status,
        total_note=note,
        currency=currency,
        currency_hint=hint,
        vat_number=find_vat_number(text, currency),
        vat=vat,
        vat_lines=vat_lines,
        date=dates[0] if dates else None,
        other_dates=dates[1:],
        auth_code=auth.group(1).upper() if auth else None,
        reprint=bool(_REPRINT.search(text)),
        not_vat_receipt=bool(_NOT_VAT_RECEIPT.search(text)),
        candidate_totals=candidates,
    )


def read_text(text: str) -> ReceiptText:
    return read_rows(text.splitlines())
