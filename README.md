# suburban
Home energy monitor for the suburbs

# Hardware
- Arduino Opta RS485
  - 2 Relays connected to 20A SSR to control loads
- Carlo Gavazzi EM112 Series Energy Monitor installed in main switchboard
- Fronius Gen24 Inverter
- Intel NUC

# Software
- Arduino IDE (not the plc one)
- Linux Mint with
  - Python
  - InfluxDB
  - Grafana
  - Cloudflare reverse proxy
