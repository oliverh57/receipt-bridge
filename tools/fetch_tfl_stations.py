"""Fetch TfL's station names (and National Rail's, as TfL lists them) into
app/tfl_stations.txt, for the journey start / end suggestions on TfL travel
records (app/tfl.py). Re-run to refresh when stations open or are renamed.

Powered by TfL Open Data: contains OS data © Crown copyright and database
rights 2016, and Geomni UK Map data © and database rights 2019.
"""
import re
import sys
import time
from pathlib import Path

import requests

MODES = ("tube", "dlr", "overground", "elizabeth-line", "tram", "national-rail", "cable-car")
STATION_TYPES = {"NaptanMetroStation", "NaptanRailStation", "NaptanPublicBusCoachTram"}
OUT = Path(__file__).resolve().parent.parent / "app" / "tfl_stations.txt"

# "Oxford Circus Underground Station" → "Oxford Circus"
SUFFIX = re.compile(r"\s+(\((London|Berks|Herts|Essex|Bucks)\)\s+)?"
                    r"(Underground|DLR|Rail|Tram)( Station| Stop)?$|\s+Tram Stop$|\s+Station$|-Underground$", re.I)


def _get(path: str) -> requests.Response:
    for attempt in range(3):
        response = requests.get(f"https://api.tfl.gov.uk{path}", timeout=120)
        if response.status_code < 500:
            break
        time.sleep(3 * (attempt + 1))
    response.raise_for_status()
    return response


def _stops(mode: str) -> list[dict]:
    """A mode's stations: all at once, or line by line when that's too big
    for TfL to answer in time (national rail)."""
    try:
        return _get(f"/StopPoint/Mode/{mode}").json().get("stopPoints", [])
    except requests.RequestException:
        stops = []
        for line in _get(f"/Line/Mode/{mode}").json():
            stops += _get(f"/Line/{line['id']}/StopPoints").json()
        return stops


def _name(stop: dict) -> str:
    if stop.get("stopType") not in STATION_TYPES:
        return ""
    if stop.get("stopType") == "NaptanPublicBusCoachTram" and "tram" not in stop.get("modes", []):
        return ""
    return re.sub(r"\s+LL$", "", SUFFIX.sub("", (stop.get("commonName") or "").strip()).strip())


def main() -> int:
    # TfL's own network first, then National Rail (the whole country, as TfL
    # lists it): suggestions rank the first part ahead on an equal match
    tfl, rail = set(), set()
    for mode in MODES:                  # one at a time: all at once times out at TfL
        names = {_name(stop) for stop in _stops(mode)} - {""}
        (rail if mode == "national-rail" else tfl).update(names)
    rail -= tfl
    if len(tfl) < 300:
        print(f"Only {len(tfl)} TfL stations: TfL's answer looks wrong, not saving.", file=sys.stderr)
        return 1
    OUT.write_text("# TfL stations: tools/fetch_tfl_stations.py. Powered by TfL Open Data.\n"
                   + "\n".join(sorted(tfl, key=str.lower))
                   + "\n# National Rail\n" + "\n".join(sorted(rail, key=str.lower)) + "\n", encoding="utf-8")
    print(f"{len(tfl)} TfL stations, {len(rail)} National Rail → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
