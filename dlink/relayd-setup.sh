#!/bin/sh
# Turn the D-Link (OpenWrt) into a Wi-Fi client bridge using relayd, so wired devices
# on its LAN ports - the Arduino power monitor - get addresses from the main router
# and are reachable on the main LAN. Follows
# https://openwrt.org/docs/guide-user/network/wifi/relayd
#
# Nothing takes effect until the reboot at the end (except bringing the Wi-Fi client
# link up early if relayd has to be downloaded and the D-Link isn't online yet).
#
# usage (from the NUC, see README.md):
#   ssh "$DLINK" 'cat > /tmp/relayd-setup.sh' < relayd-setup.sh
#   ssh -t "$DLINK" sh /tmp/relayd-setup.sh
#
# Optional env vars (e.g. ssh -t "$DLINK" WWAN_IP=192.168.0.250 sh /tmp/relayd-setup.sh):
#   WWAN_IP  fixed main-LAN address for the D-Link (default: DHCP from the main router)
#   WWAN_GW  main router address when WWAN_IP is set (default: <subnet>.1)
#   LAN_IP   the D-Link's own LAN-side address, outside the main LAN's subnet (default 192.168.2.1)
#   COUNTRY  Wi-Fi regulatory domain (default AU)
set -euf

LAN_IP=${LAN_IP:-192.168.2.1}
WWAN_IP=${WWAN_IP:-}
COUNTRY=${COUNTRY:-AU}

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
ask() { printf '%s ' "$1"; read -r REPLY; }

# Section names from `uci show`, e.g. sections wireless "mode='sta'" -> "@wifi-iface[1]".
sections() { uci -q show "$1" | sed -n "s/^$1\.\([^.=]*\)\.$2\$/\1/p"; }

wwan_addr() { ifstatus "$WWAN" 2>/dev/null | jsonfilter -e "@['ipv4-address'][0].address" 2>/dev/null || true; }

pkg_install() {
	if command -v apk >/dev/null 2>&1; then
		apk update >/dev/null && apk add "$@"
	else
		opkg update >/dev/null && opkg install "$@"
	fi
}

# Rewrite a firewall zone's network list without $2 (works whether stored as a list or one option).
zone_drop() {
	nets=$(uci -q get "firewall.$1.network" || true)
	uci -q delete "firewall.$1.network" || true
	for n in $nets; do [ "$n" = "$2" ] || uci add_list "firewall.$1.network=$n"; done
}

# --- preflight ---------------------------------------------------------------
[ -f /etc/openwrt_release ] || die "this doesn't look like OpenWrt"
. /etc/openwrt_release
say "OpenWrt ${DISTRIB_RELEASE:-?} on $(cat /tmp/sysinfo/model 2>/dev/null || echo 'unknown board')"
[ -t 0 ] || die "needs a terminal for its prompts - run it with: ssh -t"
[ -z "$(uci changes)" ] || die "there are uncommitted uci changes already (uci changes) - commit or revert them first"

# Keep the first backup on re-runs, so the undo always goes back to the pre-relayd config.
if [ -f /root/config-before-relayd.tgz ]; then
	echo "Keeping existing backup /root/config-before-relayd.tgz"
else
	tar czf /root/config-before-relayd.tgz /etc/config 2>/dev/null || die "couldn't back up /etc/config"
	echo "Saved current config to /root/config-before-relayd.tgz"
fi

# Moving the Wi-Fi side into the lan firewall zone opens SSH and LuCI to the whole main LAN.
if [ -z "$(awk -F: '$1 == "root" { print $2 }' /etc/shadow)" ]; then
	say "root has no password, and SSH/LuCI are about to be reachable from the whole main LAN - set one now"
	passwd root || die "root password not set"
fi

# Discard half-staged changes if we stop before the final commit.
COMMITTED=0
cleanup() {
	stty echo 2>/dev/null || true
	[ "$COMMITTED" = 1 ] || for c in wireless network dhcp firewall; do uci -q revert "$c" || true; done
}
trap cleanup EXIT

# --- 1. Wi-Fi client (station) link to the main router ------------------------
RADIO=$(uci -q show wireless | sed -n "s/^wireless\.\([^.=]*\)=wifi-device\$/\1/p" | head -n 1)
[ -n "$RADIO" ] || die "no Wi-Fi radio in /etc/config/wireless"

STA=$(sections wireless "mode='sta'" | head -n 1)
CHANGE_WIFI=1
if [ -n "$STA" ]; then
	WWAN=$(uci -q get "wireless.$STA.network" | awk '{ print $1 }')
	WWAN=${WWAN:-wwan}
	uci set "wireless.$STA.network=$WWAN"
	ask "Found a Wi-Fi client config for SSID '$(uci -q get "wireless.$STA.ssid")' (network '$WWAN'). Keep it? [Y/n]"
	case $REPLY in [nN]*) ;; *) CHANGE_WIFI=0 ;; esac
else
	STA=wwan WWAN=wwan
	uci set wireless.wwan=wifi-iface
	uci set "wireless.wwan.device=$RADIO"
	uci set wireless.wwan.mode=sta
	uci set wireless.wwan.network=wwan
fi

if [ "$CHANGE_WIFI" = 1 ]; then
	ask "Main router's 2.4 GHz SSID (this radio is 2.4 GHz only):"
	SSID=$REPLY
	[ -n "$SSID" ] || die "no SSID given"
	printf 'Wi-Fi password: '
	stty -echo; read -r KEY; stty echo; echo
	ask "Encryption [psk2]:"
	uci set "wireless.$STA.ssid=$SSID"
	uci set "wireless.$STA.encryption=${REPLY:-psk2}"
	uci set "wireless.$STA.key=$KEY"
