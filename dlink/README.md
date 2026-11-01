# D-Link DSL-2750B: Wi-Fi link for the power monitor

The Arduino Opta power monitor only has Ethernet, so it hangs off an old D-Link DSL-2750B
running OpenWrt. The D-Link joins the house Wi-Fi as a client and acts as a small router
for its LAN ports: it gives the Opta an address and forwards port 80 to it.

```
main router 192.168.0.1  ~~Wi-Fi~~  D-Link 192.168.0.144  --Ethernet--  Opta 192.168.8.10  --RS485-->  EM112 meter
                                    (LAN side 192.168.8.1)
```

`logger.py` reads the meter at `http://192.168.0.144/power`, which is the D-Link's port
forward to the Opta.

## Current setup (1 Nov 2026)

| | |
|---|---|
| Firmware | OpenWrt 22.03.7, `bcm63xx/generic`. The board reports itself as `ADB P.DG A4001N`, so it was flashed with the image for that similar board. Everything used here works. |
| Wi-Fi | Client of `DN8245V-996D` (the radio is 2.4 GHz only), network `wwan`, DHCP from the main router. The D-Link's own access point is disabled. |
| Main-LAN address | `192.168.0.144`, MAC `3c:1e:04:52:ad:d5`. **Reserve this on the main router**: the logger depends on it. |
| LAN side | `192.168.8.1/24` with DHCP on. Fixed lease `opta`: `a8:61:0a:50:4e:38` → `192.168.8.10`. |
| Firewall | `wwan` is in the `wan` zone: incoming traffic is rejected and outgoing traffic is NATed. Two exceptions: the `Power-monitor` port forward (tcp 80 → `192.168.8.10:80`) and `Allow-SSH-from-laptop` (tcp 22 from `192.168.0.135` only). |
| Login | `root`, **no password yet**. Set one with `passwd`. |
| Leftovers | `relayd` and `luci-proto-relay` are installed but unused. `/root/firewall.before-bridge-fix` and `/root/config-before-router-mode.tgz` are the configs from before the changes below. |

Port 80 on `192.168.0.144` goes to the Opta, so the D-Link's own web UI (LuCI) is only
reachable from a LAN port, at `http://192.168.8.1/`.

After a power cut the Opta should pick up its address once the D-Link is back (about two
minutes). It keeps retrying DHCP about once a minute. As a backstop, the current
`modbus.ino` reboots the Opta if it has gone 5 minutes without an address.

## Checking on it

```
curl http://192.168.0.144/power          # meter readings through the port forward
ssh root@192.168.0.144                   # only from the laptop at 192.168.0.135, see the firewall rule
```

On the D-Link:

```
ifstatus wwan | grep -A3 ipv4-address    # Wi-Fi link has an address
cat /tmp/dhcp.leases                     # the Opta should hold 192.168.8.10
logread | grep -iE 'wpa|dnsmasq-dhcp'    # Wi-Fi and DHCP events
```

`ssh root@192.168.0.144 sh -s < inspect.sh` prints a read-only snapshot of the config and
state, with passwords and keys masked.

To SSH in from another machine on the main LAN, change `src_ip` on the
`Allow-SSH-from-laptop` rule, or plug into a spare LAN port on the D-Link (see Recovery).

## Rebuilding from scratch

On a freshly reset OpenWrt, reached from a LAN port at `ssh root@192.168.1.1`:

```
passwd

# Wi-Fi client to the main router, with the D-Link's own access point off
uci set wireless.radio0.disabled=0
uci set wireless.default_radio0.disabled=1
uci set wireless.wwan=wifi-iface
uci set wireless.wwan.device=radio0
uci set wireless.wwan.mode=sta
uci set wireless.wwan.network=wwan
uci set wireless.wwan.ssid='DN8245V-996D'
uci set wireless.wwan.encryption=sae
uci set wireless.wwan.key='<wifi password>'
uci set network.wwan=interface
uci set network.wwan.proto=dhcp
uci add_list firewall.@zone[1].network=wwan    # zone[1] is 'wan' on a fresh install

# LAN side: off the main LAN's 192.168.0.x, fixed address for the Opta
uci set network.lan.ipaddr=192.168.8.1
uci add dhcp host
uci set dhcp.@host[-1].name=opta
uci set dhcp.@host[-1].mac=a8:61:0a:50:4e:38
uci set dhcp.@host[-1].ip=192.168.8.10

# Port 80 on the D-Link's main-LAN address -> the Opta
uci add firewall redirect
uci set firewall.@redirect[-1].name=Power-monitor
uci set firewall.@redirect[-1].target=DNAT
uci set firewall.@redirect[-1].src=wan
uci set firewall.@redirect[-1].src_dport=80
uci set firewall.@redirect[-1].dest=lan
uci set firewall.@redirect[-1].dest_ip=192.168.8.10
uci set firewall.@redirect[-1].dest_port=80
uci set firewall.@redirect[-1].proto=tcp

uci commit && reboot
```

After the reboot the D-Link's LAN side is at `192.168.8.1`. Reset the Opta so it takes its
lease, then check `curl http://192.168.0.144/power` from the main LAN.

## Recovery

- **Can't reach it over Wi-Fi:** plug a laptop into a spare LAN port. It gets a
  `192.168.8.x` address from the D-Link. Then `ssh root@192.168.8.1`. Don't borrow the NUC's
  only Ethernet port for this: it is the logger's connection to the main router.
- **Undo the switch to router mode:** `tar xzf /root/config-before-router-mode.tgz -C / && reboot`
  on the D-Link. That brings back the relayd setup described below, which didn't work with the
  new router.
- **Unknown root password / broken config:** boot OpenWrt into failsafe mode (hold reset
  while it boots, see the device's page on openwrt.org), then `firstboot && reboot` to wipe
  the config, and follow "Rebuilding from scratch".

## History: why not a bridge

Everything worked with the previous main router. After the router was replaced,
the D-Link was set up as a relayd "pseudo-bridge", meant to give the Opta an
address from the main router directly. It never worked reliably:

- The Wi-Fi network was in two firewall zones, and the rejecting `wan` zone matched first.
  So the Opta was unreachable even though relayd was running.
- With the firewall fixed, the Opta got `192.168.0.146` through relayd and served
  `/power` for a few minutes. Then it stopped answering even ARP on the D-Link's LAN port.
  After a reset, its DHCP requests never got an answer from the new router through relayd.
  The root cause wasn't pinned down.

On 1 Nov 2026 relayd was removed in favour of the router-and-port-forward setup above, which
has worked since.

## Scripts in this folder

- `inspect.sh`: read-only snapshot of the D-Link's config and state, with secrets masked.
- `find-power-monitor.sh`: scans `192.168.0.x` for anything answering `/power`. It should find
  `192.168.0.144`.
- `relayd-setup.sh`, `nuc-wired.sh`: from the relayd attempt. `relayd-setup.sh` builds the
  bridge setup that was abandoned. `nuc-wired.sh` reaches the D-Link from a cabled machine
  without disturbing its main connection, but it refuses to run on the NUC because the NUC's
  only Ethernet port is its main connection.
