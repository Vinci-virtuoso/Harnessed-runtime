"""The LLM-as-judge tier: scores with a threshold, never truths.

``deepeval`` is an optional dependency under the ``[eval]`` extra and is **never** imported at
module scope anywhere in this package, so the whole suite still collects on a base install.
Nothing under ``src/harnessed/`` imports anything from here: a judge score is a percentage, and a
percentage may not gate a promotion.
"""
