-- 022: the active timeline's scope is kept in a table instead of worked out on every read.
-- v_timeline_scope used to walk the timeline chain (one step per save reload that branched) with a recursive
-- query each time any visible-row view was read, and the planner could not use an index on the result. After
-- months of play that chain holds thousands of timelines, and every memory, fact, opinion and hygiene read
-- paid for the walk again. Now the walk runs only when the active timeline changes (a save load), through
-- triggers, and the view reads one small table with a primary key. The rows are the same as before.
CREATE VIEW v_timeline_scope_walk AS
WITH RECURSIVE anc(timeline_id, cutoff_gt) AS (
  SELECT timeline_id, 1e18 FROM timeline WHERE is_active = 1
  UNION ALL
  SELECT t.parent_id, MIN(a.cutoff_gt, t.branch_game_time)
  FROM timeline t JOIN anc a ON t.timeline_id = a.timeline_id
  WHERE t.parent_id IS NOT NULL
)
SELECT timeline_id, cutoff_gt FROM anc;

CREATE TABLE timeline_scope (
  timeline_id INTEGER PRIMARY KEY REFERENCES timeline(timeline_id),
  cutoff_gt   REAL NOT NULL
) STRICT;

INSERT INTO timeline_scope (timeline_id, cutoff_gt) SELECT timeline_id, cutoff_gt FROM v_timeline_scope_walk;

CREATE TRIGGER timeline_scope_on_insert AFTER INSERT ON timeline WHEN NEW.is_active = 1
BEGIN
  DELETE FROM timeline_scope;
  INSERT INTO timeline_scope (timeline_id, cutoff_gt) SELECT timeline_id, cutoff_gt FROM v_timeline_scope_walk;
END;

CREATE TRIGGER timeline_scope_on_update AFTER UPDATE OF is_active, parent_id, branch_game_time ON timeline
BEGIN
  DELETE FROM timeline_scope;
  INSERT INTO timeline_scope (timeline_id, cutoff_gt) SELECT timeline_id, cutoff_gt FROM v_timeline_scope_walk;
END;

CREATE TRIGGER timeline_scope_on_delete AFTER DELETE ON timeline
BEGIN
  DELETE FROM timeline_scope;
  INSERT INTO timeline_scope (timeline_id, cutoff_gt) SELECT timeline_id, cutoff_gt FROM v_timeline_scope_walk;
END;

DROP VIEW v_timeline_scope;
CREATE VIEW v_timeline_scope AS SELECT timeline_id, cutoff_gt FROM timeline_scope;

INSERT INTO schema_version(version, note) VALUES (22, 'active timeline scope kept in a table');
