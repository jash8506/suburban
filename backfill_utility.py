"""Materialize utility_readings.csv into per-day parquet files under log/.

Each output file `log/utility_YYYY-MM-DD.parquet` carries 1-minute rows with:
  _time         UTC timestamp
  meter_power   net average power over the minute (W, signed, +=import)
  import_power  gross import power (W, always >= 0)
  export_power  gross export power (W, always >= 0)

Real logger data (power_*.parquet, Fern *.gz) wins over these files on the
1-minute dedup step in energy.py, so utility data only fills holes.

Re-runnable: overwrites existing utility_*.parquet files. Run again whenever
utility_readings.csv has been refreshed.
"""

from pathlib import Path

import pandas as pd

LOG_DIR = Path("log")
UTILITY_CSV = Path("utility_readings.csv")


def parse_section(skiprows: int, nrows: int, value_name: str) -> pd.Series:
    df = pd.read_csv(UTILITY_CSV, encoding="utf-8-sig", skiprows=skiprows, nrows=nrows)
    df = df[df["Date/Time"].astype(str).str.fullmatch(r"\d{8}")]
    df = df.drop(columns=[c for c in ("Quality", "Total") if c in df.columns])
    df["date"] = pd.to_datetime(df["Date/Time"], format="%Y%m%d")
    df = df.drop(columns=["Date/Time"])
    long = df.melt(id_vars="date", var_name="time_str", value_name=value_name)
    long[value_name] = pd.to_numeric(long[value_name], errors="coerce")
    h_m = long["time_str"].str.split(":", expand=True).astype(int)
    ts = (long["date"]
          + pd.to_timedelta(h_m[0], unit="h")
          + pd.to_timedelta(h_m[1], unit="m"))
    # AEST is fixed UTC+10 per the CSV's LOCAL TIME row (no DST).
    long["ts"] = ts.dt.tz_localize("+10:00").dt.tz_convert("UTC")
    return long.set_index("ts")[value_name].sort_index()


def main() -> None:
    if not UTILITY_CSV.exists():
        print(f"{UTILITY_CSV} not found — nothing to do")
        return
    LOG_DIR.mkdir(exist_ok=True)

    exports = parse_section(3, 370, "export_kwh")
    imports = parse_section(373, 370, "import_kwh")
    five_min = pd.concat([imports, exports], axis=1).dropna(how="all").sort_index()

    # kWh over 5 min -> average power in W: kWh * 3.6e6 J / 300 s = kWh * 12000.
    import_w = five_min["import_kwh"] * 12_000.0
    export_w = five_min["export_kwh"] * 12_000.0
    out_5 = pd.DataFrame({
        "meter_power": import_w - export_w,
        "import_power": import_w,
        "export_power": export_w,
    })

    # Expand each 5-min row to 5 one-minute rows (constant within the interval).
    idx_1min = pd.date_range(
        out_5.index.min(),
        out_5.index.max() + pd.Timedelta(minutes=4),
        freq="1min", tz="UTC",
    )
    out_1 = out_5.reindex(idx_1min, method="ffill", limit=4).dropna(how="all")

    # Group by AEST date so each parquet matches the utility's own day boundaries.
    aest_date = pd.Series(out_1.index.tz_convert("+10:00").date, index=out_1.index)
    written = 0
    for date, group in out_1.groupby(aest_date):
        path = LOG_DIR / f"utility_{date}.parquet"
        group.reset_index().rename(columns={"index": "_time"}).to_parquet(path, index=False)
        written += 1
    print(f"Wrote {written} files to {LOG_DIR}/utility_*.parquet")


if __name__ == "__main__":
    main()
