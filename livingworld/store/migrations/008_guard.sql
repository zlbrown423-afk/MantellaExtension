-- Living World G1: guard careers (livingworld/guard/).
--
-- A service is one enlistment of one person in one hold's guard. The service row never changes after
-- it is written; promotions, probation ending, suspension, dismissal and merit are service_change rows
-- on the save timeline, and v_service reads the current state through v_timeline_scope. Loading an older
-- save therefore rolls rank and status back to that save, like tasks and relationships.

CREATE TABLE service (
  service_id     INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  org_key        TEXT NOT NULL,                  -- key in guard/data/orgs.json, e.g. 'whiterun_guard'
  member_id      INTEGER NOT NULL REFERENCES npc(npc_id),
  path           TEXT NOT NULL CHECK (path IN ('applied','invited','thane','sponsor','earned','admin')),
  recruiter_id   INTEGER REFERENCES npc(npc_id),
  sponsor_id     INTEGER REFERENCES npc(npc_id),
  location       TEXT,
  joined_gt      REAL NOT NULL
) STRICT;
CREATE INDEX service_member ON service(member_id, joined_gt);

CREATE TABLE service_change (
  change_id      INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  service_id     INTEGER NOT NULL REFERENCES service(service_id),
  rank_key       TEXT,                           -- set when the rank changes
  status         TEXT CHECK (status IS NULL OR status IN
                   ('probation','active','suspended','discharged','dismissed','resigned')),
  d_merit        REAL NOT NULL DEFAULT 0,
  reason         TEXT NOT NULL,
  cause_event_id INTEGER REFERENCES world_event(event_id),
  game_time      REAL NOT NULL
) STRICT;
CREATE INDEX service_change_service ON service_change(service_id, game_time);

CREATE VIEW v_service_change AS
SELECT c.* FROM service_change c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt;

-- is_reset: the developer tool wiped this service from the NPCs' minds (guard/dev.py reset); it is ignored.
CREATE VIEW v_service AS
SELECT sv.*,
       (SELECT c.rank_key FROM v_service_change c WHERE c.service_id = sv.service_id AND c.rank_key IS NOT NULL
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS rank_key,
       (SELECT c.game_time FROM v_service_change c WHERE c.service_id = sv.service_id AND c.rank_key IS NOT NULL
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS rank_gt,
       (SELECT c.status FROM v_service_change c WHERE c.service_id = sv.service_id AND c.status IS NOT NULL
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS status,
       (SELECT c.game_time FROM v_service_change c WHERE c.service_id = sv.service_id AND c.status IS NOT NULL
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS status_gt,
       COALESCE((SELECT SUM(c.d_merit) FROM v_service_change c WHERE c.service_id = sv.service_id), 0) AS merit,
       EXISTS (SELECT 1 FROM v_service_change c WHERE c.service_id = sv.service_id
               AND c.reason = 'reset by developer') AS is_reset
FROM service sv JOIN v_timeline_scope s ON s.timeline_id = sv.timeline_id AND sv.joined_gt <= s.cutoff_gt;

-- Guards the player has met: which hold's guard they belong to and, for generic guards who all share
-- one name in game ("Whiterun Guard"), the personal name they go by. Not on the timeline: a name, like a
-- voice, stays with the person.
CREATE TABLE guard_profile (
  npc_id         INTEGER PRIMARY KEY REFERENCES npc(npc_id),
  org_key        TEXT NOT NULL,
  given_name     TEXT,                           -- NULL for named NPCs (Commander Caius keeps his own)
  traits_json    TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(traits_json)),
  first_seen_gt  REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX guard_profile_name ON guard_profile(org_key, given_name) WHERE given_name IS NOT NULL;

INSERT INTO schema_version(version, note) VALUES (8, 'guard: service record and guard profiles');
