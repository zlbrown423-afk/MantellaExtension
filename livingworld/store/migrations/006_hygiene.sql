-- Hygiene (H1): how clean each person is, worked out from a change log on the save timeline, so loading
-- an older save rolls cleanliness back too. Numbered 006 because 005 is taken by romance; the store
-- applies any file not applied yet, so the two can merge in either order.

-- Where people may bathe. Seeded later (H2) from a JSON file; nothing reads it yet.
CREATE TABLE bathing_site (
  site_id         INTEGER PRIMARY KEY,
  name            TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('river','lake','hot_spring','bathhouse','inn_bath','private_tub')),
  location        TEXT,
  cell_json       TEXT NOT NULL CHECK (json_valid(cell_json)),   -- cell or worldspace + x,y,z,heading, or plugin + form id
  radius_m        REAL NOT NULL DEFAULT 6,
  privacy         REAL NOT NULL CHECK (privacy BETWEEN 0 AND 1),
  cover           REAL NOT NULL CHECK (cover BETWEEN 0 AND 1),
  safety          REAL NOT NULL CHECK (safety BETWEEN 0 AND 1),
  public          INTEGER NOT NULL DEFAULT 1 CHECK (public IN (0,1)),
  owner_ids_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(owner_ids_json)),
  requires_plugin TEXT,
  enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1))
) STRICT;

-- Every change to someone's cleanliness. dirt and blood are deltas on a 0..1 scale; a bath removes both.
CREATE TABLE hygiene_change (
  change_id   INTEGER PRIMARY KEY,
  timeline_id INTEGER NOT NULL REFERENCES timeline(timeline_id),
  npc_id      INTEGER NOT NULL REFERENCES npc(npc_id),
  game_time   REAL NOT NULL,
  cause       TEXT NOT NULL CHECK (cause IN ('seed','bath','offscreen_bath','swim','rain','fight','work','travel','groom','debug')),
  dirt        REAL NOT NULL DEFAULT 0,
  blood       REAL NOT NULL DEFAULT 0,
  site_id     INTEGER REFERENCES bathing_site(site_id),
  game_key    TEXT                       -- the game record's key, so a record sent twice counts once
) STRICT;
CREATE INDEX hygiene_change_npc ON hygiene_change(npc_id, game_time);
CREATE UNIQUE INDEX hygiene_change_game_key ON hygiene_change(timeline_id, game_key) WHERE game_key IS NOT NULL;

-- Who has already noticed what about whom, so nobody remarks on the same mud twice.
CREATE TABLE hygiene_notice (
  notice_id   INTEGER PRIMARY KEY,
  timeline_id INTEGER NOT NULL REFERENCES timeline(timeline_id),
  observer_id INTEGER NOT NULL REFERENCES npc(npc_id),
  subject_id  INTEGER NOT NULL REFERENCES npc(npc_id),
  what        TEXT NOT NULL,             -- 'grimy', 'filthy', 'bloodied', 'cleaner'
  game_time   REAL NOT NULL
) STRICT;
CREATE INDEX hygiene_notice_pair ON hygiene_notice(observer_id, subject_id, game_time);

-- Changes visible on the active timeline (same rule as v_visible_event)
CREATE VIEW v_hygiene_change AS
SELECT h.* FROM hygiene_change h
JOIN v_timeline_scope s ON s.timeline_id = h.timeline_id AND h.game_time <= s.cutoff_gt;

CREATE VIEW v_hygiene_notice AS
SELECT n.* FROM hygiene_notice n
JOIN v_timeline_scope s ON s.timeline_id = n.timeline_id AND n.game_time <= s.cutoff_gt;

INSERT INTO schema_version(version, note) VALUES (6, 'hygiene: cleanliness change log, notices, bathing sites');
