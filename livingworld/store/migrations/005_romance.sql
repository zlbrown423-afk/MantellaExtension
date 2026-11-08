-- Romance (R1): NPC-to-NPC courtship, bonds, private time and the consent behind it, off screen.
-- Everything is on the save timeline (base row + change log + view), so loading an older save rolls it back.
-- Children never appear in any of these tables (livingworld/store/minors.py).

-- Each change to a pair's agreed bond. The current bond is the latest visible change (v_romance_bond).
-- a_id < b_id always. stage 'none' means there is no bond any more (after a breakup it is 'ended').
CREATE TABLE romance_bond_change (
  change_id    INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  a_id         INTEGER NOT NULL REFERENCES npc(npc_id),
  b_id         INTEGER NOT NULL REFERENCES npc(npc_id),
  stage_from   TEXT,
  stage_to     TEXT NOT NULL CHECK (stage_to IN ('courting','dating','partners','betrothed','married','affair','ended')),
  exclusive    INTEGER NOT NULL DEFAULT 0 CHECK (exclusive IN (0,1)),
  secret       INTEGER NOT NULL DEFAULT 0 CHECK (secret IN (0,1)),
  reason       TEXT NOT NULL,
  event_id     INTEGER REFERENCES world_event(event_id),
  game_time    REAL NOT NULL,
  CHECK (a_id < b_id)
) STRICT;
CREATE INDEX romance_bond_pair ON romance_bond_change(a_id, b_id, game_time);

CREATE VIEW v_romance_bond AS
SELECT c.a_id, c.b_id, c.stage_to AS stage, c.exclusive, c.secret, c.game_time AS since_gt,
       (SELECT MIN(f.game_time) FROM romance_bond_change f
          JOIN v_timeline_scope s2 ON s2.timeline_id = f.timeline_id AND f.game_time <= s2.cutoff_gt
         WHERE f.a_id = c.a_id AND f.b_id = c.b_id) AS first_gt
FROM romance_bond_change c
JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt
WHERE c.change_id = (SELECT c2.change_id FROM romance_bond_change c2
                       JOIN v_timeline_scope s3 ON s3.timeline_id = c2.timeline_id AND c2.game_time <= s3.cutoff_gt
                      WHERE c2.a_id = c.a_id AND c2.b_id = c.b_id
                      ORDER BY c2.game_time DESC, c2.change_id DESC LIMIT 1);

-- Places where people can be alone. Seeded from livingworld/social/data/*.json ("places").
CREATE TABLE place (
  place_key    TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  location     TEXT,
  kind         TEXT NOT NULL CHECK (kind IN ('home_bedroom','inn_room','private_room','outdoor_secluded','public')),
  residents_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(residents_json)),  -- npc ids who live there
  staff_household_id INTEGER REFERENCES community(community_id),  -- who runs it (an inn's staff notice who takes a room)
  privacy      REAL NOT NULL CHECK (privacy BETWEEN 0 AND 1),
  child_in_home INTEGER NOT NULL DEFAULT 0 CHECK (child_in_home IN (0,1)),
  bed_ref      TEXT                                          -- the bed's reference, for the game side later
) STRICT;

-- Time two people spend together, planned and resolved. mode says what really happened:
-- 'offscreen' = decided on the server only, no animation; 'fade' and 'scene' come later from the game.
CREATE TABLE encounter (
  encounter_id INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  a_id         INTEGER NOT NULL REFERENCES npc(npc_id),       -- who asked
  b_id         INTEGER NOT NULL REFERENCES npc(npc_id),
  kind         TEXT NOT NULL CHECK (kind IN ('flirt','date','affection','intimate')),
  place_key    TEXT REFERENCES place(place_key),
  mode         TEXT NOT NULL DEFAULT 'offscreen' CHECK (mode IN ('offscreen','fade','scene')),
  framework    TEXT,
  outcome      TEXT NOT NULL CHECK (outcome IN ('completed','declined','withdrawn','interrupted','cancelled','failed')),
  event_id     INTEGER REFERENCES world_event(event_id),
  game_time    REAL NOT NULL,
  CHECK (a_id <> b_id)
) STRICT;
CREATE INDEX encounter_pair ON encounter(a_id, b_id, game_time);

-- Every consent answer, with the factors behind it. One row per person per phase.
CREATE TABLE consent_decision (
  decision_id  INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  encounter_id INTEGER REFERENCES encounter(encounter_id),
  npc_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  other_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  kind         TEXT NOT NULL,
  phase        TEXT NOT NULL CHECK (phase IN ('invite','arrival','start','during')),
  answer       TEXT NOT NULL CHECK (answer IN ('yes','no','withdrew')),
  reasons_json TEXT NOT NULL CHECK (json_valid(reasons_json)),
  game_time    REAL NOT NULL
) STRICT;
CREATE INDEX consent_encounter ON consent_decision(encounter_id);

CREATE VIEW v_encounter AS
SELECT e.* FROM encounter e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id AND e.game_time <= s.cutoff_gt;

-- Relationship values with the romance dimensions added. intimacy, commitment, jealousy are 0..1 and
-- satisfaction -1..1; like admiration and suspicion they live only in relationship_change deltas.
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
       MAX( 0.0, MIN(1.0, r.familiarity + COALESCE(SUM(json_extract(c.deltas_json, '$.familiarity')), 0))) AS familiarity,
       MAX( 0.0, MIN(1.0,                 COALESCE(SUM(json_extract(c.deltas_json, '$.intimacy')), 0)))    AS intimacy,
       MAX( 0.0, MIN(1.0,                 COALESCE(SUM(json_extract(c.deltas_json, '$.commitment')), 0)))  AS commitment,
       MAX(-1.0, MIN(1.0,                 COALESCE(SUM(json_extract(c.deltas_json, '$.satisfaction')), 0))) AS satisfaction,
       MAX( 0.0, MIN(1.0,                 COALESCE(SUM(json_extract(c.deltas_json, '$.jealousy')), 0)))    AS jealousy
FROM relationship r
LEFT JOIN (SELECT c.* FROM relationship_change c
           JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt) c
  ON c.from_id = r.from_id AND c.to_id = r.to_id
GROUP BY r.from_id, r.to_id;

INSERT INTO schema_version(version, note) VALUES (5, 'romance: bonds, places, encounters, consent log');
