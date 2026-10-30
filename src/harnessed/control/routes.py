"""The control app's routes: ``POST`` only, each one call into the CLI's composition (decision 49).

A handler parses a JSON body by hand, calls the composition **directly** - never a subprocess,
never Typer (MUST rule 16) - and answers ``202`` with the ids the browser reads next. Errors are
raised from the tree and leave through ``control/errors.py``; nothing here builds an error body.

``POST /control/v1/runs`` is FR-79: the prompt is checked, the worker opens the session
``run_cmd.open_session`` builds and publishes its ``run.started``, and the handler answers ``202``
with that session's id while ``run_cmd.solve_with_memory`` runs on, so a run started here writes
through the same ``db.connect()`` and ``redact.scrub`` as ``harnessed run``. The prompt travels in
the body and nowhere else. The messages route is 140's.

``POST /control/v1/providers`` is FR-88: ``{"provider", "credential"}`` in a JSON body, handed to
``onboarding.configure_provider``, the validate-then-write ``harnessed init`` calls, and answered
``200`` with its payload, which is ``init --json``'s and names the variable, never its value. It
runs on the request thread rather than the worker, because nothing of it outlives the response. The
credential is never logged, never returned and never put in an event: a refusal is re-raised as
``CredentialRefused`` carrying the refusal's status word, and its chain is dropped, so the SDK's
own message (which may quote the key) is not reachable from the error that leaves this module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import Body, FastAPI, Request

from harnessed import config
from harnessed.acp import pending
from harnessed.cli import onboarding, run_cmd, task_run
from harnessed.control.worker import Worker
from harnessed.errors import (
    ConfigError,
    CredentialRefused,
    ParameterError,
    PromptError,
    ProviderError,
)
from harnessed.tasks import store

__all__ = ["READ_PREFIX", "register"]

#: Where the read gateway answers in the same process. Spelled here, not imported (FR-77).
READ_PREFIX: Final = "/v1"

#: The file a fresh clone's ``.env`` is seeded from, as ``init`` seeds it (FR-26). ``.env`` itself
#: is ``config.ENV_FILENAME``; both are read from the working directory (MUST rule 11).
EXAMPLE_FILENAME: Final = ".env.example"


def prompt_of(body: object) -> str:
    """The one string a run is asked (MUST rule 26), or :class:`PromptError`."""
    prompt = body.get("prompt") if isinstance(body, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptError('the body must be a JSON object whose "prompt" is a non-empty string')
    return prompt


def answer_of(body: object) -> str:
    """The verdict a body gives (``approve`` or ``deny``), or :class:`ParameterError`."""
    answer = body.get("answer") if isinstance(body, dict) else None
    if answer not in pending.ANSWERS:
        raise ParameterError('the body must be a JSON object whose "answer" is "approve" or "deny"')
    return str(answer)


def provider_of(body: object) -> tuple[str, str | None]:
    """The provider and credential a body names, or :class:`ParameterError` saying which is wrong.

    No message here quotes the credential: a refusal names the key, the variable or the provider,
    and the value the operator typed appears nowhere but in the return.
    """
    if not isinstance(body, dict):
        raise ParameterError('the body must be a JSON object with "provider" and "credential"')
    provider = body.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ParameterError('"provider" must be a non-empty string')
    try:
        key_var = config.load().key_var_for(provider)
    except ConfigError as exc:
        raise ParameterError(str(exc)) from None
    credential = body.get("credential")
    if credential is not None and not isinstance(credential, str):
        raise ParameterError('"credential" must be a string')
    if key_var is not None and (credential is None or not credential.strip()):
        raise ParameterError(f'"credential" must hold a value for {key_var}. Nothing was written.')
    return provider, credential


def accepted(session_id: str) -> dict[str, str]:
    """The 202: the session id, and the two read routes that describe that session."""
    return {
        "session_id": session_id,
        "stream": f"{READ_PREFIX}/sessions/{session_id}/stream",
        "run": f"{READ_PREFIX}/runs/{session_id}",
    }


def register(app: FastAPI, prefix: str) -> None:
    """Register the control routes under ``prefix``. ``POST`` and nothing else."""

    @app.post(f"{prefix}/runs", status_code=202)
    def open_run(request: Request, body: Annotated[Any, Body()] = None) -> dict[str, str]:
        """Open a run of ``prompt`` on the worker and answer with its session id."""
        prompt = prompt_of(body)
        worker: Worker = request.app.state.worker
        session_id = worker.start(
            lambda opened: run_cmd.open_session(
                opened=opened,
                routed=True,
                answer_door=True,
                stopping=worker.stopped.is_set,
                parked=worker.park,
            ),
            lambda session, event: run_cmd.solve_with_memory(session, prompt, announced=event),
        )
        return accepted(session_id)

    @app.post(f"{prefix}/runs/{{session_id}}/messages", status_code=202)
    def post_message(
        session_id: str, request: Request, body: Annotated[Any, Body()] = None
    ) -> dict[str, str]:
        """Continue ``session_id`` with ``prompt`` on the worker and answer with the same id."""
        prompt = prompt_of(body)
        worker: Worker = request.app.state.worker
        task = store.for_session_text(session_id)
        if task is not None and task_run.route_message(task):
            # A task paused uncertain: the operator's words resume it (ticket 240, FR-141). A task
            # waiting on a permission answer or running was refused above, before any event.
            return accepted(worker.resume_with_message(task, prompt))
        continued = worker.start(
            lambda opened: run_cmd.open_session(
                opened=opened,
                session_id=session_id,
                routed=True,
                answer_door=True,
                stopping=worker.stopped.is_set,
                parked=worker.park,
            ),
            lambda session, event: run_cmd.continue_session(
                session, session_id, prompt, announced=event
            ),
        )
        return accepted(continued)

    @app.post(f"{prefix}/permissions/{{permission_id}}")
    def answer_permission(
        permission_id: str, body: Annotated[Any, Body()] = None
    ) -> dict[str, Any]:
        """Answer an open permission request as the operator, from the console (FR-130).

        ``{"answer": "approve" | "deny"}``. The same ``pending.answer`` ``harnessed permissions
        answer`` calls; an answered or expired id is 409 ``PermissionAnswered``, an unknown one 404.
        """
        row = pending.operator_answer(permission_id, answer_of(body), via="console")
        return {"permission_id": row.permission_id, "state": row.state}

    @app.post(f"{prefix}/tasks", status_code=201)
    def queue_task(body: Annotated[Any, Body()] = None) -> dict[str, str | None]:
        """Queue a task from the console (FR-140): ``store.queue`` with ``via="console"``.

        Only ``{"text"}`` is read; no budget is set from the browser, so the Settings defaults
        apply and the answer says so (``budget_source`` ``default``).
        """
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise PromptError('the body must be a JSON object whose "text" is a non-empty string')
        row = store.queue_text(text, via="console")
        return {
            "task_id": row["task_id"],
            "state": row["state"],
            "budget_iterations": row["budget_iterations"],
            "budget_seconds": row["budget_seconds"],
            "budget_dollars": row["budget_dollars"],
            "budget_source": row["budget_source"],
            "queued_via": row["queued_via"],
        }

    @app.post(f"{prefix}/providers")
    def configure_provider(body: Annotated[Any, Body()] = None) -> dict[str, Any]:
        """Prove ``credential`` for ``provider``, merge it into ``.env``, answer ``init --json``."""
        provider, credential = provider_of(body)
        try:
            return onboarding.configure_provider(
                provider,
                credential,
                validate=True,
                env_path=Path(config.ENV_FILENAME),
                example_path=Path(EXAMPLE_FILENAME),
            )
        except ProviderError as exc:
            # The message is the refusal's status word; the chain is the SDK's and is dropped.
            raise CredentialRefused(str(exc)) from None
