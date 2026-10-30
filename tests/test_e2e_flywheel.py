"""THE CRUX, falsified on purpose: the four-command proof can fail (ticket 029).

FR-25's proof is worth exactly as much as its ability to come out the other way. This file is
that ability, made into a test, and it is deliberately written **before** the happy path ticket
030 adds beside it. A proof whose failure branch is an afterthought is a proof nobody checked.

The whole crux rests on one property: ``with_procedure`` keys on **content the candidate must
actually contain**, never on the candidate's identity. So the falsifying experiment is a single
substitution - one reflection model that writes plausible prose lacking the trait strings - with
every other part of the system left exactly as an operator would run it: the real CLI, the real
corpus, the real graders, the real arithmetic. Then the chain is followed link by link:

    the candidate lacks the traits
      -> the predicate does not match, so ``base`` is served for every case
      -> the candidate run scores what the baseline scored
      -> the promoter refuses, naming ``no_gain``, and the decision record carries both numbers
      -> zero rows reach the active procedure set
      -> and the fourth command re-measures to the first command's score

``src/harnessed/**`` and the fixture corpus are both ``scope_out``, and that is the point: if the
sequence needed a production change or a friendlier fixture to reject, the crux would be staged
and the honest report would be ``blocked``. It needs neither. The one seam used is the one
``cli/improve_cmd.py`` documents as existing for this test - ``reflector(session)`` - and the
provider spy is a wrapper around the same ``FakeProvider`` the CLI would have built.

The variant a response came from is recovered by reading the committed corpus rather than by
reaching into ``FakeProvider``'s private tables: a response id is unique per file, so the map
from id to variant is data, and a corpus edit moves the assertion rather than silently voiding
it.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from conftest import DOCUMENTED_ENV_VARS, OFFLINE_PROVIDER
from harnessed import config
from harnessed.cli import improve_cmd, run_cmd
from harnessed.cli.app import app
from harnessed.improve import promoter
from harnessed.memory import db, procedural
from harnessed.providers import jev
from harnessed.providers import registry as provider_registry
from harnessed.providers.fake import DEFAULT_CORPUS_ROOT, FakeProvider
from harnessed.providers.fake_decider import FakeDecider
from harnessed.providers.jev import Decider
from harnessed.providers.types import ChatRequest, ChatResponse
from support.stub_reflector import VAGUE, VagueReflector

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = REPO_ROOT / "evidence" / "029"
EVIDENCE_030 = REPO_ROOT / "evidence" / "030"

#: The suite the cycle measures and the unseen suite it checks against, spelled as the CLI
#: defaults spell them. Named here so the four commands below read as an operator types them.
SUITE = "smoke"
HOLDOUT = "holdout"

#: The mandatory last variant of every scope. A run in which every response came from this one
#: is a run in which no candidate ever changed what the model said.
BASE = "base"

#: The two traits ``smoke-003-json-only``'s ``_index.yaml`` declares, quoted from the corpus.
#: The falsifying candidate must contain neither; asserting that here rather than trusting the
#: stub is what keeps this test honest if either the corpus or the stub is edited.
TRAITS = ("respond with a single JSON object", "no prose before or after")

#: The rule the arithmetic names when the score did not move and no case regressed. It is a word
#: from the frozen vocabulary in ``evals/scoring.py``, which is the difference between a
#: mechanism that refused and a mechanism that crashed.
NO_GAIN = "no_gain"

runner = CliRunner()
#: Streams kept apart, so ``--json`` can be asserted to have written one object to stdout and
#: nothing to stderr. Same split ``tests/test_cli.py`` uses, for the same reason.
split = CliRunner(mix_stderr=False)


# ---- reading the committed corpus ----------------------------------------------------------------


def variant_by_response_id(root: Path = DEFAULT_CORPUS_ROOT) -> dict[str, str]:
    """``response_id -> variant name`` over every fixture in the corpus.

    Built from the files rather than from ``FakeProvider``'s loaded scopes: the assertion below
    is about which variant was served, and reading the same public data the provider reads keeps
    it from depending on a private attribute that a refactor could rename.
    """
    mapping: dict[str, str] = {}
    for path in sorted(root.glob("*/*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        # ``base.2.json`` is iteration 2 of ``base``; the variant is the first dotted segment.
        mapping[str(payload["response_id"])] = path.name[: -len(".json")].partition(".")[0]
    return mapping


VARIANTS = variant_by_response_id()


# ---- the two substitutions -------------------------------------------------------------------


@dataclass
class SpyProvider:
    """The real ``FakeProvider`` behind a recorder, so every served response is observable.

    A wrapper rather than a replacement: selection, the corpus and the miss behaviour are all
    still the production ones, and the only thing added is a list. Satisfies the ``Provider``
    protocol structurally, which is all the composition root asks of it.
    """

    inner: FakeProvider
    served: list[ChatResponse] = field(default_factory=list)

    def complete(self, request: ChatRequest) -> ChatResponse:
        response = self.inner.complete(request)
        self.served.append(response)
        return response

    @property
    def variants(self) -> tuple[str, ...]:
        """The variant each served response came from, in the order they were served."""
        return tuple(VARIANTS[response.response_id] for response in self.served)


@dataclass(frozen=True)
class Falsified:
    """What the falsifying sequence did: the four payloads, and what the model was asked."""

    before: dict[str, object]
    cycle: dict[str, object]
    decision: dict[str, object]
    after: dict[str, object]
    provider: SpyProvider
    reflector: VagueReflector


# ---- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against the throwaway home, opened the way the CLI opens it."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> SpyProvider:
    """Make every command in this file build the recording provider instead of a bare one.

    Patched at ``providers/registry.py::create``, the one place ``open_session`` constructs a
    provider, so all four commands share one recorder without any command body being reached
    into.
    """
    recorder = SpyProvider(FakeProvider(config.load()))
    monkeypatch.setattr(provider_registry, "create", lambda _settings: recorder)
    return recorder


@pytest.fixture
def vague(monkeypatch: pytest.MonkeyPatch) -> VagueReflector:
    """Substitute the reflection model, and nothing else, at the seam that exists for it."""
    stub = VagueReflector()
    monkeypatch.setattr(improve_cmd, "reflector", lambda _session: stub)
    return stub


def one_object(stdout: str) -> dict[str, object]:
    """Parse ``--json`` output, asserting it is exactly one object and nothing else."""
    text = stdout.strip()
    assert text.count("\n") == 0, f"--json wrote more than one line:\n{stdout}"
    payload = json.loads(text)
    assert isinstance(payload, dict)
    return payload


def issued(argv: list[str]) -> dict[str, object]:
    """One command, as an operator issues it, asserted to have succeeded and written one object.

    Exit code zero on every command is half of the last acceptance criterion: the sequence has
    to be *refused*, and a refusal is a decision the CLI reports successfully, never an error it
    raises. A non-zero exit here would mean the proof failed by crashing.
    """
    result = split.invoke(app, argv)
    assert result.exit_code == 0, f"{argv} exited {result.exit_code}\n{result.stderr}"
    assert result.stderr == "", f"{argv} wrote to stderr: {result.stderr}"
    return one_object(result.stdout)


@pytest.fixture
def falsified(
    clean_env: None,
    tmp_harnessed_home: Path,
    conn: sqlite3.Connection,
    spy: SpyProvider,
    vague: VagueReflector,
) -> Falsified:
    """The four commands of FR-25, from a clean ``.harnessed/``, with a reflector that cannot win.

    The fourth command is ``--fresh`` deliberately. Without it the eval runner resumes the first
    command's finished run over the same suite under the same label and replays its scores off
    the ledger, so "the score did not move" would be true because nothing was measured a second
    time (ticket 026's lesson). ``--fresh`` makes it a genuine second measurement, which is what
    the criterion is actually about.
    """
    before = issued(["eval", "run", SUITE, "--json"])
    cycle = issued(["improve", "run", "--dry-run", "--json"])
    decision = issued(["improve", "promote", str(cycle["cand_id"]), "--json"])
    after = issued(["eval", "run", SUITE, "--fresh", "--json"])
    return Falsified(
        before=before,
        cycle=cycle,
        decision=decision,
        after=after,
        provider=spy,
        reflector=vague,
    )


# ---- the premise -------------------------------------------------------------------------------


def test_the_stub_candidate_carries_neither_trait_the_predicate_keys_on() -> None:
    """The premise of the whole file, asserted rather than assumed.

    Both sides are normalized the way ``providers/fake.py`` normalizes them - collapsed and
    case-folded - because that is the comparison the predicate actually performs. A stub that
    happened to contain a trait would make every assertion below pass for the wrong reason.
    """
    written = " ".join(VAGUE.split()).casefold()
    for trait in TRAITS:
        assert " ".join(trait.split()).casefold() not in written


def test_the_case_scope_still_declares_the_traits_this_test_names() -> None:
    """If a corpus edit moves a trait, this file fails here and says so, not somewhere subtler."""
    index = (DEFAULT_CORPUS_ROOT / "smoke-003-json-only" / "_index.yaml").read_text(
        encoding="utf-8"
    )
    for trait in TRAITS:
        assert trait in index


# ---- the chain, link by link ---------------------------------------------------------------------


def test_proof_can_fail(falsified: Falsified, conn: sqlite3.Connection, tmp_path: Path) -> None:
    """The whole falsifying sequence, end to end, as one readable claim.

    Every link of THE CRUX's causal chain in the order the architecture states it, so a failure
    here names the link that broke rather than an aggregate that stopped holding.
    """
    # A candidate was genuinely written, gated on a genuinely observed failure.
    assert falsified.cycle["candidate"] is True
    assert str(falsified.cycle["cand_id"]).startswith("cand_")
    assert len(falsified.reflector.requests) == 1

    # It lacked the traits, so `base` answered every case and the score did not move.
    assert set(falsified.provider.variants) == {BASE}
    assert falsified.decision["score_after"] == falsified.decision["score_before"]

    # So the promoter refused, by arithmetic, and the active set stayed empty.
    assert falsified.decision["promoted"] is False
    assert falsified.decision["rule"] == NO_GAIN
    assert procedural.active(conn) == ()

    # And a second, genuine measurement reports exactly what the first one did.
    assert falsified.after["score"] == falsified.before["score"]

    capture("proof-can-fail.txt", transcript(falsified))


def test_fake_provider_serves_the_base_variant_for_every_case(falsified: Falsified) -> None:
    """No response in the whole sequence came from a procedure-conditional variant.

    Asserted over every request the four commands issued - both eval runs and both measurements
    inside the cycle - rather than over the one case the flywheel moves, because "the predicate
    did not match" is a claim about the corpus as a whole.
    """
    assert falsified.provider.served, "the sequence issued no provider requests at all"
    assert set(falsified.provider.variants) == {BASE}


def test_the_candidate_scores_exactly_what_the_baseline_scored(falsified: Falsified) -> None:
    """``score_after == score_before``, cross-checked against the command that measured first.

    The equality is asserted against the first command's score as well as within the decision,
    so a cycle that measured a different baseline than the operator did would be visible.
    """
    assert falsified.decision["score_before"] == falsified.before["score"]
    assert falsified.decision["score_after"] == falsified.decision["score_before"]
    assert falsified.decision["pass_count_after"] == falsified.decision["pass_count_before"]


def test_the_promoter_rejects_and_the_record_states_both_numbers(
    falsified: Falsified, conn: sqlite3.Connection
) -> None:
    """The decision is recorded even though it refused, and it carries the arithmetic it used.

    A rejection is a decision, so ``promotions`` holds a row for it (ticket 025). Reading the row
    back is what makes "the decision record states both numbers" a claim about what was durably
    written rather than about what was printed once.
    """
    cand_id = str(falsified.cycle["cand_id"])
    recorded = procedural.promotion(conn, cand_id)
    assert recorded is not None, f"no decision was recorded for {cand_id}"
    assert recorded.promoted is False
    assert recorded.reason == NO_GAIN
    assert recorded.score_before == falsified.decision["score_before"]
    assert recorded.score_after == falsified.decision["score_after"]
    assert recorded.score_before == recorded.score_after


def test_zero_rows_are_added_to_the_active_procedure_set(
    falsified: Falsified, conn: sqlite3.Connection
) -> None:
    """A refused candidate changes nothing an operator would run with.

    The procedure row itself is allowed to exist - procedures are never deleted (FR-20) - so the
    claim is about the *active* set, which is the thing the next system prompt is built from.
    """
    assert procedural.active(conn) == ()
    assert falsified.cycle["promoted"] is False
    assert falsified.decision["promoted"] is False


def test_the_rejection_names_the_arithmetic_rather_than_an_exception(
    falsified: Falsified,
) -> None:
    """The mechanism refused; it did not crash.

    Three things say so together: the rule is a word from the frozen vocabulary, the sentence
    the CLI prints for it describes the score condition, and the caveat that scopes the whole
    claim is still printed under the fake provider.
    """
    assert falsified.decision["rule"] == NO_GAIN
    assert falsified.decision["regressions"] == 0

    result = runner.invoke(app, ["improve", "promote", str(falsified.cycle["cand_id"])])
    assert result.exit_code == 0, result.output
    assert "rejected" in result.output
    assert "no gain" in result.output or "no_gain" in result.output
    assert promoter.CAVEAT in result.output
    assert "Traceback" not in result.output


def test_the_socket_guard_was_active_for_the_whole_sequence(falsified: Falsified) -> None:
    """Zero outbound connections, asserted where it can be asserted rather than asserted by hope.

    The guard is autouse and raises on any of the three entry points that can open a connection,
    so a sequence that finished green is a sequence that opened none. What this adds is the
    proof that the guard was installed at all - without it, green would mean nothing.
    """
    assert getattr(socket.socket.connect, "_harnessed_socket_guard", False) is True
    assert getattr(socket.socket.connect_ex, "_harnessed_socket_guard", False) is True
    assert getattr(socket.create_connection, "_harnessed_socket_guard", False) is True


# ---- evidence ------------------------------------------------------------------------------------


def capture(name: str, text: str, directory: Path = EVIDENCE_DIR) -> Path:
    """Write one evidence artefact. Regenerated on every verify run; never committed."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def transcript(falsified: Falsified) -> str:
    """The falsifying sequence as an operator would have read it, for the ticket's manifest."""
    replay = runner.invoke(app, ["improve", "promote", str(falsified.cycle["cand_id"])])
    lines = [
        "029 - the four-command proof, falsified on purpose",
        "",
        f"reflector wrote: {VAGUE}",
        "variants served: " + ", ".join(sorted(set(falsified.provider.variants))),
        "",
        f"1. {f'eval run {SUITE}':<24}-> score {falsified.before['score']}",
        f"2. {'improve run --dry-run':<24}-> candidate {falsified.cycle['cand_id']}",
        f"3. {'improve promote':<24}-> promoted={falsified.decision['promoted']} "
        f"rule={falsified.decision['rule']} "
        f"{falsified.decision['score_before']} -> {falsified.decision['score_after']}",
        f"4. {f'eval run {SUITE} --fresh':<24}-> score {falsified.after['score']}",
        "",
        "the decision as an operator reads it:",
        replay.output.strip(),
    ]
    return "\n".join(lines) + "\n"


# ==================================================================================================
# THE CRUX, the other way up: the four-command proof succeeds (ticket 030)
# ==================================================================================================
#
# FR-25's definition of done, run as an operator runs it. Ticket 029 above proved the sequence can
# come out negative; what follows proves it comes out positive when the reflector actually writes
# the procedure the failing case needs. The two halves share every helper - `issued`, `one_object`,
# `SpyProvider`, `VARIANTS` - and differ in exactly one fixture: the happy path does **not** request
# `vague`, so the real reflector answers off the committed `_reflect` scope. That single difference
# is the whole experiment.
#
# The proof is asserted at two layers because FR-25 asserts both. In-process through `CliRunner`,
# where the socket guard and the provider spy can observe what happened; and once through a real
# shell, where nothing can be patched at all and the only claim is that the numbers come out the
# same. Neither layer alone is the proof: the first could be an artefact of the test harness, the
# second is a black box.


#: The rule the arithmetic names when the score genuinely moved up. The counterpart of
#: :data:`NO_GAIN`, and like it a word from the frozen vocabulary rather than a sentence.
SCORE = "score"

#: The procedure-conditional variant. A run that served it is a run in which the promoted
#: procedure actually changed what the model said, which is the only reason a score may move.
WITH_PROCEDURE = "with_procedure"

#: The ``_reflect`` scope's non-base variant - the reflection the real reflector receives when the
#: cycle observed ``smoke-003-json-only`` failing. It appears among the served variants because the
#: cycle asks the model to reflect as well as to answer cases, so the closed set below names it
#: rather than letting an unexplained third variant pass unnoticed.
REFLECTION = "json_only_failed"

#: The four commands of FR-25, spelled once, in order, as the ticket spells them. ``{cand}`` is the
#: only hole, because the third command names what the second one wrote.
#:
#: The fourth carries ``--fresh`` deliberately: without it the eval runner resumes the first
#: command's finished run over the same suite under the same label and replays its scores off the
#: ledger, so "the score moved" would be asserted against a measurement that never happened
#: (ticket 026's resume rule, one layer up - see ticket 029's note).
COMMANDS: tuple[tuple[str, ...], ...] = (
    ("eval", "run", SUITE, "--json"),
    ("improve", "run", "--dry-run", "--json"),
    ("improve", "promote", "{cand}", "--json"),
    ("eval", "run", SUITE, "--fresh", "--json"),
)


@dataclass(frozen=True)
class Proof:
    """The four ``--json`` payloads of one sequence, in the order the commands were issued."""

    before: dict[str, object]
    cycle: dict[str, object]
    decision: dict[str, object]
    after: dict[str, object]

    @property
    def cand_id(self) -> str:
        """The candidate the second command wrote and the third command decided on."""
        return str(self.cycle["cand_id"])


def four_commands() -> Proof:
    """Issue FR-25's four commands in order against whatever ``HARNESSED_HOME`` currently names.

    A plain function rather than a fixture so it can be called twice inside one test: the
    reproducibility criterion is about two sequences from two clean states, which a
    function-scoped fixture cannot express.
    """
    before = issued(list(COMMANDS[0]))
    cycle = issued(list(COMMANDS[1]))
    decision = issued([part.format(cand=cycle["cand_id"]) for part in COMMANDS[2]])
    after = issued(list(COMMANDS[3]))
    return Proof(before=before, cycle=cycle, decision=decision, after=after)


@pytest.fixture
def proved(
    clean_env: None,
    tmp_harnessed_home: Path,
    conn: sqlite3.Connection,
    spy: SpyProvider,
) -> Proof:
    """The four commands from a clean ``.harnessed/``, with the real reflector in place.

    Compare with :func:`falsified` above: same fixtures, same helper, minus ``vague``. Nothing
    else is substituted, and the provider spy is a wrapper around the ``FakeProvider`` the
    composition root would have built anyway, so what runs here is the production path.
    """
    return four_commands()


# ---- the proof, in process ---------------------------------------------------------------------


def test_four_command_proof(proved: Proof, spy: SpyProvider, conn: sqlite3.Connection) -> None:
    """FR-25 end to end: the score went up, nothing regressed, and the promotion is durable.

    Written as one readable claim in the order the commands ran, so a failure names the link that
    broke. The two numbers the criterion is actually about - ``score_after > score_before`` and
    ``regressions == 0`` - are asserted both inside the decision the promoter made and across the
    first and fourth commands an operator would read, because a cycle that measured a different
    baseline than the operator did would otherwise be invisible.
    """
    # 1. The baseline is genuinely imperfect. A proof that starts at a perfect score has no room
    #    to move, so the sequence could not tell an improvement from a no-op.
    assert proved.before["score"] < 1.0
    assert proved.before["resumed"] is False

    # 2. A candidate was written, gated on an observed failure, and nothing was promoted yet.
    assert proved.cycle["candidate"] is True
    assert proved.cand_id.startswith("cand_")
    assert proved.cycle["dry_run"] is True
    assert proved.cycle["promoted"] is False

    # 3. The promoter accepted, by arithmetic, with the score strictly up and no case regressed.
    assert proved.decision["promoted"] is True
    assert proved.decision["rule"] == SCORE
    assert proved.decision["regressions"] == 0
    assert proved.decision["score_after"] > proved.decision["score_before"]
    assert proved.decision["score_before"] == proved.before["score"]
    assert proved.decision["pass_count_after"] > proved.decision["pass_count_before"]

    # 4. And a second, genuine measurement of the same suite reports a strictly higher score.
    assert proved.after["fresh"] is True
    assert proved.after["resumed"] is False
    assert proved.after["score"] > proved.before["score"]
    assert proved.after["pass_count"] > proved.before["pass_count"]
    assert proved.after["case_count"] == proved.before["case_count"]

    # 5. The promotion is durable: exactly one procedure is active where none was before.
    assert len(procedural.active(conn)) == 1

    capture("four-command-proof.txt", transcript_030(proved, spy), EVIDENCE_030)


def test_no_case_regressed_between_the_two_measurements(proved: Proof) -> None:
    """Zero regressed, checked case by case rather than only through the promoter's count.

    ``regressions == 0`` is the promoter's own arithmetic over its own two runs. This walks the
    first and fourth commands - the two measurements an operator actually reads - and asserts that
    no case which passed before fails after. The same claim, independently derived, which is what
    makes the count worth trusting.
    """
    before_cases = proved.before["cases"]
    after_cases = proved.after["cases"]
    assert isinstance(before_cases, list) and isinstance(after_cases, list)
    before = {str(case["case_id"]): bool(case["passed"]) for case in before_cases}
    after = {str(case["case_id"]): bool(case["passed"]) for case in after_cases}

    assert set(before) == set(after), "the two measurements did not cover the same cases"
    regressed = sorted(case for case, passed in before.items() if passed and not after[case])
    assert regressed == [], f"cases regressed between the two measurements: {regressed}"
    # And the gain is a real case flipping the other way, not a rounding artefact.
    assert sorted(case for case, passed in before.items() if not passed and after[case])


def test_the_promoted_procedure_changed_what_the_model_said(
    proved: Proof, spy: SpyProvider
) -> None:
    """The gain came from the procedure, not from the corpus deciding to be generous.

    The mirror of ticket 029's ``base``-only assertion. There the candidate lacked the traits so
    the predicate never matched; here it carries them, so at least one response was served from
    the procedure-conditional variant. Without this the score could have moved for a reason
    unrelated to the thing being promoted.
    """
    assert spy.served, "the sequence issued no provider requests at all"
    assert WITH_PROCEDURE in spy.variants
    assert set(spy.variants) == {BASE, WITH_PROCEDURE, REFLECTION}


def test_the_promotion_output_carries_the_provider_caveat(proved: Proof) -> None:
    """The literal caveat line, byte for byte, on the output an operator reads.

    ``--json`` is a machine payload and carries no prose, so the claim is asserted against the
    rendered form. Re-issuing ``improve promote`` replays the recorded decision off the ledger
    rather than deciding again, so this reads the same promotion and not a second one.
    """
    result = runner.invoke(app, ["improve", "promote", proved.cand_id])
    assert result.exit_code == 0, result.output
    assert promoter.CAVEAT in result.output
    assert "Traceback" not in result.output


def test_the_whole_sequence_opened_no_outbound_connection(proved: Proof) -> None:
    """Zero outbound connections, with the guard proved to have been installed.

    The guard is autouse and raises on all three entry points that can open a connection, so a
    sequence that finished green opened none. What this adds is that the guard was in force at
    all - without the check, green would be indistinguishable from unguarded.
    """
    assert getattr(socket.socket.connect, "_harnessed_socket_guard", False) is True
    assert getattr(socket.socket.connect_ex, "_harnessed_socket_guard", False) is True
    assert getattr(socket.create_connection, "_harnessed_socket_guard", False) is True


# ---- the proof, twice --------------------------------------------------------------------------


def test_scores_reproduce(clean_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two full sequences from two clean states produce identical numbers.

    Not two reads of one run: each round gets its own empty ``HARNESSED_HOME``, so every ledger,
    every id and every measurement is made from scratch. Determinism is the property that lets the
    proof be checked by somebody else, so it is asserted over the whole sequence - both scores,
    both pass counts and the candidate id, which is content-addressed and therefore equal only if
    the reflector wrote the same text twice.
    """
    rounds: list[Proof] = []
    for name in ("first", "second"):
        home = tmp_path / name / ".harnessed"
        home.mkdir(parents=True)
        monkeypatch.setenv("HARNESSED_HOME", str(home))
        rounds.append(four_commands())
    first, second = rounds

    # Each round still proves what the sequence is supposed to prove, on its own.
    for proof in rounds:
        assert proof.after["score"] > proof.before["score"]
        assert proof.decision["regressions"] == 0

    assert first.before["score"] == second.before["score"]
    assert first.after["score"] == second.after["score"]
    assert first.before["pass_count"] == second.before["pass_count"]
    assert first.after["pass_count"] == second.after["pass_count"]
    assert first.decision["score_before"] == second.decision["score_before"]
    assert first.decision["score_after"] == second.decision["score_after"]
    assert first.cand_id == second.cand_id


# ---- the proof, through a real shell -------------------------------------------------------------


def shell_env(home: Path) -> dict[str, str]:
    """The process environment a fresh operator would have: everything documented, cleared.

    Built by subtraction from ``os.environ`` rather than from an empty dict, because a minimal
    environment on Windows loses ``SystemRoot`` and ``PATH`` and the interpreter will not start.
    ``HARNESSED_HOME`` is then the single variable set, which is what makes the shell run a clean
    state rather than a second read of the in-process one.
    """
    env = {key: value for key, value in os.environ.items() if key not in DOCUMENTED_ENV_VARS}
    env["HARNESSED_HOME"] = str(home)
    # FR-27 removed the silent default, so a subprocess that named nothing would now fail before
    # reaching the loop. Naming it in the environment rather than in argv is what keeps the two
    # sequences byte-identical on the command line, which is the parity this test asserts.
    env["HARNESSED_PROVIDER"] = OFFLINE_PROVIDER
    return env


def in_shell(argv: list[str], env: dict[str, str]) -> dict[str, object]:
    """One command through a real subprocess, asserted to have succeeded and written one object."""
    completed = subprocess.run(
        [sys.executable, "-m", "harnessed", *argv],
        capture_output=True,
        text=True,
        cwd=Path(env["HARNESSED_HOME"]).parent,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, (
        f"{argv} exited {completed.returncode}\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return one_object(completed.stdout)


@pytest.mark.slow
def test_shell_parity(proved: Proof, tmp_path: Path) -> None:
    """The same four commands, from a real shell, produce exactly the in-process numbers.

    Marked ``slow`` because it pays four interpreter startups; ``slow`` is a reporting marker and
    not an exclusion, and the test is offline, so it stays in the default selection. The point is
    that nothing inside the subprocess is patched - no spy, no fixture-injected reflector, no
    ``CliRunner`` - so if the in-process proof were an artefact of the test harness, the numbers
    would part company here.

    Run ids are deliberately not compared: they are fresh per run by design, and asserting them
    equal would be asserting the opposite of what the ledger promises.
    """
    home = tmp_path / "shell" / ".harnessed"
    home.mkdir(parents=True)
    env = shell_env(home)

    before = in_shell(list(COMMANDS[0]), env)
    cycle = in_shell(list(COMMANDS[1]), env)
    decision = in_shell([part.format(cand=cycle["cand_id"]) for part in COMMANDS[2]], env)
    after = in_shell(list(COMMANDS[3]), env)
    shell = Proof(before=before, cycle=cycle, decision=decision, after=after)

    # The shell run proves FR-25 on its own terms first, so a parity failure below cannot be
    # explained away as "both sides agree that nothing happened".
    assert shell.after["score"] > shell.before["score"]
    assert shell.decision["promoted"] is True
    assert shell.decision["regressions"] == 0

    for name in ("score", "pass_count", "case_count"):
        assert shell.before[name] == proved.before[name], name
        assert shell.after[name] == proved.after[name], name
    for name in ("score_before", "score_after", "regressions", "rule", "promoted"):
        assert shell.decision[name] == proved.decision[name], name
    assert shell.cand_id == proved.cand_id

    capture("shell-parity.txt", parity_transcript(proved, shell), EVIDENCE_030)


# ---- evidence ------------------------------------------------------------------------------------


def transcript_030(proof: Proof, spy: SpyProvider) -> str:
    """The successful sequence as an operator would have read it, for the ticket's manifest."""
    replay = runner.invoke(app, ["improve", "promote", proof.cand_id])
    lines = [
        "030 - the four-command self-improvement proof, in process",
        "",
        "variants served: " + ", ".join(sorted(set(spy.variants))),
        "",
        f"1. {f'eval run {SUITE}':<24}-> score {proof.before['score']} "
        f"({proof.before['pass_count']}/{proof.before['case_count']} passing)",
        f"2. {'improve run --dry-run':<24}-> candidate {proof.cand_id}",
        f"3. {'improve promote':<24}-> promoted={proof.decision['promoted']} "
        f"rule={proof.decision['rule']} "
        f"{proof.decision['score_before']} -> {proof.decision['score_after']} "
        f"regressions={proof.decision['regressions']}",
        f"4. {f'eval run {SUITE} --fresh':<24}-> score {proof.after['score']} "
        f"({proof.after['pass_count']}/{proof.after['case_count']} passing)",
        "",
        f"score_after > score_before: {proof.after['score']} > {proof.before['score']}",
        f"regressions: {proof.decision['regressions']}",
        "",
        "the decision as an operator reads it:",
        replay.output.strip(),
    ]
    return "\n".join(lines) + "\n"


def parity_transcript(in_process: Proof, shell: Proof) -> str:
    """The shell numbers beside the in-process ones, field by field, for the ticket's manifest."""
    rows: tuple[tuple[str, object, object], ...] = (
        ("eval run smoke -> score", in_process.before["score"], shell.before["score"]),
        (
            "eval run smoke -> pass_count",
            in_process.before["pass_count"],
            shell.before["pass_count"],
        ),
        ("improve run --dry-run -> cand_id", in_process.cand_id, shell.cand_id),
        (
            "improve promote -> score_before",
            in_process.decision["score_before"],
            shell.decision["score_before"],
        ),
        (
            "improve promote -> score_after",
            in_process.decision["score_after"],
            shell.decision["score_after"],
        ),
        ("improve promote -> rule", in_process.decision["rule"], shell.decision["rule"]),
        (
            "improve promote -> regressions",
            in_process.decision["regressions"],
            shell.decision["regressions"],
        ),
        ("eval run smoke --fresh -> score", in_process.after["score"], shell.after["score"]),
        (
            "eval run smoke --fresh -> pass_count",
            in_process.after["pass_count"],
            shell.after["pass_count"],
        ),
    )
    lines = [
        "030 - shell parity: python -m harnessed in a fresh HARNESSED_HOME",
        "",
        f"{'field':<38}{'in process':<24}{'shell':<24}equal",
        "-" * 92,
    ]
    lines.extend(
        f"{name:<38}{mine!s:<24}{theirs!s:<24}{mine == theirs}" for name, mine, theirs in rows
    )
    lines += ["", "run ids are not compared: they are fresh per run by design."]
    return "\n".join(lines) + "\n"


# ---- Jev never enters the flywheel (ticket 177, FR-119) ------------------------------------------

EVIDENCE_177 = REPO_ROOT / "evidence" / "177"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"


@dataclass
class DeciderSpy:
    """Every Decider built and every Decider call, whichever door opened it.

    Installed on the classes and on both names ``build_decider`` goes by, so a Decider the
    flywheel reached by any route - the composition root, a direct construction, a stray call -
    lands in one of the two lists. A wrapper rather than a replacement: each call is recorded,
    then handed to the real method.
    """

    built: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)


@pytest.fixture
def decider_spy(monkeypatch: pytest.MonkeyPatch) -> DeciderSpy:
    recorder = DeciderSpy()

    def recording_build(settings: config.Settings, *, model: str | None = None) -> Decider:
        recorder.built.append(settings.require_provider())
        return real_build(settings, model=model)

    def spied(cls: type[Any], name: str) -> None:
        real = cls.decide

        def decide(self: object, state: str, *args: object, **kwargs: object) -> object:
            recorder.calls.append(name)
            return real(self, state, *args, **kwargs)

        monkeypatch.setattr(cls, "decide", decide)

    real_build = jev.build_decider
    monkeypatch.setattr(jev, "build_decider", recording_build)
    monkeypatch.setattr(run_cmd, "build_decider", recording_build)
    spied(FakeDecider, "FakeDecider")
    spied(jev.JevDecider, "JevDecider")
    return recorder


def fixture_fingerprint(root: Path = FIXTURE_ROOT) -> dict[str, str]:
    """``relative path -> sha256`` over every committed fixture, so "0 changed" is byte-exact."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def test_the_decider_spy_counts_a_routed_door(
    clean_env: None, tmp_harnessed_home: Path, decider_spy: DeciderSpy
) -> None:
    """Not vacuous: the operator's routed door builds a Decider and the spy sees it, and a call
    through that Decider is counted. Without this the zero below could mean a spy that is blind."""
    assert config.load().router is True, "routing is off, so a zero would prove nothing"
    session = run_cmd.open_session(routed=True)
    try:
        assert decider_spy.built == [OFFLINE_PROVIDER]
        assert isinstance(session.decider, FakeDecider)
        session.decider.decide("Task: spy control", (), scope=session.decider.scopes[0])
    finally:
        session.conn.close()
    assert decider_spy.calls == ["FakeDecider"]


def test_jev_never_enters_eval_scoring_reflection_or_promotion(
    clean_env: None,
    tmp_harnessed_home: Path,
    conn: sqlite3.Connection,
    spy: SpyProvider,
    decider_spy: DeciderSpy,
) -> None:
    """FR-119: 0 Decider builds and 0 calls across the four commands and the holdout, with routing
    on, and the four-command proof still passes with 0 changed fixtures (MUST rule 30)."""
    assert config.load().router is True, "routing is off, so a zero would prove nothing"
    fixtures_before = fixture_fingerprint()

    proof = four_commands()
    holdout = issued(["eval", "run", HOLDOUT, "--fresh", "--json"])

    assert decider_spy.built == [], f"a Decider was built: {decider_spy.built}"
    assert decider_spy.calls == [], f"the Decider was called: {decider_spy.calls}"

    # The proof is the same proof, untouched: up, no regression, promoted by the arithmetic.
    assert proof.after["score"] > proof.before["score"]
    assert proof.decision["promoted"] is True
    assert proof.decision["regressions"] == 0
    assert proof.cycle["dry_run"] is True
    assert holdout["case_count"] >= 1

    fixtures_after = fixture_fingerprint()
    changed = sorted(
        name
        for name in fixtures_before.keys() | fixtures_after.keys()
        if fixtures_before.get(name) != fixtures_after.get(name)
    )
    assert changed == [], f"the sequence changed fixtures: {changed}"

    capture(
        "decider-spy.txt",
        "\n".join(
            [
                "177 - Decider spy across the flywheel, routing on (HARNESSED_ROUTER default)",
                "",
                *(f"$ harnessed {' '.join(argv)}" for argv in COMMANDS),
                f"$ harnessed eval run {HOLDOUT} --fresh --json",
                "",
                f"Decider builds: {len(decider_spy.built)}",
                f"Decider calls:  {len(decider_spy.calls)}",
                f"score {proof.before['score']} -> {proof.after['score']}, "
                f"rule {proof.decision['rule']}, regressions {proof.decision['regressions']}",
                f"holdout cases: {holdout['case_count']}, score {holdout['score']}",
                f"fixtures hashed: {len(fixtures_before)}, changed: {len(changed)}",
            ]
        )
        + "\n",
        EVIDENCE_177,
    )
