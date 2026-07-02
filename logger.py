import argparse
import collections
import datetime
import logging
import subprocess
import time
import os
import threading

import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS
import requests
import urllib3
from csv_logger import CSV_Handler
from dbfilter import DeviceFilter

# battery inverter uses a self-signed cert; we poll it with verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

EMPTY_ENTRY = {
    "inverter_power": None,
    # negative is export
    "meter_power": None,
    "meter_volts": None,
    "meter_amps": None,
    "meter_va": None,
    "meter_var": None,
    "meter_w_dmd": None,
    "meter_w_dmd_peak": None,
    "meter_pf": None,
    "meter_hz": None,
    "battery_power": None,
    "battery_inverter_temp": None,
    "load_power": None,
}


# Each device's fields are deadband-filtered and logged independently, keyed
# off that device's primary power field, so one device going offline (e.g. the
# solar inverter dropping off wifi) can't stall logging for the others.
# load_power is derived from all three, so it gets its own filter and simply
# pauses while any input is missing.
DEADBAND_W = 15
MAX_SAVE_INTERVAL_S = 15000
device_filters = [
    DeviceFilter("inverter", "inverter_power", ["inverter_power"],
                 DEADBAND_W, MAX_SAVE_INTERVAL_S),
    DeviceFilter("meter", "meter_power",
                 ["meter_power", "meter_volts", "meter_amps", "meter_va",
                  "meter_var", "meter_w_dmd", "meter_w_dmd_peak", "meter_pf",
                  "meter_hz"],
                 DEADBAND_W, MAX_SAVE_INTERVAL_S),
    DeviceFilter("battery", "battery_power",
                 ["battery_power", "battery_inverter_temp"],
                 DEADBAND_W, MAX_SAVE_INTERVAL_S),
    DeviceFilter("load", "load_power", ["load_power"],
                 DEADBAND_W, MAX_SAVE_INTERVAL_S),
]

# Sample behind wall clock so every device poller has had time to put bracketing
# readings on either side of the query time. Must be larger than the slowest
# device's natural poll interval (battery ~5 s).
QUERY_LAG_S = 15
LOOP_HZ = 3

# (column, float precision) — used for CSV formatting and header
log_columns = [
    ("time", 3),
    ("inverter_power", 2),
    ("meter_power", 2),
    ("meter_volts", 2),
    ("meter_amps", 3),
    ("meter_va", 2),
    ("meter_var", 2),
    ("meter_w_dmd", 2),
    ("meter_w_dmd_peak", 2),
    ("meter_pf", 3),
    ("meter_hz", 2),
    ("battery_power", 2),
    ("battery_inverter_temp", 1),
    ("load_power", 2),
]
log_file_name = "Fern"
log_handler = CSV_Handler(
    logging.getLogger(log_file_name),
    ",".join(k for k, _ in log_columns),
    os.path.join("./log", log_file_name),
    when="midnight",
    interval=1,
    backupCount=0,
    encoding=None,
    utc=True,
)


def _fmt(v, precision):
    return "" if v is None else "{:.{prec}f}".format(v, prec=precision)


_parser = argparse.ArgumentParser()
_parser.add_argument(
    "--raw",
    action="store_true",
    help='also write every sample to the "power_raw" measurement for debug comparison',
)
args = _parser.parse_args()

org = "fern"
bucket = "home"
connected = False
while not connected:
    try:
        token = os.environ.get("INFLUXDB_TOKEN")
        db_client = influxdb_client.InfluxDBClient(
            url="http://localhost:8086", token=token, org=org
        )
        write_api = db_client.write_api(write_options=SYNCHRONOUS)
        connected = True
    except:
        time.sleep(1)
        pass


