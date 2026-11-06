PRAGMA foreign_keys = ON;

-- ===== Conventions / ops =====
CREATE TABLE schema_version (
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  note        TEXT
) STRICT;

-- ===== Save timelines (supports "remember across reloads" OR "rewind on older save") =====
CREATE TABLE timeline (
  timeline_id      INTEGER PRIMARY KEY,
  parent_id        INTEGER REFERENCES timeline(timeline_id),
  branch_game_time REAL,                    -- game time of the save that was loaded
  save_name        TEXT,
  is_active        INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0,1)),
  created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  CHECK ((parent_id IS NULL) = (branch_game_time IS NULL))
) STRICT;
CREATE UNIQUE INDEX one_active_timeline ON timeline(is_active) WHERE is_active = 1;
INSERT INTO timeline (timeline_id, save_name, is_active) VALUES (1, 'main', 1);

-- ===== Identity =====
CREATE TABLE npc (
  npc_id        INTEGER PRIMARY KEY,
  plugin        TEXT NOT NULL,              -- 'Skyrim.esm'
  base_form_id  TEXT NOT NULL,              -- '0x0001347B' (load-order-free, plugin-relative)
  ref_form_id   TEXT,                       -- placed reference, if unique
  display_name  TEXT NOT NULL,
  race          TEXT,
  sex           TEXT CHECK (sex IN ('male','female')),
  is_player     INTEGER NOT NULL DEFAULT 0 CHECK (is_player IN (0,1)),
  is_generic    INTEGER NOT NULL DEFAULT 0 CHECK (is_generic IN (0,1)),
  home_location TEXT,
  sim_tier      TEXT NOT NULL DEFAULT 'distant' CHECK (sim_tier IN ('active','nearby','distant','dormant')),
  created_gt    REAL NOT NULL,
  UNIQUE (plugin, base_form_id, ref_form_id)
) STRICT;
CREATE UNIQUE INDEX one_player ON npc(is_player) WHERE is_player = 1;

CREATE TABLE voice_binding (
  npc_id        INTEGER PRIMARY KEY REFERENCES npc(npc_id),
  tts_service   TEXT NOT NULL,              -- 'xvasynth' | 'piper' | 'xtts'
  voice_model   TEXT NOT NULL,              -- e.g. 'femaleeventoned'
  game_voice_type TEXT,                     -- vanilla VoiceType EditorID
  locked        INTEGER NOT NULL DEFAULT 1 CHECK (locked IN (0,1)),
  bound_gt      REAL NOT NULL
) STRICT;

CREATE TABLE persona_version (
  persona_id    INTEGER PRIMARY KEY,
  npc_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  version       INTEGER NOT NULL,
  bio           TEXT NOT NULL,
  traits_json   TEXT NOT NULL CHECK (json_valid(traits_json)),
  speech_style  TEXT,
  source        TEXT NOT NULL CHECK (source IN ('mantella_csv','author','consolidation','admin')),
  created_gt    REAL NOT NULL,
  UNIQUE (npc_id, version)
) STRICT;

CREATE TABLE emotion_state (
  npc_id        INTEGER PRIMARY KEY REFERENCES npc(npc_id),
  mood_json     TEXT NOT NULL CHECK (json_valid(mood_json)),  -- {"joy":0.2,"anger":0,...} each 0..1
  baseline_json TEXT NOT NULL CHECK (json_valid(baseline_json)),
  updated_gt    REAL NOT NULL
) STRICT;

-- ===== (1) Raw conversation logs =====
CREATE TABLE conversation (
  conv_id       INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  location      TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('player','npc_npc','radiant','letter')),
  started_gt    REAL NOT NULL,
  ended_gt      REAL,
  started_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE participant (
  conv_id       INTEGER NOT NULL REFERENCES conversation(conv_id),
  npc_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  joined_seq    INTEGER NOT NULL DEFAULT 0,
  left_seq      INTEGER,
  PRIMARY KEY (conv_id, npc_id)
) STRICT;

