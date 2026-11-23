-- 029: corrections to what the speech recognition heard (livingworld/memory/corrections.py).
--
-- The raw transcript in ``utterance.text`` is never changed. A confirmed correction is a row here; the newest
-- row that is not superseded is the line's canonical text. ``v_utterance`` gives every line with ``text`` =
-- canonical text and ``raw_text`` = what was heard, so memory extraction, diaries, recaps and resumes read the
-- corrected words while the original stays for diagnostics. Same id, timeline, conversation, time and speaker.

CREATE TABLE utterance_correction (
  corr_id        INTEGER PRIMARY KEY,
  utt_id         INTEGER NOT NULL REFERENCES utterance(utt_id),
  raw_text       TEXT NOT NULL,                 -- the transcript as it was when corrected (for the audit trail)
  corrected_text TEXT NOT NULL,
  source         TEXT NOT NULL CHECK (source IN ('player_confirmed', 'admin')),
  note           TEXT,
  superseded_by  INTEGER REFERENCES utterance_correction(corr_id),
  created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE INDEX utterance_correction_utt ON utterance_correction(utt_id) WHERE superseded_by IS NULL;

CREATE VIEW v_utterance AS
SELECT u.utt_id, u.timeline_id, u.conv_id, u.seq, u.speaker_id, u.addressee_id,
       COALESCE((SELECT c.corrected_text FROM utterance_correction c
                 WHERE c.utt_id = u.utt_id AND c.superseded_by IS NULL ORDER BY c.corr_id DESC LIMIT 1), u.text) AS text,
       u.text AS raw_text,
       EXISTS (SELECT 1 FROM utterance_correction c WHERE c.utt_id = u.utt_id AND c.superseded_by IS NULL) AS corrected,
       u.voice_model, u.game_time
FROM utterance u;

INSERT INTO schema_version(version, note) VALUES (29, 'utterance corrections: confirmed fixes to misheard speech, raw kept');
