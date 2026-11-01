#!/bin/sh
# Read-only snapshot of the D-Link's OpenWrt config and state. Passwords and keys
# (Wi-Fi, PPPoE/DSL login, EAP, RADIUS, WireGuard) are masked, so the output is safe to
# paste into a chat or issue.
#
# usage (from the NUC): ssh "$DLINK" sh -s < inspect.sh | tee ~/dlink-inspect.txt

section() { printf '\n### %s\n' "$*"; }

mask() {
	sed -E "s/^([^=]*\.(key[0-9]*|[a-z_]*password|passphrase|[a-z_]*secret|[a-z_]*psk|private_key|username)=).*/\1'********'/"
}

section release
cat /etc/openwrt_release 2>/dev/null || echo "no /etc/openwrt_release - not OpenWrt?"
cat /tmp/sysinfo/model 2>/dev/null
uptime

section storage
df -h /overlay 2>/dev/null
free

section packages
if command -v apk >/dev/null 2>&1; then apk list --installed 2>/dev/null; else opkg list-installed 2>/dev/null; fi |
	grep -E '^(relayd|luci-proto-relay|luci-base|wpad|hostapd|kmod-b43|b43|kmod-mac80211|dnsmasq|firewall)' ||
	echo "(none of the interesting ones)"

section "uci network (secrets masked)"
uci show network | mask

section "uci wireless (secrets masked)"
uci show wireless | mask

section "uci firewall zones"
uci show firewall | grep -E '@zone|@forwarding'

section "uci dhcp lan"
uci show dhcp | grep -E '^dhcp\.lan\.'

section "interface status"
for i in $(ubus list 'network.interface.*' | sed 's/^network\.interface\.//'); do
	printf '%-16s up=%s %s\n' "$i" \
		"$(ifstatus "$i" | jsonfilter -e '@.up')" \
		"$(ifstatus "$i" | jsonfilter -e "@['ipv4-address'][*].address" | tr '\n' ' ')"
done

section addresses
ip addr

section routes
ip route

section "wifi"
iwinfo 2>/dev/null || iw dev

section neighbours
ip neigh

section relayd
ps w | grep '[r]elayd' || echo "relayd not running"

section "bridge / switch"
brctl show 2>/dev/null || bridge link 2>/dev/null
swconfig dev switch0 show 2>/dev/null | grep 'link:'

section "recent log"
logread | tail -n 60
