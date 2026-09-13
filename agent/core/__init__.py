"""Orchestration: what a run *is*.

  schemas.py      the typed contracts every layer talks in
  skills.py       the skill registry and per-skill dispatch
  persistence.py  the on-disk shape of a session
  recovery.py     failure classification and the recovery decision table
  events.py       per-session pub/sub, consumed by the HTTP API
"""