fi
uci -q delete "wireless.$STA.disabled" || true
uci set "wireless.$RADIO.disabled=0"
uci set "wireless.$RADIO.country=$COUNTRY"

# The bridge only needs the client link; the D-Link's own access point would share the radio.
for s in $(sections wireless "mode='ap'"); do
	if [ "$(uci -q get "wireless.$s.device")" = "$RADIO" ] && [ "$(uci -q get "wireless.$s.disabled")" != 1 ]; then
		echo "Disabling the D-Link's own access point '$(uci -q get "wireless.$s.ssid")'"
		uci set "wireless.$s.disabled=1"
	fi
done

uci -q get "network.$WWAN" >/dev/null || uci set "network.$WWAN=interface"
if [ -n "$WWAN_IP" ]; then
	WWAN_GW=${WWAN_GW:-${WWAN_IP%.*}.1}
	uci set "network.$WWAN.proto=static"
	uci set "network.$WWAN.ipaddr=$WWAN_IP"
	uci set "network.$WWAN.netmask=255.255.255.0"
	uci set "network.$WWAN.gateway=$WWAN_GW"
	uci -q delete "network.$WWAN.dns" || true
	uci add_list "network.$WWAN.dns=$WWAN_GW"
else
	uci set "network.$WWAN.proto=dhcp"
	for o in ipaddr netmask gateway dns; do uci -q delete "network.$WWAN.$o" || true; done
fi

MAIN_NET=${WWAN_IP%.*}
[ -n "$MAIN_NET" ] || { a=$(wwan_addr); MAIN_NET=${a%.*}; }
if [ -n "$MAIN_NET" ] && [ "${LAN_IP%.*}" = "$MAIN_NET" ]; then
	die "LAN_IP $LAN_IP is inside the main LAN ($MAIN_NET.x) - pick another, e.g. LAN_IP=192.168.2.1"
fi

# --- 2. relayd package ---------------------------------------------------------
if [ ! -x /usr/sbin/relayd ]; then
	FREE=$(df -k /overlay 2>/dev/null | awk 'NR == 2 { print $4 }')
	[ "${FREE:-0}" -ge 100 ] || die "only ${FREE:-?} KB free on /overlay"
	say "Installing relayd"
	if ! pkg_install relayd; then
		[ "$CHANGE_WIFI" = 1 ] || [ -z "$(wwan_addr)" ] ||
			die "couldn't download relayd although '$WWAN' is up - check DNS/internet (ping downloads.openwrt.org)"
		say "Not online yet - bringing up the Wi-Fi client link first"
		uci commit wireless
		uci commit network
		/etc/init.d/network reload
		i=0
		while [ -z "$(wwan_addr)" ] && [ $i -lt 45 ]; do sleep 2; i=$((i + 1)); done
		[ -n "$(wwan_addr)" ] || die "'$WWAN' didn't get an address - check the SSID/password (logread | grep -i wpa).
The Wi-Fi client change is saved; undo with: tar xzf /root/config-before-relayd.tgz -C / && reboot"
		echo "Wi-Fi client link up: $(wwan_addr)"
		pkg_install relayd || die "relayd install failed"
	fi
	[ ! -d /www/luci-static ] || pkg_install luci-proto-relay >/dev/null 2>&1 || true
fi
[ -x /usr/sbin/relayd ] || die "relayd isn't installed"

# --- 3. LAN side, relay and firewall ---------------------------------------------
say "Staging relayd bridge: LAN side becomes $LAN_IP with its DHCP server off"
uci set network.lan.proto=static
uci set "network.lan.ipaddr=$LAN_IP"
uci set network.lan.netmask=255.255.255.0
for o in gateway dns; do uci -q delete "network.lan.$o" || true; done

uci -q get dhcp.lan >/dev/null || { uci set dhcp.lan=dhcp; uci set dhcp.lan.interface=lan; }
uci set dhcp.lan.ignore=1
uci set dhcp.lan.dhcpv6=disabled
uci set dhcp.lan.ra=disabled

uci -q delete network.repeater_bridge || true
uci set network.repeater_bridge=interface
uci set network.repeater_bridge.proto=relay
uci add_list network.repeater_bridge.network=lan
uci add_list "network.repeater_bridge.network=$WWAN"

LANZ=$(sections firewall "name='lan'" | head -n 1)
WANZ=$(sections firewall "name='wan'" | head -n 1)
[ -n "$LANZ" ] || die "no 'lan' firewall zone"
for n in "$WWAN" repeater_bridge; do
	[ -z "$WANZ" ] || zone_drop "$WANZ" "$n"
	zone_drop "$LANZ" "$n"
	uci add_list "firewall.$LANZ.network=$n"
done

uci commit
COMMITTED=1

# --- done ----------------------------------------------------------------------
if [ -n "$WWAN_IP" ]; then MAIN_ADDR=$WWAN_IP; else MAIN_ADDR="$(wwan_addr) (DHCP, may change)"; fi
say "Config saved. After the reboot:"
echo "  D-Link on the main LAN:  $MAIN_ADDR  - SSH and LuCI reachable from the main LAN"
echo "  D-Link LAN side:         $LAN_IP (no DHCP there; wired devices get addresses from the main router)"
echo "  Undo everything:         tar xzf /root/config-before-relayd.tgz -C / && reboot"
ask "Reboot now to apply? [Y/n]"
case $REPLY in
[nN]*) echo "Not rebooting - run 'reboot' when ready." ;;
*) echo "Rebooting; this SSH session will drop."; reboot ;;
esac
