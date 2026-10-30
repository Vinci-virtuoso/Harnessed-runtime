"""FR-20 / FR-28: the samples a measurement drew survive the payload boundary.

Ticket 050 gave the promoter a rule - a comparison whose sides were drawn fewer times than the
caller's declared minimum is refused - and ticket 052 exists because that rule had never been
satisfiable. ``Measurement.result`` rebuilt its :class:`~harnessed.evals.scoring.RunResult` from
the recorded cases and let ``samples`` default to 1, so every cycle round-tripped its draws
through a payload that dropped the one number the promoter reads. ``improve run --samples 10``
against a keyed provider was refused as ``undersampled``, exactly as ``--samples 1`` is.

The shape of the test matters more than the fix here. Asserting "a cycle with ``--samples 3``
promotes" would be the same vacuous assertion 050 shipped, pointed the other way: it passes when
the minimum is loosened as happily as when the leak is closed. So the value is pinned **at the
boundary** - a spy standing in front of ``promoter.promote`` reads the ``RunResult`` objects the
cycle actually hands it - and the refusal at one draw is asserted alongside it, so a fix that
widened the rule instead of closing the leak fails here rather than passing quietly.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from harnessed import config
from harnessed.cli import improve_cmd
from harnessed.cli.app import app
from harnessed.evals.scoring import MINIMUM_LIVE_SAMPLES, UNDERSAMPLED, RunResult
from harnessed.evals.suite import Suite, load_named_suite
from harnessed.gateway import create_app
from harnessed.improve import promoter as promoter_module
from harnessed.improve import steps as steps_module
from harnessed.improve.promoter import Outcome
from harnessed.loop.engine import LoopResult, run_loop
from harnessed.loop.prompt import build_system_prompt
from harnessed.memory import db
from harnessed.providers.fake import FakeProvider
from harnessed.providers.types import ChatRequest
from harnessed.tools import default_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "evidence" / "052"

MODEL = "fake-1"

#: What a live caller declares. Read off the constant rather than written as ``3``, so a test
#: that only passed because somebody lowered the minimum cannot exist in this file.
LIVE = MINIMUM_LIVE_SAMPLES

runner = CliRunner()


# ---- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_harnessed_home: Path) -> Iterator[sqlite3.Connection]:
    """A migrated connection against a fresh database under the temporary home."""
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def smoke() -> Suite:
    return load_named_suite("smoke")


@pytest.fixture
def holdout() -> Suite:
    return load_named_suite("holdout")


def real_solver(home: Path) -> Callable[..., LoopResult]:
    """The real loop, the real FakeProvider and the real registry behind one callable."""
    provider = FakeProvider(config.load())
    registry = default_registry(home)

    def solve(request: ChatRequest, *, run_key: str | None = None) -> LoopResult:
        return run_loop(provider, registry, request, limit=6)

    return solve


@dataclass
class PromoterSpy:
    """Stands in front of ``promoter.promote`` and records what the cycle handed it.

    The point of this file. The promoter's arithmetic is exercised by ``test_promoter.py``; what
    is under test here is ``samples`` on the two results that arrive, because that is the number
    the rule reads and it is the number the payload used to drop.
    """

    before: RunResult | None = None
    after: RunResult | None = None
    unseen: RunResult | None = None
    calls: int = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> PromoterSpy:
        real = promoter_module.promote

        def spy(
            cand_id: str,
            text: str,
            before: RunResult,
            after: RunResult,
            holdout: RunResult | None,
            **kwargs: object,
        ) -> Outcome:
            self.calls += 1
            self.before, self.after, self.unseen = before, after, holdout
            return real(cand_id, text, before, after, holdout, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(steps_module.promoter_module, "promote", spy)
        return self


@dataclass
class CycleSpy:
    """Records the keyword arguments the CLI hands ``_cycle``, and runs nothing."""

    seen: list[dict[str, object]] = field(default_factory=list)

    def __call__(self, suite: str, holdout: str, **kwargs: object) -> None:
        self.seen.append(dict(kwargs))

    @property
    def samples(self) -> object:
        return self.seen[-1]["samples"]


def live_cycle(
    conn: sqlite3.Connection,
    home: Path,
    smoke: Suite,
    holdout: Suite,
    *,
    samples: int,
) -> steps_module.Cycle:
    """One cycle wired the way the composition root wires a keyed provider.

    ``minimum_samples=LIVE`` is exactly what ``improve_cmd._minimum_for`` declares for a provider
    that needs a key. The solver underneath replays, because reaching a real endpoint from a test
    is what the socket guard exists to stop: the substrate is not what is under test here, the
    number that reaches the promoter is.
    """
    return steps_module.run_cycle(
        conn,
        suite=smoke,
        holdout=holdout,
        solve=real_solver(home),
        reflector=FakeProvider(config.load()),
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
        samples=samples,
        minimum_samples=LIVE,
    )


def one_home(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    smoke: Suite,
    holdout: Suite,
    *,
    samples: int,
) -> steps_module.Cycle:
    """One live-wired cycle in a database of its own, so two arms cannot decide each other."""
    home = root / ".harnessed"
    home.mkdir(parents=True)
    monkeypatch.setenv("HARNESSED_HOME", str(home))
    connection = db.connect()
    try:
        return live_cycle(connection, home, smoke, holdout, samples=samples)
    finally:
        connection.close()


# ---- the boundary the value used to fall through -------------------------------------------------


def test_the_promoter_receives_the_samples_the_cycle_actually_drew(
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    smoke: Suite,
    holdout: Suite,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole ticket in one assertion, read off the arguments rather than off the verdict."""
    spy = PromoterSpy().install(monkeypatch)

    cycle = live_cycle(conn, tmp_harnessed_home, smoke, holdout, samples=LIVE)

    assert spy.calls == 1
    assert spy.before is not None and spy.after is not None
    assert spy.before.samples == LIVE
    assert spy.after.samples == LIVE
    assert spy.unseen is not None and spy.unseen.samples == LIVE
    assert cycle.outcome is not None
    assert cycle.outcome.rule != UNDERSAMPLED


