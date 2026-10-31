# D-Link DSL-2750B Wi-Fi bridge for the power monitor

The Arduino Opta power monitor only has Ethernet, so it hangs off an old D-Link DSL-2750B
running OpenWrt, which joins the house Wi-Fi as a client and bridges its LAN ports onto
the main network:

```
main router 192.168.0.1  ~~Wi-Fi~~  D-Link (OpenWrt, relayd)  --Ethernet--  Arduino Opta  --RS485-->  EM112 meter
```

OpenWrt can't do a plain layer-2 bridge over a Wi-Fi client link, so it uses **relayd**
(a "pseudo-bridge"): the Arduino gets its address from the main router via DHCP, but on the
main LAN it shows up with the D-Link's MAC address (`3c:1e:04:52:ad:d5`).

## What we saw before setting this up (31 Oct 2026)

- The D-Link was on the main Wi-Fi at `192.168.0.144` (also answering ARP for `.250`), but
  refused every TCP port. That is OpenWrt's default firewall on its upstream (`wan`) zone,
  which means the Wi-Fi client link worked but nothing was bridged.
- `logger.py` reads the meter at `192.168.0.144`, which the D-Link itself now holds, and
  the Arduino wasn't reachable anywhere on `192.168.0.x`.

## Setup, from the NUC

The NUC is the logger host and talks to everything over Wi-Fi. `nuc-wired.sh` adds a wired
profile that never takes the default route and keeps Wi-Fi preferred, so the logger keeps
running while the NUC is cabled to the D-Link.

1. **Get these scripts onto the NUC**: `git pull` in the repo there.

2. **Create the wired profile, then cable up.** Leave the Arduino plugged into the D-Link
   and use another of its four LAN ports for the NUC (not the DSL port).

   ```
   cd dlink
   sudo ./nuc-wired.sh up        # creates the 'dlink-setup' profile
   # plug the NUC into a D-Link LAN port
   ./nuc-wired.sh find           # lists the D-Link's addresses and what's listening
   ./nuc-wired.sh status         # default route must still be the Wi-Fi interface
   ```

   `find` prints one or more `export DLINK='root@...'` lines. Use the one whose `ssh` line
   mentions `dropbear` (OpenWrt's SSH server), preferably the `fe80::` address: it doesn't
   change when the D-Link's LAN IP does, so the session survives the reconfiguration.
   If the web line says `title: DSL-2750B` or similar instead of LuCI, it's still stock
   D-Link firmware, and OpenWrt has to be flashed first.

3. **Log in.** A fresh OpenWrt has no root password. If it asks for one and you don't know
   it, see Recovery below.

   ```
   export DLINK='root@fe80::xxxx%eno1'   # from step 2
   ssh "$DLINK"                          # check you can get in, then exit
   ```

4. **Back up and inspect** (both read-only). The backup includes the Wi-Fi password in
   plain text, so it goes in your home directory, not the repo.

   ```
   ssh "$DLINK" 'sysupgrade -b /tmp/backup.tar.gz && cat /tmp/backup.tar.gz' > ~/dlink-backup-$(date +%F).tar.gz
   ssh "$DLINK" sh -s < inspect.sh | tee ~/dlink-inspect.txt
   ```

   `dlink-inspect.txt` masks Wi-Fi keys. Paste it to Claude if you want the config checked
   before changing anything.

5. **Configure the bridge.**

   ```
   ssh "$DLINK" 'cat > /tmp/relayd-setup.sh' < relayd-setup.sh
   ssh -t "$DLINK" WWAN_IP=192.168.0.250 sh /tmp/relayd-setup.sh
   ```

   The script:
   - keeps the existing Wi-Fi client settings or asks for the **2.4 GHz** SSID and password
     (the DSL-2750B radio is 2.4 GHz only)
   - turns off the D-Link's own access point
   - installs `relayd` (and `luci-proto-relay` if LuCI is installed)
   - moves the D-Link's LAN side to `192.168.2.1` and turns off its DHCP server there
   - creates the relay between `lan` and the Wi-Fi client network
   - puts both in the `lan` firewall zone
   - saves the old config to `/root/config-before-relayd.tgz` and reboots

   `WWAN_IP` gives the D-Link a fixed address on the main LAN, so it stops taking `.144`.
   Pick one outside the main router's DHCP range; `.250` is already associated with the
   D-Link. Leave `WWAN_IP` off to keep using DHCP. `LAN_IP` and `COUNTRY` (default `AU`)
   can be overridden the same way.

6. **Reset the Arduino** once the D-Link is back up (about two minutes). Firmware older
   than this commit only asks for an address once, at boot. Then find it:

   ```
   ./find-power-monitor.sh               # scans 192.168.0.x for /power
   ```

   Point `fetch_power_mon()` in `logger.py` at the address it finds, or reserve that address
   for the Arduino in the main router's DHCP settings. The router sees the Arduino's own MAC
   inside the relayed DHCP request, so a reservation should work. If the router won't honour
   it, give the Opta a static IP in `modbus.ino` instead.

7. **Clean up.** Unplug the NUC's cable first, so NetworkManager can't fall back to a
   default wired profile that takes the default route. Then:

   ```
   sudo ./nuc-wired.sh down
   ```

   Also set a root password on the D-Link: `ssh -t root@192.168.0.250 passwd`.

## Checking on it later

From the main LAN, at the D-Link's `WWAN_IP`:

```
ssh root@192.168.0.250
ifstatus wwan | grep -A3 ipv4-address   # Wi-Fi client link has an address
pgrep -a relayd                          # relay running
logread | grep -iE 'relayd|wpa'          # recent relay / Wi-Fi events
```

LuCI is at `http://192.168.0.250/`. If the Arduino disappears after a power cut, the
firmware retries DHCP every 30 s, so it should come back by itself within a minute or two of
the D-Link.

## Recovery

- **Undo the bridge setup:** `tar xzf /root/config-before-relayd.tgz -C / && reboot` on the
  D-Link, or restore the backup from step 4 with `sysupgrade -r`.
- **Lost access over Wi-Fi:** cable the NUC to a LAN port and use `./nuc-wired.sh find`.
  IPv6 link-local SSH works regardless of the D-Link's IPv4 settings.
- **Unknown root password / broken config:** boot OpenWrt into failsafe mode (hold reset
  while it boots, see the device's page on openwrt.org), then `firstboot && reboot` to wipe
  the config and start again at step 2.
