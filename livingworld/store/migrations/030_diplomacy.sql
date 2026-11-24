-- 030: diplomacy (livingworld/diplomacy/): what was said about the war and the Thalmor, who knows it, what each
-- person believes, what was promised, and the meetings people propose.
--
-- The rules here only remember; they never decide. A statement is a line someone actually said (validated against
-- the utterance table); a position is one person's view, kept apart from what they say in public; a commitment's
-- and an invitation's status only change from the person's own words or from game time passing.
--
-- New tables only: nothing existing is altered. Every row is on the save timeline, and every status is an
-- append-only change row read through a view, so loading an older save shows the politics as they were then.

-- One change to one person's view of one dimension, in one layer.
--   dimension: empire, stormcloaks, thalmor, negotiation, concordat, ceasefire, alliance
--   layer: personal (what they believe) or official (the line their faction holds, as they understand it)
CREATE TABLE political_position (
  pos_id       INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  dimension    TEXT NOT NULL,
  layer        TEXT NOT NULL CHECK (layer IN ('personal','official')),
  value        REAL NOT NULL CHECK (value BETWEEN -1 AND 1),
  confidence   REAL NOT NULL DEFAULT 0.7 CHECK (confidence BETWEEN 0 AND 1),
  source       TEXT NOT NULL CHECK (source IN ('seed','statement','admin')),
  stmt_id      INTEGER REFERENCES dip_statement(stmt_id),
  reason       TEXT NOT NULL DEFAULT '',
  game_time    REAL NOT NULL
) STRICT;
CREATE INDEX political_position_npc ON political_position(npc_id, dimension, layer, game_time);

CREATE VIEW v_political_position AS
SELECT p.* FROM political_position p
JOIN v_timeline_scope s ON s.timeline_id = p.timeline_id AND p.game_time <= s.cutoff_gt
WHERE p.pos_id = (SELECT p2.pos_id FROM political_position p2
                  JOIN v_timeline_scope s2 ON s2.timeline_id = p2.timeline_id AND p2.game_time <= s2.cutoff_gt
                  WHERE p2.npc_id = p.npc_id AND p2.dimension = p.dimension AND p2.layer = p.layer
                  ORDER BY p2.game_time DESC, p2.pos_id DESC LIMIT 1);

-- One diplomatic thing said on one line of a conversation. speaker_id said the line; subject_id is whose view,
-- proposal or promise it is (the same person unless the line reports someone else); target_id is who it is
-- addressed to or about (the invitee of an invitation, the creditor of a promise). A report about someone never met
-- (no npc row yet) keeps subject_id = the speaker and the person's name in about_name.
CREATE TABLE dip_statement (
  stmt_id      INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  conv_id      INTEGER NOT NULL REFERENCES conversation(conv_id),
  utt_id       INTEGER NOT NULL REFERENCES utterance(utt_id),
  speaker_id   INTEGER NOT NULL REFERENCES npc(npc_id),
  subject_id   INTEGER NOT NULL REFERENCES npc(npc_id),
  target_id    INTEGER REFERENCES npc(npc_id),
  about_name   TEXT,                            -- a report about someone Living World has not met yet, by name
  kind         TEXT NOT NULL CHECK (kind IN ('proposal','position','support','opposition','condition','commitment',
                                              'refusal','request','invitation','invitation_response',
                                              'meeting_location','goodwill','retraction','fulfilment',
                                              'disagreement','agreement','report')),
  issue        TEXT NOT NULL DEFAULT 'other',
  factions_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(factions_json)),
  stance       REAL CHECK (stance IS NULL OR stance BETWEEN -1 AND 1),
  modality     TEXT NOT NULL CHECK (modality IN ('asserted','conditional','hypothetical','question','reported','rumor')),
  audience     TEXT NOT NULL CHECK (audience IN ('private','public')),
  response     TEXT CHECK (response IS NULL OR response IN ('accepted','conditional','declined','countered')),
  summary      TEXT NOT NULL,
  quote        TEXT NOT NULL DEFAULT '',
  condition    TEXT,
  venue        TEXT,
  due_days     REAL,
  confidence   REAL NOT NULL DEFAULT 0.8 CHECK (confidence BETWEEN 0 AND 1),
  meeting_id   INTEGER REFERENCES dip_meeting(meeting_id),
  claim_id     INTEGER REFERENCES claim(claim_id),
  game_time    REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX dip_statement_once ON dip_statement(utt_id, kind, issue, subject_id);
CREATE INDEX dip_statement_subject ON dip_statement(subject_id, issue, game_time);
CREATE INDEX dip_statement_conv ON dip_statement(conv_id);

