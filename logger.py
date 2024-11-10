import datetime
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor

import influxdb_client
import requests

# from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

token = os.environ.get("INFLUXDB_TOKEN")
org = "fern"
db_client = influxdb_client.InfluxDBClient(
    url="http://localhost:8086", token=token, org=org
)
bucket = "home"
write_api = db_client.write_api(write_options=SYNCHRONOUS)


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
}


def get_dummy_entry():
    return {
        "inverter_power": random.random() * 10000,
        "meter_power": random.random() * 10000,
        "meter_volts": random.random() * 10000,
        "meter_amps": random.random() * 10000,
        "meter_va": random.random() * 10000,
        "meter_var": random.random() * 10000,
        "meter_w_dmd": random.random() * 10000,
        "meter_w_dmd_peak": random.random() * 10000,
        "meter_pf": random.random() * 10000,
        "meter_hz": random.random() * 10000,
    }


def fetch_json(url):
    try:
        response = requests.get(url)
        return response.json()
    except:
        return None


def log_to_influx(db_client):
    # schedule the next attempt
    threading.Timer(0.5, log_to_influx, args=[db_client]).start()
    try:
        # parallel fetches for inverter and power monitor. Failure should return None
        with ThreadPoolExecutor(max_workers=2) as executor:
            power_mon_json, inverter_json = executor.map(
                fetch_json,
                [
                    "http://192.168.1.83:80/power",
                    "http://192.168.1.50/solar_api/v1/GetInverterRealtimeData.cgi",
                ],
            )
        payload = EMPTY_ENTRY.copy()
        if inverter_json:
            payload["inverter_power"] = inverter_json["Body"]["Data"]["PAC"]["Values"][
                "1"
            ]
        if power_mon_json:
            # scaling factors from https://www.gavazzionline.com/pdf/EM511%20CP%20Rev1.8.pdf
            payload["meter_power"] = power_mon_json["W"] / 10
            payload["meter_volts"] = power_mon_json["V"] / 10
            payload["meter_amps"] = power_mon_json["A"] / 1000
            payload["meter_va"] = power_mon_json["VA"] / 10
            payload["meter_var"] = power_mon_json["VAR"] / 10
            payload["meter_w_dmd"] = power_mon_json["W dmd"] / 10
            payload["meter_w_dmd_peak"] = power_mon_json["W dmd peak"] / 10
            payload["meter_pf"] = power_mon_json["PF"] / 1000
            payload["meter_hz"] = power_mon_json["Hz"] / 10

        write_api.write(
            bucket=bucket,
            org="fern",
            record=[
                {
                    "measurement": "power",
                    "time": datetime.datetime.utcnow().isoformat()[:-7] + "Z",
                    "fields": payload,
                },
            ],
        )
    except ValueError as e:
        print(e)
    finally:
        pass


log_to_influx(db_client)
