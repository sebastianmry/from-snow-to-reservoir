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
import statistics
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import earthaccess
import pandas as pd
from shapely.geometry import box as shp_box
from shapely.ops import unary_union

from download_common import (
    AOIS, DATE_START, DATE_END, FOOTPRINT_MIN_COVER,
    granule_date, granule_footprint, download_and_clip,
)
from extract_timeseries import (
    mosaic_tiles, extract_s1_stats, load_catchment, load_reservoir, NODATA,
)

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
    parsed_dates = [datetime.strptime(d, "%Y%m%d").date() for d in dates]
    return (max(parsed_dates) - min(parsed_dates)).days if dates else 0


# ─────────────────────────────────────────────
# STAGE A - footprint screen
# ─────────────────────────────────────────────

def date_coverage(granules, aoi: dict) -> dict[str, float]:
    """Per date: union of tile footprints intersected with the AOI, as a fraction
    of the AOI area. Dates whose footprints cannot be parsed are reported as 1.0
    (fail-open, same convention as download_common.covered_dates)."""
    aoi_poly = shp_box(*aoi["bbox"])
    aoi_area = aoi_poly.area

    granules_by_date = defaultdict(list)
    for granule in granules:
        granules_by_date[granule_date(granule)].append(granule)

    cover = {}
    for date_str, date_granules in granules_by_date.items():
        if date_str == "unknown":
            continue
        footprints = [fp for fp in (granule_footprint(g) for g in date_granules)
                      if fp is not None]
        cover[date_str] = (1.0 if not footprints
                           else unary_union(footprints).intersection(aoi_poly).area / aoi_area)
    return cover


def group_by_phase(full_dates: list[str]) -> dict[int, list[str]]:
    """Group full-coverage dates by 12-day repeat phase (relative to the earliest)."""
    if not full_dates:
        return {}
    ref_ordinal = _ordinal(min(full_dates))
    by_phase: dict[int, list[str]] = defaultdict(list)
    for date_str in full_dates:
        by_phase[(_ordinal(date_str) - ref_ordinal) % S1_REPEAT_DAYS].append(date_str)
    return by_phase


def probe_hls(aoi: dict):
    granules = earthaccess.search_data(
        short_name=COLLECTIONS["hls"], bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END), count=-1,
    )
    cover = date_coverage(granules, aoi)
    full_dates = sorted(d for d, c in cover.items() if c >= FOOTPRINT_MIN_COVER)
    print(f"  HLS : {len(granules)} granules, {len(cover)} dates, "
          f"{len(full_dates)} cover >= {FOOTPRINT_MIN_COVER*100:.0f}% AOI "
          f"(rest are cloud/partial; HLS keeps all clear dates, no orbit anchor)")


def probe_s1_footprint(aoi: dict):
    """Stage A for S1. Returns (granules_by_date, by_phase, candidate_phases)."""
    granules = earthaccess.search_data(
        short_name=COLLECTIONS["s1"], bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END), count=-1,
    )
    by_date = defaultdict(list)
    for granule in granules:
        by_date[granule_date(granule)].append(granule)

    cover = date_coverage(granules, aoi)
    full_dates = sorted(d for d, c in cover.items() if c >= FOOTPRINT_MIN_COVER)
    print(f"  S1  : {len(granules)} granules, {len(cover)} dates, "
          f"{len(full_dates)} full-coverage (>= {FOOTPRINT_MIN_COVER*100:.0f}% AOI footprint)")
    by_phase = group_by_phase(full_dates)
    if not by_phase:
        print("    no full-coverage S1 dates - widen the box or lower the threshold")
        return by_date, {}, []

    max_count = max(len(phase_dates) for phase_dates in by_phase.values())
    candidates = []
    print("    full-coverage dates by 12-day phase (phase: count, span, first):")
    for phase in sorted(by_phase):
        phase_dates = sorted(by_phase[phase])
        is_candidate = (len(phase_dates) >= MIN_CANDIDATE_FRACTION * max_count
                        and span_days(phase_dates) >= MIN_CANDIDATE_SPAN_DAYS)
        if is_candidate:
            candidates.append(phase)
        tag = " <- candidate" if is_candidate else " (short/late track, skipped)"
        print(f"      phase {phase:>2}: {len(phase_dates):>3} dates, "
              f"span {span_days(phase_dates):>4}d, first {phase_dates[0]}{tag}")
    return by_date, by_phase, candidates


