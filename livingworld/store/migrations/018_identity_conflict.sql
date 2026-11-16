-- 018: identity conflicts (livingworld/guard/identity_guard.py). The game's actor reference is the only thing
-- that says who an NPC is. When a line has an NPC introduce themselves by the name of someone else standing in
-- the conversation (two "Whiterun Guard"s sharing one chat, the second saying "I'm Kolbeinn"), the claim is
-- refused: nobody learns the wrong name, the dialogue model is corrected on the player's next line, and the
-- attempt is written here so it can be inspected. Rows are a log only; nothing reads them to decide who anyone is.
CREATE TABLE identity_conflict (
  conflict_id   INTEGER PRIMARY KEY,
  conv_id       INTEGER,
  game_time     REAL NOT NULL,
  claimant_id   INTEGER NOT NULL REFERENCES npc(npc_id),   -- the actor whose line made the claim
  owner_id      INTEGER NOT NULL REFERENCES npc(npc_id),   -- the actor the claimed name belongs to
  claimed_name  TEXT NOT NULL,
  line          TEXT NOT NULL                              -- the start of the line, for debugging
) STRICT;
CREATE INDEX identity_conflict_pair ON identity_conflict(claimant_id, owner_id);

INSERT INTO schema_version(version, note) VALUES (18, 'log of NPC lines claiming another actor''s name');
