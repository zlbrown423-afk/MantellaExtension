-- 026: relationships and reputations fade (architecture audit M4).
--
-- Until now every change to how one person feels about another (v_relationship) and every change to their view of
-- someone's honesty, decency and so on (v_standing) was summed for ever: one bad evening set a permanent ceiling on
-- how an NPC could feel about the player. Now each change fades with its own age, so feelings left without contact
-- drift back toward where the relationship started (the seeded base row), while people who keep meeting keep
-- adding fresh changes.
--
-- How one change of size d, made t game days ago, counts today (per feeling: half H days, floor f, cap P):
--   lasting part  f * d          - summed over all changes, then held within -P..P: a lasting mark, never a ceiling
--   fading part   (1 - f) * d * H / (H + MAX(0, t - 3))   - nothing fades for 3 days, half is gone after 3 + H days
-- The value shown is base + lasting + fading, clamped to the feeling's range as before.
--
--   feeling       H (days)  floor  cap
--   trust               30    0.4   0.4
--   affinity            30   0.35   0.4
--   fear                 7   0.15   0.2
--   respect             45    0.5   0.5
--   admiration          30    0.4   0.4
--   suspicion           10    0.2  0.25
--   attraction          20    0.3   0.3
--   familiarity        120    0.6   0.8
--   intimacy            30    0.4   0.4
--   commitment          30    0.4   0.4
--   satisfaction        20    0.3   0.3
--   jealousy             7    0.1  0.15
--
-- Never faded: kin (kinship set) and spouses or partners (status), who live together and meet every day
-- off-screen where no change is recorded. Children have no rows here at all (Living World leaves them out).
-- Standing (reputation by domain) fades the same way, with H = 40 days for what the holder saw and 15 days for
-- what they were told; floor 0.4, cap 0.4.
--
-- "Now" is the start of the newest conversation on the active timeline, or the game time of the save just
-- loaded (fade_clock, kept by triggers), so loading an older save shows feelings as they were then.
-- The env flag MANTELLA_LW_FADE=0 sets fade_clock.enabled = 0 when the world opens (livingworld/store/fading.py)
-- and the views then show the plain sums exactly as before. The unfaded sums stay readable in v_relationship_raw
-- and v_standing_raw. Nothing is deleted or rewritten: every change row is kept as it was.

CREATE TABLE fade_clock (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  enabled  INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
  now_gt   REAL
) STRICT;

INSERT INTO fade_clock (id, enabled, now_gt)
SELECT 1, 1, (SELECT MAX(c.started_gt) FROM conversation c
              JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.started_gt <= s.cutoff_gt);

CREATE TRIGGER fade_clock_on_conversation AFTER INSERT ON conversation
WHEN NEW.timeline_id = (SELECT timeline_id FROM timeline WHERE is_active = 1)
BEGIN
  UPDATE fade_clock SET now_gt = NEW.started_gt WHERE id = 1;
END;

CREATE TRIGGER fade_clock_on_load AFTER INSERT ON game_load
BEGIN
  UPDATE fade_clock SET now_gt = NEW.loaded_gt WHERE id = 1;
END;

-- Reading one pair (what almost every caller does) no longer scans every change in the world.
CREATE INDEX IF NOT EXISTS relationship_change_pair ON relationship_change(from_id, to_id, game_time);

