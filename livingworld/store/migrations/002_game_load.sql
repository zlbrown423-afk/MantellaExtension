-- Each game load the game reports, so a load is handled once even if it is reported twice.
CREATE TABLE game_load (
  nonce            INTEGER PRIMARY KEY,         -- random number Papyrus picks at each load
  save_timeline_id INTEGER,                     -- timeline stored in the loaded save (NULL: older save)
  loaded_gt        REAL NOT NULL,               -- game time of the loaded save
  timeline_id      INTEGER NOT NULL REFERENCES timeline(timeline_id),  -- timeline made active
  branched         INTEGER NOT NULL CHECK (branched IN (0,1)),
  at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

INSERT INTO schema_version(version, note) VALUES (2, 'game_load');
