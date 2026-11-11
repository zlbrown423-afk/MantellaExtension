-- 015: Mantella's own conversation summaries (one text file per NPC) roll back with the save like everything
-- else (livingworld/memory/summary_timeline.py). Each paragraph Mantella writes, and each new summary file a
-- resummary starts, is noted here with the timeline and game time it was written on. Reading leaves out the
-- paragraphs, and the files, that only exist on a timeline an older save left behind. Nothing is deleted.
CREATE TABLE summary_line (
  line_id     INTEGER PRIMARY KEY,
  timeline_id INTEGER NOT NULL REFERENCES timeline(timeline_id),
  folder      TEXT NOT NULL,          -- the NPC's summary folder, relative to Mantella's conversation folder
  line_hash   TEXT NOT NULL,          -- sha1 of the stripped line
  game_time   REAL NOT NULL
);
CREATE INDEX summary_line_by_folder ON summary_line(folder, line_hash);

CREATE TABLE summary_file (
  file_id     INTEGER PRIMARY KEY,
  timeline_id INTEGER NOT NULL REFERENCES timeline(timeline_id),
  folder      TEXT NOT NULL,
  file_number INTEGER NOT NULL,       -- the N of <name>_summary_N.txt a resummary started
  game_time   REAL NOT NULL
);
CREATE INDEX summary_file_by_folder ON summary_file(folder, file_number);

INSERT INTO schema_version(version, note) VALUES (15, 'Mantella summaries roll back with the save');