class DevicePoller(threading.Thread):
    """Background thread that polls a device and keeps the last `history_size`
    readings, each tagged with a wall-clock timestamp. `value_at(t)` linearly
    interpolates stored readings to produce a dict of field values at time `t`,
    or None if `t` falls outside the history or on the far side of a gap
    larger than `max_gap_s` (which indicates the device went offline)."""

    def __init__(
        self, name, fetch_fn, history_size=100, min_interval=0.25, max_gap_s=30
    ):
        super().__init__(daemon=True, name=f"poller-{name}")
        self.name = name
        self._fetch = fetch_fn
        self._history = collections.deque(maxlen=history_size)
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._max_gap_s = max_gap_s

    def run(self):
        ok = fail = empty = 0
        slowest = 0.0
        last_heartbeat = time.monotonic()
        while True:
            started = time.monotonic()
            data = None
            failed = False
            try:
                data = self._fetch()
            except Exception as e:
                failed = True
                fail += 1
                print(f"[{self.name}] poll failed after {time.monotonic()-started:.2f}s: {e}")
            slowest = max(slowest, time.monotonic() - started)
            if data:
                ok += 1
                ts = datetime.datetime.now().timestamp()
                with self._lock:
                    if self._history and ts - self._history[-1][0] > self._max_gap_s:
                        gap = ts - self._history[-1][0]
                        print(f"[{self.name}] gap {gap:.1f}s > max {self._max_gap_s}s — clearing history")
                        self._history.clear()
                    self._history.append((ts, data))
            elif not failed:
                empty += 1
            now = time.monotonic()
            if now - last_heartbeat >= 60:
                with self._lock:
                    hist_len = len(self._history)
                    last_age = (
                        datetime.datetime.now().timestamp() - self._history[-1][0]
                    ) if self._history else None
                age_str = f"{last_age:.1f}s ago" if last_age is not None else "never"
                print(
                    f"[{self.name}] heartbeat: ok={ok} fail={fail} empty={empty} "
                    f"slowest={slowest:.2f}s hist={hist_len} last_reading={age_str}"
                )
                ok = fail = empty = 0
                slowest = 0.0
                last_heartbeat = now
            elapsed = time.monotonic() - started
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)

    def value_at(self, target):
        with self._lock:
            hist = list(self._history)
        if len(hist) < 2:
            return None
        if target < hist[0][0] or target > hist[-1][0]:
            return None
        lo, hi = 0, len(hist) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if hist[mid][0] <= target:
                lo = mid
            else:
                hi = mid
        t0, d0 = hist[lo]
        t1, d1 = hist[hi]
        if t1 == t0:
            return dict(d0)
        frac = (target - t0) / (t1 - t0)
        out = {}
        for k in d0:
            v0 = d0.get(k)
            v1 = d1.get(k)
            if v0 is not None and v1 is not None:
                out[k] = v0 + frac * (v1 - v0)
        return out


def fetch_power_mon():
    resp = requests.get("http://192.168.0.144:80/power", timeout=4)
    resp.raise_for_status()
    j = resp.json()
    # scaling factors from https://www.gavazzionline.com/pdf/EM511%20CP%20Rev1.8.pdf
    return {
        "meter_power": j["W"] / 10,
        "meter_volts": j["V"] / 10,
        "meter_amps": j["A"] / 1000,
        "meter_va": j["VA"] / 10,
        "meter_var": j["VAR"] / 10,
        "meter_w_dmd": j["W dmd"] / 10,
        "meter_w_dmd_peak": j["W dmd peak"] / 10,
        "meter_pf": j["PF"] / 1000,
        "meter_hz": j["Hz"] / 10,
    }


def fetch_inverter():
    resp = requests.get(
        "http://192.168.0.175/solar_api/v1/GetInverterRealtimeData.cgi", timeout=4
    )
    resp.raise_for_status()
    return {"inverter_power": resp.json()["Body"]["Data"]["PAC"]["Values"]["1"]}


# The Solplanet wifi dongle falls over if hit with overlapping or rapid-fire
# requests, so every call to it funnels through this gate — it serializes
# requests and enforces at least 5 s between request *starts*.
# A single Session is reused so we keep one keep-alive TCP+TLS connection open
# instead of churning the dongle's tiny connection table on every poll.
_SOLPLANET_HOST = "192.168.0.137"
_solplanet_lock = threading.Lock()
_solplanet_last_request = 0.0
_SOLPLANET_MIN_GAP_S = 5.0
_solplanet_last_wake = 0.0
_SOLPLANET_WAKE_MIN_GAP_S = 10.0
_solplanet_session = requests.Session()
_solplanet_session.verify = False


