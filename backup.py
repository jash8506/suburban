import influxdb_client
import pandas as pd
import os
import datetime
import time
# from influxdb_client import InfluxDBClient, Point, WritePrecision
token = os.environ.get("INFLUXDB_TOKEN")
org = "fern"
db_client = influxdb_client.InfluxDBClient(
    url="http://localhost:8086", token=token, org=org
)

query_api = db_client.query_api()

start = pd.Timestamp('2024-12-12', tz="UTC")
# move forward 11 hours to match Sydney daylight saving time
start = start + pd.Timedelta('11h')

while start < datetime.datetime.now(tz=datetime.timezone.utc):
    end = start + pd.Timedelta('1d')
    query=f'from(bucket:"home")\
|> range(start: {int(start.timestamp())}, stop: {int(end.timestamp())})\
|> filter(fn:(r) => r._measurement == "power")\
|> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")'
    df = query_api.query_data_frame(query)
    df.to_parquet(f'log/power_{start.strftime("%Y-%m-%d")}.parquet')
    start = start + pd.Timedelta('1d')
    time.sleep(10)