-- The plain sums, the same values v_relationship showed before this migration.
CREATE VIEW v_relationship_raw AS
SELECT r.from_id, r.to_id, r.kinship, r.status,
       MAX(-1.0, MIN(1.0, r.trust        + COALESCE(SUM(json_extract(c.deltas_json, '$.trust')), 0))) AS trust,
       MAX(-1.0, MIN(1.0, r.affinity     + COALESCE(SUM(json_extract(c.deltas_json, '$.affinity')), 0))) AS affinity,
       MAX( 0.0, MIN(1.0, r.fear         + COALESCE(SUM(json_extract(c.deltas_json, '$.fear')), 0))) AS fear,
       MAX(-1.0, MIN(1.0, r.respect      + COALESCE(SUM(json_extract(c.deltas_json, '$.respect')), 0))) AS respect,
       MAX(-1.0, MIN(1.0,                  COALESCE(SUM(json_extract(c.deltas_json, '$.admiration')), 0))) AS admiration,
       MAX( 0.0, MIN(1.0,                  COALESCE(SUM(json_extract(c.deltas_json, '$.suspicion')), 0))) AS suspicion,
       MAX( 0.0, MIN(1.0, r.attraction   + COALESCE(SUM(json_extract(c.deltas_json, '$.attraction')), 0))) AS attraction,
       MAX( 0.0, MIN(1.0, r.familiarity  + COALESCE(SUM(json_extract(c.deltas_json, '$.familiarity')), 0))) AS familiarity,
       MAX( 0.0, MIN(1.0,                  COALESCE(SUM(json_extract(c.deltas_json, '$.intimacy')), 0))) AS intimacy,
       MAX( 0.0, MIN(1.0,                  COALESCE(SUM(json_extract(c.deltas_json, '$.commitment')), 0))) AS commitment,
       MAX(-1.0, MIN(1.0,                  COALESCE(SUM(json_extract(c.deltas_json, '$.satisfaction')), 0))) AS satisfaction,
       MAX( 0.0, MIN(1.0,                  COALESCE(SUM(json_extract(c.deltas_json, '$.jealousy')), 0))) AS jealousy
FROM relationship r
LEFT JOIN relationship_change c
  ON c.from_id = r.from_id AND c.to_id = r.to_id
 AND c.game_time <= (SELECT s.cutoff_gt FROM v_timeline_scope s WHERE s.timeline_id = c.timeline_id)
GROUP BY r.from_id, r.to_id;

