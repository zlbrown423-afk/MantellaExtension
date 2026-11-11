-- 016: another placed reference of an NPC already known (livingworld/store/rebind.py). The LWSaloniaSurrender
-- plugin swaps Salonia's road-encounter reference (deleted by the game later) for a persistent copy; the copy
-- has another form ID, and without this she was a stranger with no memories. Both references now resolve to
-- the same npc_id. An alias, not an overwrite: an older save that still has the old reference finds her too.
CREATE TABLE npc_ref_alias (
  plugin        TEXT NOT NULL,
  base_form_id  TEXT NOT NULL,
  ref_form_id   TEXT NOT NULL,
  npc_id        INTEGER NOT NULL REFERENCES npc(npc_id),
  created_gt    REAL NOT NULL,
  PRIMARY KEY (plugin, base_form_id, ref_form_id)
) STRICT;

INSERT INTO schema_version(version, note) VALUES (16, 'a new reference of a known NPC keeps their npc_id');
