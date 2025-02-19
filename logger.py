import datetime
import time
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor

import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS
import requests
import grequests
import logger
from dbfilter import DeadbandFilter

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
    "load_power": None
}


dbf_settings = {"inverter_power": 50, "meter_power": 50}
dbf_keys = [k for k in dbf_settings] + ['time']
deadbandFilter = DeadbandFilter(dbf_settings, 15000, debug=False)
last_point = {}
log_file_name = 'Fern'
log_columns = ['time'].concat(EMPTY_ENTRY.keys())
log_handler = logger.CSV_Handler(logging.getLogger(log_file_name), ','.join(log_columns), os.path.join('./log', log_file_name), when='midnight', interval=1, backupCount=0, encoding=None, utc=True)

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
    print('fetch', url)
    print(datetime.datetime.now())
    try:
        response = requests.get(url)
        return response.json()
    except:
        return None

last_read = datetime.datetime.now()

def exception_handler(request, exception):
    print("Request failed", request)

def log_to_influx():
    try:
        # parallel fetches for inverter and power monitor. Failure should return None
        rs = [grequests.get(u) for u in ["http://192.168.1.83:80/power", "http://192.168.1.50/solar_api/v1/GetInverterRealtimeData.cgi"]]
        power_mon_json, inverter_json = [r.json() for r in grequests.map(rs, exception_handler=exception_handler)]
        #with ThreadPoolExecutor(max_workers=2) as executor:
        #    power_mon_json, inverter_json = executor.map(
        #        fetch_json,
        #        [
        #            "http://192.168.1.83:80/power",
        #            "http://192.168.1.50/solar_api/v1/GetInverterRealtimeData.cgi",
        #        ],
        #    )
        payload = EMPTY_ENTRY.copy()
        payload['time'] = datetime.datetime.now().timestamp()
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
            if payload["inverter_power"] != None:
                payload["load_power"] = payload["inverter_power"] + payload["meter_power"]


        save_point = deadbandFilter.filter({k:payload[k] for k in dbf_settings.keys()})
        if save_point:
            #filter call returns previously passed in data. Now other values that were not considered by the filter are added
            k_list = [k for k in last_point.keys() if k not in dbf_keys]
            for k in k_list:
                save_point[k]=self.last_point[k]
            # write to log file
            log_line = ','.join(['{:.{prec}f}'.format(save_point[k], prec=precision) for k, precision in log_columns])
            log_handler.logger.info(log_line)
            time = save_point.pop('time')
            # write to influxdb
            write_api.write(
                bucket=bucket,
                org="fern",
                record=[
                    {
                        "measurement": "power",
                        "time": time,
                        "fields": save_point,
                    },
                ],
            )
        last_point = payload
    except Exception as e: 
        print('issue!!')
        print(e)
    finally:
        pass


while True:
    #scheduling was killing itself racing the ThreadPoolExecutor, so something more basic
    start = datetime.datetime.now()
    log_to_influx()
    end = datetime.datetime.now()
    time_delta = end - start 
    seconds_float = time_delta.seconds+time_delta.microseconds/999999
    print('sleep for ', 0.5 - seconds_float)
    if seconds_float < 0.5:
        time.sleep(0.5 - seconds_float)
