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
  I added a Battery! 50kWh for AU$3949. Thank you Federal Gov!
- Solplanet ASW5000H-S2 Hybrid inverter (no solar connected)
- 5 x Solplanet ASW5120-LB-G3

# Software

- Arduino IDE (not the plc one)
- Linux Mint
  - Python
  - InfluxDB
  - Grafana
  - Cloudflare reverse proxy to connect from anywhere.

# notes

1. Create `run.sh` file. Contents:

```
export INFLUXDB_TOKEN=<influxdb token>
python logger.py
```

2. Make it executable:

```
chmod +x run.sh
```

3. Start on boot with a systemd service (see below).

### grafana dashboards

example_grafana_dashboard.json shows how to set up a dashboard with 2 y axes and aggregate function on query.

### run logger as a systemd service

`@reboot` in crontab isn't reliable (no restart on crash, fires before the network is up). Use systemd instead. `suburban-logger.service` lives in the repo root; install it with:

```
sudo cp suburban-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suburban-logger.service
```

Useful commands:

```
systemctl status suburban-logger       # check it's running
journalctl -u suburban-logger -f       # tail logs
sudo systemctl restart suburban-logger # restart after code changes
```

### daily log backup to GitHub

`backup.sh` stages everything under `log/`, commits, and pushes so the parquet/csv files are mirrored to the remote. `suburban-backup.timer` fires it once a day.

1. Make sure `git push` works non-interactively for the `john` user on the NUC. Either switch the remote to SSH (`git remote set-url origin git@github.com:jash8506/suburban.git` with a keypair in `~/.ssh`) or configure a credential helper storing a personal access token (`git config --global credential.helper store` then push once by hand).

2. Install the unit files (they live in the repo root):

```
sudo cp suburban-backup.service suburban-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suburban-backup.timer
```

Enable the timer, not the service — the timer triggers it. `Persistent=true` in the timer catches up a missed backup after boot; `RandomizedDelaySec` jitters the fire time off midnight.

Useful commands:

```
systemctl list-timers suburban-backup*        # next/last run
systemctl status suburban-backup.service      # last run result
journalctl -u suburban-backup.service         # output of past runs
sudo systemctl start suburban-backup.service  # run now, out of schedule
```

# Misc

For some reason, I needed to run this to allow the NUC to see the battery interface.
`sudo ip neigh replace 192.168.0.137 lladdr 94:51:dc:20:87:04 dev wlp58s0`
OR also fixed by forcing my NUC to use the 2.4GHz band.
