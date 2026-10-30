"""The deterministic tier: binary outcomes only.

Every file here carries two tiers, split by a banner comment. Above it, an offline tier driven by
``ScriptedClient`` that runs on a fresh clone with no key. Below it, a live tier carrying the
``live_eval`` marker and a ``skipif`` that names both gates it is waiting on.
"""
