-- Reputation layers (S2): what each person thinks of someone in each area of life (standing), the
-- opinions they form from it (belief, read through v_belief), what a community and a faction think
-- (computed, never stored), and the hints the game has already shown the player.
--
-- Every row is on the save timeline, so loading an older save rolls reputations back with it.

-- One person's change of view of another in one domain, from something they saw or heard.
-- domain is open text so later systems can add their own ('trade' for professional standing):
--   generosity, honesty, reliability, gentleness, decency, lawfulness, standing, trade
-- Positive is always the good end (gentleness < 0 means violent, lawfulness < 0 means criminal).
CREATE TABLE standing_change (
  change_id      INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  holder_id      INTEGER NOT NULL REFERENCES npc(npc_id),
  subject_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  domain         TEXT NOT NULL,
  delta          REAL NOT NULL CHECK (delta BETWEEN -1 AND 1),
  firsthand      INTEGER NOT NULL DEFAULT 0 CHECK (firsthand IN (0,1)),
  claim_id       INTEGER REFERENCES claim(claim_id),
  cause_event_id INTEGER REFERENCES world_event(event_id),
  reason         TEXT NOT NULL DEFAULT '',
  game_time      REAL NOT NULL
) STRICT;
CREATE INDEX standing_pair ON standing_change(holder_id, subject_id, domain);
CREATE INDEX standing_subject ON standing_change(subject_id, domain);

-- Personal standing: one holder's summed view of one subject in one domain, on the active timeline.
CREATE VIEW v_standing AS
SELECT c.holder_id, c.subject_id, c.domain,
       MAX(-1.0, MIN(1.0, SUM(c.delta))) AS value,
       COUNT(*) AS evidence,
       SUM(c.firsthand) AS firsthand,
       MAX(c.game_time) AS last_gt
FROM standing_change c
JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt
GROUP BY c.holder_id, c.subject_id, c.domain;

-- Beliefs (the existing belief table): an opinion formed when a standing passes a threshold. A belief is
-- never edited: a changed or dropped opinion is a newer row for the same holder, subject and topic
-- (statement '' when it is no longer held). This view shows the newest one on the active timeline.
CREATE INDEX belief_key ON belief(holder_id, about_id, topic, formed_gt);
CREATE VIEW v_belief AS
SELECT b.* FROM belief b
JOIN v_timeline_scope s ON s.timeline_id = b.timeline_id AND b.formed_gt <= s.cutoff_gt
WHERE b.statement <> ''
  AND b.belief_id = (SELECT b2.belief_id FROM belief b2
                     JOIN v_timeline_scope s2 ON s2.timeline_id = b2.timeline_id AND b2.formed_gt <= s2.cutoff_gt
                     WHERE b2.holder_id = b.holder_id AND b2.about_id IS b.about_id AND b2.topic IS b.topic
                     ORDER BY b2.formed_gt DESC, b2.belief_id DESC LIMIT 1);

-- How much a community cares about each domain (weight 0..3, 1 = normal). Guards weigh lawfulness
-- heavily; a thieves' guild would not. Communities with no rows weigh every domain 1.
CREATE TABLE community_stance (
  community_id INTEGER NOT NULL REFERENCES community(community_id),
  domain       TEXT NOT NULL,
  weight       REAL NOT NULL CHECK (weight BETWEEN 0 AND 3),
  PRIMARY KEY (community_id, domain)
) STRICT;

-- Community reputation: the influence-weighted average of what members who hold a view think, per domain.
-- 'regard' is overall liking and trust from v_relationship ((trust + affinity + respect) / 3).
-- reach = share of the community's influence that holds any view at all (0 = nobody has heard of them).
CREATE VIEW v_community_reputation AS
WITH members AS (
  SELECT m.community_id, m.npc_id, m.influence FROM membership m JOIN npc n ON n.npc_id = m.npc_id
  WHERE n.is_child = 0 AND n.is_player = 0 AND m.influence > 0
), totals AS (
  SELECT community_id, SUM(influence) AS total FROM members GROUP BY community_id
), views AS (
  SELECT holder_id, subject_id, domain, value FROM v_standing
  UNION ALL
  SELECT from_id, to_id, 'regard', (trust + affinity + respect) / 3.0 FROM v_relationship
)
SELECT mb.community_id, v.subject_id, v.domain,
       SUM(mb.influence * v.value) / SUM(mb.influence) AS value,
       SUM(mb.influence) / t.total AS reach,
       COUNT(*) AS holders
FROM members mb
JOIN views v ON v.holder_id = mb.npc_id AND v.subject_id <> mb.npc_id
JOIN totals t ON t.community_id = mb.community_id
GROUP BY mb.community_id, v.subject_id, v.domain;

-- Faction reputation: one weighted score per faction or guild, each domain counted by the faction's
-- stance. Kept apart from Skyrim's own bounty, which stays the game's business.
CREATE VIEW v_faction_reputation AS
SELECT r.community_id, c.key AS community_key, r.subject_id,
       SUM(r.value * COALESCE(st.weight, 1.0)) / SUM(COALESCE(st.weight, 1.0)) AS score,
       MAX(r.reach) AS reach
FROM v_community_reputation r
JOIN community c ON c.community_id = r.community_id AND c.kind IN ('faction','guild')
LEFT JOIN community_stance st ON st.community_id = r.community_id AND st.domain = r.domain
WHERE COALESCE(st.weight, 1.0) > 0
GROUP BY r.community_id, r.subject_id;

-- Hints the game has shown the player (MANTELLA_LW_INFO_LEVEL), so none repeats or goes beyond the level.
CREATE TABLE player_notice (
  notice_id   INTEGER PRIMARY KEY,
  timeline_id INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  claim_id    INTEGER REFERENCES claim(claim_id),
  event_id    INTEGER REFERENCES world_event(event_id),
  level       TEXT NOT NULL CHECK (level IN ('balanced','informative')),
  kind        TEXT NOT NULL DEFAULT 'hint' CHECK (kind IN ('hint','summary')),
  community   TEXT,
  shown_gt    REAL NOT NULL,
  text        TEXT NOT NULL
) STRICT;
CREATE INDEX player_notice_claim ON player_notice(claim_id);
CREATE VIEW v_player_notice AS
SELECT p.* FROM player_notice p JOIN v_timeline_scope s ON s.timeline_id = p.timeline_id AND p.shown_gt <= s.cutoff_gt;

-- Each retelling of a claim may grow or shrink it; witnesses may later set it straight.
-- gossip_exchange payloads carry {"claim_id", "distorted": d} and corrections are 'gossip_correction' events.

INSERT INTO schema_version(version, note) VALUES (9, 'reputation: standing by domain, beliefs, community and faction views, player notices');
