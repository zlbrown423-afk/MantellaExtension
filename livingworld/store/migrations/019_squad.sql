-- 019: temporary squads (livingworld/squad/). A squad is the player's walking party: NPCs the player asked, in
-- conversation, to come along, who said yes. It is bookkeeping next to Mantella's own conversation hold, not a
-- follower framework: nobody is put in a follower faction and vanilla combat AI is untouched. Membership is
-- ended by the player (dismissal), by the NPC declining, or when the conversation that holds them ends.
CREATE TABLE squad (
  squad_id      INTEGER PRIMARY KEY,
  leader_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  formation     TEXT NOT NULL DEFAULT 'loose' CHECK (formation IN ('loose','close','autonomous','patrol')),
  formed_gt     REAL NOT NULL,
  disbanded_gt  REAL
) STRICT;
CREATE UNIQUE INDEX squad_one_active_per_leader ON squad(leader_id) WHERE disbanded_gt IS NULL;

CREATE TABLE squad_member (
  member_row    INTEGER PRIMARY KEY,
  squad_id      INTEGER NOT NULL REFERENCES squad(squad_id),
  npc_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  status        TEXT NOT NULL CHECK (status IN ('asked','active','declined','dismissed','dropped')),
  asked_gt      REAL NOT NULL,
  joined_gt     REAL,
  ended_gt      REAL,
  reason        TEXT
) STRICT;
-- one NPC is in at most one live squad (asked or active)
CREATE UNIQUE INDEX squad_member_one_live ON squad_member(npc_id) WHERE status IN ('asked','active');
CREATE INDEX squad_member_squad ON squad_member(squad_id, status);

INSERT INTO schema_version(version, note) VALUES (19, 'temporary squads: players walking party, membership and formation');
