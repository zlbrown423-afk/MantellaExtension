-- 014: an OStim scene still running when a conversation closes is stored then, marked ongoing
-- (livingworld/intimacy/scenes.py), and completed once when the game's end record for it arrives.
-- world_event stays append-only for everything else: only a row whose payload says ongoing may change,
-- and never its id, timeline, type, people or time.
DROP TRIGGER event_no_update;
CREATE TRIGGER event_no_update BEFORE UPDATE ON world_event
WHEN NOT (COALESCE(json_extract(OLD.payload_json, '$.ongoing'), 0) = 1 AND NEW.event_id IS OLD.event_id
          AND NEW.timeline_id IS OLD.timeline_id AND NEW.type IS OLD.type AND NEW.actors_json IS OLD.actors_json
          AND NEW.game_time IS OLD.game_time)
BEGIN SELECT RAISE(ABORT,'world_event is append-only'); END;

INSERT INTO schema_version(version, note) VALUES (14, 'scenes stored while still running can be completed once');
