-- Trace Processor's BOOTTIME snapshots must agree with normalized trace time.
SELECT ts, clock_id, clock_name, clock_value
FROM clock_snapshot WHERE clock_name = 'BOOTTIME' ORDER BY ts;
