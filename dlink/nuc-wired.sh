#!/usr/bin/env bash
# Temporary wired link from the NUC to a LAN port on the D-Link, used to reach the
# D-Link's OpenWrt admin (SSH/LuCI) without disturbing the NUC's Wi-Fi, which the
# logger depends on: the wired profile never takes the default route, its routes
# have a high metric so Wi-Fi stays preferred, and DNS from it is ignored.
set -euo pipefail

CON=dlink-setup

usage() {
	cat <<EOF
usage: $0 up [iface]   create the wired profile (needs sudo) and connect if the cable is in
       $0 find         show the D-Link's addresses and what is listening on them
       $0 status       show the wired link and confirm the default route is still Wi-Fi
       $0 down         remove the profile (needs sudo) - unplug the cable first
EOF
	exit 1
}

die() { echo "ERROR: $*" >&2; exit 1; }

need_root() { [ "$EUID" -eq 0 ] || exec sudo "$0" "$@"; }

profile_iface() {
	nmcli -g connection.interface-name connection show "$CON" 2>/dev/null | grep . ||
		die "no '$CON' profile - run: sudo $0 up"
}

carrier() { [ "$(cat "/sys/class/net/$1/carrier" 2>/dev/null)" = 1 ]; }

wait_for_ipv4() {
	for _ in $(seq 1 25); do
		ip -4 -o addr show dev "$1" | grep -q inet && return 0
		sleep 1
	done
	return 1
}

cmd_up() {
	local ifc=${1:-$(nmcli -t -f DEVICE,TYPE device | awk -F: '$2 == "ethernet" { print $1; exit }')}
	[ -n "$ifc" ] || die "no ethernet device found (nmcli device)"
	if ip route show default | grep -q " dev $ifc "; then
		die "$ifc carries the NUC's default route - is it already wired to the main router? Not touching it."
	fi

	nmcli connection delete "$CON" >/dev/null 2>&1 || true
	nmcli connection add type ethernet ifname "$ifc" con-name "$CON" \
		connection.autoconnect yes connection.autoconnect-priority 100 \
		ipv4.method auto ipv4.may-fail yes ipv4.dhcp-timeout 20 \
		ipv4.never-default yes ipv4.ignore-auto-dns yes ipv4.route-metric 2000 \
		ipv6.method link-local >/dev/null
	echo "Created NetworkManager profile '$CON' on $ifc."

	if carrier "$ifc"; then
		nmcli -w 30 connection up "$CON" >/dev/null
		echo "Connected. Waiting for DHCP (fine if none - IPv6 link-local is enough)..."
		wait_for_ipv4 "$ifc" || true
		sudo -u "${SUDO_USER:-root}" "$0" find
	else
		echo "No cable detected on $ifc. Plug it into a LAN port on the D-Link, then run: $0 find"
	fi
}

# Print the SSH banner on port 22 (OpenWrt runs dropbear).
ssh_banner() {
	python3 -I - "$1" <<'EOF'
import socket, sys
try:
    family, kind, _, _, addr = socket.getaddrinfo(sys.argv[1], 22, type=socket.SOCK_STREAM)[0]
    s = socket.socket(family, kind)
    s.settimeout(3)
    s.connect(addr)
    print(s.recv(80).decode(errors="replace").strip() or "open")
except Exception as e:
    print(f"closed ({type(e).__name__})")
EOF
}

# Name what's serving the web page on port 80: OpenWrt's LuCI or the stock D-Link UI.
http_kind() {
	local body
	body=$(curl -g -s -m 4 -L "$1" 2>/dev/null) || { echo "no response"; return; }
	if grep -qi 'luci' <<<"$body"; then
		echo "OpenWrt LuCI"
	else
		grep -io '<title>[^<]*' <<<"$body" | head -1 | sed 's/<title>/title: /I' || echo "unknown page"
	fi
}

cmd_find() {
	local ifc h
	ifc=$(profile_iface)
	carrier "$ifc" || die "no cable/link on $ifc"
	nmcli -t -f GENERAL.STATE device show "$ifc" | grep -q ':100' ||
		{ echo "Waiting for $ifc to connect..."; nmcli -w 30 connection up "$CON" >/dev/null 2>&1 || true; }
	wait_for_ipv4 "$ifc" || true

	local hosts=()
	echo "== IPv4 on $ifc"
	ip -4 -o addr show dev "$ifc" | awk '{ print "   this NUC: " $4 }' | grep . ||
		echo "   none (the D-Link isn't handing out DHCP on its LAN)"
	for h in $(nmcli -g DHCP4.OPTION device show "$ifc" 2>/dev/null | tr '|' '\n' |
		awk -F' = ' '$1 ~ /routers|dhcp_server_identifier/ { print $2 }' | sort -u); do
		hosts+=("$h")
	done

	echo "== IPv6 link-local neighbours on $ifc"
	local mine
	mine=$(ip -6 -o addr show dev "$ifc" scope link | awk '{ split($4, a, "/"); print a[1] }')
	for h in $(ping -6 -c 2 -W 1 "ff02::1%$ifc" 2>/dev/null | grep -o 'from fe80[^ ]*' |
		sed -e 's/^from //' -e 's/:$//' -e 's/%.*//' | sort -u); do
		[ "$h" = "$mine" ] || { echo "   $h"; hosts+=("$h%$ifc"); }
	done
	[ ${#hosts[@]} -gt 0 ] ||
		die "nothing answered on $ifc - check the cable is in a LAN port (not the DSL port) and the D-Link is powered"

	echo
	for h in "${hosts[@]}"; do
		local url="http://$h/"
		[[ $h != *:* ]] || url="http://[${h/\%/%25}]/"
		echo "-- $h"
		echo "   ssh:  $(ssh_banner "$h")"
		echo "   web:  $(http_kind "$url")"
	done

	echo
	echo "SSH in using the address whose ssh line mentions dropbear (OpenWrt), e.g.:"
	for h in "${hosts[@]}"; do echo "   export DLINK='root@$h'"; done
	echo "Prefer an fe80:: address: it keeps working while the D-Link's LAN IP changes."
}

cmd_status() {
	local ifc
	ifc=$(profile_iface)
	nmcli -f GENERAL.STATE,IP4.ADDRESS,IP6.ADDRESS device show "$ifc"
	echo "== default route (should NOT be $ifc):"
	ip route show default
}

cmd_down() {
	local ifc
	ifc=$(profile_iface)
	if carrier "$ifc"; then
		echo "WARNING: $ifc still has a cable in. With this profile gone NetworkManager may auto-connect"
		echo "a default wired profile that DOES take the default route. Unplug the cable, then rerun."
		exit 1
	fi
	nmcli connection delete "$CON" >/dev/null && echo "Removed '$CON'."
}

case ${1:-} in
up)     need_root "$@"; shift; cmd_up "$@" ;;
down)   need_root "$@"; cmd_down ;;
find)   cmd_find ;;
status) cmd_status ;;
*)      usage ;;
esac
