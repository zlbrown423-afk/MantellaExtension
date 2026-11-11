-- Undo "attacks" that were really harmless spells cast on someone: animation and expression spells
-- (Anim: Kneel, Face: Gasp) and the Debug Bath test spell were stored as violence, so NPCs remembered
-- the player attacking them and trusted them less. From now on such hits are not stored at all.
--
-- Memories and the knowledge copies of the claims are hidden (superseded by themselves), the trust and
-- standing changes they caused are removed. The event rows stay, so nothing else that points at them breaks.

CREATE TEMP TABLE harmless_hit AS
SELECT event_id FROM world_event
WHERE type = 'violence'
  AND (lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'anim:%'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'anim %'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'animation%'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'face:%'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'face %'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'expression%'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'emote%'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'pose%'
    OR lower(ltrim(json_extract(payload_json, '$.weapon'))) LIKE 'debug%');

CREATE TEMP TABLE harmless_mem AS
SELECT mem_id FROM memory WHERE event_id IN (SELECT event_id FROM harmless_hit);

CREATE TEMP TABLE harmless_claim AS
SELECT claim_id FROM claim WHERE truth_event_id IN (SELECT event_id FROM harmless_hit);

DELETE FROM relationship_change
WHERE cause_event_id IN (SELECT event_id FROM harmless_hit)
   OR cause_mem_id IN (SELECT mem_id FROM harmless_mem);

DELETE FROM standing_change
WHERE cause_event_id IN (SELECT event_id FROM harmless_hit)
   OR claim_id IN (SELECT claim_id FROM harmless_claim);

UPDATE memory SET superseded_by = mem_id
WHERE superseded_by IS NULL AND mem_id IN (SELECT mem_id FROM harmless_mem);

UPDATE knowledge SET superseded_by = know_id
WHERE superseded_by IS NULL AND claim_id IN (SELECT claim_id FROM harmless_claim);

DROP TABLE harmless_hit;
DROP TABLE harmless_mem;
DROP TABLE harmless_claim;

INSERT INTO schema_version(version, note) VALUES (10, 'harmless spells are not attacks: undo the ones stored');
