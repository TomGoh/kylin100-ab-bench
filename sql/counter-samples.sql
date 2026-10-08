-- Preserve raw tracks and normalized trace timestamps. Unit/source validation
-- and boot/run binding precede any power or memory interpretation.
SELECT c.ts AS ts_ns, t.id AS track_id, t.name, t.type, t.unit,
       t.source_arg_set_id, t.dimension_arg_set_id, c.value
FROM counter c JOIN counter_track t ON c.track_id = t.id
ORDER BY c.ts, t.id;
