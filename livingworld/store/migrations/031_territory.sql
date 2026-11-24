-- 031: territory (livingworld/territory/): who holds which camp, who garrisons it, and who knows what about it.
--
-- The GAME is the truth (LWTerritory.esp keeps ownership, timers and soldiers in the save); these tables are a
-- read-only mirror the game feeds with `type=territory` records, plus what each person has seen, been told or
-- heard. Nothing here moves a soldier or changes an owner in the game. Every row is on the save timeline and
-- every state is an append-only change row read through a view, so loading an older save shows the map, the
-- garrisons and the news as they were then.

-- One change to who holds one site. kind: occupied (a force took an eligible site), reinforced (strength up or
-- replacements), cleared (the holder was driven out), withdrawn (the holder left of its own accord),
-- established (a Plains Wardens holding), contested (an attack began or ended), abandoned (nobody left alive).
CREATE TABLE territory_change (
  change_id     INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  site_key      TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('occupied','reinforced','cleared','withdrawn','established',
                                              'contested','abandoned')),
  owner_before  TEXT NOT NULL,
  owner_after   TEXT NOT NULL,
  strength      INTEGER NOT NULL DEFAULT 0,
  contested     INTEGER NOT NULL DEFAULT 0 CHECK (contested IN (0,1)),
  held_since    REAL NOT NULL,                 -- when the current owner took it (the occupation date)
  cooldown_gt   REAL,                          -- no reclamation before this (set when the player clears a site)
  source        TEXT NOT NULL DEFAULT 'game' CHECK (source IN ('game','admin')),
  event_id      INTEGER REFERENCES world_event(event_id),
  game_key      TEXT,
  game_time     REAL NOT NULL
) STRICT;
CREATE INDEX territory_change_site ON territory_change(site_key, game_time);
CREATE UNIQUE INDEX territory_change_key ON territory_change(timeline_id, game_key) WHERE game_key IS NOT NULL;

CREATE VIEW v_territory_change AS
SELECT c.* FROM territory_change c JOIN v_timeline_scope s ON s.timeline_id = c.timeline_id AND c.game_time <= s.cutoff_gt;

-- Where each site stands now (a site with no row has never changed hands: it is 'unoccupied').
CREATE VIEW v_territory_site AS
SELECT c.* FROM v_territory_change c
WHERE c.change_id = (SELECT c2.change_id FROM v_territory_change c2 WHERE c2.site_key = c.site_key
                     ORDER BY c2.game_time DESC, c2.change_id DESC LIMIT 1);

-- A soldier posted to a site. faction: thalmor | wardens. One live assignment per person (the view and the
-- code both enforce it); moving someone writes a new assignment and ends the old one as 'transferred'.
CREATE TABLE garrison_assignment (
  assign_id      INTEGER PRIMARY KEY,
  timeline_id    INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  site_key       TEXT NOT NULL,
  npc_id         INTEGER NOT NULL REFERENCES npc(npc_id),
  faction        TEXT NOT NULL CHECK (faction IN ('thalmor','wardens')),
  rank           TEXT NOT NULL DEFAULT 'soldier',
  squad          TEXT,
  duty           TEXT,
  equipment_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(equipment_json)),
  assigned_gt    REAL NOT NULL,
  game_key       TEXT
) STRICT;
CREATE INDEX garrison_assignment_site ON garrison_assignment(site_key, npc_id);
CREATE UNIQUE INDEX garrison_assignment_key ON garrison_assignment(timeline_id, game_key) WHERE game_key IS NOT NULL;

CREATE TABLE garrison_status (
  status_id   INTEGER PRIMARY KEY,
  timeline_id INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  assign_id   INTEGER NOT NULL REFERENCES garrison_assignment(assign_id),
  status      TEXT NOT NULL CHECK (status IN ('assigned','recalled','transferred','dead','replaced','withdrawn')),
  health      REAL,                              -- 0..1, when the game reported it
  location    TEXT,
  note        TEXT NOT NULL DEFAULT '',
  game_time   REAL NOT NULL
) STRICT;
CREATE INDEX garrison_status_assign ON garrison_status(assign_id, game_time);

CREATE VIEW v_garrison_status AS
SELECT g.* FROM garrison_status g JOIN v_timeline_scope s ON s.timeline_id = g.timeline_id AND g.game_time <= s.cutoff_gt;

-- Live garrison: the latest visible status of each visible assignment is 'assigned'.
CREATE VIEW v_garrison AS
SELECT a.*, st.health AS health, st.location AS last_location, st.game_time AS status_gt
FROM garrison_assignment a
JOIN v_timeline_scope s ON s.timeline_id = a.timeline_id AND a.assigned_gt <= s.cutoff_gt
JOIN v_garrison_status st ON st.status_id = (SELECT g2.status_id FROM v_garrison_status g2 WHERE g2.assign_id = a.assign_id
                                             ORDER BY g2.game_time DESC, g2.status_id DESC LIMIT 1)
WHERE st.status = 'assigned';

-- What one person knows about one site, and how they know it. witnessed = their own eyes; reported = a
-- garrison report from one of their own soldiers; told = a named person said so; rumor = general talk.
CREATE TABLE territory_knowledge (
  know_id      INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  holder_id    INTEGER NOT NULL REFERENCES npc(npc_id),
  site_key     TEXT NOT NULL,
  owner        TEXT NOT NULL,
  strength     INTEGER NOT NULL DEFAULT 0,
  contested    INTEGER NOT NULL DEFAULT 0 CHECK (contested IN (0,1)),
  origin       TEXT NOT NULL CHECK (origin IN ('witnessed','reported','told','rumor')),
  told_by_id   INTEGER REFERENCES npc(npc_id),
  confidence   REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  about_gt     REAL NOT NULL,                    -- when it was true
  learned_gt   REAL NOT NULL,
  change_id    INTEGER REFERENCES territory_change(change_id),
  CHECK ((origin IN ('told','rumor')) = (told_by_id IS NOT NULL) OR origin = 'rumor'),
  CHECK (told_by_id IS NULL OR told_by_id <> holder_id)
) STRICT;
CREATE INDEX territory_knowledge_holder ON territory_knowledge(holder_id, site_key, about_gt);
CREATE UNIQUE INDEX territory_knowledge_once ON territory_knowledge(timeline_id, holder_id, change_id, origin)
  WHERE change_id IS NOT NULL;

CREATE VIEW v_territory_knowledge AS
SELECT k.* FROM territory_knowledge k JOIN v_timeline_scope s ON s.timeline_id = k.timeline_id AND k.learned_gt <= s.cutoff_gt;

INSERT INTO schema_version(version, note) VALUES (31, 'territory: ownership mirror, garrison roster, who knows what');
