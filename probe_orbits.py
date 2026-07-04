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
from datetime import datetime
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
    match = re.search(r"_(S1[A-C])_", name)
    return match.group(1) if match else "?"


def dig_additional_attributes(granule) -> dict:
    """Pull AdditionalAttributes whose name hints at orbit/track/pass."""
    orbit_attributes = {}
    try:
        attributes = granule["umm"].get("AdditionalAttributes", [])
    except Exception:
        return orbit_attributes
    for attribute in attributes:
        name = attribute.get("Name", "")
        if any(hint in name.upper() for hint in ORBIT_KEY_HINTS):
            values = attribute.get("Values", [])
            orbit_attributes[name] = values[0] if len(values) == 1 else values
    return orbit_attributes


def orbit_domain(granule) -> dict:
    """Pull OrbitCalculatedSpatialDomains (orbit number, start/stop) if present."""
    try:
        domains = granule["umm"]["SpatialExtent"]["OrbitCalculatedSpatialDomains"]
        return domains[0] if domains else {}
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

    granules_by_date = defaultdict(list)
    for granule in granules:
        granules_by_date[granule_date(granule)].append(granule)

    # Show which UMM fields are even available, using the first granule
    sample_granule = granules[0]
    print("\n-- sample AdditionalAttributes (orbit-related) --")
    print(dig_additional_attributes(sample_granule) or "  (none found)")
    print("-- sample OrbitCalculatedSpatialDomains --")
    print(orbit_domain(sample_granule) or "  (none found)")

    ref_ordinal = None
    print(f"\n{'date':<10} {'phase':>5}  {'sat(s)':<12} orbit-info")
    for date_str in sorted(granules_by_date):
        if date_str == "unknown":
            continue
        date_ordinal = datetime.strptime(date_str, "%Y%m%d").toordinal()
        ref_ordinal = date_ordinal if ref_ordinal is None else ref_ordinal
        phase = (date_ordinal - ref_ordinal) % 12

        date_granules = granules_by_date[date_str]
        satellites = sorted({satellite(native_name(g)) for g in date_granules})
        orbit_attributes = {}
        for granule in date_granules:
            orbit_attributes.update(dig_additional_attributes(granule))
        domain = orbit_domain(date_granules[0])
        orbit_info = {**orbit_attributes}
        if domain.get("OrbitNumber") is not None:
            orbit_info["OrbitNumber"] = domain["OrbitNumber"]
        print(f"{date_str:<10} {phase:>5}  {','.join(satellites):<12} {orbit_info}")


if __name__ == "__main__":
    print("NASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception:
        earthaccess.login(strategy="interactive", persist=True)
    print("OK")

    wanted_aoi = sys.argv[1].lower() if len(sys.argv) > 1 else None
    for aoi in AOIS:
        if wanted_aoi and aoi["name"] != wanted_aoi:
            continue
        probe_aoi(aoi)