CREATE VIEW v_dip_statement AS
SELECT d.* FROM dip_statement d JOIN v_timeline_scope s ON s.timeline_id = d.timeline_id AND d.game_time <= s.cutoff_gt;

-- One person knowing one statement, and how: they said it, heard it said (witnessed), were told it by someone
-- else, got it by courier, or picked it up as a rumour.
CREATE TABLE dip_awareness (
  aware_id     INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  holder_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  stmt_id      INTEGER NOT NULL REFERENCES dip_statement(stmt_id),
  origin       TEXT NOT NULL CHECK (origin IN ('said','witnessed','told','courier','rumor')),
  told_by_id   INTEGER REFERENCES npc(npc_id),
  know_id      INTEGER REFERENCES knowledge(know_id),
  confidence   REAL NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  learned_gt   REAL NOT NULL,
  CHECK ((origin IN ('told','courier','rumor')) = (told_by_id IS NOT NULL))
) STRICT;
CREATE UNIQUE INDEX dip_awareness_once ON dip_awareness(holder_id, stmt_id);
CREATE INDEX dip_awareness_stmt ON dip_awareness(stmt_id);

CREATE VIEW v_dip_awareness AS
SELECT a.* FROM dip_awareness a JOIN v_timeline_scope s ON s.timeline_id = a.timeline_id AND a.learned_gt <= s.cutoff_gt;

