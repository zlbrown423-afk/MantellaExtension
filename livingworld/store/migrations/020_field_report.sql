-- 020: field reports (livingworld/spatial/). What the player's party ran into while someone had an assignment,
-- set against where that assignment pointed. Bookkeeping only: it never changes a task or any quest.
CREATE TABLE field_report (
  report_id    INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  event_id     INTEGER REFERENCES world_event(event_id),
  member_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  giver_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  task_id      INTEGER,
  duty_title   TEXT NOT NULL,
  assigned     TEXT NOT NULL,                 -- the places the assignment named, '; ' separated
  location     TEXT,
  relation     TEXT NOT NULL CHECK (relation IN ('assigned','open_road','wilds','elsewhere')),
  kind         TEXT NOT NULL DEFAULT 'fight',
  game_time    REAL NOT NULL,
  told_gt      REAL
) STRICT;
CREATE INDEX field_report_giver ON field_report(giver_id, told_gt, game_time);
CREATE UNIQUE INDEX field_report_once ON field_report(event_id, member_id) WHERE event_id IS NOT NULL;

INSERT INTO schema_version(version, note) VALUES (20, 'field reports: encounters set against assignments');
