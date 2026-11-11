-- Fog: who knows whose name, and which places the player has heard about.
--
-- The player sees a named NPC as a description ("Nord woman") until they learn the NPC's name, and an NPC
-- does not know the player's name until the player (or someone present) says it. Names spread by being said:
-- an NPC saying their own name, someone in the conversation naming a person who is standing there, the player
-- introducing themselves, or gossip about the player from someone who knows the name.
--
-- Every row is on the save timeline, so loading an older save forgets names learned after it (the game keeps
-- its own copy of the display names in the save, so both sides roll back together).

-- The description the game shows for a named NPC the player has not met ('Nord woman'); NULL for people the
-- game never hides (generic NPCs, children). Sent by the game with each conversation.
ALTER TABLE npc ADD COLUMN stranger_label TEXT;

-- One row each time someone learns (or is told a new) name for someone. name is what the knower calls them,
-- which is not always the real name: the player can introduce themselves as anyone ('call me Wolf').
CREATE TABLE name_known (
  known_id     INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  knower_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  subject_id   INTEGER NOT NULL REFERENCES npc(npc_id),
  name         TEXT NOT NULL,
  how          TEXT NOT NULL CHECK (how IN ('introduced','named','said','gossip','met_before','debug')),
  told_by_id   INTEGER REFERENCES npc(npc_id),
  conv_id      INTEGER REFERENCES conversation(conv_id),
  game_time    REAL NOT NULL,
  CHECK (knower_id <> subject_id)
) STRICT;
CREATE INDEX name_known_pair ON name_known(knower_id, subject_id, game_time);

-- The name each knower uses for each subject now, on the active timeline.
CREATE VIEW v_name_known AS
SELECT knower_id, subject_id, name, how, game_time FROM (
  SELECT k.knower_id, k.subject_id, k.name, k.how, k.game_time,
         ROW_NUMBER() OVER (PARTITION BY k.knower_id, k.subject_id ORDER BY k.game_time DESC, k.known_id DESC) AS rn
  FROM name_known k
  JOIN v_timeline_scope s ON s.timeline_id = k.timeline_id AND k.game_time <= s.cutoff_gt)
WHERE rn = 1;

-- Places the player has heard about from someone (the game adds their map marker, undiscovered).
CREATE TABLE place_known (
  place_id     INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  name         TEXT NOT NULL,
  plugin       TEXT,
  marker_form_id TEXT,                       -- '0x0001A2B3', plugin-relative map marker reference
  how          TEXT NOT NULL CHECK (how IN ('told','debug')),
  told_by_id   INTEGER REFERENCES npc(npc_id),
  conv_id      INTEGER REFERENCES conversation(conv_id),
  game_time    REAL NOT NULL
) STRICT;
CREATE INDEX place_known_name ON place_known(name, game_time);

-- Everyone the player has already talked to through Mantella counts as met, both ways.
INSERT INTO name_known (timeline_id, knower_id, subject_id, name, how, conv_id, game_time)
SELECT c.timeline_id, pl.npc_id, n.npc_id, n.display_name, 'met_before', MIN(c.conv_id), MIN(c.started_gt)
FROM conversation c
JOIN participant pp ON pp.conv_id = c.conv_id JOIN npc pl ON pl.npc_id = pp.npc_id AND pl.is_player = 1
JOIN participant po ON po.conv_id = c.conv_id JOIN npc n ON n.npc_id = po.npc_id AND n.is_player = 0
GROUP BY c.timeline_id, pl.npc_id, n.npc_id;

INSERT INTO name_known (timeline_id, knower_id, subject_id, name, how, conv_id, game_time)
SELECT c.timeline_id, n.npc_id, pl.npc_id, pl.display_name, 'met_before', MIN(c.conv_id), MIN(c.started_gt)
FROM conversation c
JOIN participant pp ON pp.conv_id = c.conv_id JOIN npc pl ON pl.npc_id = pp.npc_id AND pl.is_player = 1
JOIN participant po ON po.conv_id = c.conv_id JOIN npc n ON n.npc_id = po.npc_id AND n.is_player = 0
GROUP BY c.timeline_id, pl.npc_id, n.npc_id;

INSERT INTO schema_version(version, note) VALUES (12, 'fog: who knows whose name, places the player heard about');
