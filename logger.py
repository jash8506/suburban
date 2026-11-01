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
# device's poll interval (the battery polls every ~10 s but the flaky dongle
# still stalls for longer, so we keep a conservative margin) so its newest
# reading is behind the query time, and smaller than the fast pollers' history
# window (history_size / poll rate) so their readings still bracket the query
# time. Could be lowered toward ~15 s now the battery polls faster, trading dropout
# tolerance for less logging latency.
QUERY_LAG_S = 35
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
        self, name, fetch_fn, history_size=300, min_interval=0.25, max_gap_s=30,
        diag=None
    ):
        super().__init__(daemon=True, name=f"poller-{name}")
        self.name = name
        self._fetch = fetch_fn
        self._history = collections.deque(maxlen=history_size)
        self._lock = threading.Lock()
        self._min_interval = min_interval
        self._max_gap_s = max_gap_s
        self._diag = diag

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
            latency = time.monotonic() - started
            slowest = max(slowest, latency)
            if self._diag is not None:
                # Record every completed attempt; ok iff the fetch returned data.
                self._diag.record_read(bool(data), latency)
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


class DeviceDiag:
    """Per-device diagnostic counters, snapshotted and reset once a minute by
    diag_loop() and written to the 'diagnostics' Influx measurement (one point
    per device, tagged `device`) so a separate Grafana dashboard can show read
    health and deadband compression per device, independently of the power data.

    Every read source records its attempts here (the DevicePoller threads and
    bms_loop via record_read); sample_once records deadband drops via record_drop.
    All counters are cheap ints behind one lock, so recording is contention-free
    for the poller threads."""

    def __init__(self, name):
        self.name = name
        self._lock = threading.Lock()
        self._ok = 0
        self._fail = 0
        self._latency_sum = 0.0
        self._latency_n = 0
        self._drops = 0

    def record_read(self, ok, latency_s):
        # ok is True when the read returned usable data, False on any failure
        # (exception or a response with no usable fields). Latency is recorded
        # for every attempt including failures, so a link going slow (rising avg
        # latency) shows up before it starts timing out.
        with self._lock:
            if ok:
                self._ok += 1
            else:
                self._fail += 1
            self._latency_sum += latency_s
            self._latency_n += 1

    def record_drop(self):
        with self._lock:
            self._drops += 1

    def snapshot_reset(self):
        """Return the counts accumulated since the last call and zero them."""
        with self._lock:
            ok, fail = self._ok, self._fail
            lat_sum, lat_n, drops = self._latency_sum, self._latency_n, self._drops
            self._ok = self._fail = 0
            self._latency_sum = 0.0
            self._latency_n = 0
            self._drops = 0
        avg_latency = (lat_sum / lat_n) if lat_n else None
        return {
            "ok": ok,
            "fail": fail,
            "avg_latency": avg_latency,
            "drops": drops,
        }


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
# Proactively arping the dongle on this cadence keeps the router's bridge FDB /
# ARP cache from aging out its MAC during the idle gaps between polls — gaps that
# grew when we slowed the poll rate. It's an L2-only nudge and shares the wake
# rate limiter above, so it costs the dongle's HTTP stack nothing.
_SOLPLANET_KEEPALIVE_S = 60.0
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
    """Best-effort: ARP-probe the dongle to refresh stale ARP / AP client state.
    The dongle has been observed unreachable after long idle gaps even though it
    answers fine once any traffic gets through to wake the path.

    The NUC and the 2.4 GHz-only dongle are both on the Telstra modem's 2.4 GHz
    SSID, so this is not a cross-band bridging problem — the dongle appears to get
    parked by the AP's power-save (APSD) buffering / client-inactivity aging after
    idle, and stale ARP compounds it. Any traffic wakes the path back up.

    NOTE (2026-07): APSD was turned off and the modem's 2.4 GHz radio pinned to
    802.11b/g/n + 20 MHz, which may make this wake path (and solplanet_keepalive_
    loop) unnecessary — remove both once ~a week of logs confirms no dropouts.

    arping (vs. ICMP ping) works even when the IP path is wedged, and needs sudo
    + an explicit interface (resolved at runtime)."""
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


