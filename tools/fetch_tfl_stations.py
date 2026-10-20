"""Fetch TfL's station names into app/tfl_stations.txt, for the journey
start / end suggestions on TfL travel records (app/tfl.py). Re-run to
refresh when stations open or are renamed.

Powered by TfL Open Data: contains OS data © Crown copyright and database
rights 2016, and Geomni UK Map data © and database rights 2019.
"""
import re
import sys
from pathlib import Path

import requests

MODES = ("tube", "dlr", "overground", "elizabeth-line", "tram")
STATION_TYPES = {"NaptanMetroStation", "NaptanRailStation", "NaptanPublicBusCoachTram"}
OUT = Path(__file__).resolve().parent.parent / "app" / "tfl_stations.txt"

# "Oxford Circus Underground Station" → "Oxford Circus"
SUFFIX = re.compile(r"\s+(\((London|Berks|Herts|Essex|Bucks)\)\s+)?"
                    r"(Underground|DLR|Rail|Tram)( Station| Stop)?$|\s+Tram Stop$|\s+Station$|-Underground$", re.I)


def main() -> int:
    stops = []
    for mode in MODES:                  # one at a time: all at once times out at TfL
        response = requests.get(f"https://api.tfl.gov.uk/StopPoint/Mode/{mode}", timeout=120)
        response.raise_for_status()
        stops += response.json().get("stopPoints", [])
    names = set()
    for stop in stops:
        if stop.get("stopType") not in STATION_TYPES:
            continue
        if stop.get("stopType") == "NaptanPublicBusCoachTram" and "tram" not in stop.get("modes", []):
            continue
        name = SUFFIX.sub("", (stop.get("commonName") or "").strip()).strip()
        if name:
            names.add(name)
    if len(names) < 300:
        print(f"Only {len(names)} stations: TfL's answer looks wrong, not saving.", file=sys.stderr)
        return 1
    OUT.write_text("# TfL stations: tools/fetch_tfl_stations.py. Powered by TfL Open Data.\n"
                   + "\n".join(sorted(names, key=str.lower)) + "\n", encoding="utf-8")
    print(f"{len(names)} stations → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
