"""The digests and the truncation rule (decision 58, FR-101; the scrub half of FR-94).

Plain text in labelled sections: ``Task:``, ``Operator and memory:``, ``This session so far:``
and, on a chain hop only, ``Previous step:`` (ticket 176, FR-117): the prior agent result's last
:data:`PREVIOUS_STEP_CAP` characters. ``Recently refusing:`` (ticket 194) is one optional last
line per entry that refused for quota within the hour; the caller reads the ledger, since this
package may not import ``acp``. The truncation rule drops session-digest lines oldest first,
then cuts the previous-step digest, then tail-cuts the task (decision 58).

**Scrubbed once, on the way in.** The task and the previous step are the parts of this state a
caller has not already had scrubbed: a fact's text is scrubbed at ``semantic.put`` time and a
stored session message is scrubbed at journal-write time (``loop/journal.py``), so scrubbing those
two here - before any count or any truncation - is what makes ``count_tokens`` measure the exact
bytes this module returns, rather than a bigger, unscrubbed candidate that happens to shrink
afterwards.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from harnessed.loop.prompt import CONTEXT_CLOSE, CONTEXT_OPEN
from harnessed.memory import redact, semantic
from harnessed.memory.budget import count_tokens
from harnessed.memory.totals import totals

__all__ = [
    "OPERATOR_MEMORY_CAP",
    "OPERATOR_TAG",
    "PREVIOUS_STEP_CAP",
    "SESSION_MESSAGE_CAP",
    "SESSION_MESSAGE_COUNT",
    "STATE_BUDGET",
    "THE_TASK_CAP",
    "State",
    "build_state",
]

#: The model's context less room for the questions and the answer (decision 58, the pinned model's
#: 32k window from `providers/quirks.py::ROUTER_MODELS`).
STATE_BUDGET: Final = 24_000

#: What `harnessed memory add --tag operator "..."` writes, and what this module reads back.
OPERATOR_TAG: Final = "operator"

#: The whole "Operator and memory:" section, however many facts it names (decision 58).
OPERATOR_MEMORY_CAP: Final = 600

#: The last N `user`/`assistant` messages that make up "This session so far:" (decision 58).
SESSION_MESSAGE_COUNT: Final = 6
#: Each of those messages, rendered, capped at this many characters (decision 58).
SESSION_MESSAGE_CAP: Final = 200

#: A task's text and its figures, rendered under ``The task:`` (decision 58, ticket 240).
THE_TASK_CAP: Final = 600

#: The prior agent result's tail a chain hop's "Previous step:" quotes (decision 58).
PREVIOUS_STEP_CAP: Final = 2000

#: The tail of a task session's last reply a continuation's question quotes as "Last reply:" when
#: the session digest cut it (ticket 264); the newest text is the part kept.
LAST_REPLY_CAP: Final = 2000

_NO_OPERATOR: Final = "The operator's name is not recorded."
_MEMORY_SENTENCE: Final = (
    "Questions about the operator, past sessions, earlier decisions or project facts are "
    "answered from memory."
)


@dataclass(frozen=True, slots=True)
class State:
    """One message's built state: the text a Decider call is asked against, and whether the
    named truncation rule had to cut anything to fit :data:`STATE_BUDGET` (decision 58)."""

    text: str
    truncated: bool


def _operator_and_memory(conn: sqlite3.Connection) -> str:
    facts = semantic.recent_by_tag(conn, OPERATOR_TAG, limit=3)
    operator = "\n".join(f"- {fact.text}" for fact in facts) if facts else _NO_OPERATOR
    counted = totals(conn)
    counts = (
        f"Memory holds {counted.facts_active} facts, {counted.episodes} past sessions, "
        f"{counted.procedures_active} procedures."
    )
    return "\n".join((operator, counts, _MEMORY_SENTENCE))[:OPERATOR_MEMORY_CAP]


def _stripped(content: str) -> str:
    """``content`` with one retrieved-context block removed (``loop/prompt.py``'s own markers).

    A router digest describes what the operator and the model said, not what retrieval already
    handed the model in a past turn - that block is redundant here and would burn budget twice.
    """
    if CONTEXT_OPEN not in content:
        return content
    _, _, after_open = content.partition(CONTEXT_OPEN)
    _, _, after_close = after_open.partition(CONTEXT_CLOSE)
    return after_close.strip()


def _session_lines(session: Sequence[tuple[str, str]]) -> list[str]:
    """One line per kept message, oldest first - the order the truncation rule drops from."""
    kept = list(session)[-SESSION_MESSAGE_COUNT:]
    return [f"{role}: {_stripped(content)[:SESSION_MESSAGE_CAP]}" for role, content in kept]


def _render(
    task: str,
    memory: str,
    session_lines: Sequence[str],
    previous: str | None = None,
    previous_by: str | None = None,
    refusing: Sequence[str] = (),
    the_task: str | None = None,
    last_reply: str | None = None,
) -> str:
    sections = [
        f"Task: {task}",
        "",
        *([] if the_task is None else ["The task:", the_task, ""]),
        "Operator and memory:",
        memory,
        "",
        "This session so far:",
        "\n".join(session_lines),
    ]
    if previous is not None:
        label = "Previous step:" if previous_by is None else f"Previous step: (by {previous_by})"
        sections += ["", label, previous]
    if last_reply is not None:
        sections += ["", "Last reply:", last_reply]
    sections += [f"Recently refusing: {name} (out of usage)" for name in refusing]
    return "\n".join(sections)


def _longest(size: int, fits: Callable[[int], bool]) -> int:
    """The largest ``n`` in ``[0, size]`` for which ``fits(n)`` holds, else ``0``."""
    low, high, best = 0, size, 0
    while low <= high:
        mid = (low + high) // 2
        if fits(mid):
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return best


def build_state(
    conn: sqlite3.Connection,
    task: str,
    session: Sequence[tuple[str, str]] = (),
    *,
    previous: str | None = None,
    previous_by: str | None = None,
    refusing: Sequence[str] = (),
    the_task: str | None = None,
    last_reply: str | None = None,
    budget: int = STATE_BUDGET,
) -> State:
    """The whole state for one Decider call: the digests, then the named truncation rule.

    ``session`` is ``(role, content)`` pairs already filtered to ``user``/``assistant`` and in
    order - ``cli/routed.py``'s job, since that is where the ``Message`` type this history arrives
    as may be named (``router/`` may not import ``providers.types``, decision 56's import row).

    ``previous`` is a chain hop's prior agent result (ticket 176): ``None`` on a message's first
    decision, which has no ``Previous step:`` section at all. ``previous_by`` names the entry that
    wrote it (ticket 220): the label reads ``Previous step: (by X)``, keeping the literal
    ``previous step:`` the chain fixtures' matchers select on.

    Truncation drops session-digest lines oldest first, then cuts the previous-step digest from
    its head (its newest output is what is kept), then tail-cuts the task (decision 58).

    ``the_task`` is a task's own text with its used figures beside its budget, as the store serves
    them (ticket 240): a section of its own, scrubbed and capped, that no truncation step cuts.

    ``last_reply`` is a task continuation's last assistant reply (ticket 264): quoted as
    ``Last reply:`` only when the session digest does not already show it whole, scrubbed, its
    newest :data:`LAST_REPLY_CAP` characters kept, and cut from its head before the task is.
    """
    task = redact.scrub(task)
    if the_task is not None:
        the_task = redact.scrub(the_task)[:THE_TASK_CAP]
    if previous is not None:
        previous = redact.scrub(previous)[-PREVIOUS_STEP_CAP:]
    memory = _operator_and_memory(conn)
    session_lines = _session_lines(session)
    if last_reply is not None:
        last_reply = redact.scrub(_stripped(last_reply))
        if f"assistant: {last_reply}" in session_lines:
            last_reply = None
        else:
            last_reply = last_reply[-LAST_REPLY_CAP:]

    def rendered(task: str, previous: str | None, reply: str | None) -> str:
        return _render(
            task, memory, session_lines, previous, previous_by, refusing, the_task, reply
        )

    def fits(task: str, previous: str | None, reply: str | None = last_reply) -> bool:
        return count_tokens(rendered(task, previous, reply)) <= budget

    if fits(task, previous):
        return State(text=rendered(task, previous, last_reply), truncated=False)

    while session_lines and not fits(task, previous):
        session_lines = session_lines[1:]

    if previous is not None and not fits(task, previous):
        whole = previous
        keep = _longest(len(whole), lambda n: fits(task, whole[len(whole) - n :]))
        previous = whole[len(whole) - keep :]

    if last_reply is not None and not fits(task, previous):
        whole_reply, kept_previous = last_reply, previous
        keep = _longest(
            len(whole_reply),
            lambda n: fits(task, kept_previous, whole_reply[len(whole_reply) - n :]),
        )
        last_reply = whole_reply[len(whole_reply) - keep :]

    if not fits(task, previous):
        whole_task, kept_previous, kept_reply = task, previous, last_reply
        task = whole_task[
            : _longest(len(whole_task), lambda n: fits(whole_task[:n], kept_previous, kept_reply))
        ]
    return State(text=rendered(task, previous, last_reply), truncated=True)
