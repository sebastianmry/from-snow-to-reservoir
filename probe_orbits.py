"""
FROM SNOW TO RESERVOIR - S1 orbit metadata probe (read-only, no download)

Re-queries the same DSWx-S1 granules as download_s1.py and inspects their
metadata to recover, per date:
  - satellite (S1A / S1C, from the native granule name)
  - pass direction (ascending / descending)
  - relative orbit / track number (if present in the UMM)
  - the 12-day repeat phase used by the dedup (ordinal % 12)

Purpose: validate that the phase-based orbit dedup picks a consistent track,
and check whether S1A/S1C share relative orbits (potential 6-day densification).

    python probe_orbits.py            # both AOIs
    python probe_orbits.py enguri     # one AOI
"""

import sys
import re
from collections import defaultdict
from pathlib import Path

import earthaccess

from download_common import AOIS, DATE_START, DATE_END, granule_date

SHORT_NAME = "OPERA_L3_DSWX-S1_V1"

# UMM keys that may carry orbit / track / pass information (case-insensitive)
ORBIT_KEY_HINTS = ("ORBIT", "TRACK", "PASS", "DIRECTION", "FRAME")


def native_name(granule) -> str:
    for link in granule.data_links():
        return Path(link).name
    return ""


def satellite(name: str) -> str:
    m = re.search(r"_(S1[A-C])_", name)
    return m.group(1) if m else "?"


def dig_additional_attributes(granule) -> dict:
    """Pull AdditionalAttributes whose name hints at orbit/track/pass."""
    out = {}
    try:
        attrs = granule["umm"].get("AdditionalAttributes", [])
    except Exception:
        return out
    for a in attrs:
        name = a.get("Name", "")
        if any(h in name.upper() for h in ORBIT_KEY_HINTS):
            vals = a.get("Values", [])
            out[name] = vals[0] if len(vals) == 1 else vals
    return out


def orbit_domain(granule) -> dict:
    """Pull OrbitCalculatedSpatialDomains (orbit number, start/stop) if present."""
    try:
        doms = granule["umm"]["SpatialExtent"]["OrbitCalculatedSpatialDomains"]
        return doms[0] if doms else {}
    except Exception:
        return {}


def probe_aoi(aoi: dict):
    print(f"\n{'='*70}\nAOI: {aoi['label']}\n{'='*70}")
    granules = earthaccess.search_data(
        short_name=SHORT_NAME,
        bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END),
        count=-1,
    )
    print(f"{len(granules)} granules found")
    if not granules:
        return

    by_date = defaultdict(list)
    for g in granules:
        by_date[granule_date(g)].append(g)

    # Show which UMM fields are even available, using the first granule
    sample = granules[0]
    print("\n-- sample AdditionalAttributes (orbit-related) --")
    print(dig_additional_attributes(sample) or "  (none found)")
    print("-- sample OrbitCalculatedSpatialDomains --")
    print(orbit_domain(sample) or "  (none found)")

    ref = None
    print(f"\n{'date':<10} {'phase':>5}  {'sat(s)':<12} orbit-info")
    for date_str in sorted(by_date):
        if date_str == "unknown":
            continue
        from datetime import datetime
        ordn = datetime.strptime(date_str, "%Y%m%d").toordinal()
        ref = ordn if ref is None else ref
        phase = (ordn - ref) % 12

        gs = by_date[date_str]
        sats = sorted({satellite(native_name(g)) for g in gs})
        attrs = {}
        for g in gs:
            attrs.update(dig_additional_attributes(g))
        dom = orbit_domain(gs[0])
        info = {**attrs}
        if dom.get("OrbitNumber") is not None:
            info["OrbitNumber"] = dom["OrbitNumber"]
        print(f"{date_str:<10} {phase:>5}  {','.join(sats):<12} {info}")


if __name__ == "__main__":
    print("NASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception:
        earthaccess.login(strategy="interactive", persist=True)
    print("OK")

    wanted = sys.argv[1].lower() if len(sys.argv) > 1 else None
    for aoi in AOIS:
        if wanted and aoi["name"] != wanted:
            continue
        probe_aoi(aoi)