# ─────────────────────────────────────────────
# STAGE B - real-pixel coverage on a sample
# ─────────────────────────────────────────────

def pick_sample_dates(dates_sorted: list[str], n_samples: int) -> list[str]:
    """Evenly spaced sample (first, ..., last) so coverage is judged across the
    whole period, not just consecutive scenes."""
    if len(dates_sorted) <= n_samples:
        return dates_sorted
    indices = sorted({round(i * (len(dates_sorted) - 1) / (n_samples - 1))
                      for i in range(n_samples)})
    return [dates_sorted[i] for i in indices]


def wtr_links_for_date(granules_on_date) -> list[str]:
    links = []
    for granule in granules_on_date:
        for link in granule.data_links():
            if "B01_WTR" in Path(link).name:
                links.append(link)
    return links


def sample_phase_coverage(aoi: dict, by_date: dict, phase_dates: list[str],
                          session, catchment, n_samples: int) -> list[float]:
    """Download N sample dates of one phase, mosaic + clip + catchment-mask them
    exactly like the real run, and return the per-date valid_px_pct list."""
    sample_dates = pick_sample_dates(sorted(phase_dates), n_samples)

    # Download every sample date's tiles concurrently (network-bound; the clip is
    # cheap). Each date keeps its own ordered tile list for the mosaic step below.
    def fetch_date(date_str):
        links = wtr_links_for_date(by_date[date_str])
        tiles = [t for t in
                 (download_and_clip(session, link, aoi["clip_box"]) for link in links)
                 if t is not None]
        return date_str, tiles

    with ThreadPoolExecutor(max_workers=8) as pool:
        tiles_by_date = dict(pool.map(fetch_date, sample_dates))

    coverages = []
    for date_str in sample_dates:
        tiles = tiles_by_date[date_str]
        if not tiles:
            print(f"      {date_str}: no readable tiles")
            continue
        mosaic = mosaic_tiles(tiles, NODATA, aoi["clip_box"])
        if mosaic is None:
            print(f"      {date_str}: mosaic failed")
            continue
        stats = extract_s1_stats(mosaic, None, catchment)
        coverage = stats["valid_px_pct"]
        coverages.append(coverage)
        print(f"      {date_str}: valid_px_pct {coverage:5.1f}%  ({len(tiles)} tiles)")
    return coverages


def probe_s1_sample(aoi: dict, by_date: dict, by_phase: dict,
                    candidates: list[int], n_samples: int, only_phase: int = None):
    """Stage B: compare real pixel coverage of the candidate phases and recommend."""
    if only_phase is not None:
        candidates = [p for p in candidates if p == only_phase]
        if not candidates:
            print(f"    phase {only_phase} is not a candidate phase")
            return
    if not candidates:
        print("    no candidate phases to sample")
        return
    print(f"\n    Stage B - real pixel coverage, {n_samples} sample dates per candidate phase:")
    session = earthaccess.get_requests_https_session()
    catchment = load_catchment(aoi["name"])
    if catchment is not None:
        print("      (valid_px_pct is catchment-relative, like the real run)")

    results = {}
    for phase in candidates:
        phase_dates = sorted(by_phase[phase])
        print(f"    phase {phase} (anchor candidate {phase_dates[0]}, {len(phase_dates)} dates):")
        coverages = sample_phase_coverage(aoi, by_date, phase_dates, session,
                                          catchment, n_samples)
        if coverages:
            results[phase] = (sum(coverages) / len(coverages), min(coverages),
                              len(phase_dates), phase_dates[0])

    if not results:
        print("    no coverage measured")
        return

    print(f"\n    {'phase':>5}  {'mean cov':>8}  {'min cov':>7}  {'#dates':>6}  anchor")
    for phase, (mean_cov, min_cov, n_dates, first_date) in sorted(results.items()):
        print(f"    {phase:>5}  {mean_cov:>7.1f}%  {min_cov:>6.1f}%  {n_dates:>6}  {first_date}")

    # Recommend: best mean pixel coverage; ties broken by more dates.
    best_phase = max(results, key=lambda p: (round(results[p][0], 1), results[p][2]))
    mean_cov, min_cov, n_dates, first_date = results[best_phase]
    print(f"    -> recommended s1_anchor = {first_date}  (phase {best_phase}, "
          f"mean cov {mean_cov:.1f}%, {n_dates} dates) - paste into aoi_config.py")


