#!/system/bin/sh
# Snapshot only; call outside formal power windows.
# Usage: sh snapshot-memory.sh NEW_OUTPUT_DIR [ZRAM_DIR]
set -eu
out=${1:?new output directory required}
zram=${2:-/sys/block/zram0}
[ ! -e "$out" ] || exit 2
mkdir -p "$out"
cat /proc/sys/kernel/random/boot_id > "$out/boot-id.txt"
for index in 1 2 3; do
    cat /proc/uptime > "$out/$index-before-uptime.txt"
    cat /proc/meminfo > "$out/$index-meminfo.txt"
    if [ -r "$zram/mm_stat" ]; then
        cat "$zram/mm_stat" > "$out/$index-zram-mm-stat.txt"
    fi
    cat /proc/uptime > "$out/$index-after-uptime.txt"
    [ "$index" -eq 3 ] || sleep 10
done
cat /proc/swaps > "$out/swaps.txt"
# Detailed PSS is deliberately a separate executor-controlled operation.
