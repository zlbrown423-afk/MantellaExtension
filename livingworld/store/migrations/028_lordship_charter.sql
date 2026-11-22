-- 028: lordship L1, the charter (livingworld/lordship/).
--
-- A charter is the Jarl's written agreement with a company (the Plains Wardens): terms, territory and powers.
-- The row never changes; renegotiation writes a new version whose parent is the old one. Status (active, under
-- review, suspended...) is a charter_change row. Every 30 game days the monthly reckoning writes one compliance
-- row per term, one influence_snapshot, and an official_stance row for each official whose view moved.
-- All on the save timeline, so an older save shows the charter, money and opinions as they were then.

CREATE TABLE charter (
  charter_id        INTEGER PRIMARY KEY,
  timeline_id       INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  grantor           TEXT NOT NULL,              -- institution key, 'whiterun_court'
  holder            TEXT NOT NULL,              -- institution key, 'plains_wardens'
  holder_npc_id     INTEGER NOT NULL REFERENCES npc(npc_id),   -- the person who signed for the holder
  version           INTEGER NOT NULL DEFAULT 1,
  parent_charter_id INTEGER REFERENCES charter(charter_id),
  level             INTEGER NOT NULL CHECK (level BETWEEN 1 AND 5),
  district_key      TEXT,
  terms_json        TEXT NOT NULL CHECK (json_valid(terms_json)),
  signed_gt         REAL NOT NULL,
  review_gt         REAL,
  conv_id           INTEGER REFERENCES conversation(conv_id),
  source            TEXT NOT NULL DEFAULT 'game' CHECK (source IN ('game','admin','negotiation'))
) STRICT;
CREATE INDEX charter_holder ON charter(holder, signed_gt);

CREATE TABLE charter_change (
  change_id      INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  charter_id     INTEGER NOT NULL REFERENCES charter(charter_id),
  status         TEXT NOT NULL CHECK (status IN ('proposed','active','under_review','suspended','revoked',
                                                 'expired','superseded','repudiated')),
  reason         TEXT NOT NULL,
  cause_event_id INTEGER REFERENCES world_event(event_id),
  game_time      REAL NOT NULL
) STRICT;
CREATE INDEX charter_change_charter ON charter_change(charter_id, game_time);

CREATE VIEW v_charter_change AS
SELECT c.* FROM charter_change c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt;

CREATE VIEW v_charter AS
SELECT ch.*,
       (SELECT c.status FROM v_charter_change c WHERE c.charter_id = ch.charter_id
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS status,
       (SELECT c.game_time FROM v_charter_change c WHERE c.charter_id = ch.charter_id
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS status_gt
FROM charter ch JOIN v_timeline_scope s ON s.timeline_id = ch.timeline_id AND ch.signed_gt <= s.cutoff_gt;

-- One row per term per reckoning period (period = 1, 2, 3... counted from the charter's signing).
CREATE TABLE compliance (
  comp_id        INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  charter_id     INTEGER NOT NULL REFERENCES charter(charter_id),
  period         INTEGER NOT NULL,
  term_key       TEXT NOT NULL,                 -- 'personnel', 'patrols', 'report'
  required       REAL,
  delivered      REAL,
  outcome        TEXT NOT NULL CHECK (outcome IN ('met','short','missed','excused')),
  note           TEXT NOT NULL DEFAULT '',
  game_time      REAL NOT NULL                  -- the end of the period
) STRICT;
CREATE INDEX compliance_charter ON compliance(charter_id, period);

CREATE VIEW v_compliance AS
SELECT c.* FROM compliance c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt;

-- The seven measures, power, legitimacy and the gap, once per period (a cache: rebuilt by the next reckoning).
CREATE TABLE influence_snapshot (
  snap_id        INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  charter_id     INTEGER NOT NULL REFERENCES charter(charter_id),
  period         INTEGER NOT NULL,
  measures_json  TEXT NOT NULL CHECK (json_valid(measures_json)),
  power          REAL NOT NULL,
  legitimacy     REAL NOT NULL,
  gap            REAL NOT NULL,
  band           TEXT NOT NULL CHECK (band IN ('none','small','medium','large','extreme')),
  game_time      REAL NOT NULL
) STRICT;
CREATE INDEX influence_snapshot_charter ON influence_snapshot(charter_id, period);

CREATE VIEW v_influence_snapshot AS
SELECT i.* FROM influence_snapshot i JOIN v_timeline_scope s ON s.timeline_id = i.timeline_id AND i.game_time <= s.cutoff_gt;

-- How an official sees a company: written only when the position or score moves.
CREATE TABLE official_stance (
  stance_id      INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id         INTEGER NOT NULL REFERENCES npc(npc_id),
  official_key   TEXT NOT NULL,                 -- key in lordship/data/court.json
  subject        TEXT NOT NULL,                 -- institution key, 'plains_wardens'
  position       TEXT NOT NULL CHECK (position IN ('champion','support','wary','oppose','hostile')),
  score          REAL NOT NULL CHECK (score BETWEEN -1 AND 1),
  reasons_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(reasons_json)),
  move           TEXT,                          -- the political move this official made this period, if any
  period         INTEGER,
  game_time      REAL NOT NULL
) STRICT;
CREATE INDEX official_stance_npc ON official_stance(npc_id, subject, game_time);

CREATE VIEW v_official_stance AS
SELECT o.* FROM official_stance o JOIN v_timeline_scope s ON s.timeline_id = o.timeline_id AND o.game_time <= s.cutoff_gt;

CREATE VIEW v_official_stance_now AS
SELECT o.* FROM v_official_stance o
WHERE o.stance_id = (SELECT o2.stance_id FROM v_official_stance o2 WHERE o2.npc_id = o.npc_id AND o2.subject = o.subject
                     ORDER BY o2.game_time DESC, o2.stance_id DESC LIMIT 1);

INSERT INTO schema_version(version, note) VALUES (28, 'lordship L1: charter, compliance, influence, official stance');
