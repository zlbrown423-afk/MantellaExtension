-- 023: a conversation picked back up after a save load (livingworld/continuity/resume.py).
-- One row per resumed conversation: which conversation it continues, and how many of that conversation's
-- lines were copied into it (0 when the old conversation's memories are still visible, so nothing is
-- extracted twice). Nothing in the old conversation is changed.
CREATE TABLE conversation_resume (
  conv_id          INTEGER PRIMARY KEY REFERENCES conversation(conv_id),
  resumed_conv_id  INTEGER NOT NULL REFERENCES conversation(conv_id),
  lines_copied     INTEGER NOT NULL DEFAULT 0
) STRICT;

INSERT INTO schema_version(version, note) VALUES (23, 'conversations picked back up after a save load');