CREATE TABLE utterance (
  utt_id        INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  conv_id       INTEGER NOT NULL REFERENCES conversation(conv_id),
  seq           INTEGER NOT NULL,
  speaker_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  addressee_id  INTEGER REFERENCES npc(npc_id),
  text          TEXT NOT NULL,
  voice_model   TEXT,                       -- what TTS actually used; NULL for player
  game_time     REAL NOT NULL,
  UNIQUE (conv_id, seq)
) STRICT;

-- ===== (2) Objective world events =====
CREATE TABLE world_event (
  event_id      INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  type          TEXT NOT NULL,              -- 'player_left_location','crime','death','sim_tick',...
  location      TEXT,
  actors_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(actors_json)),
  payload_json  TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
  source        TEXT NOT NULL CHECK (source IN ('game','sim','admin')),
  game_time     REAL NOT NULL
) STRICT;

CREATE TABLE event_witness (
  event_id      INTEGER NOT NULL REFERENCES world_event(event_id),
  npc_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  how           TEXT NOT NULL CHECK (how IN ('saw','heard','participated','found_evidence')),
  PRIMARY KEY (event_id, npc_id)
) STRICT;

-- ===== (5)+(8) Knowledge: personal facts, preferences, and everything learned =====
CREATE TABLE knowledge (
  know_id        INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  holder_id      INTEGER NOT NULL REFERENCES npc(npc_id),   -- whose head this is in
  subject_id     INTEGER REFERENCES npc(npc_id),            -- who it is about (NULL = topic)
  topic          TEXT,                                      -- when not about a person
  predicate      TEXT NOT NULL,                             -- 'age','favorite_color','fears',...
  value          TEXT NOT NULL,
  origin         TEXT NOT NULL CHECK (origin IN ('self','witnessed','told','overheard','read','inferred','rumor')),
  told_by_id     INTEGER REFERENCES npc(npc_id),            -- the person who said it to holder
  parent_know_id INTEGER REFERENCES knowledge(know_id),     -- the teller's copy (chain of custody)
  source_utt_id  INTEGER REFERENCES utterance(utt_id),
  source_event_id INTEGER REFERENCES world_event(event_id),
  rumor_id       INTEGER REFERENCES rumor(rumor_id),
  confidence     REAL NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
  privacy        TEXT NOT NULL DEFAULT 'shareable' CHECK (privacy IN ('secret','private','shareable','public')),
  importance     REAL NOT NULL DEFAULT 0.5 CHECK (importance BETWEEN 0 AND 1),
  learned_gt     REAL NOT NULL,
  superseded_by  INTEGER REFERENCES knowledge(know_id),
  CHECK (subject_id IS NOT NULL OR topic IS NOT NULL),
  -- self-knowledge is only ever about yourself
  CHECK (origin <> 'self' OR (subject_id = holder_id AND told_by_id IS NULL)),
  -- second-hand knowledge must name its teller, and the teller is never the holder
  CHECK ((origin IN ('told','overheard','read','rumor')) = (told_by_id IS NOT NULL)),
  CHECK (told_by_id IS NULL OR told_by_id <> holder_id)
) STRICT;
CREATE INDEX know_holder ON knowledge(holder_id, subject_id, predicate) WHERE superseded_by IS NULL;

-- ===== (8) Rumors: canonical claims that spread (spread itself = knowledge chain) =====
CREATE TABLE rumor (
  rumor_id       INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  origin_know_id INTEGER REFERENCES knowledge(know_id),
  canonical_claim TEXT NOT NULL,
  is_true        INTEGER CHECK (is_true IN (0,1)),          -- NULL = unknown
  created_gt     REAL NOT NULL
) STRICT;

