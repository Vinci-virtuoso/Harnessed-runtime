"""System prompt assembly, including procedure envelopes.

The system prompt is a base instruction plus zero or more learned procedures, in that order.
Procedures may only ever be **appended** (architecture decision 5): a promoted procedure is
additive text, never a rewrite of the base, so the operator can always read the original
instruction unchanged at the top of the prompt.

Each procedure is wrapped in one envelope, ``<procedure>...</procedure>``. The shape is
deliberately dull and single-purpose. Two things depend on it:

* **Ticket 023 appends candidates through this seam.** A candidate is text, this function is
  where text becomes prompt, and nothing else in the package concatenates the two.
* **The FR-25 proof reads the assembled prompt.** ``FakeProvider`` selects its fixture on
  whether the prompt contains declared trait strings (THE CRUX), so the envelope must never
  transform, summarize or truncate the procedure text - it may only surround it. A wrapper that
  edited its payload would break the causal chain the proof measures.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "BASE_INSTRUCTION",
    "CONTEXT_CLOSE",
    "CONTEXT_OPEN",
    "ENVELOPE_CLOSE",
    "ENVELOPE_OPEN",
    "SYNTHESIS_INSTRUCTION",
    "build_system_prompt",
    "build_user_turn",
    "envelope",
]

ENVELOPE_OPEN = "<procedure>"
ENVELOPE_CLOSE = "</procedure>"

CONTEXT_OPEN = "<retrieved_context>"
CONTEXT_CLOSE = "</retrieved_context>"

#: What the model is told it is before any procedure (decision 60, FR-107). ``open_session``
#: composes the registry without ``delegate_to_agent`` - a Groq route delegates from code, never
#: by the model asking - so this text carries 0 mentions of delegation. Ticket 179 deleted the
#: fallback flag and the v4 text it used to choose between; this is now the one base instruction
#: every session builds its system prompt from.
#:
#: It is static text on purpose: the system prompt must be the same bytes on every run so the
#: provider's prefix cache holds (see ``build_user_turn``).
#:
#: It must never contain a trait string the scripted test provider's fixtures match on: a base
#: instruction that satisfied a predicate would make every candidate look promoted and break the
#: FR-25 proof. ``tests/test_prompt_prefix.py`` pins that.
BASE_INSTRUCTION = (
    "You are Harnessed, an agent harness that carries out one task for an operator. "
    "Your own tools are fs_read and fs_write, which read and write files in the working "
    "directory. You have no web access of your own. "
    "Do the task rather than describing how the operator could do it: never answer with a "
    "script or instructions for the operator to run in place of the result. "
    "If the task cannot be done with the tools you have, say so plainly."
)

#: The closing pass of a chain (ticket 199): a previous step already did the task, and the model
#: writes the operator's reply from its result. A second static prefix rather than a per-run
#: string, so the provider's prefix cache holds for both paths (decision 60). ``BASE_INSTRUCTION``
#: tells the model it has no web access and to say so when it cannot do the task, which made it
#: refuse to write up a finished step. Same trait-free, delegation-free pins as the base.
SYNTHESIS_INSTRUCTION = (
    "You are Harnessed, an agent harness that carries out one task for an operator. "
    "A previous step has already carried out the task: its result is the retrieved_context "
    "block at the top of the message, and the task it answers follows the block. "
    "Write the operator's reply from that result. State what the step found, clean and "
    "structured, in the operator's terms. Do not narrate what you would do, do not restate the "
    "task, and do not say what you cannot access: the work is done and its result is in front "
    "of you. If the result does not answer the task, say what it did find and what is missing. "
    "Your own tools are fs_read and fs_write, which read and write files in the working "
    "directory."
)


def envelope(text: str) -> str:
    """Wrap one procedure. The text passes through verbatim; only the surroundings are ours."""
    return f"{ENVELOPE_OPEN}\n{text.strip()}\n{ENVELOPE_CLOSE}"


def build_system_prompt(base: str, procedures: Iterable[str] = ()) -> str:
    """The base instruction, then one envelope per procedure, in the order given.

    An empty or whitespace-only procedure gets no envelope: an empty wrapper would add a trait
    string to the prompt without adding an instruction, which is noise the fixture predicates
    would then have to reason about.
    """
    sections = [base.strip()]
    sections.extend(envelope(text) for text in procedures if text.strip())
    return "\n\n".join(section for section in sections if section)


def build_user_turn(context: str, task: str) -> str:
    """One run's retrieved memory as a tagged block, then the task (architecture section 7).

    Retrieval rides in the user turn rather than the system prompt so the system prompt is the
    same bytes on every run: Groq and DeepSeek cache only an exact prefix from the first token,
    and a prompt that opened with this run's memory shared no prefix with the last one.

    An empty or whitespace-only context returns the task unchanged, so an unretrieved request is
    byte-identical to one from before retrieval moved here.
    """
    if not context.strip():
        return task
    return f"{CONTEXT_OPEN}\n{context.strip()}\n{CONTEXT_CLOSE}\n\n{task}"
