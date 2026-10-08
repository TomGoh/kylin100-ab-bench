#!/system/bin/sh
# Read persisted Android events; do not record, clear logs or reboot.
# Usage: sh snapshot-boot.sh NEW_OUTPUT_DIR
set -eu
out=${1:?new output directory required}
[ ! -e "$out" ] || exit 2
mkdir -p "$out"
cat /proc/sys/kernel/random/boot_id > "$out/boot-id.txt"
cat /proc/uptime > "$out/read-before-uptime.txt"
bootstat -p > "$out/bootstat.txt" 2> "$out/bootstat.stderr.txt" || :
getprop > "$out/getprop.txt"
logcat -b events -d -v monotonic -s \
    boot_progress_start:I boot_progress_preload_start:I \
    boot_progress_preload_end:I boot_progress_system_run:I \
    boot_progress_pms_start:I boot_progress_pms_ready:I \
    boot_progress_ams_ready:I boot_progress_enable_screen:I \
    wm_boot_animation_done:I sf_boot_animation_done:I \
    > "$out/boot-events.txt" 2> "$out/boot-events.stderr.txt" || :
cat /proc/uptime > "$out/read-after-uptime.txt"
