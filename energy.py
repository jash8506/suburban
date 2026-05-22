"""Aggregate suburban power logs into a daily energy and cost summary.

Pipeline:
  1. Load every log/power_*.parquet, resample to 1-minute mean power (UTC).
  2. Reindex to a continuous 1-minute grid; any NaN minute is a gap >1 min.
  3. Fill gaps with the mean of the same minute-of-day in a +/-15 day window.
  4. Trapezoidal integration of power -> kWh per minute interval.
  5. Group by local (Sydney) date, split imports/load by peak vs off-peak window.
  6. Compute "no solar/battery" cost (load * tariff) vs actual cost
     (imports * tariff - exports * feed-in + supply).

Outputs:
  log_summary/minute.parquet  - gap-filled 1-minute power series
  log_summary/daily.parquet   - daily summary used by app.py
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

LOG_DIR = Path("log")
OUT_DIR = Path("log_summary")
DAILY_JSON = Path("daily.json")
TZ = "Australia/Sydney"

# Tariff (cents). Edit these to match your retailer.
PEAK_C_PER_KWH = 56.0
OFFPEAK_C_PER_KWH = 22.0
FEED_IN_C_PER_KWH = 2.0
SUPPLY_C_PER_DAY = 90.0
PEAK_START_HOUR = 15  # inclusive, local time
PEAK_END_HOUR = 21    # exclusive

GAP_FILL_WINDOW_DAYS = 30
MEASURED_COLUMNS = ["load_power", "inverter_power", "meter_power"]
PARQUET_COLUMNS = ["_time", *MEASURED_COLUMNS, "import_power", "export_power"]
CSV_COLUMNS = ["time", *MEASURED_COLUMNS]


def _resample_parquet(path: Path) -> pd.DataFrame | None:
    import pyarrow.parquet as pq

    # A few days were logged as empty parquet files (zero columns). Skip them.
    schema = pq.read_schema(path)
    names = {field.name for field in schema}
    if "_time" not in names:
        return None
    cols = [c for c in PARQUET_COLUMNS if c in names]
    df = pd.read_parquet(path, columns=cols).set_index("_time").sort_index()
    return df.resample("1min").mean()


def _resample_csv(path: Path) -> pd.DataFrame | None:
    """Load a Fern CSV log (gzipped or plain). `time` is unix epoch (UTC)."""
    df = pd.read_csv(path, usecols=lambda c: c in CSV_COLUMNS, on_bad_lines="skip")
    if df.empty:
        return None
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    if df.empty:
        return None
    df.index = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.drop(columns=["time"]).sort_index()
    return df.resample("1min").mean()


def load_minute_resampled() -> pd.DataFrame:
    parquets = sorted(LOG_DIR.glob("power_*.parquet"))
    # Fern files are the post-Apr-2026 CSV format: `Fern YYYY-MM-DD.gz` for
    # rolled-over days plus a live `Fern` (no extension) for today.
    csvs = sorted(LOG_DIR.glob("Fern *.gz"))
    live = LOG_DIR / "Fern"
    if live.exists():
        csvs.append(live)
    # Utility parquets are loaded LAST so real logger data wins the dedup below.
    utility_files = sorted(LOG_DIR.glob("utility_*.parquet"))

    print(f"Loading {len(parquets)} parquet + {len(csvs)} CSV + "
          f"{len(utility_files)} utility files...")

    frames = []
    skipped = 0
    for i, f in enumerate(parquets, 1):
        m = _resample_parquet(f)
        if m is None:
            skipped += 1
        else:
            frames.append(m)
        if i % 100 == 0:
            print(f"  parquet {i}/{len(parquets)}")
    for i, f in enumerate(csvs, 1):
        m = _resample_csv(f)
        if m is None:
            skipped += 1
        else:
            frames.append(m)
        if i % 10 == 0:
            print(f"  csv {i}/{len(csvs)}")
    for f in utility_files:
        m = _resample_parquet(f)
        if m is not None:
            frames.append(m)

    if skipped:
        print(f"Skipped {skipped} empty file(s)")
    # Dedup BEFORE sort_index so concat order is the tiebreaker: logger frames
    # come first, so when a logger row and a utility row share a minute the
    # logger row wins. (sort_index defaults to non-stable quicksort.)
    out = pd.concat(frames)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    full = pd.date_range(out.index.min(), out.index.max(), freq="1min", tz="UTC")
    return out.reindex(full)


def fill_gaps(df: pd.DataFrame, window_days: int = GAP_FILL_WINDOW_DAYS) -> pd.DataFrame:
    """Fill NaN minutes with the mean of the same minute-of-day in a centred 30-day window.

    Only the three measured columns (load/inverter/meter) get filled. The utility
    `import_power`/`export_power` columns stay NaN where the utility didn't write
    them — `integrate_and_summarise` uses those columns as a presence flag.
    """
    minute_of_day = df.index.hour * 60 + df.index.minute
    out = df.copy()
    for col in MEASURED_COLUMNS:
        if col not in df.columns:
            continue
        s = df[col]
        # Within each minute-of-day group rows are in date order, so a 30-row
        # centred rolling mean is a 30-day centred mean across that time of day.
        rolling = s.groupby(minute_of_day).transform(
            lambda x: x.rolling(window_days, min_periods=1, center=True).mean()
        )
        filled = s.fillna(rolling)
        # Any minute-of-day that was never observed in the whole window falls back
        # to the overall column mean so downstream math stays NaN-free.
        out[col] = filled.fillna(filled.mean())
    return out


def integrate_and_summarise(df: pd.DataFrame) -> pd.DataFrame:
    # Trapezoidal energy per 1-min interval ending at row i, in kWh.
    kwh_per_w_min = 60.0 / 3.6e6

    def trapz(col: pd.Series) -> pd.Series:
        return (col + col.shift(1)) / 2.0 * kwh_per_w_min

    load_kwh = trapz(df["load_power"]).clip(lower=0)
    solar_kwh = trapz(df["inverter_power"]).clip(lower=0)
    meter_kwh = trapz(df["meter_power"])  # +ve = import, -ve = export
    import_kwh = meter_kwh.clip(lower=0)
    export_kwh = (-meter_kwh).clip(lower=0)

    # Utility log files carry gross import/export power separately. Where those
    # columns are set, use them directly — that preserves the gross totals when
    # the meter crossed zero inside a 5-minute interval (info that the net-only
    # trapezoidal would have collapsed).
    if "import_power" in df.columns and "export_power" in df.columns:
        has_util = df["import_power"].notna()
        if has_util.any():
            util_imp = df["import_power"] / 60_000.0  # avg W over 1 min -> kWh
            util_exp = df["export_power"] / 60_000.0
            import_kwh = import_kwh.where(~has_util, util_imp)
            export_kwh = export_kwh.where(~has_util, util_exp)
            print(f"Utility log coverage: {int(has_util.sum()):,} minutes "
                  f"({has_util.groupby(df.index.tz_convert(TZ).date).any().sum()} days)")

    local = df.index.tz_convert(TZ)
    peak = (local.hour >= PEAK_START_HOUR) & (local.hour < PEAK_END_HOUR)
    local_date = pd.Series(local.date, index=df.index)

    per_minute = pd.DataFrame({
        "load_kwh": load_kwh,
        "solar_kwh": solar_kwh,
        "import_kwh": import_kwh,
        "export_kwh": export_kwh,
        "load_kwh_peak": load_kwh.where(peak, 0.0),
        "load_kwh_offpeak": load_kwh.where(~peak, 0.0),
        "import_kwh_peak": import_kwh.where(peak, 0.0),
        "import_kwh_offpeak": import_kwh.where(~peak, 0.0),
    })
    daily = per_minute.groupby(local_date).sum()
    daily.index.name = "date"

    peak_rate = PEAK_C_PER_KWH / 100.0
    off_rate = OFFPEAK_C_PER_KWH / 100.0
    feed_in = FEED_IN_C_PER_KWH / 100.0
    supply = SUPPLY_C_PER_DAY / 100.0

    daily["cost_no_solar"] = (
        daily["load_kwh_peak"] * peak_rate
        + daily["load_kwh_offpeak"] * off_rate
        + supply
    )
    daily["cost_actual"] = (
        daily["import_kwh_peak"] * peak_rate
        + daily["import_kwh_offpeak"] * off_rate
        - daily["export_kwh"] * feed_in
        + supply
    )
    daily["savings"] = daily["cost_no_solar"] - daily["cost_actual"]
    return daily


def list_log_files() -> list[str]:
    """Inventory of raw log files the static page can fetch directly."""
    keep = []
    for p in sorted(LOG_DIR.iterdir()):
        if not p.is_file():
            continue
        name = p.name
        if (name.startswith("power_") and name.endswith(".parquet")) \
           or (name.startswith("utility_") and name.endswith(".parquet")) \
           or (name.startswith("Fern ") and name.endswith(".gz")) \
           or name == "Fern":
            keep.append(name)
    return keep


def write_json(daily: pd.DataFrame, path: Path) -> None:
    cols = [
        "load_kwh", "solar_kwh", "import_kwh", "export_kwh",
        "load_kwh_peak", "load_kwh_offpeak",
        "import_kwh_peak", "import_kwh_offpeak",
        "cost_no_solar", "cost_actual", "savings",
    ]
    days = [
        {"date": d.isoformat(), **{c: round(float(r[c]), 3) for c in cols}}
        for d, r in daily.iterrows()
    ]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tariff": {
            "peak_c_per_kwh": PEAK_C_PER_KWH,
            "offpeak_c_per_kwh": OFFPEAK_C_PER_KWH,
            "feed_in_c_per_kwh": FEED_IN_C_PER_KWH,
            "supply_c_per_day": SUPPLY_C_PER_DAY,
            "peak_window": f"{PEAK_START_HOUR:02d}:00-{PEAK_END_HOUR:02d}:00 {TZ}",
        },
        "days": days,
        "log_files": list_log_files(),
    }
    path.write_text(json.dumps(payload, separators=(",", ":")))


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    raw = load_minute_resampled()
    nan_minutes = raw["load_power"].isna().sum()
    print(f"Resampled to {len(raw):,} minutes; {nan_minutes:,} missing in load_power")
    filled = fill_gaps(raw)
    filled.to_parquet(OUT_DIR / "minute.parquet")
    daily = integrate_and_summarise(filled)
    daily.to_parquet(OUT_DIR / "daily.parquet")
    write_json(daily, DAILY_JSON)
    print(f"Wrote {len(daily)} days to {OUT_DIR/'daily.parquet'} and {DAILY_JSON}")
    print(daily.tail().round(2))


if __name__ == "__main__":
    main()
