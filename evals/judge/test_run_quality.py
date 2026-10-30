"""The judge grades the run, not just the reply (ticket 044).

The tier's first two cases both asked "was that a good answer". Three questions an operator
actually has about an agent were unasked, and two of them were unaskable until ticket 041 put the
retrieval layer on the run path:

* **Retrieval** - was the context the model got the context the task needed?
* **Tool selection** - a deterministic case can say a tool fired; only judgement says it was the
  right one with the right arguments.
* **Reasoning** - do the steps of the transcript follow from the task and from what came back?

Each is one question. A metric scoring "was this run good" produces a reason that reads like a
review, and nobody can act on a review.

**Every case here goes through :func:`evals.judge.metric.discriminate`**, which measures a
scripted good input and a scripted bad input through the same metric configuration before it
reports the real one. That is ticket 043's rule made structural: a case that cannot separate its
own references is a case that could be stuck at a constant, and it fails loudly instead of
reporting a number nobody should believe.

The runs under test are driven with a **scripted client**; the judging is live. What is being
measured is the harness's own behaviour - which context it assembled, which call it made - and a
live model would add a second source of variance to a claim that is not about the model at all.

That split needs care, and getting it wrong is silent. ``make_harness`` names a provider in the
environment as well as on the session, and ``judge_model`` builds its own provider from
``config.load()`` - so a harness built with the offline provider leaves the *judge* pointed at
FakeProvider, which then has no fixture for a rubric it has never seen. Every case here therefore
passes ``provider=None``: the session still runs on the scripted client handed to it, and the
configured live provider is what the judge finds when it looks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from evals.helpers import (
    ask_with_memory,
    live_eval,
    make_harness,
    text_response,
    tool_response,
)
from evals.judge.judge_model import available
from evals.judge.metric import discriminate
from harnessed.memory import semantic

#: What the judge needs left in the environment to reach the operator's own endpoint. The
#: harness clears every documented variable, and the judge builds its provider afterwards.
JUDGE_ENV = ("HARNESSED_MODEL", "HARNESSED_BASE_URL")

needs_judge = pytest.mark.skipif(
    not available(),
    reason=(
        "the judge tier needs the [eval] extra (uv sync --all-extras) and a configured provider "
        "key; deepeval is never a base install"
    ),
)


# ---- retrieval ---------------------------------------------------------------------------------

RETRIEVAL_TASK = "What port does the ingest service listen on?"

RETRIEVAL_RUBRIC = (
    "The actual output is the block of remembered notes that was handed to an assistant before it "
    "answered the input question. Score it on whether the notes actually contain what is needed "
    "to answer that question. Full marks if the answering fact is present and the block is not "
    "padded with unrelated notes; no marks if the answering fact is absent, however many other "
    "notes are there. Judge only relevance to the question - do not judge writing style, and do "
    "not penalise the block for being a list."
)

#: The fact the task needs, written so its words overlap the question - retrieval rebuilds its
#: query from the task's own words, so a fact sharing none of them would be missing for a reason
#: that has nothing to do with what is being measured.
ANSWERING_FACT = "the ingest service listens on port 8471"

#: Notes that are plainly about something else. These are what a store returns when its search
#: found nothing useful, and what the bad reference is built from.
DISTRACTOR_FACTS = (
    "the billing exporter runs every Sunday at 02:00",
    "the staging cluster has three nodes",
    "invoices are archived after seven years",
)


@live_eval
@needs_judge
def test_the_retrieved_context_is_judged_for_relevance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: Any
) -> None:
    """A run whose store holds one answering fact among distractors. Did retrieval find it?"""
    monkeypatch.chdir(tmp_path)
    from evals.helpers import ScriptedClient

    client = ScriptedClient([text_response("Port 8471.")])
    session = make_harness(
        tmp_path / "home", monkeypatch, client=client, provider=None, keep=JUDGE_ENV
    )
    semantic.put(session.conn, ANSWERING_FACT, source="test")
    for distractor in DISTRACTOR_FACTS:
        semantic.put(session.conn, distractor, source="test")

    run = ask_with_memory(session, RETRIEVAL_TASK)

    judged = discriminate(
        name="retrieval_relevance",
        rubric=RETRIEVAL_RUBRIC,
        task=RETRIEVAL_TASK,
        good=f"# Facts\n- {ANSWERING_FACT}",
        bad="# Facts\n" + "\n".join(f"- {fact}" for fact in DISTRACTOR_FACTS),
        observed=run.assembled.text or "(nothing was retrieved)",
    )
    record(judged.proof())
    record(judged.measurement())
    judged.assert_metric_is_sound()


# ---- tool selection ----------------------------------------------------------------------------

TOOL_TASK = "Read the file notes.txt and tell me what it says."

TOOL_RUBRIC = (
    "The actual output is the single tool call an assistant chose to make in response to the "
    "input request, written as a function name and its arguments. Score it on whether that is the "
    "right call for the request: the right operation, aimed at the right target. A call that "
    "reads when the request asks to read scores full marks; a call that writes, deletes, or "
    "targets something the request never mentioned scores none. Judge the choice of call only - "
    "there is no prose here to assess."
)


@live_eval
@needs_judge
def test_the_tool_the_model_chose_is_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: Any
) -> None:
    """That *a* tool fired is a deterministic fact. That it was the right one is judgement."""
    monkeypatch.chdir(tmp_path)
    from evals.helpers import ScriptedClient

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "notes.txt").write_text("the roof needs fixing\n", encoding="utf-8")

    client = ScriptedClient(
        [
            tool_response("fs_read", {"path": "notes.txt"}),
            text_response("It says the roof needs fixing."),
        ]
    )
    session = make_harness(home, monkeypatch, client=client, provider=None, keep=JUDGE_ENV)
    run = ask_with_memory(session, TOOL_TASK)

    assert run.result.tool_calls, "the scripted run made no tool call at all"
    chosen = run.result.tool_calls[0]

    judged = discriminate(
        name="tool_selection",
        rubric=TOOL_RUBRIC,
        task=TOOL_TASK,
        good='fs_read(path="notes.txt")',
        bad='fs_write(path="secrets.txt", content="overwritten")',
        observed=f"{chosen.name}({', '.join(f'{k}={v!r}' for k, v in chosen.arguments.items())})",
    )
    record(judged.proof())
    record(judged.measurement())
    judged.assert_metric_is_sound()


# ---- reasoning ---------------------------------------------------------------------------------

REASONING_TASK = "Read the file notes.txt and tell me what it says."

REASONING_RUBRIC = (
    "The actual output is a transcript of the steps an assistant took to answer the input "
    "request, in order. Score it on whether each step follows from the request and from what the "
    "previous step returned: the assistant should look at what it was asked about, and its final "
    "answer should be consistent with what it actually got back. Penalise a transcript whose "
    "final answer contradicts, or has nothing to do with, what the earlier steps returned. Judge "
    "the coherence of the sequence, not the writing."
)

GOOD_TRANSCRIPT = (
    "1. asked to read notes.txt\n"
    "2. read notes.txt, which returned: the roof needs fixing\n"
    "3. answered: it says the roof needs fixing"
)

BAD_TRANSCRIPT = (
    "1. asked to read notes.txt\n"
    "2. read notes.txt, which returned: the roof needs fixing\n"
    "3. answered: the invoice totals 4,000 euros and is due on Friday"
)


def _transcript_of(run: Any) -> str:
    """The run's steps, as lines a reader could check against each other."""
    lines = [f"1. asked to {REASONING_TASK[0].lower()}{REASONING_TASK[1:]}"]
    step = 2
    for call in run.result.tool_calls:
        arguments = ", ".join(f"{k}={v!r}" for k, v in call.arguments.items())
        lines.append(f"{step}. called {call.name}({arguments})")
        step += 1
    lines.append(f"{step}. answered: {run.result.reply}")
    return "\n".join(lines)


@live_eval
@needs_judge
def test_the_transcript_is_judged_for_coherence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, record: Any
) -> None:
    """Do the steps follow from each other, or did the answer arrive from somewhere else?"""
    monkeypatch.chdir(tmp_path)
    from evals.helpers import ScriptedClient

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "notes.txt").write_text("the roof needs fixing\n", encoding="utf-8")

    client = ScriptedClient(
        [
            tool_response("fs_read", {"path": "notes.txt"}),
            text_response("It says the roof needs fixing."),
        ]
    )
    session = make_harness(home, monkeypatch, client=client, provider=None, keep=JUDGE_ENV)
    run = ask_with_memory(session, REASONING_TASK)

    judged = discriminate(
        name="transcript_coherence",
        rubric=REASONING_RUBRIC,
        task=REASONING_TASK,
        good=GOOD_TRANSCRIPT,
        bad=BAD_TRANSCRIPT,
        observed=_transcript_of(run),
    )
    record(judged.proof())
    record(judged.measurement())
    judged.assert_metric_is_sound()