-- ===== (3) Individual memories =====
CREATE TABLE memory (
  mem_id          INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  owner_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  kind            TEXT NOT NULL CHECK (kind IN ('episodic','semantic','reflection')),
  content         TEXT NOT NULL,            -- first person, owner's point of view
  about_ids_json  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(about_ids_json)),
  location        TEXT,
  occurred_gt     REAL NOT NULL,
  importance      REAL NOT NULL CHECK (importance BETWEEN 0 AND 1),
  valence         REAL NOT NULL DEFAULT 0 CHECK (valence BETWEEN -1 AND 1),
  intensity       REAL NOT NULL DEFAULT 0 CHECK (intensity BETWEEN 0 AND 1),
  rehearsal_count INTEGER NOT NULL DEFAULT 0,
  last_recalled_gt REAL,
  superseded_by   INTEGER REFERENCES memory(mem_id),
  created_by      TEXT NOT NULL CHECK (created_by IN ('extraction','consolidation','admin','seed'))
) STRICT;
CREATE INDEX mem_owner ON memory(owner_id, occurred_gt) WHERE superseded_by IS NULL;

CREATE TABLE memory_source (
  mem_id     INTEGER NOT NULL REFERENCES memory(mem_id),
  utt_id     INTEGER REFERENCES utterance(utt_id),
  event_id   INTEGER REFERENCES world_event(event_id),
  know_id    INTEGER REFERENCES knowledge(know_id),
  child_mem_id INTEGER REFERENCES memory(mem_id),           -- consolidation inputs
  CHECK ((utt_id IS NOT NULL) + (event_id IS NOT NULL) + (know_id IS NOT NULL) + (child_mem_id IS NOT NULL) = 1)
) STRICT;
CREATE INDEX memsrc_mem ON memory_source(mem_id);

CREATE TABLE memory_embedding (
  mem_id     INTEGER NOT NULL REFERENCES memory(mem_id),
  model      TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,                 -- float32 little-endian
  PRIMARY KEY (mem_id, model)
) STRICT;

CREATE VIRTUAL TABLE memory_fts USING fts5(content, content='memory', content_rowid='mem_id');

-- ===== (4) Beliefs and interpretations =====
CREATE TABLE belief (
  belief_id     INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  holder_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  about_id      INTEGER REFERENCES npc(npc_id),
  topic         TEXT,
  statement     TEXT NOT NULL,              -- 'Hod gossips too much'
  stance        REAL NOT NULL CHECK (stance BETWEEN -1 AND 1),
  confidence    REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  evidence_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence_json)), -- [{"mem":12},{"know":7}]
  formed_gt     REAL NOT NULL,
  superseded_by INTEGER REFERENCES belief(belief_id),
  CHECK (about_id IS NOT NULL OR topic IS NOT NULL)
) STRICT;

-- ===== (6) Relationships and emotional history =====
CREATE TABLE relationship (
  from_id     INTEGER NOT NULL REFERENCES npc(npc_id),
  to_id       INTEGER NOT NULL REFERENCES npc(npc_id),
  kinship     TEXT,                         -- 'spouse','sibling','parent','child',...
  status      TEXT NOT NULL DEFAULT 'acquaintance'
              CHECK (status IN ('stranger','acquaintance','friend','close_friend','courting','partner','spouse','ex','rival','enemy')),
  affinity    REAL NOT NULL DEFAULT 0 CHECK (affinity BETWEEN -1 AND 1),
  trust       REAL NOT NULL DEFAULT 0 CHECK (trust BETWEEN -1 AND 1),
  respect     REAL NOT NULL DEFAULT 0 CHECK (respect BETWEEN -1 AND 1),
  attraction  REAL NOT NULL DEFAULT 0 CHECK (attraction BETWEEN 0 AND 1),
  fear        REAL NOT NULL DEFAULT 0 CHECK (fear BETWEEN 0 AND 1),
  familiarity REAL NOT NULL DEFAULT 0 CHECK (familiarity BETWEEN 0 AND 1),
  updated_gt  REAL NOT NULL,
  PRIMARY KEY (from_id, to_id),
  CHECK (from_id <> to_id)
) STRICT;

