-- 017: personality profiles (livingworld/personality). Who an adult woman is in love and intimacy, generated once,
-- changed slowly by what happens to her, and what the player has found out about it.
-- Hand-set values live in the settings file (personality.json), never here: this database only holds what the
-- simulation worked out. Children are never given a profile (livingworld/store/minors.py).

-- The generated starting point. Not on the save timeline: it is who she is, worked out from her bio, traits and
-- form id, so the same person gets the same profile in every save. Regenerating replaces it (see personality_admin).
CREATE TABLE personality_profile (
  npc_id            INTEGER PRIMARY KEY REFERENCES npc(npc_id),
  generator_version INTEGER NOT NULL,
  generated_json    TEXT NOT NULL CHECK (json_valid(generated_json)),
  sources_json      TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(sources_json)),   -- what each value came from
  created_gt        REAL NOT NULL,
  updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

-- Every change since then, on the save timeline (loading an older save rolls them back).
-- kind 'drift' moves a number by delta; 'list_add' / 'list_remove' change one list item (value_json = the item);
-- 'reset' drops every change before it for that person (attribute '*'); 'checkpoint' marks how far change over
-- time has looked at what happened to her (attribute '*', nothing changes).
CREATE TABLE personality_change (
  change_id    INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  attribute    TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('drift','list_add','list_remove','reset','checkpoint')),
  delta        REAL,
  value_json   TEXT CHECK (value_json IS NULL OR json_valid(value_json)),
  reason       TEXT NOT NULL,
  cause        TEXT,                                -- e.g. 'conv:12', 'encounter:4', 'bond:a-b'
  game_time    REAL NOT NULL,
  real_time    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE INDEX personality_change_npc ON personality_change(npc_id, game_time);

CREATE VIEW v_personality_change AS
SELECT c.* FROM personality_change c
JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt;

-- What someone (the player) knows about a person's preferences, on the save timeline.
-- item is an attribute key, or 'list_attribute:value' for one list item ('boundaries:no_unasked_touch').
-- state: 'suspected' (seen or heard, not told), 'known' (she said so), 'disclosed' (a boundary she stated).
CREATE TABLE personality_knowledge (
  knowledge_id INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  knower_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  item         TEXT NOT NULL,
  state        TEXT NOT NULL CHECK (state IN ('suspected','known','disclosed')),
  source       TEXT NOT NULL CHECK (source IN ('told','observed','gossip','admin')),
  conv_id      INTEGER REFERENCES conversation(conv_id),
  evidence     TEXT,
  game_time    REAL NOT NULL,
  CHECK (npc_id <> knower_id)
) STRICT;
CREATE INDEX personality_knowledge_pair ON personality_knowledge(npc_id, knower_id, item);

-- The strongest state each knower has reached for each item, within the current timeline.
CREATE VIEW v_personality_knowledge AS
SELECT k.npc_id, k.knower_id, k.item,
       CASE MAX(CASE k.state WHEN 'disclosed' THEN 3 WHEN 'known' THEN 2 ELSE 1 END)
            WHEN 3 THEN 'disclosed' WHEN 2 THEN 'known' ELSE 'suspected' END AS state,
       MIN(k.game_time) AS first_gt
FROM personality_knowledge k
JOIN v_timeline_scope s ON s.timeline_id = k.timeline_id AND k.game_time <= s.cutoff_gt
GROUP BY k.npc_id, k.knower_id, k.item;

-- What an administrator did (import, reset, regenerate, enable, disable), for the history. Not on the timeline.
CREATE TABLE personality_admin (
  admin_id     INTEGER PRIMARY KEY,
  npc_id       INTEGER REFERENCES npc(npc_id),
  action       TEXT NOT NULL,
  detail_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
  real_time    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

INSERT INTO schema_version(version, note) VALUES (17, 'personality profiles: generated base, timeline changes, discovery');