def solplanet_keepalive_loop():
    """Periodically wake the L2 path to the dongle so its MAC never ages out of
    the router's bridge FDB. _wake_solplanet already runs reactively after a
    failed request; this adds a steady proactive cadence so the path stays warm
    through the (now longer) idle gaps between polls and recovers faster during a
    dropout. Reuses _wake_solplanet, so its rate limiter suppresses a keepalive
    tick that lands right after a reactive wake."""
    while True:
        try:
            _wake_solplanet()
        except Exception as e:
            print(f"[solplanet] keepalive failed: {e}")
        time.sleep(_SOLPLANET_KEEPALIVE_S)


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


# Per-device diagnostic counters, flushed to Influx every minute by diag_loop().
# Keyed by a canonical device name: the meter's poller is "power_mon" but we tag
# it "meter" to match its deadband filter, and the battery is split into its two
# separate dongle reads — device=2 (power/temp, fetch_battery) and device=4 (BMS
# SOC/voltage, fetch_bms) — since the user tracks them as two devices.
diagnostics = {
    "meter": DeviceDiag("meter"),
    "inverter": DeviceDiag("inverter"),
    "battery_dev2": DeviceDiag("battery_dev2"),
    "battery_dev4": DeviceDiag("battery_dev4"),
}
# Maps each DeviceFilter's name to the diagnostics key its deadband drops feed.
# The derived "load" filter has no entry (it's not a real device, so we don't
# report its drops), and battery_dev4 has none either (BMS data is written
# straight to Influx, never deadband-filtered, so it has no drops).
_FILTER_DIAG = {
    "inverter": "inverter",
    "meter": "meter",
    "battery": "battery_dev2",
}

pollers = [
    DevicePoller("power_mon", fetch_power_mon, diag=diagnostics["meter"]),
    DevicePoller("inverter", fetch_inverter, diag=diagnostics["inverter"]),
    # The Solplanet wifi dongle delivers at most ~1 reading/10 s, so poll it every
    # 10 s — its practical ceiling. (Faster is pointless: the dongle can't produce
    # readings quicker, and the 5 s _SOLPLANET_MIN_GAP_S gate serialises requests
    # anyway.) max_gap_s=60 keeps history across a few missed polls but discards it
    # after a real dropout so we don't interpolate a straight line across a hole.
    DevicePoller("battery", fetch_battery, min_interval=10, max_gap_s=60,
                 diag=diagnostics["battery_dev2"]),
]
for p in pollers:
    p.start()

threading.Thread(
    target=solplanet_keepalive_loop, daemon=True, name="solplanet-keepalive"
).start()


def fetch_bms():
    """Battery BMS data (device=4): state-of-charge, DC pack voltage, pack
    temperature and DC battery power. All come back in the same response, so we
    read them together and spare the flaky dongle extra requests."""
    resp = solplanet_get(
        f"https://{_SOLPLANET_HOST}/getdevdata.cgi?device=4&sn=PB50005S125C0610"
    )
    resp.raise_for_status()
    j = resp.json()
    out = {}
    soc = j.get("soc")
    if soc is not None:
        out["battery_soc"] = float(soc)
    vb = j.get("vb")
    if vb is not None:
        # vb is the pack voltage in centivolts (e.g. 5490 -> 54.90 V)
        out["battery_voltage"] = vb / 100.0
    tb = j.get("tb")
    if tb is not None:
        # tb is the pack temperature in deci-degrees C (e.g. 230 -> 23.0 C)
        out["battery_pack_temp"] = tb / 10.0
    pb = j.get("pb")
    if pb is not None:
        # DC power at the battery as reported by the BMS, in watts. Sign
        # convention (which polarity is charging) is not yet confirmed for this
        # register, so don't trust it on a dashboard until verified against
        # battery_power (device=2's pac, which is negative when charging per the
        # load balance) during a known charge or discharge. Kept as a distinct
        # field from battery_power regardless.
        out["battery_dc_power"] = float(pb)
    return out