CREATE TABLE relationship_change (
  change_id   INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  from_id     INTEGER NOT NULL,
  to_id       INTEGER NOT NULL,
  deltas_json TEXT NOT NULL CHECK (json_valid(deltas_json)),
  status_from TEXT,
  status_to   TEXT,
  cause_mem_id   INTEGER REFERENCES memory(mem_id),
  cause_event_id INTEGER REFERENCES world_event(event_id),
  reason      TEXT NOT NULL,
  game_time   REAL NOT NULL,
  FOREIGN KEY (from_id, to_id) REFERENCES relationship(from_id, to_id)
) STRICT;

-- ===== (7) Promises, obligations, conflicts =====
CREATE TABLE obligation (
  obl_id       INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  kind         TEXT NOT NULL CHECK (kind IN ('promise','debt','favor','grudge','conflict','secret_kept')),
  debtor_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  creditor_id  INTEGER NOT NULL REFERENCES npc(npc_id),
  description  TEXT NOT NULL,
  due_gt       REAL,
  status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','fulfilled','broken','forgiven','expired')),
  source_utt_id INTEGER REFERENCES utterance(utt_id),
  source_mem_id INTEGER REFERENCES memory(mem_id),
  created_gt   REAL NOT NULL,
  resolved_gt  REAL,
  CHECK (debtor_id <> creditor_id)
) STRICT;

-- ===== Autonomy and governance (later milestones) =====
CREATE TABLE goal (
  goal_id     INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id      INTEGER NOT NULL REFERENCES npc(npc_id),
  description TEXT NOT NULL,
  priority    REAL NOT NULL CHECK (priority BETWEEN 0 AND 1),
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','paused','achieved','abandoned')),
  deadline_gt REAL,
  created_gt  REAL NOT NULL
) STRICT;

CREATE TABLE action_policy (
  action_type  TEXT PRIMARY KEY,
  mode         TEXT NOT NULL CHECK (mode IN ('allow','confirm','deny')),
  daily_cap    INTEGER,
  irreversible INTEGER NOT NULL DEFAULT 0 CHECK (irreversible IN (0,1))
) STRICT;

CREATE TABLE action_proposal (
  proposal_id INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id      INTEGER NOT NULL REFERENCES npc(npc_id),
  action_type TEXT NOT NULL REFERENCES action_policy(action_type),
  params_json TEXT NOT NULL CHECK (json_valid(params_json)),
  rationale   TEXT NOT NULL,
  trace_id    INTEGER REFERENCES decision_trace(trace_id),
  status      TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed','approved','denied','executed','failed')),
  created_gt  REAL NOT NULL
) STRICT;

-- ===== Ops =====
CREATE TABLE job (
  job_id          INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  kind            TEXT NOT NULL,            -- 'extract_conversation','consolidate_npc','embed_memory','sim_tick'
  payload_json    TEXT NOT NULL CHECK (json_valid(payload_json)),
  idempotency_key TEXT NOT NULL UNIQUE,     -- e.g. 'extract:conv:1'
  status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed','dead')),
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_run_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  last_error      TEXT
) STRICT;

