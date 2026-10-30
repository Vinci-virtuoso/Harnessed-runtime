"""The one error tree. Every module raises a named child of :class:`HarnessError`.

No bare ``Exception``, no ``KeyError`` escaping a lookup, no traceback as a user message. The
CLI maps ``HarnessError`` to stderr plus exit code 1; anything else propagates as a traceback,
because an unexpected traceback is a bug report and must not be dressed up as a friendly
message.

The whole tree ships at once even though most children have no raiser yet. Later tickets raise
from it. One extends it: FR-27's ``ProviderNotConfiguredError``, added by ticket 032, because an
unconfigured harness is a distinct failure from a misconfigured one and the CLI has to tell them
apart without matching on a message.
"""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "AcpVersionMismatch",
    "AgentNotFoundError",
    "CandidateRejectedError",
    "ConfigError",
    "ControlError",
    "CorpusMissing",
    "CredentialRefused",
    "CursorError",
    "DecisionDropped",
    "DecisionError",
    "DecisionRefused",
    "DecisionTimeout",
    "EntryMissing",
    "DecisionUnreachable",
    "DelegationError",
    "DelegationTimeout",
    "DirtyTreeRefused",
    "QuotaRefused",
    "EvalError",
    "EvidenceIncomplete",
    "EventError",
    "FixtureCorpusError",
    "FixtureMissError",
    "GatewayError",
    "GatewayExtraMissing",
    "GraderError",
    "HarnessError",
    "HomeNotFoundError",
    "ImproveError",
    "InvalidCandidateError",
    "LimitError",
    "MigrationChecksumError",
    "MigrationError",
    "MissingCredentialError",
    "ParameterError",
    "PermissionAnswered",
    "ProviderError",
    "PromptError",
    "ProviderNotConfiguredError",
    "RouterError",
    "RunInFlight",
    "SchemaOutOfDateError",
    "StorageError",
    "SuiteLoadError",
    "SuiteMismatch",
    "TaskAwaitingAnswer",
    "TaskStateError",
    "SurfaceNotFound",
    "ToolArgumentError",
    "ToolError",
    "ToolNotFoundError",
    "TranslationError",
    "TransportDropped",
    "UnknownEventError",
]


class HarnessError(Exception):
    """Root of the tree. Never raised directly except by the offline contract in doctor."""


# ---- configuration -------------------------------------------------------------------------


class ConfigError(HarnessError):
    """The environment or a configuration file is wrong."""


class CorpusMissing(ConfigError):
    """The shipped corpus directory or its ``MANIFEST.sha256`` is absent (FR-176, decision 88)."""


class MissingCredentialError(ConfigError):
    """A live provider was constructed without its key. Names both places it may go.

    This wording is the third. It once said "Add a line to .env" while ``config.py`` read no key
    back out of that file, which told an operator whose file already carried the line to add it
    again (ticket 037). It then said a ``.env`` file is *not* read for keys, which was true when
    it was written and false the moment ticket 039 made the file the third arm of the key order
    as well as the provider's. It now names the file **and** the export, because both work and
    the environment is the one that wins.

    ``provider`` and ``env_var`` stay fields: ``cli/render.py`` formats guidance off them, and a
    renderer that string-matched this prose would have broken on both of these edits.
    """

    def __init__(self, provider: str, env_var: str) -> None:
        self.provider = provider
        self.env_var = env_var
        super().__init__(
            f"provider {provider!r} needs an API key and none is set. "
            f"Put {env_var} in a .env file in this directory:\n"
            f"    {env_var}=<your key>\n"
            f"or set it in your environment, which wins over the file:\n"
            f"    PowerShell   $env:{env_var} = '<your key>'\n"
            f"    bash/zsh     export {env_var}=<your key>\n"
            f"`harnessed init` writes that file for you.\n"
            f"Or stay offline with HARNESSED_PROVIDER=fake, which needs no key."
        )


