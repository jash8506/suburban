import os

import matplotlib.pyplot as plt
import pandas as pd

from Battery import Battery

df = pd.DataFrame()
for f in os.listdir("log"):
    day_df = pd.read_parquet(f"./log/{f}")
    # append to df
    df = pd.concat([df, day_df])
# sort by _time
df.sort_values("_time", inplace=True)
df["_time_delta"] = df["_time"] - df["_time"].shift(1)

# day_df = day_df.set_index("_time")
print(df[df["_time_delta"].dt.total_seconds() > 3600])
# df['load_power'] =
df["load_kwh"] = (
    ((df["load_power"] + df["load_power"].shift(1)) / 2)
    * (df["_time_delta"].dt.total_seconds())
    / (3600 * 1000)
)
df["solar_kwh"] = (
    ((df["inverter_power"] + df["inverter_power"].shift(1)) / 2)
    * (df["_time_delta"].dt.total_seconds())
    / (3600 * 1000)
)
df["meter_kwh"] = (
    ((df["meter_power"] + df["meter_power"].shift(1)) / 2)
    * (df["_time_delta"].dt.total_seconds())
    / (3600 * 1000)
)

# summarise solar production, total consumption, exports and imports
solar_production = df["solar_kwh"].sum()
total_consumption = df["load_kwh"].sum()
total_imports = df["meter_kwh"][df["meter_kwh"] > 0].sum()
total_exports = df["meter_kwh"][df["meter_kwh"] < 0].sum()

print(solar_production)
print(total_consumption)
print(total_imports)
print(total_exports)

battery = Battery(
    # capacity in J
    15000 * 3600,
    4000,
    5000,
    0.9,
)
total_charge = 0
total_discharge = 0
total_empty = 0
df["delta_s"] = df["_time_delta"].dt.total_seconds()

soc = []
for ix, r in df.iterrows():
    try:
        if (
            r["delta_s"] > 60
            or pd.isnull(r["_time_delta"])
            or pd.isnull(r["meter_power"])
        ):
            pass
        elif r["meter_power"] > 0:
            desired_discharge = r["meter_kwh"]
            battery_discharge = battery.discharge(r["meter_power"], r["delta_s"])
            if battery_discharge == 0:
                total_empty += desired_discharge
            total_discharge += battery_discharge
        elif r["meter_power"] < 0:
            desired_charge = -r["meter_kwh"]
            battery_charge = battery.charge(-r["meter_power"], r["delta_s"])
            total_charge += battery_charge
        soc.append(battery.soc)
    except Exception as e:
        print(e)
        soc.append(battery.soc)
        pass

print(total_charge / 3600000)
print(total_discharge / 3600000)
print(total_empty)
df["soc"] = soc
# df = df.set_index("_time")
# df = df.sort_index()  # add column with time delta
df.set_index("_time").sort_index()["inverter_power"].plot(secondary_y=True)
df.set_index("_time").sort_index()["meter_power"].plot(secondary_y=True)
df.set_index("_time").sort_index()["soc"].plot()
plt.show()