-- What everyone reads: the same columns, faded.
DROP VIEW v_relationship;
CREATE VIEW v_relationship AS
SELECT r.from_id, r.to_id, r.kinship, r.status,
       MAX(-1.0, MIN(1.0, r.trust + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.trust')), 0)
           ELSE MAX(-0.4, MIN(0.4, 0.4 * COALESCE(SUM(json_extract(c.deltas_json, '$.trust')), 0)))
                + 0.6 * COALESCE(SUM(json_extract(c.deltas_json, '$.trust') * 30.0 / (30.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS trust,
       MAX(-1.0, MIN(1.0, r.affinity + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.affinity')), 0)
           ELSE MAX(-0.4, MIN(0.4, 0.35 * COALESCE(SUM(json_extract(c.deltas_json, '$.affinity')), 0)))
                + 0.65 * COALESCE(SUM(json_extract(c.deltas_json, '$.affinity') * 30.0 / (30.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS affinity,
       MAX(0.0, MIN(1.0, r.fear + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.fear')), 0)
           ELSE MAX(-0.2, MIN(0.2, 0.15 * COALESCE(SUM(json_extract(c.deltas_json, '$.fear')), 0)))
                + 0.85 * COALESCE(SUM(json_extract(c.deltas_json, '$.fear') * 7.0 / (7.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS fear,
       MAX(-1.0, MIN(1.0, r.respect + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.respect')), 0)
           ELSE MAX(-0.5, MIN(0.5, 0.5 * COALESCE(SUM(json_extract(c.deltas_json, '$.respect')), 0)))
                + 0.5 * COALESCE(SUM(json_extract(c.deltas_json, '$.respect') * 45.0 / (45.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS respect,
       MAX(-1.0, MIN(1.0, 0.0 + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.admiration')), 0)
           ELSE MAX(-0.4, MIN(0.4, 0.4 * COALESCE(SUM(json_extract(c.deltas_json, '$.admiration')), 0)))
                + 0.6 * COALESCE(SUM(json_extract(c.deltas_json, '$.admiration') * 30.0 / (30.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS admiration,
       MAX(0.0, MIN(1.0, 0.0 + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.suspicion')), 0)
           ELSE MAX(-0.25, MIN(0.25, 0.2 * COALESCE(SUM(json_extract(c.deltas_json, '$.suspicion')), 0)))
                + 0.8 * COALESCE(SUM(json_extract(c.deltas_json, '$.suspicion') * 10.0 / (10.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS suspicion,
       MAX(0.0, MIN(1.0, r.attraction + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.attraction')), 0)
           ELSE MAX(-0.3, MIN(0.3, 0.3 * COALESCE(SUM(json_extract(c.deltas_json, '$.attraction')), 0)))
                + 0.7 * COALESCE(SUM(json_extract(c.deltas_json, '$.attraction') * 20.0 / (20.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS attraction,
       MAX(0.0, MIN(1.0, r.familiarity + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.familiarity')), 0)
           ELSE MAX(-0.8, MIN(0.8, 0.6 * COALESCE(SUM(json_extract(c.deltas_json, '$.familiarity')), 0)))
                + 0.4 * COALESCE(SUM(json_extract(c.deltas_json, '$.familiarity') * 120.0 / (120.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS familiarity,
       MAX(0.0, MIN(1.0, 0.0 + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.intimacy')), 0)
           ELSE MAX(-0.4, MIN(0.4, 0.4 * COALESCE(SUM(json_extract(c.deltas_json, '$.intimacy')), 0)))
                + 0.6 * COALESCE(SUM(json_extract(c.deltas_json, '$.intimacy') * 30.0 / (30.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS intimacy,
       MAX(0.0, MIN(1.0, 0.0 + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.commitment')), 0)
           ELSE MAX(-0.4, MIN(0.4, 0.4 * COALESCE(SUM(json_extract(c.deltas_json, '$.commitment')), 0)))
                + 0.6 * COALESCE(SUM(json_extract(c.deltas_json, '$.commitment') * 30.0 / (30.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS commitment,
       MAX(-1.0, MIN(1.0, 0.0 + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.satisfaction')), 0)
           ELSE MAX(-0.3, MIN(0.3, 0.3 * COALESCE(SUM(json_extract(c.deltas_json, '$.satisfaction')), 0)))
                + 0.7 * COALESCE(SUM(json_extract(c.deltas_json, '$.satisfaction') * 20.0 / (20.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS satisfaction,
       MAX(0.0, MIN(1.0, 0.0 + CASE WHEN k.enabled = 0 OR k.now_gt IS NULL OR r.kinship IS NOT NULL OR r.status IN ('spouse','partner') THEN COALESCE(SUM(json_extract(c.deltas_json, '$.jealousy')), 0)
           ELSE MAX(-0.15, MIN(0.15, 0.1 * COALESCE(SUM(json_extract(c.deltas_json, '$.jealousy')), 0)))
                + 0.9 * COALESCE(SUM(json_extract(c.deltas_json, '$.jealousy') * 7.0 / (7.0 + MAX(0.0, k.now_gt - c.game_time - 3.0))), 0) END)) AS jealousy
FROM relationship r
CROSS JOIN fade_clock k
LEFT JOIN relationship_change c
  ON c.from_id = r.from_id AND c.to_id = r.to_id
 AND c.game_time <= (SELECT s.cutoff_gt FROM v_timeline_scope s WHERE s.timeline_id = c.timeline_id)
GROUP BY r.from_id, r.to_id;

-- Standing: the plain sums, as v_standing showed them before this migration.
CREATE VIEW v_standing_raw AS
SELECT c.holder_id, c.subject_id, c.domain,
       MAX(-1.0, MIN(1.0, SUM(c.delta))) AS value,
       COUNT(*) AS evidence,
       SUM(c.firsthand) AS firsthand,
       MAX(c.game_time) AS last_gt
FROM standing_change c
JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt
GROUP BY c.holder_id, c.subject_id, c.domain;

-- Standing, faded: what was seen fades over 40 days, what was only heard over 15.
DROP VIEW v_standing;
CREATE VIEW v_standing AS
SELECT c.holder_id, c.subject_id, c.domain,
       MAX(-1.0, MIN(1.0, CASE WHEN k.enabled = 0 OR k.now_gt IS NULL THEN SUM(c.delta)
           ELSE MAX(-0.4, MIN(0.4, 0.4 * SUM(c.delta)))
                + 0.6 * SUM(c.delta * (CASE WHEN c.firsthand = 1 THEN 40.0 ELSE 15.0 END)
                            / ((CASE WHEN c.firsthand = 1 THEN 40.0 ELSE 15.0 END)
                               + MAX(0.0, k.now_gt - c.game_time - 3.0))) END)) AS value,
       COUNT(*) AS evidence,
       SUM(c.firsthand) AS firsthand,
       MAX(c.game_time) AS last_gt
FROM standing_change c
JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt
CROSS JOIN fade_clock k
GROUP BY c.holder_id, c.subject_id, c.domain;

INSERT INTO schema_version(version, note) VALUES (26, 'relationships and reputations fade without contact');
