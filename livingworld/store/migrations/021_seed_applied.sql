-- 021: what the seed files have already written into this world (livingworld/social/seed.py).
-- A seed entry is written once and never again: before this, the seed was written back over the world at
-- every server start, which could undo changes made in play (a romance status, for one). One row per person
-- (traits), per membership and per relationship. The worlds that exist now already hold what the old seed
-- wrote, so those rows are marked as written (applied_gt 0) and stay as they are.
CREATE TABLE seed_applied (
  kind        TEXT NOT NULL CHECK (kind IN ('person', 'membership', 'relationship')),
  key         TEXT NOT NULL,          -- person: npc_id; membership: npc_id:community key; relationship: from_id:to_id
  applied_gt  REAL NOT NULL,
  PRIMARY KEY (kind, key)
) STRICT;

INSERT OR IGNORE INTO seed_applied (kind, key, applied_gt)
SELECT 'membership', m.npc_id || ':' || c.key, 0
  FROM membership m JOIN community c ON c.community_id = m.community_id
 WHERE c.key IN ('riverwood', 'riverwood_mill', 'riverwood_trader', 'alvor_house', 'sven_house', 'sleeping_giant');

INSERT OR IGNORE INTO seed_applied (kind, key, applied_gt)
SELECT 'person', m.npc_id, 0
  FROM membership m JOIN community c ON c.community_id = m.community_id
 WHERE c.key = 'riverwood';

INSERT OR IGNORE INTO seed_applied (kind, key, applied_gt)
SELECT 'relationship', r.from_id || ':' || r.to_id, 0
  FROM relationship r
 WHERE r.from_id IN (SELECT m.npc_id FROM membership m JOIN community c ON c.community_id = m.community_id WHERE c.key = 'riverwood')
   AND r.to_id   IN (SELECT m.npc_id FROM membership m JOIN community c ON c.community_id = m.community_id WHERE c.key = 'riverwood');

INSERT INTO schema_version(version, note) VALUES (21, 'seed entries are written once per world');