class ProviderNotConfiguredError(ConfigError):
    """No provider was named, by flag, by environment or by ``.env`` (FR-27).

    There is deliberately no fallback arm to fall into. ``fake`` replays a committed fixture
    corpus, so an unconfigured harness that silently selected it would look like a working agent
    while doing nothing real, and an operator would only find out later.

    The variable and the remedy are fields rather than a sentence to be re-parsed: ``render.py``
    formats guidance off them, and a renderer that string-matched the message would break the
    first time the wording was edited.
    """

    env_var: str = "HARNESSED_PROVIDER"
    remedy: str = "harnessed init"

    def __init__(self) -> None:
        super().__init__(
            f"no provider is configured, and there is no default. "
            f"Run `{self.remedy}` to choose one, "
            f"or set {self.env_var}=<provider> for this shell. "
            f"{self.env_var}=fake replays the committed fixture corpus offline."
        )


class ExportMissing(ConfigError):
    """``harnessed dashboard`` found no staged export in the installed package (FR-66).

    Raised by ``cli/dashboard_cmd.py`` before the app is built, so a checkout that never ran the
    build stage exits with the one command that fixes it rather than a ``RuntimeError`` out of
    the static mount (decision 44). ``doctor`` reports rung 4 as broken with the same remedy,
    read off this class so the two never name different commands.
    """

    remedy: str = "npm --prefix dashboard run build"

    def __init__(self, directory: Path) -> None:
        super().__init__(
            f"no dashboard export at {directory}; the dashboard was never built or staged. "
            f"Run `{self.remedy}` and try again."
        )


class BindRefused(ConfigError):
    """``harnessed dashboard --host`` named an address that is not loopback (FR-66).

    Raised by ``cli/dashboard_cmd.py`` before settings are resolved, the app is built or any
    socket exists: the process carries the control app, which starts runs and writes ``.env``
    with no auth in front of it, so a wider bind is refused rather than warned about (decision
    46). ``serve`` keeps its warning because it has nothing to authorize.
    """

    def __init__(self, host: str) -> None:
        super().__init__(
            f"refusing to bind {host}: it is not a loopback address, and this process serves "
            f"the control app (/control/v1), which starts runs and writes credentials with no "
            f"auth in front of it. Bind 127.0.0.1 or ::1, or run `harnessed serve` for the "
            f"read-only gateway alone."
        )


class CredentialRefused(ConfigError):
    """A provider refused the key it was handed, so nothing was written (FR-88, decision 49).

    The control app maps it to 400: the key is the client's to fix. The message carries the
    refusal's status word and never the SDK's own message, which can quote the key back.
    """


