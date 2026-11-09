-- Leads: the people, places and things the player has been asking around about ("Vald", "the Pawned Prawn").
-- Once a name has come up, the player can ask about it again without saying it ("Have you seen the man
-- I'm looking for?"), which spares the player saying a name that speech-to-text keeps getting wrong.
--
-- One row each time a lead is learned or asked about again. The player's current leads are the latest
-- rows in the timeline's scope, so loading an older save forgets leads learned after it.

CREATE TABLE player_lead (
  lead_id      INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  name         TEXT NOT NULL,                       -- as spelled by an NPC or the misheard-word list
  kind         TEXT NOT NULL CHECK (kind IN ('person','place','thing')),
  source       TEXT NOT NULL CHECK (source IN ('heard','npc_named','said')),
  heard_as     TEXT,                                -- what speech-to-text wrote, when it differed
  conv_id      INTEGER REFERENCES conversation(conv_id),
  game_time    REAL NOT NULL
);

CREATE INDEX idx_player_lead_time ON player_lead (timeline_id, game_time);

INSERT INTO schema_version(version, note) VALUES (11, 'player leads: names the player has been asking around about');
