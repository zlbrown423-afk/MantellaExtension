-- Perception: physical interactions, consumption and witnesses, recorded by the game and turned into
-- one objective world_event plus a separate first-person memory for each NPC who took part or saw it.

-- Objective event: the extra fields the perception spec asks for. Everything else stays in payload_json
-- (target, object, interaction type, body part, hand, item category, ...).
ALTER TABLE world_event ADD COLUMN game_key   TEXT;               -- the game's own id for the event (deduplication)
ALTER TABLE world_event ADD COLUMN real_time  TEXT;               -- when the server stored it (UTC)
ALTER TABLE world_event ADD COLUMN duration_s REAL;               -- seconds, for held / ongoing interactions
ALTER TABLE world_event ADD COLUMN confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1);
CREATE UNIQUE INDEX world_event_game_key ON world_event(timeline_id, game_key) WHERE game_key IS NOT NULL;

-- What each witness could actually perceive at the time
ALTER TABLE event_witness ADD COLUMN distance_m REAL;
ALTER TABLE event_witness ADD COLUMN had_los    INTEGER CHECK (had_los IS NULL OR had_los IN (0,1));

-- Per-NPC memory: how the NPC came to know it, what they made of it, how sure they are, and the event
-- it came from. memory.created_by gains 'perception'; SQLite cannot change a CHECK in place, so the
-- table is rebuilt. Foreign keys pointing at memory are deferred to the end of this migration.
-- (Copy out, drop, recreate under the same name, copy back: no RENAME, so views that name memory stay valid.)
PRAGMA defer_foreign_keys = ON;
CREATE TEMP TABLE memory_copy AS SELECT * FROM memory;
DROP TABLE memory;
CREATE TABLE memory (
  mem_id          INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  owner_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  kind            TEXT NOT NULL CHECK (kind IN ('episodic','semantic','reflection')),
  content         TEXT NOT NULL,            -- first person, owner's point of view
  about_ids_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(about_ids_json)),
  location        TEXT,
  occurred_gt     REAL NOT NULL,
  importance      REAL NOT NULL CHECK (importance BETWEEN 0 AND 1),
  valence         REAL NOT NULL DEFAULT 0 CHECK (valence BETWEEN -1 AND 1),
  intensity       REAL NOT NULL DEFAULT 0 CHECK (intensity BETWEEN 0 AND 1),
  rehearsal_count INTEGER NOT NULL DEFAULT 0,
  last_recalled_gt REAL,
  superseded_by   INTEGER REFERENCES memory(mem_id),
  created_by      TEXT NOT NULL CHECK (created_by IN ('extraction','consolidation','admin','seed','perception')),
  source_kind     TEXT CHECK (source_kind IS NULL OR source_kind IN ('experienced','witnessed','heard','told','inferred')),
  interpretation  TEXT,                     -- what the owner made of it, in their own words
  confidence      REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
  event_id        INTEGER REFERENCES world_event(event_id)
) STRICT;
INSERT INTO memory (mem_id, timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,
                       importance, valence, intensity, rehearsal_count, last_recalled_gt, superseded_by, created_by)
SELECT mem_id, timeline_id, owner_id, kind, content, about_ids_json, location, occurred_gt,
       importance, valence, intensity, rehearsal_count, last_recalled_gt, superseded_by, created_by FROM memory_copy;
DROP TABLE memory_copy;
CREATE INDEX mem_owner ON memory(owner_id, occurred_gt) WHERE superseded_by IS NULL;
CREATE INDEX memory_event ON memory(event_id) WHERE event_id IS NOT NULL;
CREATE TRIGGER memory_fts_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid, content) VALUES (NEW.mem_id, NEW.content); END;

-- Events visible on the active timeline (same rule as v_visible_memory)
CREATE VIEW v_visible_event AS
SELECT e.* FROM world_event e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id AND e.game_time <= s.cutoff_gt;

-- Relationship values as seen on the active timeline: the base row plus every visible change.
-- relationship has no timeline column, so event-driven changes are kept only as relationship_change
-- rows and summed here; loading an older save then undoes them automatically.
CREATE VIEW v_relationship AS
SELECT r.from_id, r.to_id, r.kinship, r.status,
       MAX(-1.0, MIN(1.0, r.trust    + COALESCE(SUM(json_extract(c.deltas_json, '$.trust')), 0)))    AS trust,
       MAX(-1.0, MIN(1.0, r.affinity + COALESCE(SUM(json_extract(c.deltas_json, '$.affinity')), 0))) AS affinity,
       MAX( 0.0, MIN(1.0, r.fear     + COALESCE(SUM(json_extract(c.deltas_json, '$.fear')), 0)))     AS fear
FROM relationship r
LEFT JOIN (SELECT c.* FROM relationship_change c
           JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt) c
  ON c.from_id = r.from_id AND c.to_id = r.to_id
GROUP BY r.from_id, r.to_id;

INSERT INTO schema_version(version, note) VALUES (3, 'perception');
