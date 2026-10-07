import re, glob, sys
# ---- amounts: comma or point; ",dd" = decimals, ",ddd" = thousands; no decimals for yen
AMT = re.compile(r'(-?)\s*(?:[£€$¥·]|EUR|GBP|DKK|AUD|JPY)?\s*(\d{1,3}(?:[,.]\d{3})+|\d+)(?:[.,](\d{2}))?(?!\d)\s*(?:€|EUR|DKK|円)?\s*(?:\d|[A-Z])?\s*$')
def amount(row):
    row = re.sub(r'(\d)[.,]\s+(\d{3})\b', r'\1,\2', row.replace('）', '').replace(')', ''))
    m = AMT.search(row)
    if not m: return None
    whole = re.sub(r'[,.]', '', m.group(2))
    return f"{int(whole)}.{m.group(3) or '00'}"
# ---- labels in several languages; generic, not per shop
TOTAL  = re.compile(r'^\s*\d*\s*(BALANCE DUE|TOTAL|AMOUNT DUE|TO PAY|ZU ZAHLEN|SUMME|GESAMT|TOTALE|À PAYER|総合計|合計|金額)', re.I)
NOT_TOTAL = re.compile(r'SUB\s*TOTAL|FOOD TOTAL|DRINK TOTAL|POINTS|EX\s*(TAX|VAT|GST)|TAXIMETER|本体合計|小計|NETTO', re.I)
TENDER = re.compile(r'^\s*(CARD|DEBIT MASTERCARD|MASTERCARD|VISA|AMEX|CREDIT CARD|KARTENZAHLUNG|EC-?KARTE|その他クレジット|クレジット|差引き現金支払い額|現金|CASH|BAR|AMOUNT)', re.I)
CHANGE = re.compile(r'お預かり|お釣り|つり銭|RÜCKGELD|CHANGE|GEGEBEN|SURCHARGE|GEBÜHR', re.I)
LETTERS = re.compile('[A-Za-z\u3040-\u30ff\u4e00-\u9fff]{3}')
def near(rows, i):
    for j in (i + 1, i - 1):
        # a neighbouring row must look like money (symbol or decimals), so a
        # masked card number like "2685" is never read as a payment
        if 0 <= j < len(rows) and amount(rows[j]) and re.search(r'[£€$¥·]|\d[.,]\d{2}\b', rows[j]) and (not LETTERS.search(rows[j]) or TENDER.search(rows[j])):
            return amount(rows[j])
    for j in (i + 1,):          # label row, amount-label on next row ("金額 ·3,080" / "AMOUNT")
        pass
    return None
for path in sys.argv[1:]:
    rows = open(path).read().splitlines()
    totals, tenders = [], []
    for i, r in enumerate(rows):
        if CHANGE.search(r): continue
        a = amount(r) if re.search(r'\d', r) else None
        a = a or near(rows, i)
        if not a or a == '0.00': continue
        if TOTAL.search(r) and not NOT_TOTAL.search(r): totals.append(a)
        elif TENDER.search(r): tenders.append(a)
    t = sorted(set(totals), key=lambda x: -float(x))
    pay = sum(float(x) for x in tenders)
    def fix8(c):
        return c[1:] if c.startswith('8') and len(tenders) == 1 and c[1:] in t else c
    tenders = [fix8(c) for c in tenders]; pay = sum(float(x) for x in tenders)
    if t and tenders and any(abs(float(x) - pay) < 0.005 for x in t):
        x = [x for x in t if abs(float(x) - pay) < 0.005][0]
        how = 'confirmed by card line' if len(tenders) == 1 else f'confirmed: payments {" + ".join(tenders)}'
        verdict = f"{x} {how}"
    elif len(t) == 1: verdict = f"{t[0]} UNCONFIRMED"
    else: verdict = f"NEEDS ATTENTION totals={t} payments={tenders}"
    print(f"{path[:-4]:32} {verdict}")
