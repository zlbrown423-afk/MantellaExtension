"""Headless test harness: run Mantella's game protocol without Skyrim, an LLM API or a TTS service.

Import the modules directly (eg ``from livingworld.harness.session import HeadlessSession``).
This package does not import them eagerly, because ``src/http/routes/mantella_route.py``
imports ``livingworld.harness.recorder`` and an eager import here would be circular.
"""
