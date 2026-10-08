#!/system/bin/sh
# Read-only reference sampler. Use for awake workloads, not deep standby.
# Usage: sh sample-battery.sh OUTPUT DURATION_S INTERVAL_S [BATTERY_DIR] [USB_ONLINE]
set -eu
out=${1:?output required}
duration=${2:?duration required}
interval=${3:-1}
battery=${4:-/sys/class/power_supply/battery}
external=${5:-/sys/class/power_supply/usb/online}
case "$duration" in ''|*[!0-9]*) exit 2;; esac
case "$interval" in 1|2|3|4|5) ;; *) exit 2;; esac
[ "$duration" -ge 1 ] && [ "$duration" -le 1800 ] || exit 2
[ ! -e "$out" ] && [ ! -e "$out.partial" ] || exit 2
[ -d "$battery" ] || exit 2
boot=$(cat /proc/sys/kernel/random/boot_id)
read_value() {
    if [ -r "$1" ]; then
        value=$(cat "$1" 2>/dev/null) || value=NA
        case "$value" in ''|*[!0-9-]*) printf NA;; *) printf '%s' "$value";; esac
    else
        printf NA
    fi
}
read start idle < /proc/uptime
start_int=${start%%.*}
printf '%s\n' 't_s,read_end_s,boot_id,voltage_uv,current_ua,charge_uah,external_online,battery_temp_decic' > "$out.partial"
trap 'exit 130' INT TERM
while :; do
    read before idle < /proc/uptime
    voltage=$(read_value "$battery/voltage_now")
    current=$(read_value "$battery/current_now")
    charge=$(read_value "$battery/charge_counter")
    online=$(read_value "$external")
    temp=$(read_value "$battery/temp")
    read after idle < /proc/uptime
    printf '%s,%s,%s,%s,%s,%s,%s,%s\n' "$before" "$after" "$boot" "$voltage" "$current" "$charge" "$online" "$temp" >> "$out.partial"
    now_int=${after%%.*}
    [ $((now_int - start_int)) -lt "$duration" ] || break
    sleep "$interval"
done
mv "$out.partial" "$out"
