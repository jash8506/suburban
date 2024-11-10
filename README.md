# suburban
Home energy monitor for the suburbs

I live in a house in suburban Sydney. It has an electrical load setup that is quite typical. The main ones are: 
- 10kW (10kW inverter, 13kW panels) solar system.
- Pool with a chlorinator (needs to be run a few hours a day).
- plugin hybrid car (18kWh battery)
- gas hot water (for now)
- Reverse cycle AC

Rather than pay Fronius a monthly fee to monitor my usage, I'm doing it myself. The plan is to analyse usage over summer and winter and then start controlling some loads listed above with the Arduino.
[I did something similar years ago when I was in an apartment](https://www.hackster.io/user0813287607/home-energy-monitor-f49f9c). Unfortunately the hardware didn't last. Might have been the DB load running off an SD card. Hopefully the 8yr old NUC does a better job.

# Hardware
- Arduino Opta RS485
  - 2 Relays connected to 20A SSR to control loads
- Carlo Gavazzi EM112 Series Energy Monitor installed in main switchboard
- Fronius Gen24 Inverter
- Old Intel NUC (i3-7100U)

# Software
- Arduino IDE (not the plc one)
- Linux Mint
  - Python
  - InfluxDB
  - Grafana
  - Cloudflare reverse proxy to connect from anywhere.
## notes
example_grafana_dashboard.json shows how to set up a dashboard with 2 y axes and aggregate function on query.
