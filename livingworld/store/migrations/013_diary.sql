-- Diaries: NPCs the player spends a lot of time with keep a private diary, written by the background model
-- from their own memories. The game shows it as a book ("Gerdur's Diary") the NPC carries or leaves at home.
--
-- Every row is on the save timeline, so loading an older save takes back the entries written after it.

-- One row each time an entry is written. A day's entry can be rewritten after later conversations that day;
-- the newest version wins (v_diary_entry).
CREATE TABLE diary_entry (
  entry_id     INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  game_day     INTEGER NOT NULL,             -- whole game days passed (the day the entry is about)
  text         TEXT NOT NULL,
  last_mem_id  INTEGER,                      -- the newest memory the writer was shown (skip a rewrite with nothing new)
  conv_id      INTEGER REFERENCES conversation(conv_id),
  written_gt   REAL NOT NULL
) STRICT;
CREATE INDEX diary_entry_npc ON diary_entry(npc_id, game_day, written_gt);

CREATE VIEW v_diary_entry AS
SELECT entry_id, npc_id, game_day, text, last_mem_id, conv_id, written_gt, versions FROM (
  SELECT d.*, ROW_NUMBER() OVER (PARTITION BY d.npc_id, d.game_day ORDER BY d.written_gt DESC, d.entry_id DESC) AS rn,
         COUNT(*) OVER (PARTITION BY d.npc_id, d.game_day) AS versions
  FROM diary_entry d
  JOIN v_timeline_scope s ON s.timeline_id = d.timeline_id AND d.written_gt <= s.cutoff_gt)
WHERE rn = 1;

-- Each time the player opened someone's diary in the game.
CREATE TABLE diary_read (
  read_id      INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  owner_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  reader_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  seen_by_owner INTEGER NOT NULL DEFAULT 0 CHECK (seen_by_owner IN (0,1)),
  event_id     INTEGER REFERENCES world_event(event_id),
  location     TEXT,
  game_time    REAL NOT NULL
) STRICT;

INSERT INTO schema_version(version, note) VALUES (13, 'diaries: entries and reads');
