-- A92 towards Washington Street: Ustyianovycha (48) -> Slovatskoho (8).
BEGIN;

DO $patch$
DECLARE
    target record;
    corrected geometry;
BEGIN
    SELECT r.id AS route_id, l.sequence_order, s.id AS segment_id,
           s.start_stop_id, s.end_stop_id, s.geom::geometry AS original
    INTO STRICT target
    FROM routes r
    JOIN route_segment_links l ON l.route_id = r.id
    JOIN route_segments s ON s.id = l.segment_id
    JOIN stops a ON a.id = s.start_stop_id
    JOIN stops b ON b.id = s.end_stop_id
    WHERE r.source = 'easyway' AND r.external_id = '97_0'
      AND a.source = 'easyway' AND a.external_id = '5249'
      AND b.source = 'easyway' AND b.external_id = '44977'
    FOR UPDATE OF s;

    IF EXISTS (
        SELECT 1 FROM route_segment_links
        WHERE segment_id = target.segment_id AND route_id <> target.route_id
    ) THEN
        RAISE EXCEPTION 'Segment % is shared with another route', target.segment_id;
    END IF;

    -- Intermediate vertices follow the road along the park (EasyWay shape 40117).
    -- Keep the imported endpoints so this line joins the adjacent segments.
    corrected := ST_MakeLine(ARRAY[
        ST_StartPoint(target.original),
        ST_SetSRID(ST_MakePoint(24.01632, 49.83620), 4326),
        ST_SetSRID(ST_MakePoint(24.01678, 49.83593), 4326),
        ST_SetSRID(ST_MakePoint(24.01709, 49.83587), 4326),
        ST_SetSRID(ST_MakePoint(24.01727, 49.83587), 4326),
        ST_SetSRID(ST_MakePoint(24.01841, 49.83685), 4326),
        ST_SetSRID(ST_MakePoint(24.01935, 49.83661), 4326),
        ST_SetSRID(ST_MakePoint(24.02020, 49.83709), 4326),
        ST_EndPoint(target.original)
    ]);

    UPDATE route_segments SET geom = corrected::geography
    WHERE id = target.segment_id;

    INSERT INTO route_segment_geometries (route_id, sequence_order, start_stop_id, end_stop_id, geom)
    VALUES (target.route_id, target.sequence_order, target.start_stop_id, target.end_stop_id, corrected::geography)
    ON CONFLICT (route_id, sequence_order) DO UPDATE
    SET start_stop_id = EXCLUDED.start_stop_id, end_stop_id = EXCLUDED.end_stop_id, geom = EXCLUDED.geom;

    RAISE NOTICE 'Updated A92 segment %, sequence %, % vertices',
        target.segment_id, target.sequence_order, ST_NPoints(corrected);
END
$patch$;

COMMIT;