# SOC, pack voltage, temperature and DC power all come from device=4 and change
# slowly, so poll them gently on one shared 300 s cadence and write straight to
# Influx rather than interpolating through the main sample loop (which would
# require QUERY_LAG_S > 300 s). The single device=4 request keeps load off the
# flaky dongle.
def bms_loop():
    diag = diagnostics["battery_dev4"]
    while True:
        started = time.monotonic()
        try:
            fields = fetch_bms()
            diag.record_read(bool(fields), time.monotonic() - started)
            if fields:
                write_api.write(
                    bucket=bucket,
                    org="fern",
                    record=[
                        {
                            "measurement": "power",
                            "time": datetime.datetime.now(tz=datetime.timezone.utc),
                            "fields": fields,
                        },
                    ],
                )
                parts = []
                if "battery_soc" in fields:
                    parts.append(f"soc={fields['battery_soc']:.0f}%")
                if "battery_voltage" in fields:
                    parts.append(f"vb={fields['battery_voltage']:.2f}V")
                if "battery_pack_temp" in fields:
                    parts.append(f"tb={fields['battery_pack_temp']:.1f}C")
                if "battery_dc_power" in fields:
                    parts.append(f"pb={fields['battery_dc_power']:.0f}W")
                print(f"[bms] {' '.join(parts)}")
            else:
                print("[bms] no battery fields in response")
        except Exception as e:
            diag.record_read(False, time.monotonic() - started)
            print(f"[bms] poll failed: {e}")
        elapsed = time.monotonic() - started
        if elapsed < 300:
            time.sleep(300 - elapsed)


threading.Thread(target=bms_loop, daemon=True, name="poller-bms").start()


def diag_loop():
    """Once a minute, flush each device's diagnostic counters to the
    'diagnostics' Influx measurement (one point per device, tagged `device`) and
    print a summary line. Fields per device:
      reads_ok / reads_fail — read attempt outcomes this minute
      avg_latency_ms — mean latency over all completed attempts (incl. failures)
      deadband_drops — samples the deadband filter held back (compression)
    This feeds a dedicated Grafana dashboard (grafana/dashboards/diagnostics.json),
    kept separate from the power data. battery_dev4 (the BMS read) has no deadband
    drops, so that field stays 0 for it."""
    while True:
        started = time.monotonic()
        stamp = datetime.datetime.now(tz=datetime.timezone.utc)
        points = []
        for name, d in diagnostics.items():
            s = d.snapshot_reset()
            fields = {
                "reads_ok": s["ok"],
                "reads_fail": s["fail"],
                "deadband_drops": s["drops"],
            }
            if s["avg_latency"] is not None:
                fields["avg_latency_ms"] = s["avg_latency"] * 1000.0
            points.append(
                {
                    "measurement": "diagnostics",
                    "time": stamp,
                    "tags": {"device": name},
                    "fields": fields,
                }
            )
            lat = (
                f"{s['avg_latency']*1000:.0f}ms"
                if s["avg_latency"] is not None
                else "-"
            )
            print(
                f"[diag] {name}: ok={s['ok']} fail={s['fail']} "
                f"avg_latency={lat} deadband_drops={s['drops']}"
            )
        try:
            write_api.write(bucket=bucket, org="fern", record=points)
        except Exception as e:
            print(f"[diag] influx write failed: {e}")
        elapsed = time.monotonic() - started
        if elapsed < 60:
            time.sleep(60 - elapsed)


threading.Thread(target=diag_loop, daemon=True, name="diag").start()


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
        had_value = payload.get(f.watch) is not None
        point = f.process(payload)
        if point:
            _write_power(point)
        elif had_value and f.name in _FILTER_DIAG:
            # The device had a reading this sample but the deadband filter held
            # it back (it fell inside the trajectory band) — count it as dropped.
            # A missing reading (had_value False) is an outage, not a drop. The
            # derived "load" filter isn't in _FILTER_DIAG, so it's skipped.
            diagnostics[_FILTER_DIAG[f.name]].record_drop()


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
