#!/usr/bin/env bash
# Scan a /24 for the Arduino power monitor: the host whose GET /power returns its JSON.
# usage: ./find-power-monitor.sh [192.168.0]
net=${1:-192.168.0}

found=$(
	for i in $(seq 1 254); do
		(
			body=$(curl -s -m 3 "http://$net.$i/power") &&
				[[ $body == *'"W"'* && $body == *'"V"'* ]] &&
				echo "$net.$i  $body"
		) &
	done
	wait
)
echo "${found:-no power monitor found on $net.0/24 - if it was powered before the bridge came up, reset it}"
