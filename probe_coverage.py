"""
FROM SNOW TO RESERVOIR - S1 orbit selector (two-stage, read-only)
Author: Sebastian Macherey | github.com/sebastianmry/from-snow-to-reservoir

Picks the Sentinel-1 relative orbit (the s1_anchor in aoi_config.py) to anchor
the time series to, BEFORE the full re-download. Run it after a new clip_box is
set (e.g. after a catchment AOI change). Two stages:

  Stage A - footprint screen (free, no pixel download)
    Re-queries the OPERA granules over the AOI bbox and, purely from the granule
    footprint metadata, finds the dates whose tiles cover >= FOOTPRINT_MIN_COVER
    of the AOI. These full-coverage dates are grouped by 12-day repeat phase
    (= one relative orbit per satellite). This cheaply narrows ~all orbits down
    to the 2-3 candidate phases that actually span the whole period.

  Stage B - real-pixel coverage on a sample (--sample N)
    Footprint area is only the tile outline; the real per-pixel coverage
    (valid_px_pct after the AOI clip and the catchment mask) can differ between
    ascending/descending orbits because SAR layover & shadow leave NoData holes
    in steep terrain. So for each candidate phase we download N sample dates'
    B01_WTR tiles, mosaic + clip + mask them with the SAME code the real run
    uses (extract_timeseries.mosaic_tiles / extract_s1_stats), and report the
    real valid_px_pct. The phase with the best (and most consistent) pixel
    coverage is the recommended s1_anchor - an objective choice, not just the
    densest footprint.

  HLS is reported in stage A only (optical; the series keeps all clear dates,
  there is no single-orbit anchor to choose).

    python probe_coverage.py                 # stage A only (footprint), both AOIs
    python probe_coverage.py --sample 3      # + stage B: 3 test dates per phase
    python probe_coverage.py enguri --sample 4
    python probe_coverage.py zhinvali s1 --sample 3

Workflow: run with --sample, paste the recommended s1_anchor per AOI into
aoi_config.py, THEN run download_s1.py (which downloads only that one orbit).
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import earthaccess
from shapely.geometry import box as shp_box
from shapely.ops import unary_union

from download_common import (
    AOIS, DATE_START, DATE_END, FOOTPRINT_MIN_COVER,
    granule_date, granule_footprint, download_and_clip,
)
from extract_timeseries import mosaic_tiles, extract_s1_stats, load_catchment, NODATA

S1_REPEAT_DAYS = 12

COLLECTIONS = {
    "s1":  "OPERA_L3_DSWX-S1_V1",
    "hls": "OPERA_L3_DSWX-HLS_V1",
}

# Stage A -> stage B candidate filter: keep phases that look like genuine full
# orbits (enough full-coverage dates over a long span), drop the short late-start
# tracks (e.g. the S1C-only phase that begins May 2025).
MIN_CANDIDATE_FRACTION = 0.5    # >= 50% of the densest phase's date count
MIN_CANDIDATE_SPAN_DAYS = 300


def _ordinal(date_str: str) -> int:
    return datetime.strptime(date_str, "%Y%m%d").date().toordinal()


def span_days(dates: list[str]) -> int:
    ds = [datetime.strptime(d, "%Y%m%d").date() for d in dates]
    return (max(ds) - min(ds)).days if dates else 0


# ─────────────────────────────────────────────
# STAGE A - footprint screen
# ─────────────────────────────────────────────

def date_coverage(granules, aoi: dict) -> dict[str, float]:
    """Per date: union of tile footprints intersected with the AOI, as a fraction
    of the AOI area. Dates whose footprints cannot be parsed are reported as 1.0
    (fail-open, same convention as download_common.covered_dates)."""
    aoi_poly = shp_box(*aoi["bbox"])
    aoi_area = aoi_poly.area

    by_date = defaultdict(list)
    for g in granules:
        by_date[granule_date(g)].append(g)

    cover = {}
    for date_str, gs in by_date.items():
        if date_str == "unknown":
            continue
        fps = [fp for fp in (granule_footprint(g) for g in gs) if fp is not None]
        cover[date_str] = (1.0 if not fps
                           else unary_union(fps).intersection(aoi_poly).area / aoi_area)
    return cover


def group_by_phase(full_dates: list[str]) -> dict[int, list[str]]:
    """Group full-coverage dates by 12-day repeat phase (relative to the earliest)."""
    if not full_dates:
        return {}
    ref = _ordinal(min(full_dates))
    by_phase: dict[int, list[str]] = defaultdict(list)
    for d in full_dates:
        by_phase[(_ordinal(d) - ref) % S1_REPEAT_DAYS].append(d)
    return by_phase


def probe_hls(aoi: dict):
    granules = earthaccess.search_data(
        short_name=COLLECTIONS["hls"], bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END), count=-1,
    )
    cover = date_coverage(granules, aoi)
    full = sorted(d for d, c in cover.items() if c >= FOOTPRINT_MIN_COVER)
    print(f"  HLS : {len(granules)} granules, {len(cover)} dates, "
          f"{len(full)} cover >= {FOOTPRINT_MIN_COVER*100:.0f}% AOI "
          f"(rest are cloud/partial; HLS keeps all clear dates, no orbit anchor)")


def probe_s1_footprint(aoi: dict):
    """Stage A for S1. Returns (granules_by_date, by_phase, candidate_phases)."""
    granules = earthaccess.search_data(
        short_name=COLLECTIONS["s1"], bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END), count=-1,
    )
    by_date = defaultdict(list)
    for g in granules:
        by_date[granule_date(g)].append(g)

    cover = date_coverage(granules, aoi)
    full = sorted(d for d, c in cover.items() if c >= FOOTPRINT_MIN_COVER)
    print(f"  S1  : {len(granules)} granules, {len(cover)} dates, "
          f"{len(full)} full-coverage (>= {FOOTPRINT_MIN_COVER*100:.0f}% AOI footprint)")
    by_phase = group_by_phase(full)
    if not by_phase:
        print("    no full-coverage S1 dates - widen the box or lower the threshold")
        return by_date, {}, []

    max_count = max(len(v) for v in by_phase.values())
    candidates = []
    print(f"    full-coverage dates by 12-day phase (phase: count, span, first):")
    for phase in sorted(by_phase):
        ds = sorted(by_phase[phase])
        is_cand = (len(ds) >= MIN_CANDIDATE_FRACTION * max_count
                   and span_days(ds) >= MIN_CANDIDATE_SPAN_DAYS)
        if is_cand:
            candidates.append(phase)
        tag = " <- candidate" if is_cand else " (short/late track, skipped)"
        print(f"      phase {phase:>2}: {len(ds):>3} dates, span {span_days(ds):>4}d, "
              f"first {ds[0]}{tag}")
    return by_date, by_phase, candidates


# ─────────────────────────────────────────────
# STAGE B - real-pixel coverage on a sample
# ─────────────────────────────────────────────

def pick_sample_dates(dates_sorted: list[str], n: int) -> list[str]:
    """Evenly spaced sample (first, ..., last) so coverage is judged across the
    whole period, not just consecutive scenes."""
    if len(dates_sorted) <= n:
        return dates_sorted
    idx = sorted({round(i * (len(dates_sorted) - 1) / (n - 1)) for i in range(n)})
    return [dates_sorted[i] for i in idx]


def wtr_links_for_date(granules_on_date) -> list[str]:
    links = []
    for g in granules_on_date:
        for link in g.data_links():
            if "B01_WTR" in Path(link).name:
                links.append(link)
    return links


def sample_phase_coverage(aoi: dict, by_date: dict, phase_dates: list[str],
                          fs, catchment, n: int) -> list[float]:
    """Download N sample dates of one phase, mosaic + clip + catchment-mask them
    exactly like the real run, and return the per-date valid_px_pct list."""
    sample = pick_sample_dates(sorted(phase_dates), n)
    covs = []
    for date_str in sample:
        links = wtr_links_for_date(by_date[date_str])
        tiles = [b for b in (download_and_clip(fs, l, aoi["clip_box"]) for l in links)
                 if b is not None]
        if not tiles:
            print(f"      {date_str}: no readable tiles")
            continue
        mosaic = mosaic_tiles(tiles, NODATA, aoi["clip_box"])
        if mosaic is None:
            print(f"      {date_str}: mosaic failed")
            continue
        stats = extract_s1_stats(mosaic, None, catchment)
        cov = stats["valid_px_pct"]
        covs.append(cov)
        print(f"      {date_str}: valid_px_pct {cov:5.1f}%  ({len(tiles)} tiles)")
    return covs


def probe_s1_sample(aoi: dict, by_date: dict, by_phase: dict,
                    candidates: list[int], n: int):
    """Stage B: compare real pixel coverage of the candidate phases and recommend."""
    if not candidates:
        print("    no candidate phases to sample")
        return
    print(f"\n    Stage B - real pixel coverage, {n} sample dates per candidate phase:")
    fs = earthaccess.get_requests_https_session()
    catchment = load_catchment(aoi["name"])
    if catchment is not None:
        print("      (valid_px_pct is catchment-relative, like the real run)")

    results = {}
    for phase in candidates:
        ds = sorted(by_phase[phase])
        print(f"    phase {phase} (anchor candidate {ds[0]}, {len(ds)} dates):")
        covs = sample_phase_coverage(aoi, by_date, ds, fs, catchment, n)
        if covs:
            results[phase] = (sum(covs) / len(covs), min(covs), len(ds), ds[0])

    if not results:
        print("    no coverage measured")
        return

    print(f"\n    {'phase':>5}  {'mean cov':>8}  {'min cov':>7}  {'#dates':>6}  anchor")
    for phase, (mean_c, min_c, ndates, first) in sorted(results.items()):
        print(f"    {phase:>5}  {mean_c:>7.1f}%  {min_c:>6.1f}%  {ndates:>6}  {first}")

    # Recommend: best mean pixel coverage; ties broken by more dates.
    best = max(results, key=lambda p: (round(results[p][0], 1), results[p][2]))
    mean_c, min_c, ndates, first = results[best]
    print(f"    -> recommended s1_anchor = {first}  (phase {best}, "
          f"mean cov {mean_c:.1f}%, {ndates} dates) - paste into aoi_config.py")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Two-stage Sentinel-1 orbit selector")
    ap.add_argument("filters", nargs="*",
                    help="optional AOI name (enguri/zhinvali) and/or sensor (s1/hls)")
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="stage B: download N test dates per candidate phase and "
                         "measure real valid_px_pct (default 0 = footprint only)")
    args = ap.parse_args()

    flt = [a.lower() for a in args.filters]
    want_aoi = next((a for a in flt if a in {x["name"] for x in AOIS}), None)
    want_sensor = next((a for a in flt if a in COLLECTIONS), None)

    print("NASA Earthdata Login...")
    try:
        earthaccess.login(strategy="netrc")
    except Exception:
        earthaccess.login(strategy="interactive", persist=True)
    print("OK")

    for aoi in AOIS:
        if want_aoi and aoi["name"] != want_aoi:
            continue
        print(f"\n{'='*70}\nAOI: {aoi['label']}  bbox={aoi['bbox']}\n{'='*70}")
        if want_sensor in (None, "hls"):
            probe_hls(aoi)
        if want_sensor in (None, "s1"):
            by_date, by_phase, candidates = probe_s1_footprint(aoi)
            if args.sample > 0:
                probe_s1_sample(aoi, by_date, by_phase, candidates, args.sample)


if __name__ == "__main__":
    main()