# ─────────────────────────────────────────────
# SECOND-ORBIT CONSISTENCY CHECK
# ─────────────────────────────────────────────
# Cheap test of whether a SECOND ~99% orbit (different 12-day phase = different
# acquisition days) could densify the S1 reservoir series from ~12 to ~6 days
# WITHOUT introducing a look-geometry sawtooth. We sample a few dates of the
# second orbit, compute reservoir_area_km2 (same code as the real run) and put
# each next to the nearest date of the existing (anchor-orbit) series. If the
# values interleave smoothly (small, unbiased diff) the orbits agree on the lake
# and can be combined; a systematic offset means keep the single orbit.

def compare_second_orbit(aoi: dict, orbit_date: str, n_samples: int):
    site = aoi["name"]
    parquet_path = Path(f"{site}_s1_timeseries.parquet")
    if not parquet_path.exists():
        print(f"  no {parquet_path} to compare against (run extract_timeseries.py first)")
        return
    series_df = pd.read_parquet(parquet_path)
    if "reservoir_area_km2" not in series_df.columns:
        print("  existing series has no reservoir_area_km2")
        return
    series_df = series_df.dropna(subset=["reservoir_area_km2"]).copy()
    series_df["date_dt"] = pd.to_datetime(series_df["date"])
    if series_df.empty:
        print("  existing reservoir series is empty")
        return

    granules = earthaccess.search_data(
        short_name=COLLECTIONS["s1"], bounding_box=aoi["bbox"],
        temporal=(DATE_START, DATE_END), count=-1,
    )
    by_date = defaultdict(list)
    for granule in granules:
        by_date[granule_date(granule)].append(granule)
    cover = date_coverage(granules, aoi)
    full_dates = sorted(d for d, c in cover.items() if c >= FOOTPRINT_MIN_COVER)
    orbit_ordinal = _ordinal(orbit_date)
    second_orbit_dates = [d for d in full_dates
                          if (_ordinal(d) - orbit_ordinal) % S1_REPEAT_DAYS == 0]
    if not second_orbit_dates:
        print(f"  no full-coverage S1 dates on the orbit of {orbit_date}")
        return
    sample_dates = pick_sample_dates(second_orbit_dates, n_samples)

    session = earthaccess.get_requests_https_session()
    catchment = load_catchment(site)
    reservoir = load_reservoir(site)
    if reservoir is None:
        print("  no reservoir polygon (run derive_reservoir.py first)")
        return

    print(f"  2nd orbit (anchor {orbit_date}): {len(second_orbit_dates)} full-coverage "
          f"dates, sampling {len(sample_dates)}. Existing anchor series: "
          f"{len(series_df)} reservoir dates.")
    print(f"  {'2nd-orbit':>10} {'res_2nd':>8} {'valid%':>7} {'resvalid%':>9}   "
          f"{'near-anchor':>11} {'res_anchor':>10} {'gap_d':>5} {'diff':>7}")
    diffs = []
    for date_str in sample_dates:
        links = wtr_links_for_date(by_date[date_str])
        tiles = [tile_bytes for tile_bytes in
                 (download_and_clip(session, link, aoi["clip_box"]) for link in links)
                 if tile_bytes is not None]
        if not tiles:
            print(f"  {date_str:>10}  (no readable tiles)")
            continue
        mosaic = mosaic_tiles(tiles, NODATA, aoi["clip_box"])
        if mosaic is None:
            print(f"  {date_str:>10}  (mosaic failed)")
            continue
        stats = extract_s1_stats(mosaic, reservoir, catchment)
        reservoir_area = stats.get("reservoir_area_km2")
        valid_pct = stats.get("valid_px_pct")
        reservoir_valid_pct = stats.get("reservoir_valid_pct", 100.0)
        date_dt = pd.Timestamp(datetime.strptime(date_str, "%Y%m%d"))
        nearest = series_df.loc[(series_df["date_dt"] - date_dt).abs().idxmin()]
        gap_days = abs((nearest["date_dt"] - date_dt).days)
        anchor_area = nearest["reservoir_area_km2"]
        # reservoir_area == itself: not NaN (the data guard did not fire)
        is_comparable = (reservoir_area == reservoir_area) and reservoir_valid_pct >= 95.0
        diff_str = f"{reservoir_area - anchor_area:+7.2f}" if is_comparable else "    -  "
        if is_comparable:
            diffs.append(reservoir_area - anchor_area)
        area_str = f"{reservoir_area:8.2f}" if reservoir_area == reservoir_area else "     nan"
        print(f"  {date_str:>10} {area_str} {valid_pct:6.1f}% {reservoir_valid_pct:8.1f}%   "
              f"{nearest['date_dt'].strftime('%Y%m%d'):>11} {anchor_area:10.2f} "
              f"{gap_days:5d} {diff_str}")

    if diffs:
        mean_diff = statistics.mean(diffs)
        mean_abs_diff = statistics.mean(abs(x) for x in diffs)
        print(f"\n  -> mean diff {mean_diff:+.2f} km2, mean |diff| {mean_abs_diff:.2f} km2 "
              f"over {len(diffs)} comparable dates")
        print("     small & unbiased  -> orbits agree on the lake, combine for ~6-day density")
        print("     systematic offset -> keep the single orbit (geometry sawtooth)")
    else:
        print("\n  no comparable dates (all guarded/partial)")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Two-stage Sentinel-1 orbit selector")
    parser.add_argument("filters", nargs="*",
                        help="optional AOI name (enguri/zhinvali) and/or sensor (s1/hls)")
    parser.add_argument("--sample", type=int, default=0, metavar="N",
                        help="stage B: download N test dates per candidate phase and "
                             "measure real valid_px_pct (default 0 = footprint only)")
    parser.add_argument("--only-phase", type=int, default=None, metavar="P",
                        help="stage B: restrict sampling to this 12-day phase only "
                             "(skips the other candidate phases)")
    parser.add_argument("--compare-orbit", metavar="YYYYMMDD", default=None,
                        help="second-orbit check: sample dates of the orbit through this "
                             "date and compare reservoir_area_km2 to the existing series "
                             "(needs an AOI filter). Use --sample N for the count.")
    args = parser.parse_args()

    filters = [token.lower() for token in args.filters]
    want_aoi = next((t for t in filters if t in {x["name"] for x in AOIS}), None)
    want_sensor = next((t for t in filters if t in COLLECTIONS), None)

    if args.compare_orbit and not want_aoi:
        print("ERROR: --compare-orbit needs an AOI (the orbit date is AOI-specific), "
              "e.g. 'python probe_coverage.py zhinvali --compare-orbit 20240831 --sample 8'")
        return

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
        if args.compare_orbit:
            compare_second_orbit(aoi, args.compare_orbit, args.sample or 8)
            continue
        if want_sensor in (None, "hls"):
            probe_hls(aoi)
        if want_sensor in (None, "s1"):
            by_date, by_phase, candidates = probe_s1_footprint(aoi)
            if args.sample > 0:
                probe_s1_sample(aoi, by_date, by_phase, candidates, args.sample,
                                only_phase=args.only_phase)


if __name__ == "__main__":
    main()
