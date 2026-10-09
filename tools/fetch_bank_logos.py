"""Fetch every bank's own icon into data/bank-logos/ now, rather than as
each is first shown. Re-run to refresh."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.bank_logos import SITES, fetch  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "data" / "bank-logos"
for key in SITES:
    got = fetch(key, OUT)
    print(f"{key:15} {got.name if got else 'FAILED'}")