class HomeNotFoundError(ConfigError):
    """``HARNESSED_HOME`` names a directory that does not exist (FR-34).

    Raised by ``cli/serve_cmd.py`` before anything opens the database. The default home is the
    relative ``.harnessed``, so a serving process started from the wrong directory would
    otherwise create an empty database there and serve it as if it were the real one. Both
    spellings are fields: the value as configured and where it resolved to from the working
    directory, so the operator can see which of the two is wrong.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.resolved = home.resolve()
        super().__init__(
            f"HARNESSED_HOME={home} names no directory: it resolves to {self.resolved} from the "
            f"current working directory, and nothing is there. Start from the directory that "
            f"holds the home, or set HARNESSED_HOME to its absolute path. Nothing was created."
        )


# ---- providers -----------------------------------------------------------------------------


class ProviderError(HarnessError):
    """A provider could not produce a response."""


class TransportDropped(ProviderError):
    """A chat call lost its connection mid-flight (ticket 231, FR-160), as opposed to an endpoint
    that answered. ``attempts`` counts the calls made and ``error_class`` names the drop."""

    def __init__(self, message: str, *, error_class: str = "", attempts: int = 1) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.attempts = attempts


class FixtureMissError(ProviderError):
    """FakeProvider found no fixture for the computed key. Names the path it looked for."""


class FixtureCorpusError(ProviderError):
    """The fixture corpus itself is malformed. Raised eagerly at load, not at request time."""


class TranslationError(ProviderError):
    """A wire shape could not be normalized, or a normalized shape could not be serialized."""


class DecisionError(ProviderError):
    """The Decisions API (decision 57) could not answer, or answered something the Decider seam
    refuses to parse. Only this tree falls back to Groq chat (decision 59); anything else is a bug
    in ``providers/jev.py``, not routing policy."""


class DecisionRefused(DecisionError):
    """A non-2xx response, or a parsed response with a named fault: a missing answer, an option
    nobody asked for, a probability outside ``[0, 1]``, or a missing ``usage.cost``."""


class DecisionUnreachable(DecisionError):
    """The Decisions host could not be reached at all."""


class DecisionDropped(DecisionError):
    """A Decisions call lost its connection mid-flight (ticket 231, FR-160): a reset, a TLS
    record failure or a half-read body. The one :class:`DecisionError` a live ``eval run``
    retries; ``attempts`` counts the calls made and ``error_class`` names the underlying drop."""

    def __init__(self, message: str, *, error_class: str = "", attempts: int = 1) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.attempts = attempts


class DecisionTimeout(DecisionError):
    """The call was still open at ``JevDecider.TIMEOUT_S``."""


# ---- storage -------------------------------------------------------------------------------


class StorageError(HarnessError):
    """The SQLite store could not be opened, migrated or written."""


class MigrationError(StorageError):
    """The migration set is malformed: a gap, a duplicate number, or an unreadable file."""


class MigrationChecksumError(MigrationError):
    """An already-applied migration file changed. Names the file and both checksums."""


class SchemaOutOfDateError(StorageError):
    """The database is behind the migrations the package ships (FR-33, decision 18).

    Raised by ``gateway/deps.py`` before the gateway serves anything. The gateway is a checker
    and never an applier: ``db.connect()`` migrates on open, so the one process that must not
    write refuses instead and names the CLI command that does. The two versions and the remedy
    are fields so a caller compares numbers rather than re-parsing the sentence.
    """

    remedy: str = "harnessed doctor"

    def __init__(self, *, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"the database is at schema version {actual} and this package ships version "
            f"{expected}. The gateway migrates nothing: run `{self.remedy}` to bring it current, "
            f"then start `harnessed serve` again."
        )


# ---- tools ---------------------------------------------------------------------------------


class ToolError(HarnessError):
    """A tool could not be declared or dispatched."""


class ToolNotFoundError(ToolError):
    """The model asked for a tool that is not registered."""


class ToolArgumentError(ToolError):
    """Tool arguments failed validation against the registered JSON schema."""


# ---- evals ---------------------------------------------------------------------------------


class EvalError(HarnessError):
    """An eval suite could not be loaded, run or scored."""


class EntryMissing(EvalError):
    """A work case is pinned to an agent entry this home does not install (ticket 236): the case
    is left unscored with the reason ``entry_missing``, and the run completes."""


class SuiteLoadError(EvalError):
    """A YAML suite is malformed or has duplicate case ids."""


class GraderError(EvalError):
    """A grader could not produce a score in [0.0, 1.0]."""


class EvidenceIncomplete(EvalError):
    """A live command finished with unscored cases (ticket 231, FR-160): its evidence file was
    left byte-identical and the partial output was kept beside it as ``<path>.incomplete``."""


class SuiteMismatch(EvalError):
    """Two eval run ids of different suites were handed to one comparison (decision 37).

    Raised by ``evals/runner.py::comparison``, naming both ids and both suites. A delta across
    two suites is not a weaker comparison, it is a different question, and answering it would
    serve a difference between two things that were never the same measurement. The gateway maps
    it to 400: asking it is the client's mistake, not the server's failure.
    """


# ---- improvement -----------------------------------------------------------------------------


class ImproveError(HarnessError):
    """The flywheel could not complete a cycle."""


class CandidateRejectedError(ImproveError):
    """A candidate did not pass the promotion rule."""


class InvalidCandidateError(ImproveError):
    """A candidate is malformed: empty text, or a procedure that is not append-only."""


# ---- delegation ------------------------------------------------------------------------------


class DelegationError(HarnessError):
    """A delegation to an external coding agent could not be completed."""


class AgentNotFoundError(DelegationError):
    """agents.toml does not declare the requested agent."""


class AcpVersionMismatch(DelegationError):
    """The agent answered ``initialize`` with an incompatible protocol version."""


class QuotaRefused(DelegationError):
    """An agent refused a delegation because its usage allowance is spent (decision 63, FR-116).

    Raised when the agent's JSON-RPC error carries a marker its template recorded from a real
    refusal (``acp/config.py::quota_markers_for``). Distinct from a timeout and from the unnamed
    failure, so the ledger, the events and the router's state can tell them apart.
    """


class DirtyTreeRefused(DelegationError):
    """A delegation was refused because its working tree had uncommitted changes (FR-146).

    The agent is never launched. ``--allow-dirty`` (on ``task add``, ``task run`` or ``run``) says
    the operator wants it anyway; the harness never cleans, stashes or commits on his behalf.
    """


class DelegationTimeout(DelegationError):
    """The agent exceeded its wall-clock ceiling and its process tree was killed.

    ``settled`` says whether the coroutine had actually stopped by the time the caller was
    released (ticket 060). It matters because the caller reads the delegation's collected state
    immediately afterwards and persists it: a partial gathered while the loop thread was still
    writing is a durable record that is not reliably true. ``False`` means the grace bound expired
    and what follows may be torn.
    """

    def __init__(self, message: str, *, settled: bool = True) -> None:
        super().__init__(message)
        self.settled = settled


# ---- events ------------------------------------------------------------------------------------


class EventError(HarnessError):
    """The event bus refused a publication."""


class UnknownEventError(EventError):
    """The event name is absent from the frozen set in ``events/names.py``."""


# ---- gateway -----------------------------------------------------------------------------------


class GatewayError(HarnessError):
    """The HTTP surface could not answer a request."""


class SurfaceNotFound(GatewayError):
    """A read surface was asked for an entity the ledgers do not hold.

    Raised by the store-side callable that looked (``loop/runs.py::run_payload`` first, ticket
    066), never invented on the HTTP path: the gateway maps it to 404 with decision 24's one
    error body, and the CLI maps it to stderr plus exit 1 like every other ``HarnessError``. An
    unknown key is an error rather than an empty payload because an empty payload for a session
    id that never ran reads as a run that did nothing, which is a different claim.
    """


class GatewayExtraMissing(GatewayError):
    """``harnessed serve`` was invoked without the ``gateway`` extra installed (FR-31).

    Raised by ``cli/serve_cmd.py`` when its function-body import of ``harnessed.gateway`` or
    ``uvicorn`` fails, which is what lets the message survive the extra being absent: the
    ``ImportError`` becomes a sentence naming the one command that fixes it, never a traceback
    (decision 21). The remedy is a field so ``render.py`` never has to re-parse the prose.
    """

    remedy: str = "uv sync --extra gateway"

    def __init__(self, command: str = "harnessed serve") -> None:
        super().__init__(
            f"the gateway extra is not installed, so `{command}` has nothing to serve. "
            f"Run `{self.remedy}` and try again."
        )


class CursorError(GatewayError):
    """A stream resumption cursor is not a non-negative integer (decision 20).

    Raised by ``gateway/sse.py::parse_cursor`` on a malformed ``Last-Event-ID``; the gateway maps
    it to 400 with decision 24's one error body.
    """


class LimitError(GatewayError):
    """A ``?limit=`` query parameter is not a positive integer (decision 24, ticket 080).

    Raised by ``gateway/routes/__init__.py::parse_limit``; the gateway maps it to 400 with
    decision 24's one error body. A malformed query is the client's mistake, so it must not
    leave as the 500 that the ``GatewayError`` base keeps for the server's own failures.
    """


class ParameterError(GatewayError):
    """A required query parameter is missing or empty (decision 37, ticket 098).

    Raised by ``gateway/routes/__init__.py::required``; the gateway maps it to 400 with decision
    24's one error body, exactly as :class:`LimitError` does for a malformed ``?limit=``. An
    absent parameter is refused before any store is read, so a half-stated question never reaches
    the ledgers.
    """


# ---- control -----------------------------------------------------------------------------------


class ControlError(HarnessError):
    """The write surface could not act on a request (FR-77, decision 49).

    Mapped by ``control/errors.py``, which is also where a method a control route does not serve
    is reported under this name, as ``GatewayError`` is for the read surface.
    """


class RunInFlight(ControlError):
    """A run is already in flight, and the control app runs one at a time (decision 49).

    The control app maps it to 409. The session id in flight is a field and in the message, so a
    client can open that run's stream rather than guess which run is holding the worker.
    """

    #: How much of a task's text the sentence keeps (one line, cut).
    TEXT_CUT = 60

    def __init__(
        self, session_id: str, task_id: str | None = None, text: str | None = None
    ) -> None:
        self.session_id = session_id
        self.task_id = task_id
        self.text = None if text is None else self.one_line(text)
        super().__init__(self.sentence(session_id, task_id, self.text))

    @classmethod
    def one_line(cls, text: str) -> str:
        """``text`` on one line, cut at :data:`TEXT_CUT` characters."""
        flat = " ".join(text.split())
        return flat if len(flat) <= cls.TEXT_CUT else flat[: cls.TEXT_CUT - 3].rstrip() + "..."

    @staticmethod
    def sentence(session_id: str, task_id: str | None, text: str | None) -> str:
        """The one sentence the CLI prints and the control app's 409 carries (decision 103)."""
        if task_id is None:
            return (
                f"a run is already in flight: session id {session_id}. The control app runs one "
                f"at a time; wait for it to finish, then try again."
            )
        return (
            f"a task is running: {task_id} {text or ''}".rstrip()
            + f"; wait for it, or harnessed task cancel {task_id}"
        )