CREATE TABLE llm_request (
  req_id      INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  task        TEXT NOT NULL,                -- 'dialogue','extract','consolidate','decide','embed'
  model       TEXT NOT NULL,
  npc_id      INTEGER REFERENCES npc(npc_id),
  job_id      INTEGER REFERENCES job(job_id),
  conv_id     INTEGER REFERENCES conversation(conv_id),
  trace_id    INTEGER REFERENCES decision_trace(trace_id),
  prompt_tokens INTEGER, completion_tokens INTEGER,
  cost_usd    REAL NOT NULL DEFAULT 0,
  latency_ms  INTEGER,
  status      TEXT NOT NULL CHECK (status IN ('ok','error','rate_limited','fallback')),
  error       TEXT,
  at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE decision_trace (
  trace_id    INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  npc_id      INTEGER NOT NULL REFERENCES npc(npc_id),
  summary     TEXT NOT NULL,
  inputs_json TEXT NOT NULL CHECK (json_valid(inputs_json)),  -- {"mems":[..],"know":[..],"beliefs":[..]}
  game_time   REAL NOT NULL
) STRICT;

CREATE TABLE sim_clock (
  id          INTEGER PRIMARY KEY CHECK (id = 1),
  game_days_passed REAL NOT NULL,           -- mirrors Papyrus GameDaysPassed
  last_tick_gt REAL NOT NULL
) STRICT;

-- ===== Guards =====
-- Append-only raw logs
CREATE TRIGGER utterance_no_update BEFORE UPDATE ON utterance BEGIN SELECT RAISE(ABORT,'utterance is append-only'); END;
CREATE TRIGGER utterance_no_delete BEFORE DELETE ON utterance BEGIN SELECT RAISE(ABORT,'utterance is append-only'); END;
CREATE TRIGGER event_no_update BEFORE UPDATE ON world_event BEGIN SELECT RAISE(ABORT,'world_event is append-only'); END;
CREATE TRIGGER event_no_delete BEFORE DELETE ON world_event BEGIN SELECT RAISE(ABORT,'world_event is append-only'); END;
CREATE TRIGGER conversation_no_delete BEFORE DELETE ON conversation BEGIN SELECT RAISE(ABORT,'conversation is append-only'); END;
CREATE TRIGGER conversation_close_only BEFORE UPDATE ON conversation
  WHEN OLD.ended_gt IS NOT NULL OR NEW.conv_id <> OLD.conv_id OR NEW.location <> OLD.location
    OR NEW.kind <> OLD.kind OR NEW.started_gt <> OLD.started_gt
  BEGIN SELECT RAISE(ABORT,'conversation can only be closed once'); END;

-- Speaker must be a participant of the conversation
CREATE TRIGGER utterance_speaker_is_participant BEFORE INSERT ON utterance
  WHEN NOT EXISTS (SELECT 1 FROM participant p WHERE p.conv_id = NEW.conv_id AND p.npc_id = NEW.speaker_id)
  BEGIN SELECT RAISE(ABORT,'speaker is not a participant'); END;

-- Voice isolation: an NPC line must use that NPC's bound voice
CREATE TRIGGER utterance_voice_matches BEFORE INSERT ON utterance
  WHEN NEW.voice_model IS NOT NULL AND NEW.voice_model IS NOT
       (SELECT voice_model FROM voice_binding v WHERE v.npc_id = NEW.speaker_id)
  BEGIN SELECT RAISE(ABORT,'voice does not match speaker binding'); END;

-- Locked voices cannot be rebound
CREATE TRIGGER voice_locked BEFORE UPDATE ON voice_binding WHEN OLD.locked = 1 AND NEW.locked = 1
  BEGIN SELECT RAISE(ABORT,'voice binding is locked'); END;

-- Told knowledge: the teller must actually have been in the source conversation
CREATE TRIGGER knowledge_teller_present BEFORE INSERT ON knowledge
  WHEN NEW.source_utt_id IS NOT NULL AND NEW.origin = 'told' AND NOT EXISTS (
    SELECT 1 FROM utterance u WHERE u.utt_id = NEW.source_utt_id AND u.speaker_id = NEW.told_by_id)
  BEGIN SELECT RAISE(ABORT,'told_by must be the speaker of source_utt_id'); END;

-- Self knowledge taken from a conversation must come from the holder's own line
CREATE TRIGGER knowledge_self_from_own_line BEFORE INSERT ON knowledge
  WHEN NEW.origin = 'self' AND NEW.source_utt_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM utterance u WHERE u.utt_id = NEW.source_utt_id AND u.speaker_id = NEW.holder_id)
  BEGIN SELECT RAISE(ABORT,'self fact must come from the holder''s own line'); END;

