"""Duplicate-purchase check, prototype for PLAN.md §8.3.

Two photos of one purchase (a reprint, a "DUPLICATE" copy, or the same
receipt photographed twice) must be caught before filing. Identical files
are caught earlier by content hash; this catches *different* photos of the
same purchase.

    python3 duplicates.py ../tests/fixtures/photos/ocr
"""

from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

from receipt_rules import read

# Card authorisation codes are six characters and unique per payment.
AUTH = re.compile(r'AUTH(?:ORI[SZ]ATION)?\.?\s*(?:CODE|NO)?\.?\s*[:.]?\s*([A-Z0-9]{6})\b', re.I)
REPRINT = re.compile(r'DUPLICATE|RE-?PRINTED|REPRINT|\bCOPY OF\b', re.I)


def facts(path: Path) -> dict:
    text = path.read_text()
    r = read(path)
    auth = AUTH.search(text)
    return {**r, 'auth': auth.group(1).upper() if auth else None,
            'reprint': bool(REPRINT.search(text))}


def same_purchase(a: dict, b: dict) -> str | None:
    if a['total'] is None or a['total'] != b['total']:
        return None
    if a['auth'] and a['auth'] == b['auth']:
        return f"same amount and auth code {a['auth']}"
    if a['date'] and b['date'] and a['date'] == b['date'] and a['vat_number'] == b['vat_number']:
        return 'same amount, date and supplier VAT number'
    return None


if __name__ == '__main__':
    receipts = {p.stem: facts(p) for p in sorted(Path(sys.argv[1]).glob('*.txt'))}
    for (na, a), (nb, b) in itertools.combinations(receipts.items(), 2):
        why = same_purchase(a, b)
        if why:
            print(f'DUPLICATE  {na}  ≡  {nb}   ({why})')
    for name, f in receipts.items():
        if f['reprint']:
            print(f'REPRINT    {name}  (says DUPLICATE / RE-PRINTED: check the original is not already filed)')
