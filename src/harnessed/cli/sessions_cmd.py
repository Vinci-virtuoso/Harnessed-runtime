"""``harnessed sessions list`` - the session ids the events table holds, newest first.

FR-48's CLI twin. A session id is minted where its first event is published and, until this
command, printed nowhere an operator could go back to: an eval run or an improvement cycle
scrolled past and the id went with it, which is why UJ-11 could not start. The listing is the
same one ``GET /v1/sessions`` serves, through the same two callables, so the ``--json`` line and
the body are the same bytes (FR-37).

Nothing is composed here. ``events/read.py`` owns the query and ``read.sessions_payload`` owns
the shape; this module parses a flag and hands what came back to ``render``. The database is
opened directly rather than through ``run_cmd.open_session``, like the ``eval list`` and
``memory`` reads: asking which sessions exist has no business constructing a provider, and
constructing one is what would make this fail on a checkout configured for a live model with no
key.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from harnessed.cli import render
from harnessed.errors import HarnessError
from harnessed.events import read
from harnessed.loop import messages
from harnessed.memory import db

__all__ = [
    "list_sessions",
    "recent_messages",
    "session_events",
    "session_messages",
    "session_stream",
    "sessions_app",
]

sessions_app = typer.Typer(
    name="sessions",
    help="Find a session by id: what opened it, when, and whether it finished.",
    no_args_is_help=True,
)


@sessions_app.callback()
def _group() -> None:
    """Keep ``sessions`` a command group (see ``eval_cmd._group`` for why)."""


@sessions_app.command("list")
def list_sessions(
    limit: Annotated[int, typer.Option("--limit", help="How many sessions to show.")] = (
        read.DEFAULT_SESSION_LIMIT
    ),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print recent sessions, newest first: the id to paste into every other command.

    A row's ``terminal_event`` is empty while the session is still open. An improvement cycle
    whose baseline eval has already finished is still open, because a stream ends on the finish
    paired with its own start and never on a name alone (MUST rule 20).
    """
    found = read.sessions(db.connect(), limit)
    if as_json:
        render.json_object(read.sessions_payload(found))
        return
    render.rows(
        f"{len(found)} session(s), newest first",
        tuple(
            f"{row.session_id}  {row.first_event:<22} {row.first_ts}  "
            f"{row.terminal_event or 'open'}"
            for row in found
        ),
    )


@sessions_app.command("messages")
def session_messages(
    session_id: Annotated[str, typer.Argument(help="The session id `harnessed run` printed.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print one session's messages, in order: the reply, the hops and the cost of each."""
    conn = db.connect()
    found = messages.for_session(conn, session_id)
    if as_json:
        render.json_object(messages.messages_payload(found, messages.task_for(conn, session_id)))
        return
    if not found:
        render.line(f"session {session_id}: no messages recorded")
        return
    render.rows(
        f"{len(found)} message(s) for session {session_id}",
        tuple(
            f"seq {row.message_seq}  outcome={row.outcome}  handler={row.handler}  "
            f"steps={row.steps}  seconds={row.seconds}"
            for row in found
        ),
    )


@sessions_app.command("recent")
def recent_messages(
    limit: Annotated[int, typer.Option("--limit", help="How many messages to show.")] = (
        messages.DEFAULT_MESSAGE_LIMIT
    ),
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print recent routed messages, newest first: the activity list's own twin."""
    found = messages.recent(db.connect(), limit)
    if as_json:
        render.json_object(messages.recent_payload(found))
        return
    render.rows(
        f"{len(found)} message(s), newest first",
        tuple(f"{row.session_id}  seq {row.message_seq}  outcome={row.outcome}" for row in found),
    )


@sessions_app.command("events")
def session_events(
    session_id: Annotated[str, typer.Argument(help="The session id `harnessed run` printed.")],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit a single JSON object on stdout and nothing else.")
    ] = False,
) -> None:
    """Print every event one session recorded, in order (ticket 232).

    The twin of ``GET /v1/sessions/{session_id}/events``: ``events/read.py::json_payload``, so the
    ``--json`` line and the body are the same bytes.
    """
    payload = read.json_payload(db.connect(), session_id)
    if as_json:
        render.json_object(payload)
        return
    events = payload["events"]
    assert isinstance(events, list)
    render.rows(
        f"{len(events)} event(s) for session {session_id}",
        tuple(f"{item['seq']}  {item['name']}  {item['ts']}" for item in events),
    )


@sessions_app.command("stream")
def session_stream(
    session_id: Annotated[str, typer.Argument(help="The session id `harnessed run` printed.")],
    after: Annotated[
        int, typer.Option("--after", min=0, help="Only frames after this seq (Last-Event-ID).")
    ] = 0,
) -> None:
    """Write one finished session's event stream, frame for frame as the gateway sends it.

    The twin of ``GET /v1/sessions/{session_id}/stream`` (ticket 232): the same ``id:``, ``event:``
    and ``data:`` frames, from ``--after`` on, written as bytes (an SSE stream is not one JSON
    object, so there is no ``--json``). A session that has not finished is refused, exit 1: this
    command reads the table and never waits on it.
    """
    from harnessed.gateway import sse

    conn = db.connect()
    newest = read.latest(conn, session_id)
    if newest.name != read.terminal_for(conn, session_id):
        raise HarnessError(
            f"session {session_id!r} has not finished. `sessions stream` writes a finished "
            f"session's frames and never waits; `sessions events` reads what it holds so far."
        )
    frames = b"".join(sse.frame(event) for event in read.after(conn, session_id, after))
    sys.stdout.buffer.write(frames)
    sys.stdout.buffer.flush()