-- A promise one person made to another (the player included), and every change of its status.
CREATE TABLE dip_commitment (
  commit_id    INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  stmt_id      INTEGER NOT NULL REFERENCES dip_statement(stmt_id),
  debtor_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  creditor_id  INTEGER REFERENCES npc(npc_id),
  issue        TEXT NOT NULL,
  description  TEXT NOT NULL,
  condition    TEXT,
  due_gt       REAL,
  audience     TEXT NOT NULL CHECK (audience IN ('private','public')),
  meeting_id   INTEGER REFERENCES dip_meeting(meeting_id),
  made_gt      REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX dip_commitment_once ON dip_commitment(stmt_id);
CREATE INDEX dip_commitment_debtor ON dip_commitment(debtor_id, issue);

CREATE TABLE dip_commitment_change (
  change_id    INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  commit_id    INTEGER NOT NULL REFERENCES dip_commitment(commit_id),
  status       TEXT NOT NULL CHECK (status IN ('open','kept','withdrawn','lapsed','broken')),
  reason       TEXT NOT NULL,
  stmt_id      INTEGER REFERENCES dip_statement(stmt_id),
  game_time    REAL NOT NULL
) STRICT;
CREATE INDEX dip_commitment_change_commit ON dip_commitment_change(commit_id, game_time);

CREATE VIEW v_dip_commitment AS
SELECT c.*,
       (SELECT x.status FROM dip_commitment_change x JOIN v_timeline_scope s ON s.timeline_id = x.timeline_id
          AND x.game_time <= s.cutoff_gt WHERE x.commit_id = c.commit_id
        ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1) AS status,
       (SELECT x.game_time FROM dip_commitment_change x JOIN v_timeline_scope s ON s.timeline_id = x.timeline_id
          AND x.game_time <= s.cutoff_gt WHERE x.commit_id = c.commit_id
        ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1) AS status_gt
FROM dip_commitment c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.made_gt <= s.cutoff_gt;

-- A meeting someone proposed, and every change of its status.
CREATE TABLE dip_meeting (
  meeting_id     INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  venue          TEXT NOT NULL,                  -- as said: 'Dragonsreach'
  venue_key      TEXT NOT NULL,                  -- normalised: 'dragonsreach'
  purpose        TEXT NOT NULL DEFAULT '',
  proposed_by_id INTEGER NOT NULL REFERENCES npc(npc_id),
  host_id        INTEGER REFERENCES npc(npc_id),
  stmt_id        INTEGER REFERENCES dip_statement(stmt_id),
  proposed_gt    REAL NOT NULL
) STRICT;
CREATE INDEX dip_meeting_venue ON dip_meeting(venue_key, proposed_gt);

CREATE TABLE dip_meeting_change (
  change_id    INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  meeting_id   INTEGER NOT NULL REFERENCES dip_meeting(meeting_id),
  status       TEXT NOT NULL CHECK (status IN ('proposed','agreed','convened','concluded','abandoned')),
  conv_id      INTEGER REFERENCES conversation(conv_id),
  reason       TEXT NOT NULL,
  game_time    REAL NOT NULL
) STRICT;
CREATE INDEX dip_meeting_change_meeting ON dip_meeting_change(meeting_id, game_time);

CREATE VIEW v_dip_meeting AS
SELECT m.*,
       (SELECT x.status FROM dip_meeting_change x JOIN v_timeline_scope s ON s.timeline_id = x.timeline_id
          AND x.game_time <= s.cutoff_gt WHERE x.meeting_id = m.meeting_id
        ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1) AS status,
       (SELECT x.conv_id FROM dip_meeting_change x JOIN v_timeline_scope s ON s.timeline_id = x.timeline_id
          AND x.game_time <= s.cutoff_gt WHERE x.meeting_id = m.meeting_id
        ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1) AS status_conv_id
FROM dip_meeting m JOIN v_timeline_scope s ON s.timeline_id = m.timeline_id AND m.proposed_gt <= s.cutoff_gt;

-- One invitee of one meeting, and every answer they give (only their own words change it).
CREATE TABLE dip_invitation (
  inv_id         INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  meeting_id     INTEGER NOT NULL REFERENCES dip_meeting(meeting_id),
  invitee_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  invited_by_id  INTEGER NOT NULL REFERENCES npc(npc_id),
  stmt_id        INTEGER REFERENCES dip_statement(stmt_id),
  invited_gt     REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX dip_invitation_once ON dip_invitation(meeting_id, invitee_id);

CREATE TABLE dip_invitation_change (
  change_id      INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  inv_id         INTEGER NOT NULL REFERENCES dip_invitation(inv_id),
  status         TEXT NOT NULL CHECK (status IN ('pending','delivered','accepted','conditional','declined',
                                                 'countered','withdrawn')),
  conditions     TEXT,
  counter_venue  TEXT,
  stmt_id        INTEGER REFERENCES dip_statement(stmt_id),
  game_time      REAL NOT NULL
) STRICT;
CREATE INDEX dip_invitation_change_inv ON dip_invitation_change(inv_id, game_time);

CREATE VIEW v_dip_invitation AS
SELECT i.*,
       (SELECT x.status FROM dip_invitation_change x JOIN v_timeline_scope s ON s.timeline_id = x.timeline_id
          AND x.game_time <= s.cutoff_gt WHERE x.inv_id = i.inv_id
        ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1) AS status,
       (SELECT x.conditions FROM dip_invitation_change x JOIN v_timeline_scope s ON s.timeline_id = x.timeline_id
          AND x.game_time <= s.cutoff_gt WHERE x.inv_id = i.inv_id
        ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1) AS conditions,
       (SELECT x.counter_venue FROM dip_invitation_change x JOIN v_timeline_scope s ON s.timeline_id = x.timeline_id
          AND x.game_time <= s.cutoff_gt WHERE x.inv_id = i.inv_id
        ORDER BY x.game_time DESC, x.change_id DESC LIMIT 1) AS counter_venue
FROM dip_invitation i JOIN v_timeline_scope s ON s.timeline_id = i.timeline_id AND i.invited_gt <= s.cutoff_gt;

-- Who was there when a meeting convened.
CREATE TABLE dip_attendance (
  att_id       INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  meeting_id   INTEGER NOT NULL REFERENCES dip_meeting(meeting_id),
  npc_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  status       TEXT NOT NULL CHECK (status IN ('present','absent')),
  conv_id      INTEGER REFERENCES conversation(conv_id),
  game_time    REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX dip_attendance_once ON dip_attendance(meeting_id, npc_id, conv_id);

CREATE VIEW v_dip_attendance AS
SELECT a.* FROM dip_attendance a JOIN v_timeline_scope s ON s.timeline_id = a.timeline_id AND a.game_time <= s.cutoff_gt;

-- A consequence scheduled for later game time (a courier carrying an answer). Delivered when its world_event exists.
CREATE TABLE dip_followup (
  fu_id        INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  key          TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('courier')),
  stmt_id      INTEGER NOT NULL REFERENCES dip_statement(stmt_id),
  from_id      INTEGER NOT NULL REFERENCES npc(npc_id),
  to_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  due_gt       REAL NOT NULL,
  created_gt   REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX dip_followup_key ON dip_followup(timeline_id, key);

CREATE VIEW v_dip_followup AS
SELECT f.* FROM dip_followup f JOIN v_timeline_scope s ON s.timeline_id = f.timeline_id AND f.created_gt <= s.cutoff_gt;

INSERT INTO schema_version(version, note) VALUES (30, 'diplomacy: statements, awareness, positions, commitments, meetings');