def test_a_live_minimum_is_met_by_drawing_enough_and_the_score_decides(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """FR-28's second half: against a live minimum, a promotion is reachable at all."""
    cycle = live_cycle(conn, tmp_harnessed_home, smoke, holdout, samples=LIVE)

    assert cycle.outcome is not None
    assert cycle.outcome.rule == "score"
    assert cycle.outcome.promoted is True
    assert cycle.outcome.samples_before == LIVE
    assert cycle.outcome.samples_after == LIVE


def test_one_draw_against_a_live_minimum_is_still_refused(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """The rule is repaired, not removed. Lowering the minimum would fail here."""
    cycle = live_cycle(conn, tmp_harnessed_home, smoke, holdout, samples=1)

    assert cycle.outcome is not None
    assert cycle.outcome.promoted is False
    assert cycle.outcome.rule == UNDERSAMPLED


def test_a_recorded_measurement_carries_its_draws_back_off_the_ledger(
    conn: sqlite3.Connection,
) -> None:
    """The payload is the boundary, so the row is what has to hold the number."""
    run_id, _ = steps_module.start(conn, "smoke")
    stored = steps_module.put(
        conn,
        run_id,
        steps_module.BASELINE,
        "smoke",
        {
            "eval_run_id": "evr_1",
            "suite": "smoke",
            "label": "baseline",
            "samples": LIVE,
            "cases": [{"case_id": "a", "score": 1.0, "passed": True, "detail": "", "attempt": 1}],
        },
    )

    recovered = steps_module.measurement(stored)

    assert recovered.samples == LIVE
    assert recovered.result.samples == LIVE


def test_a_measurement_row_written_before_this_ticket_reads_as_one_draw(
    conn: sqlite3.Connection,
) -> None:
    """A row with no ``samples`` key is one draw, which is what it was when it was written."""
    run_id, _ = steps_module.start(conn, "smoke")
    stored = steps_module.put(
        conn,
        run_id,
        steps_module.BASELINE,
        "smoke",
        {
            "eval_run_id": "evr_1",
            "suite": "smoke",
            "label": "baseline",
            "cases": [{"case_id": "a", "score": 1.0, "passed": True, "detail": "", "attempt": 1}],
        },
    )

    assert steps_module.measurement(stored).samples == 1


def test_a_resumed_cycle_reports_the_draws_the_first_pass_paid_for(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """The replay is the other lossy path, so the recorded decision carries its draws too."""
    first = live_cycle(conn, tmp_harnessed_home, smoke, holdout, samples=LIVE)
    resumed = live_cycle(conn, tmp_harnessed_home, smoke, holdout, samples=LIVE)

    assert first.outcome is not None and resumed.outcome is not None
    assert resumed.outcome.replayed is True
    assert resumed.outcome.samples_before == first.outcome.samples_before == LIVE
    assert resumed.outcome.samples_after == first.outcome.samples_after == LIVE


# ---- the CLI, where the promote path dropped it entirely -----------------------------------------


def test_promote_hands_cycle_the_same_samples_the_run_path_would(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC2. ``improve promote`` used to call ``_cycle`` with no ``samples`` argument at all."""
    spy = CycleSpy()
    monkeypatch.setattr(improve_cmd, "_cycle", spy)

    assert runner.invoke(app, ["improve", "run"]).exit_code == 0
    from_run = spy.samples
    assert runner.invoke(app, ["improve", "promote", "cand_x"]).exit_code == 0

    assert spy.samples == from_run


def test_promote_forwards_the_draws_it_was_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """And the flag is real: a value given on the promote path reaches ``_cycle`` unchanged."""
    spy = CycleSpy()
    monkeypatch.setattr(improve_cmd, "_cycle", spy)

    result = runner.invoke(app, ["improve", "promote", "cand_x", "--samples", str(LIVE)])

    assert result.exit_code == 0
    assert spy.samples == LIVE


# ---- ticket 095: an undecided cycle serves null, not zero ---------------------------------------

#: The six fields FR-46 says no ledger holds when nothing was decided or measured.
UNDECIDED_FIELDS = (
    "score_before",
    "score_after",
    "pass_count_before",
    "pass_count_after",
    "regressions",
    "soft_drops",
)

#: The decided cycle's bytes as they were before ticket 095, run id elided. Captured from the
#: unchanged emitter on the same seeded cycle; a decided payload must not move.
DECIDED_BYTES = (
    '{"cand_id":"cand_4820460a275e0bc8","candidate":true,"dry_run":false,'
    '"improvement_run_id":"IMP","model":"fake-1","pass_count_after":5,"pass_count_before":4,'
    '"promoted":true,"provider":"fake","regressions":0,"resumed":false,"rule":"score","score_after":1.0,"score_before":0.8,'
    '"scored":5,"soft_drops":0,"steps":["baseline","reflect","candidate","holdout","promote"],'
    '"suite":"smoke"}'
)


def full_cycle(conn: sqlite3.Connection, home: Path) -> steps_module.Cycle:
    """One fresh, full, decided cycle on the shipped suites: the shape ``cycle_for`` reads back."""
    provider = FakeProvider(config.load())
    return steps_module.run_cycle(
        conn,
        suite=load_named_suite("smoke"),
        holdout=load_named_suite("holdout"),
        solve=real_solver(home),
        reflector=provider,
        model=MODEL,
        provider="fake",
        build=build_system_prompt,
    )


def served(
    monkeypatch: pytest.MonkeyPatch, home: Path, improvement_run_id: str
) -> tuple[bytes, bytes]:
    """The cycle's bytes through ``improve run --json`` and through the gateway, both halves.

    The CLI half runs the real command and the real renderer; only the cycle it renders is the
    recorded one, because ``run_cycle`` always measures and so cannot produce an empty cycle.
    """
    reader = db.connect()
    try:
        cycle = steps_module.cycle_for(reader, improvement_run_id)
    finally:
        reader.close()
    assert cycle is not None
    monkeypatch.setattr(improve_cmd.steps, "run_cycle", lambda *args, **kwargs: cycle)
    result = runner.invoke(app, ["improve", "run", "--json"])
    assert result.exit_code == 0, result.output
    with TestClient(create_app(home / db.DATABASE_FILENAME)) as client:
        response = client.get(f"/v1/improve/cycles/{improvement_run_id}")
    assert response.status_code == 200, response.text
    return result.stdout.encode("utf-8").rstrip(b"\n"), response.content


def undecided_run(conn: sqlite3.Connection) -> str:
    """A started run with no step rows: interrupted before anything was measured."""
    improvement_run_id, _ = steps_module.start(conn, "smoke", fresh=True)
    return improvement_run_id


def baseline_only_run(conn: sqlite3.Connection, home: Path) -> tuple[str, float]:
    """A run whose ledger holds a measured baseline and nothing after it."""
    decided = full_cycle(conn, home)
    baseline = steps_module.get(conn, decided.improvement_run_id, steps_module.BASELINE, "smoke")
    assert baseline is not None
    improvement_run_id = undecided_run(conn)
    steps_module.put(conn, improvement_run_id, steps_module.BASELINE, "smoke", baseline.payload)
    return improvement_run_id, steps_module.measurement(baseline).result.score


def test_an_undecided_cycle_serves_six_nulls_on_both_surfaces(
    clean_env: None,
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    improvement_run_id = undecided_run(conn)
    cli, http = served(monkeypatch, tmp_harnessed_home, improvement_run_id)
    assert cli == http
    payload = json.loads(http)
    assert payload["rule"] == ""
    assert {name: payload[name] for name in UNDECIDED_FIELDS} == dict.fromkeys(UNDECIDED_FIELDS)
    assert [name for name in UNDECIDED_FIELDS if payload[name] == 0] == []


def test_a_baseline_without_a_candidate_serves_its_measured_score_and_nulls(
    clean_env: None,
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    improvement_run_id, measured = baseline_only_run(conn, tmp_harnessed_home)
    cli, http = served(monkeypatch, tmp_harnessed_home, improvement_run_id)
    assert cli == http
    payload = json.loads(http)
    assert payload["rule"] == ""
    assert payload["score_before"] == measured
    assert isinstance(payload["score_before"], float)
    assert payload["score_after"] is None
    for name in UNDECIDED_FIELDS[2:]:
        assert payload[name] is None, name


def test_a_decided_cycle_serves_the_bytes_it_served_before(
    clean_env: None,
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decided = full_cycle(conn, tmp_harnessed_home)
    pinned = DECIDED_BYTES.replace("IMP", decided.improvement_run_id).encode("utf-8")
    assert steps_module.render_json(decided).encode("utf-8") == pinned
    cli, http = served(monkeypatch, tmp_harnessed_home, decided.improvement_run_id)
    assert cli == http == pinned


def test_describe_reads_an_unmeasured_candidate_without_inventing_a_zero() -> None:
    cycle = steps_module.Cycle(
        improvement_run_id="imp_x",
        suite="smoke",
        cand_id="cand_x",
        resumed=False,
        dry_run=True,
        steps=(),
    )
    assert cycle.score_before is None
    assert cycle.score_after is None
    text = steps_module.describe(cycle, provider="fake")
    assert "score 0.0" not in text
    assert "not measured" in text


def test_capture_the_undecided_cycle_bytes(
    clean_env: None,
    conn: sqlite3.Connection,
    tmp_harnessed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence for 095: the CLI and HTTP bytes of the three cycle shapes, side by side."""
    shapes = {
        "undecided": undecided_run(conn),
        "baseline-only": baseline_only_run(conn, tmp_harnessed_home)[0],
        "decided": full_cycle(conn, tmp_harnessed_home).improvement_run_id,
    }
    lines: list[str] = []
    for label, improvement_run_id in shapes.items():
        cli, http = served(monkeypatch, tmp_harnessed_home, improvement_run_id)
        assert cli == http
        lines += [f"# {label}", f"cli:  {cli.decode()}", f"http: {http.decode()}", ""]
    target = REPO_ROOT / "evidence" / "095" / "undecided-cycle.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


# ---- evidence ------------------------------------------------------------------------------------


def test_capture_the_samples_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, smoke: Suite, holdout: Suite
) -> None:
    """Write ``evidence/052/samples-survive.txt``: the same cycle at one draw and at the minimum.

    Each arm gets its own home. A promotion is content-addressed over the candidate, so a second
    cycle in the same database replays the first one's recorded outcome rather than deciding
    again - which would print the one-draw refusal twice and call it a comparison.
    """
    refused = one_home(tmp_path / "refused", monkeypatch, smoke, holdout, samples=1)
    decided = one_home(tmp_path / "decided", monkeypatch, smoke, holdout, samples=LIVE)

    transcript = "\n\n".join(
        (
            f"--samples 1, minimum {LIVE}:",
            steps_module.describe(refused, provider="fake"),
            f"--samples {LIVE}, minimum {LIVE}:",
            steps_module.describe(decided, provider="fake"),
        )
    )
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "samples-survive.txt").write_text(transcript + "\n", encoding="utf-8")

    assert refused.rule == UNDERSAMPLED
    assert decided.rule == "score"


# ---- ticket 053: the caveat is a function of the provider, not a constant -----------------------


#: A keyed live provider, named once. The caveat belongs to ``fake`` and to nothing else.
LIVE_PROVIDER = "deepseek"


def test_describe_prints_the_caveat_for_a_fake_run_and_not_for_a_live_one(
    conn: sqlite3.Connection, tmp_harnessed_home: Path, smoke: Suite, holdout: Suite
) -> None:
    """The pairing, one rendering layer below the CLI.

    Asserted as a pair rather than as two facts, because either half alone is satisfiable by a
    bug: the presence half passed while the argument was hardcoded, and the absence half passes
    if ``describe`` stops rendering the decision at all.
    """
    cycle = live_cycle(conn, tmp_harnessed_home, smoke, holdout, samples=LIVE)

    fake = steps_module.describe(cycle, provider=promoter_module.FAKE).splitlines()
    live = steps_module.describe(cycle, provider=LIVE_PROVIDER).splitlines()

    assert promoter_module.CAVEAT in fake
    assert promoter_module.CAVEAT not in live
    assert live == [line for line in fake if line != promoter_module.CAVEAT]


def test_describe_cannot_be_called_without_being_told_the_provider() -> None:
    """AC3, as a shape rather than as a string: no default is where the old literal would return."""
    provider = inspect.signature(steps_module.describe).parameters["provider"]

    assert provider.kind is inspect.Parameter.KEYWORD_ONLY
    assert provider.default is inspect.Parameter.empty


def test_steps_names_no_provider_literal_at_the_promoter_boundary() -> None:
    """AC3. The composition root has the real value; the renderer must not supply one.

    A scan rather than an assertion on output, because the defect this ticket fixes was invisible
    to output: the caveat was printed, correctly worded, on every run - and MUST rule 12 is that
    an assertion on the wording of a claim cannot detect a false claim.
    """
    source = (Path(steps_module.__file__)).read_text(encoding="utf-8")

    assert "promoter_module.FAKE" not in source
    assert '"fake"' not in source and "'fake'" not in source
