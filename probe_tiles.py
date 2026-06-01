"""
Diagnostic: list the MGRS tiles present per S1 date on the anchored orbit.

Helps decide whether the coverage difference between early and late dates on the
same 12-day orbit is just tile availability (same tiles, more of them produced
later) or a different/additional swath (more distinct MGRS tiles -> mixed look
geometry). Read-only; needs Google Drive auth like extract_timeseries.py.

    python probe_tiles.py
"""

import re
from collections import defaultdict

from extract_timeseries import (
    authenticate, get_folder_id, list_tifs_in_folder,
    DRIVE_PARENT, AOI_1, AOI_2, orbit_phase,
)

TILE_RE = re.compile(r"_(T\d{2}[A-Z]{3})_")


def main():
    drive = authenticate()
    opera = get_folder_id(drive, DRIVE_PARENT, "root")
    s1 = get_folder_id(drive, "s1", opera)

    for aoi in [AOI_1, AOI_2]:
        site = aoi["name"]
        anchor = aoi.get("s1_anchor")
        folder = get_folder_id(drive, site, s1)
        if not folder:
            print(f"{site}: folder not found")
            continue

        by_date = defaultdict(set)
        for f in list_tifs_in_folder(drive, folder):
            if "B01_WTR" not in f["title"]:
                continue
            m_date = re.search(r"_(\d{8})_", f["title"])
            m_tile = TILE_RE.search(f["title"])
            if not m_date:
                continue
            date = m_date.group(1)
            if anchor and orbit_phase(date, anchor) != 0:
                continue
            by_date[date].add(m_tile.group(1) if m_tile else "(no-tile-id)")

        print(f"\n=== {site} (anchor {anchor}) ===")
        for date in sorted(by_date):
            tiles = sorted(by_date[date])
            print(f"  {date}: {len(tiles):2d} tiles  {tiles}")

        # Which tiles appear only early vs only late?
        dates = sorted(by_date)
        if len(dates) >= 6:
            cut = dates[len(dates) // 2]
            early = set().union(*(by_date[d] for d in dates if d < cut))
            late = set().union(*(by_date[d] for d in dates if d >= cut))
            print(f"  --- distinct tiles early(<{cut}): {sorted(early)}")
            print(f"  --- distinct tiles late (>={cut}): {sorted(late)}")
            print(f"  --- only late: {sorted(late - early)}   only early: {sorted(early - late)}")


if __name__ == "__main__":
    main()