def _solplanet_iface():
    """Resolve the egress interface toward the dongle at runtime.

    The Wi-Fi interface name is not stable across this host — it has already
    changed once (wlp58s0 -> wlan0), which silently broke the arping below
    because it targeted an interface that no longer existed. Ask the kernel
    which interface routes to the dongle instead of hardcoding a name."""
    try:
        out = subprocess.run(
            ["ip", "route", "get", _SOLPLANET_HOST],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.split()
        if "dev" in out:
            return out[out.index("dev") + 1]
    except Exception as e:
        print(f"[solplanet] iface lookup failed: {e}")
    return None


def _wake_solplanet():
    """Best-effort: ARP-probe the dongle to refresh stale AP bridge FDB / ARP
    state. The dongle has been observed unreachable after long idle gaps even
    though it answers fine once any traffic gets through to wake the path.

    Root cause is the ISP-provided router doing 5 GHz ↔ 2.4 GHz band bridging
    poorly: the NUC is on 5 GHz, the Solplanet dongle is 2.4 GHz only, and the
    router's bridging FDB seems to age out / lose the dongle's MAC after idle
    periods. Any traffic in either direction re-populates the table and the
    path comes back. This is a workaround for that router — not something we
    can fix in software.

    arping (vs. ICMP ping) targets the bridge FDB at L2 directly, which is the
    layer that's actually aging out — and works even when the IP path is
    wedged. Needs sudo + an explicit interface (resolved at runtime)."""
    global _solplanet_last_wake
    since = time.monotonic() - _solplanet_last_wake
    if since < _SOLPLANET_WAKE_MIN_GAP_S:
        print(f"[solplanet] wake arping skipped (last was {since:.1f}s ago)")
        return
    _solplanet_last_wake = time.monotonic()
    iface = _solplanet_iface()
    if iface is None:
        print(f"[solplanet] wake arping skipped (no route to {_SOLPLANET_HOST})")
        return
    try:
        result = subprocess.run(
            ["sudo", "arping", "-c", "2", "-w", "3", "-I", iface, _SOLPLANET_HOST],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
        )
        print(f"[solplanet] wake arping rc={result.returncode} via {iface}")
    except Exception as e:
        print(f"[solplanet] wake arping failed: {e}")


def solplanet_get(url):
    global _solplanet_last_request, _solplanet_session
    device = url.split("device=", 1)[-1].split("&", 1)[0] if "device=" in url else "?"
    with _solplanet_lock:
        wait = _SOLPLANET_MIN_GAP_S - (time.monotonic() - _solplanet_last_request)
        if wait > 0:
            time.sleep(wait)
        _solplanet_last_request = time.monotonic()
        started = time.monotonic()
        try:
            resp = _solplanet_session.get(url, timeout=20)
            elapsed = time.monotonic() - started
            if elapsed > 3.0 or resp.status_code != 200:
                print(f"[solplanet] device={device} {resp.status_code} in {elapsed:.2f}s")
            return resp
        except requests.RequestException as e:
            elapsed = time.monotonic() - started
            print(f"[solplanet] device={device} FAILED after {elapsed:.2f}s: {e}")
            # On any transport-level failure, throw away the session so the next
            # call rebuilds the TCP+TLS connection from scratch rather than
            # retrying through a wedged socket.
            try:
                _solplanet_session.close()
            except Exception:
                pass
            _solplanet_session = requests.Session()
            _solplanet_session.verify = False
            # Wake the path before the next HTTPS attempt — an ARP probe
            # repopulates the AP's bridge FDB / dongle's ARP cache when they've
            # aged out, which is what was causing the multi-day dropouts.
            _wake_solplanet()
            raise


def fetch_battery():
    resp = solplanet_get(
        f"https://{_SOLPLANET_HOST}/getdevdata.cgi?device=2&sn=PB50005S125C0610"
    )
    resp.raise_for_status()
    j = resp.json()
    pac = j.get("pac")
    if pac is None:
        return None
    # float so the Influx field stays float-typed across interpolated values
    out = {"battery_power": float(pac)}
    tmp = j.get("tmp")
    if tmp is not None:
        out["battery_inverter_temp"] = tmp / 10.0
    return out


pollers = [
    DevicePoller("power_mon", fetch_power_mon),
    DevicePoller("inverter", fetch_inverter),
    # Solplanet wifi dongle drops out intermittently under load; poll every ~5 s
    # (well above its ~2.5 s natural fetch time) to reduce pressure on it.
    DevicePoller("battery", fetch_battery, min_interval=10, max_gap_s=60),
]
for p in pollers:
    p.start()


def fetch_soc():
    resp = solplanet_get(
        f"https://{_SOLPLANET_HOST}/getdevdata.cgi?device=4&sn=PB50005S125C0610"
    )
    resp.raise_for_status()
    soc = resp.json().get("soc")
    return None if soc is None else float(soc)


# SOC changes slowly and lives on a different endpoint; poll on its own 60 s
# cadence and write straight to Influx rather than interpolating through the
# main sample loop (which would require a QUERY_LAG_S > 60 s).
def soc_loop():
    while True:
        started = time.monotonic()
        try:
            soc = fetch_soc()
            if soc is not None:
                write_api.write(
                    bucket=bucket,
                    org="fern",
                    record=[
                        {
                            "measurement": "power",
                            "time": datetime.datetime.now(tz=datetime.timezone.utc),
                            "fields": {"battery_soc": soc},
                        },
                    ],
                )
                print(f"[soc] {soc:.0f}%")
            else:
                print("[soc] no soc field in response")
        except Exception as e:
            print(f"[soc] poll failed: {e}")
        elapsed = time.monotonic() - started
        if elapsed < 60:
            time.sleep(60 - elapsed)


threading.Thread(target=soc_loop, daemon=True, name="poller-soc").start()


def _fill_load_power(payload):
    if (
        payload.get("inverter_power") is not None
        and payload.get("meter_power") is not None
        and payload.get("battery_power") is not None
    ):
        payload["load_power"] = (
            payload["inverter_power"]
            + payload["meter_power"]
            + payload["battery_power"]
        )


def _write_raw(payload):
    if not args.raw:
        return
    raw_fields = {k: v for k, v in payload.items() if k != "time" and v is not None}
    if raw_fields:
        write_api.write(
            bucket=bucket,
            org="fern",
            record=[
                {
                    "measurement": "power_raw",
                    "time": datetime.datetime.fromtimestamp(
                        payload["time"], tz=datetime.timezone.utc
                    ),
                    "fields": raw_fields,
                },
            ],
        )


def _write_power(payload):
    log_line = ",".join(_fmt(payload.get(k), p) for k, p in log_columns)
    log_handler.logger.info(log_line)
    influx_fields = {k: v for k, v in payload.items() if k != "time" and v is not None}
    if influx_fields:
        write_api.write(
            bucket=bucket,
            org="fern",
            record=[
                {
                    "measurement": "power",
                    "time": datetime.datetime.fromtimestamp(
                        payload["time"], tz=datetime.timezone.utc
                    ),
                    "fields": influx_fields,
                },
            ],
        )


def sample_once():
    target = datetime.datetime.now().timestamp() - QUERY_LAG_S
    payload = EMPTY_ENTRY.copy()
    payload["time"] = target
    for p in pollers:
        data = p.value_at(target)
        if data:
            for k, v in data.items():
                payload[k] = v
    _fill_load_power(payload)
    _write_raw(payload)
    for f in device_filters:
        point = f.process(payload)
        if point:
            _write_power(point)


loop_period = 1.0 / LOOP_HZ
loop_iters = 0
loop_errors = 0
loop_overruns = 0
last_loop_heartbeat = time.monotonic()
print(f"[main] starting sample loop at {LOOP_HZ} Hz, QUERY_LAG_S={QUERY_LAG_S}s")
while True:
    started = time.monotonic()
    try:
        sample_once()
    except Exception as e:
        loop_errors += 1
        print(f"[main] sample_once error: {e}")
    elapsed = time.monotonic() - started
    loop_iters += 1
    if elapsed > loop_period * 1.5:
        loop_overruns += 1
        print(f"[main] loop overrun: {elapsed:.3f}s")
    now = time.monotonic()
    if now - last_loop_heartbeat >= 60:
        outages = ",".join(f.name for f in device_filters if f.in_outage) or "none"
        print(
            f"[main] heartbeat: iters={loop_iters} errors={loop_errors} "
            f"overruns={loop_overruns} outages={outages}"
        )
        loop_iters = loop_errors = loop_overruns = 0
        last_loop_heartbeat = now
    if elapsed < loop_period:
        time.sleep(loop_period - elapsed)
