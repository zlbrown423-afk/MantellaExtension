-- Living World W1: NPC tasks and shared work (livingworld/work/).
--
-- A task is one piece of work someone needs done ("Gerdur needs firewood stacked by the mill").
-- The task row never changes after it is written; everything that happens to it (offered, accepted,
-- progress, paused, completed, a deadline moved) is a task_change row on the save timeline, and
-- v_task reads the current state through v_timeline_scope. Loading an older save therefore rolls a
-- task back to where it was in that save, like relationships and memories.

CREATE TABLE task (
  task_id        INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  template       TEXT NOT NULL,                 -- key in work/data/*.json, e.g. 'mill_firewood'
  giver_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  location       TEXT,
  community_key  TEXT,
  title          TEXT NOT NULL,                 -- 'stack firewood by the mill'
  need_item      TEXT,                          -- 'Firewood' (matched by name, case-insensitive)
  need_count     INTEGER NOT NULL DEFAULT 1 CHECK (need_count >= 1),
  units          TEXT NOT NULL DEFAULT 'loads',
  reward_gold    INTEGER NOT NULL DEFAULT 0 CHECK (reward_gold >= 0),
  size           REAL NOT NULL DEFAULT 0.3 CHECK (size BETWEEN 0 AND 1),   -- how big a favour it is
  deadline_days  REAL,                          -- days from acceptance; NULL = no deadline
  source         TEXT NOT NULL CHECK (source IN ('sim','admin')),
  created_gt     REAL NOT NULL
) STRICT;
CREATE INDEX task_giver ON task(giver_id, created_gt);

CREATE TABLE task_change (
  change_id      INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  task_id        INTEGER NOT NULL REFERENCES task(task_id),
  state          TEXT CHECK (state IS NULL OR state IN
                   ('available','offered','accepted','in_progress','paused','completed','failed','expired')),
  assignee_id    INTEGER REFERENCES npc(npc_id),   -- set when someone takes the task on
  d_progress     REAL NOT NULL DEFAULT 0,          -- units of work added by this change
  deadline_gt    REAL,                             -- set when a deadline is fixed or moved
  mood           INTEGER CHECK (mood IS NULL OR mood BETWEEN 0 AND 10),  -- giver's mood seen while the work was on
  reason         TEXT NOT NULL,                    -- 'offered in conversation', 'declined', 'gave 3 Firewood', ...
  cause_event_id INTEGER REFERENCES world_event(event_id),
  game_time      REAL NOT NULL
) STRICT;
CREATE INDEX task_change_task ON task_change(task_id, game_time);

-- Tasks visible on the active timeline, with their current state, assignee, progress and deadline.
-- is_reset: the developer tool wiped this task from the NPCs' minds (work/dev.py reset); it is ignored.
CREATE VIEW v_task_change AS
SELECT c.* FROM task_change c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt;

CREATE VIEW v_task AS
SELECT t.*,
       COALESCE((SELECT c.state FROM v_task_change c WHERE c.task_id = t.task_id AND c.state IS NOT NULL
                 ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1), 'available') AS state,
       (SELECT c.game_time FROM v_task_change c WHERE c.task_id = t.task_id AND c.state IS NOT NULL
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS state_gt,
       (SELECT c.assignee_id FROM v_task_change c WHERE c.task_id = t.task_id AND c.assignee_id IS NOT NULL
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS assignee_id,
       (SELECT c.deadline_gt FROM v_task_change c WHERE c.task_id = t.task_id AND c.deadline_gt IS NOT NULL
        ORDER BY c.game_time DESC, c.change_id DESC LIMIT 1) AS deadline_gt,
       COALESCE((SELECT SUM(c.d_progress) FROM v_task_change c WHERE c.task_id = t.task_id), 0) AS progress,
       EXISTS (SELECT 1 FROM v_task_change c WHERE c.task_id = t.task_id AND c.reason = 'reset by developer') AS is_reset
FROM task t JOIN v_timeline_scope s ON s.timeline_id = t.timeline_id AND t.created_gt <= s.cutoff_gt;

INSERT INTO schema_version(version, note) VALUES (6, 'work: NPC tasks and shared work');
