"""Player voice measurements (vocal perception, Stage 1: Voice Lab).

Pure measurement code with no Mantella imports: it takes one utterance of audio and returns numbers, each
with a reliability flag. Nothing here stores audio, talks to the network or reads game state.

- ``features``: the seven Voice Lab measurements for one utterance.
- ``pitch``: a pitch tracker (Praat through parselmouth when installed, otherwise a numpy YIN tracker).
- ``baseline``: the player's own baseline (robust medians and spreads) and z-scores against it.
- ``validation``: which measurements separate deliberate changes from natural variation, and whether a
  baseline held from one day to the next.

The standalone tool that records the protocol lives in ``tools/voice_lab``. Nothing in the game or the
Mantella server uses this package yet (that is Stage 2, behind ``MANTELLA_LW_VOICE``).
"""
