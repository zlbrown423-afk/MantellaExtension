"""Personality profiles for adult women (Living World): who she is in love and intimacy, how it changes, and what
the player has found out. See README.md in this folder. Off with MANTELLA_LW_PERSONALITY=0.

Modules: schema (attributes), config (the admin settings file), eligibility (adults only), generate (AUTOMATIC
starting point), profile (precedence: off switches > hand-set > change over time > generated), evolve (change over
time), discovery (what the player knows), prompt (lines for Mantella), bridge (romance and consent read profiles),
admin + __main__ (inspect, export / import, reset, regenerate), debug (in-conversation commands), service (the
one object integration.py talks to).
"""
from livingworld.personality.bridge import enabled  # noqa: F401
