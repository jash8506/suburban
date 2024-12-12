import influxdb_client
import pandas as pd
import os

org = "fern"
bucket = "home"
connected = False
token = os.environ.get("INFLUXDB_TOKEN")
db_client = influxdb_client.InfluxDBClient(
    url="http://localhost:8086", token=token, org=org
)

start = pd.Timestamp('2024-11-13', tz="UTC")
# move forward 11 hours to match Sydney daylight saving time
start = start + pd.Timedelta('11h')
end = start - pd.Timedelta('1d')
query_api = db_client.query_api()
query = f'from(bucket:"home")\
|> range(start:{start.isoformat()}, stop:{end.isoformat()}\
|> filter(fn:(r) => r._measurement == "power")'
df = db_client.query_api().query_data_frame(org=org, query=query)
