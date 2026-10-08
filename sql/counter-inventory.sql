-- Counts are telemetry availability, not independent experimental samples.
SELECT t.id AS track_id, t.name, t.type, t.unit,
       t.source_arg_set_id, t.dimension_arg_set_id,
       COUNT(c.id) AS sample_count, MIN(c.ts) AS first_ts_ns,
       MAX(c.ts) AS last_ts_ns, MIN(c.value) AS min_value,
       MAX(c.value) AS max_value
FROM counter_track t
LEFT JOIN counter c ON c.track_id = t.id
GROUP BY t.id, t.name, t.type, t.unit,
         t.source_arg_set_id, t.dimension_arg_set_id
ORDER BY t.name, t.id;