-- Told knowledge: the holder must have been present to hear it
CREATE TRIGGER knowledge_holder_present BEFORE INSERT ON knowledge
  WHEN NEW.source_utt_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM utterance u JOIN participant p ON p.conv_id = u.conv_id
    WHERE u.utt_id = NEW.source_utt_id AND p.npc_id = NEW.holder_id)
  BEGIN SELECT RAISE(ABORT,'holder was not in that conversation'); END;

-- Chain of custody: parent copy must belong to the teller and be about the same subject/predicate
CREATE TRIGGER knowledge_parent_matches BEFORE INSERT ON knowledge
  WHEN NEW.parent_know_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM knowledge k WHERE k.know_id = NEW.parent_know_id
      AND k.holder_id = NEW.told_by_id
      AND k.subject_id IS NEW.subject_id AND k.predicate = NEW.predicate)
  BEGIN SELECT RAISE(ABORT,'parent knowledge must be the teller''s copy of the same fact'); END;

-- FTS sync
CREATE TRIGGER memory_fts_ai AFTER INSERT ON memory BEGIN
  INSERT INTO memory_fts(rowid, content) VALUES (NEW.mem_id, NEW.content); END;

-- ===== Views =====
-- Rows visible on the active timeline: the active branch in full, each ancestor up to its fork point.
CREATE VIEW v_timeline_scope AS
WITH RECURSIVE anc(timeline_id, cutoff_gt) AS (
  SELECT timeline_id, 1e18 FROM timeline WHERE is_active = 1
  UNION ALL
  SELECT t.parent_id, MIN(a.cutoff_gt, t.branch_game_time)
  FROM timeline t JOIN anc a ON t.timeline_id = a.timeline_id
  WHERE t.parent_id IS NOT NULL
)
SELECT timeline_id, cutoff_gt FROM anc;

CREATE VIEW v_visible_memory AS
SELECT m.* FROM memory m JOIN v_timeline_scope s ON s.timeline_id = m.timeline_id AND m.occurred_gt <= s.cutoff_gt
WHERE m.superseded_by IS NULL;
-- What one NPC may see. Prompt builders read ONLY through these, always with holder = speaker.
CREATE VIEW v_known_facts AS
SELECT k.holder_id, h.display_name AS holder, s.display_name AS subject, k.topic, k.predicate, k.value,
       k.origin, t.display_name AS told_by, k.confidence, k.learned_gt, k.know_id,
       CASE
         WHEN k.origin = 'self' THEN 'My ' || k.predicate || ' is ' || k.value
         WHEN k.origin IN ('told','overheard','read','rumor')
           THEN t.display_name || ' ' || CASE k.origin WHEN 'told' THEN 'told me' WHEN 'rumor' THEN 'passed on a rumor' ELSE k.origin END
                || ' that ' || COALESCE(s.display_name, k.topic) || '''s ' || k.predicate || ' is ' || k.value
         ELSE 'I know ' || COALESCE(s.display_name, k.topic) || '''s ' || k.predicate || ' is ' || k.value
       END AS prompt_line
FROM knowledge k
JOIN npc h ON h.npc_id = k.holder_id
LEFT JOIN npc s ON s.npc_id = k.subject_id
LEFT JOIN npc t ON t.npc_id = k.told_by_id
JOIN v_timeline_scope bs ON bs.timeline_id = k.timeline_id AND k.learned_gt <= bs.cutoff_gt
WHERE k.superseded_by IS NULL;

CREATE VIEW v_rumor_spread AS
WITH RECURSIVE chain(know_id, holder_id, told_by_id, depth, root_id) AS (
  SELECT know_id, holder_id, told_by_id, 0, know_id FROM knowledge WHERE parent_know_id IS NULL
  UNION ALL
  SELECT k.know_id, k.holder_id, k.told_by_id, c.depth + 1, c.root_id
  FROM knowledge k JOIN chain c ON k.parent_know_id = c.know_id
)
SELECT * FROM chain;

INSERT INTO schema_version(version, note) VALUES (1, 'initial schema');
