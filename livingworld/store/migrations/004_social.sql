-- Reputation and gossip (S1): communities, structured claims that spread between NPCs, how far each
-- rule set has read the event log, and every relationship dimension summed on the active timeline.

-- Child NPCs are left out of every Living World calculation (see store/minors.py).
ALTER TABLE npc ADD COLUMN is_child INTEGER NOT NULL DEFAULT 0 CHECK (is_child IN (0,1));

-- Settlements, households, factions. Seeded from livingworld/social/data/*.json.
CREATE TABLE community (
  community_id INTEGER PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,              -- 'riverwood', 'riverwood_mill'
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('settlement','household','faction','guild')),
  location     TEXT,
  parent_id    INTEGER REFERENCES community(community_id)
) STRICT;

CREATE TABLE membership (
  npc_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  community_id INTEGER NOT NULL REFERENCES community(community_id),
  role         TEXT,
  influence    REAL NOT NULL DEFAULT 0.3 CHECK (influence BETWEEN 0 AND 1),
  loyalty      REAL NOT NULL DEFAULT 0.5 CHECK (loyalty BETWEEN 0 AND 1),
  PRIMARY KEY (npc_id, community_id)
) STRICT;
CREATE INDEX membership_community ON membership(community_id);

-- A statement that can spread: "<subject> <act> <object><detail>". The truth is the event it came
-- from (truth_event_id); a claim made up or reported in conversation has none.
CREATE TABLE claim (
  claim_id       INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  subject_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  act            TEXT NOT NULL,                  -- 'grabbed', 'held', 'bought_round', 'reported', ...
  object_id      INTEGER REFERENCES npc(npc_id),
  detail         TEXT NOT NULL DEFAULT '',       -- ' by the left forearm'; for 'reported' the whole phrase
  valence        REAL NOT NULL DEFAULT 0 CHECK (valence BETWEEN -1 AND 1),  -- how it reflects on the subject
  magnitude      REAL NOT NULL CHECK (magnitude BETWEEN 0 AND 1),
  location       TEXT,
  game_time      REAL NOT NULL,                  -- when it (supposedly) happened
  truth_event_id INTEGER REFERENCES world_event(event_id),
  sensitivity    TEXT NOT NULL DEFAULT 'public' CHECK (sensitivity IN ('public','private','secret'))
) STRICT;
CREATE INDEX claim_event ON claim(truth_event_id) WHERE truth_event_id IS NOT NULL;

-- Each holder's own copy of a claim lives in knowledge (chain of custody, timeline, teller).
ALTER TABLE knowledge ADD COLUMN claim_id INTEGER REFERENCES claim(claim_id);
ALTER TABLE knowledge ADD COLUMN heard_magnitude REAL CHECK (heard_magnitude IS NULL OR heard_magnitude BETWEEN 0 AND 1);
ALTER TABLE knowledge ADD COLUMN believed REAL CHECK (believed IS NULL OR believed BETWEEN 0 AND 1);
ALTER TABLE knowledge ADD COLUMN will_share TEXT CHECK (will_share IS NULL OR will_share IN ('yes','trusted_only','no'));
CREATE INDEX know_claim ON knowledge(claim_id, holder_id) WHERE claim_id IS NOT NULL;
ALTER TABLE rumor ADD COLUMN claim_id INTEGER REFERENCES claim(claim_id);

-- How far each consumer (appraisal, later goals, player knowledge, body language) has read world_event.
CREATE TABLE event_cursor (
  consumer      TEXT PRIMARY KEY,
  last_event_id INTEGER NOT NULL DEFAULT 0
) STRICT;

-- Claims as one holder knows them, on the active timeline.
CREATE VIEW v_claim_knowledge AS
SELECT k.know_id, k.holder_id, k.origin, k.told_by_id, k.parent_know_id, k.learned_gt, k.believed,
       k.heard_magnitude, k.will_share, k.source_event_id, k.source_utt_id,
       c.claim_id, c.subject_id, c.act, c.object_id, c.detail, c.valence, c.magnitude, c.location,
       c.game_time AS claim_gt, c.truth_event_id, c.sensitivity
FROM knowledge k
JOIN claim c ON c.claim_id = k.claim_id
JOIN v_timeline_scope s ON s.timeline_id = k.timeline_id AND k.learned_gt <= s.cutoff_gt
WHERE k.superseded_by IS NULL;

-- Every relationship dimension: the base row plus every visible change (respect was missing before).
DROP VIEW v_relationship;
CREATE VIEW v_relationship AS
SELECT r.from_id, r.to_id, r.kinship, r.status,
       MAX(-1.0, MIN(1.0, r.trust       + COALESCE(SUM(json_extract(c.deltas_json, '$.trust')), 0)))       AS trust,
       MAX(-1.0, MIN(1.0, r.affinity    + COALESCE(SUM(json_extract(c.deltas_json, '$.affinity')), 0)))    AS affinity,
       MAX( 0.0, MIN(1.0, r.fear        + COALESCE(SUM(json_extract(c.deltas_json, '$.fear')), 0)))        AS fear,
       MAX(-1.0, MIN(1.0, r.respect     + COALESCE(SUM(json_extract(c.deltas_json, '$.respect')), 0)))     AS respect,
       MAX(-1.0, MIN(1.0,                 COALESCE(SUM(json_extract(c.deltas_json, '$.admiration')), 0)))  AS admiration,
       MAX( 0.0, MIN(1.0,                 COALESCE(SUM(json_extract(c.deltas_json, '$.suspicion')), 0)))   AS suspicion,
       MAX( 0.0, MIN(1.0, r.attraction  + COALESCE(SUM(json_extract(c.deltas_json, '$.attraction')), 0)))  AS attraction,
       MAX( 0.0, MIN(1.0, r.familiarity + COALESCE(SUM(json_extract(c.deltas_json, '$.familiarity')), 0))) AS familiarity
FROM relationship r
LEFT JOIN (SELECT c.* FROM relationship_change c
           JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt) c
  ON c.from_id = r.from_id AND c.to_id = r.to_id
GROUP BY r.from_id, r.to_id;

INSERT INTO schema_version(version, note) VALUES (4, 'social: communities, claims, event cursor');