class PermissionAnswered(ControlError):
    """A permission request was already answered, or has expired (FR-130, decision 74).

    Raised by ``acp/pending.py::answer`` when its compare-and-set matched no row. It carries what
    the row recorded, so the control app answers 409 with those fields and the CLI prints the same
    sentence and exits 1: whoever lost the race learns who won it, and when.
    """

    def __init__(self, answer: str, rule: str, answered_via: str | None, answered_at: str) -> None:
        self.answer = answer
        self.rule = rule
        self.answered_via = answered_via
        self.answered_at = answered_at
        verdict = {"approve": "approved", "deny": "denied"}.get(answer, answer)
        if rule == "ceiling":
            how = "when its ceiling passed"
        elif answered_via is None:
            how = "by the harness"
        else:
            how = f"from the {answered_via}"
        super().__init__(f"already answered: {verdict} {how} at {answered_at[11:16]}")


class TaskStateError(HarnessError):
    """A verb was asked of a task whose state does not allow it (FR-140, decision 76).

    ``harnessed task cancel`` on a running or finished task is refused with the state in the
    message and exit 1 through ``main``; the control app has no such route today.
    """

    def __init__(self, task_id: str, state: str, message: str) -> None:
        self.task_id = task_id
        self.state = state
        super().__init__(message)


class TaskAwaitingAnswer(ControlError):
    """A message was posted to a task's session while it waits on the operator's answer to the
    agent's request (FR-141, decision 77): answer the request first, then the task goes on.

    The control app maps it to 409, before any event is published for the message.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        super().__init__(
            f"the task on session {session_id} is waiting for your answer to the agent's "
            f"request: answer it, then the task carries on."
        )


class PromptError(ControlError):
    """A request's ``prompt`` is missing, empty, or not a string (decision 49).

    The control app maps it to 400 before anything is opened or submitted, so a refused prompt
    starts no run and writes no row.
    """


class RouterError(HarnessError):
    """The router (decision 56) could not complete a message."""


class SessionNotContinued(ControlError):
    """A run key that cannot be continued as a session (FR-80, decision 50).

    Raised by ``cli/run_cmd.py::continue_session`` for a ``case_run`` key, because an eval case is
    scored once and is never continued, and for a session opened on a different run id than the
    one it was asked to continue. A key no journal holds is ``SurfaceNotFound`` instead.
    """
