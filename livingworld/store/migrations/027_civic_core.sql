-- 027: civic core (plan C1 + C2, Whiterun only): institutions and a paper coin ledger (livingworld/civic/).
--
-- An institution is a body that can hold authority and money: the Jarl's court, a chartered company such as the
-- Plains Wardens, later guards, temples and settlements. A purse is an institution's (or a person's) money on
-- paper; every movement is a ledger_entry row, so a balance is always the sum of its entries. A transfer between
-- two purses is two rows with the same transfer_key. Nothing here touches gold in the player's inventory.
--
-- Both tables are on the save timeline: loading an older save hides institutions founded and money moved after
-- it, like every other change.

CREATE TABLE institution (
  inst_id      INTEGER PRIMARY KEY,
  timeline_id  INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  key          TEXT NOT NULL,                     -- 'whiterun_court', 'plains_wardens'
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL CHECK (kind IN ('court','guard','company','settlement','temple','guild')),
  hold         TEXT,
  head_id      INTEGER REFERENCES npc(npc_id),    -- Jarl, founder or commander
  founded_gt   REAL NOT NULL
) STRICT;
CREATE INDEX institution_key ON institution(key, founded_gt);

CREATE VIEW v_institution AS
SELECT i.* FROM institution i JOIN v_timeline_scope s ON s.timeline_id = i.timeline_id AND i.founded_gt <= s.cutoff_gt;

CREATE TABLE ledger_entry (
  entry_id      INTEGER PRIMARY KEY,
  timeline_id   INTEGER NOT NULL DEFAULT 1 REFERENCES timeline(timeline_id),
  purse         TEXT NOT NULL,                    -- 'inst:plains_wardens', 'inst:whiterun_court', 'npc:12'
  amount        INTEGER NOT NULL,                 -- gold; positive in, negative out
  kind          TEXT NOT NULL CHECK (kind IN ('stipend','allowance','upkeep','wage','fee','build','gift','fine',
                                                'tribute','loan','repayment','opening','adjustment')),
  counterparty  TEXT,                             -- the other purse, or a plain description
  transfer_key  TEXT,                             -- shared by the two rows of one transfer
  memo          TEXT NOT NULL DEFAULT '',
  cause_event_id INTEGER REFERENCES world_event(event_id),
  game_time     REAL NOT NULL
) STRICT;
CREATE INDEX ledger_purse ON ledger_entry(purse, game_time);

CREATE VIEW v_ledger_entry AS
SELECT e.* FROM ledger_entry e JOIN v_timeline_scope s ON s.timeline_id = e.timeline_id AND e.game_time <= s.cutoff_gt;

CREATE VIEW v_purse_balance AS
SELECT purse, SUM(amount) AS balance, COUNT(*) AS entries, MAX(game_time) AS last_gt FROM v_ledger_entry GROUP BY purse;

CREATE TRIGGER ledger_entry_no_update BEFORE UPDATE ON ledger_entry BEGIN SELECT RAISE(ABORT,'ledger_entry is append-only'); END;

INSERT INTO schema_version(version, note) VALUES (27, 'civic core: institutions and paper ledger');
