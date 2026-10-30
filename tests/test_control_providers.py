"""FR-88: a key set from the browser never appears in a URL, a log or a response (decision 49).

``POST /control/v1/providers`` takes ``{"provider", "credential"}`` as a JSON body and calls
``cli/onboarding.py::configure_provider``, the composition ``harnessed init`` calls (MUST rule 16).
The proof that matters is an absence: the typed credential is swept for in the response body, in
everything uvicorn logs about the request, and in the process's own stderr, and it is found **0**
times in each. The request that carried it is censused too: its path and its query hold none.

The server is a real uvicorn on a loopback port with its access log **on** and its loggers at
``debug``, which is louder than the dashboard process ever runs it (``access_log=False``, ticket
137): a credential that stayed out of this log stays out of the quieter one. The provider is a
stand-in registered through ``providers.registry.create``, so validation makes its one call offline.

Every test sits chdir'd into its own ``tmp_path``, because ``.env`` is read from the working
directory (MUST rule 11), and the browser form's attributes are read off its source: the card's
rendering is ticket 147's unit suite, and neither ticket claims the other's proof.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from harnessed import config, control
from harnessed.cli import onboarding
from harnessed.errors import ProviderError
from harnessed.memory import db
from harnessed.providers import registry as provider_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / ".env.example"
EVIDENCE = REPO_ROOT / "evidence" / "146"
DASHBOARD = REPO_ROOT / "dashboard"
PROVIDER_CARD = DASHBOARD / "components" / "ProviderCard.tsx"
CONTROL_CLIENT = DASHBOARD / "lib" / "control-client.ts"

PROVIDERS = "/control/v1/providers"

#: The provider every test configures, and its variable quoted from ``config`` rather than retyped.
PROVIDER = "groq"
KEY_VAR = config.PROVIDER_KEY_VARS[PROVIDER]

#: What the operator typed. Distinctive, so a leak anywhere is unmissable, and obviously not real.
TYPED = "not-a-real-value-31415926535897932384"

#: What a vendor SDK might say when it refuses: it is free to echo the credential back.
SDK_MESSAGE = f"invalid credential {TYPED} rejected by upstream"

#: The keys of ``init --json``'s payload, the 200's whole body.
INIT_JSON_KEYS = {
    "credential_live",
    "credential_var",
    "env_file",
    "provider",
    "rungs",
    "unchanged",
}


def evidence(name: str, text: str) -> None:
    """Write one evidence file. Absolute, because every test runs chdir'd into ``tmp_path``."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(text, encoding="utf-8")


class Refused(ProviderError):
    """A refusal in the ``Provider`` protocol's shape, carrying the SDK's echoing message."""

    status_code = 401


class RefusedWithoutStatus(ProviderError):
    """A refusal that carries no HTTP status, so the status word is the class name."""


def refusing(error: type[ProviderError]) -> Any:
    def create(_settings: config.Settings) -> object:
        class Refuses:
            def complete(self, request: object) -> object:
                raise error(SDK_MESSAGE)

        return Refuses()

    return create


def accepting(_settings: config.Settings) -> object:
    class Accepts:
        def complete(self, request: object) -> object:
            return object()

    return Accepts()


@pytest.fixture
def clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None, tmp_harnessed_home: Path
) -> Iterator[Path]:
    """A checkout with ``.env.example``, no ``.env``, a migrated home, and the process in it."""
    (tmp_path / ".env.example").write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    db.connect().close()
    yield tmp_path


@pytest.fixture
def accepted(clone: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(provider_registry, "create", accepting)
    return clone


@pytest.fixture
def client(clone: Path, tmp_harnessed_home: Path) -> Iterator[TestClient]:
    with TestClient(control.create_app(tmp_harnessed_home), raise_server_exceptions=False) as c:
        yield c


class Served:
    """The control app under a real uvicorn on a loopback port, access log on, loggers at debug."""

    def __init__(self, app: Any) -> None:
        import uvicorn

        self.config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_config=None,
            log_level="debug",
            access_log=True,
            lifespan="off",
        )
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def table_counts(home: Path) -> dict[str, int]:
    """Every ledger table's row count, so a write anywhere in the database shows as a change."""
    with sqlite3.connect(home / db.DATABASE_FILENAME) as conn:
        names = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in sorted(names)
        }


# ---- AC 1: a valid credential is a 200 carrying init --json's payload, and 0 echoes ----------


def test_a_valid_credential_is_a_200_and_appears_in_no_body_log_stderr_or_url(
    accepted: Path,
    tmp_harnessed_home: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    requests: list[httpx.Request] = []
    hooks = {"request": [requests.append]}
    with (
        Served(control.create_app(tmp_harnessed_home)) as base,
        httpx.Client(base_url=base, event_hooks=hooks) as http,
    ):
        response = http.post(PROVIDERS, json={"provider": PROVIDER, "credential": TYPED})
    out, err = capfd.readouterr()
    logged = "\n".join(
        f"{record.name} {record.getMessage()} {record.args!r}" for record in caplog.records
    )
    uvicorn_lines = [r.getMessage() for r in caplog.records if r.name.startswith("uvicorn")]
    sent = requests[0].url

    evidence(
        "valid-200.txt",
        f"status {response.status_code}\nbody {response.text}\n\n"
        f"credential in body: {response.text.count(TYPED)}\n"
        f"credential in uvicorn output ({len(uvicorn_lines)} lines): {logged.count(TYPED)}\n"
        f"credential in process stderr ({len(err)} chars): {err.count(TYPED)}\n"
        f"credential in process stdout ({len(out)} chars): {out.count(TYPED)}\n\n"
        f"request url {sent}\npath {sent.path}\nquery {sent.query!r}\n"
        f"credential in path: {sent.path.count(TYPED)}\n"
        f"credential in query: {sent.query.decode('utf-8').count(TYPED)}\n\n"
        "uvicorn output:\n" + "\n".join(uvicorn_lines) + "\n",
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == INIT_JSON_KEYS
    assert body["provider"] == PROVIDER
    assert body["credential_var"] == KEY_VAR
    assert body["credential_live"] is True
    assert body["unchanged"] is False
    assert any(
        "POST" in line and PROVIDERS in line for line in uvicorn_lines
    ), "the access log was on and logged nothing, so its sweep proves nothing"
    assert response.text.count(TYPED) == 0
    assert logged.count(TYPED) == 0
    assert err.count(TYPED) == 0
    assert out.count(TYPED) == 0
    assert sent.path == PROVIDERS
    assert sent.query == b""
    # It did reach disk, which is the point of the call.
    assert f"{KEY_VAR}={TYPED}" in (accepted / ".env").read_text(encoding="utf-8")


def test_the_200_is_the_payload_init_json_prints_for_the_same_input(
    accepted: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One composition, two callers: the route's body is ``configure_provider``'s return."""
    calls: list[tuple[Any, ...]] = []
    real = onboarding.configure_provider

    def recording(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(onboarding, "configure_provider", recording)
    response = client.post(PROVIDERS, json={"provider": PROVIDER, "credential": TYPED})
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == (PROVIDER, TYPED)
    assert kwargs["validate"] is True
    assert Path(kwargs["env_path"]).name == config.ENV_FILENAME
    again = real(PROVIDER, TYPED, validate=True, env_path=accepted / config.ENV_FILENAME)
    assert again == {**response.json(), "unchanged": True}


def test_a_second_post_of_the_same_credential_writes_nothing_and_says_so(
    accepted: Path, client: TestClient
) -> None:
    first = client.post(PROVIDERS, json={"provider": PROVIDER, "credential": TYPED})
    written = (accepted / ".env").read_bytes()
    second = client.post(PROVIDERS, json={"provider": PROVIDER, "credential": TYPED})
    assert first.json()["unchanged"] is False
    assert second.status_code == 200
    assert second.json()["unchanged"] is True
    assert (accepted / ".env").read_bytes() == written


# ---- AC 2: a refused credential is a mapped 400 with a status word, and .env is untouched -----


@pytest.mark.parametrize(
    ("error", "word"), [(Refused, "401"), (RefusedWithoutStatus, "RefusedWithoutStatus")]
)
def test_a_refused_credential_is_a_400_with_a_status_word_and_env_is_byte_identical(
    clone: Path,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    error: type[ProviderError],
    word: str,
) -> None:
    env = clone / ".env"
    env.write_bytes(b"# the operator's own file\nHARNESSED_PROVIDER=fake\nKEEP=me\n")
    before = env.read_bytes()
    monkeypatch.setattr(provider_registry, "create", refusing(error))

    response = client.post(PROVIDERS, json={"provider": PROVIDER, "credential": TYPED})

    after = env.read_bytes()
    evidence(
        f"refused-{word}.txt",
        f"status {response.status_code}\nbody {response.text}\n"
        f"credential in body: {response.text.count(TYPED)}\n"
        f"SDK message in body: {response.text.count(SDK_MESSAGE)}\n"
        f".env byte-identical: {after == before} "
        f"({len(before)} bytes before, {len(after)} after)\n",
    )
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["error"] == "CredentialRefused"
    assert word in body["message"]
    assert TYPED not in response.text
    assert "rejected by upstream" not in response.text
    assert after == before


def test_a_refused_credential_on_a_fresh_clone_leaves_no_env_behind(
    clone: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(provider_registry, "create", refusing(Refused))
    response = client.post(PROVIDERS, json={"provider": PROVIDER, "credential": TYPED})
    assert response.status_code == 400
    assert not (clone / ".env").exists()


@pytest.mark.parametrize(
    "body",
    [
        None,
        [PROVIDER, TYPED],
        {"credential": TYPED},
        {"provider": "", "credential": TYPED},
        {"provider": 7, "credential": TYPED},
        {"provider": "no-such-provider", "credential": TYPED},
        {"provider": PROVIDER},
        {"provider": PROVIDER, "credential": ""},
        {"provider": PROVIDER, "credential": "   "},
        {"provider": PROVIDER, "credential": 31415},
    ],
)
def test_a_malformed_body_is_a_400_parameter_error_that_writes_nothing(
    accepted: Path, client: TestClient, body: Any
) -> None:
    response = client.post(PROVIDERS, json=body)
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "ParameterError"
    assert TYPED not in response.text
    assert not (accepted / ".env").exists()


# ---- AC 4: the models walk's delta is .env and nothing else -----------------------------------


def test_setting_a_credential_changes_env_and_0_ledger_rows(
    accepted: Path, tmp_harnessed_home: Path, client: TestClient
) -> None:
    rows_before = table_counts(tmp_harnessed_home)
    env_before = (accepted / ".env").read_bytes() if (accepted / ".env").exists() else None

    response = client.post(PROVIDERS, json={"provider": PROVIDER, "credential": TYPED})

    rows_after = table_counts(tmp_harnessed_home)
    env_after = (accepted / ".env").read_bytes()
    changed = {
        name: (rows_before[name], n) for name, n in rows_after.items() if rows_before[name] != n
    }
    evidence(
        "models-walk-delta.txt",
        f"status {response.status_code}\n.env changed: {env_after != env_before}\n"
        f"tables {len(rows_after)}, tables whose row count changed: {changed}\n",
    )
    assert response.status_code == 200
    assert env_after != env_before
    assert rows_after == rows_before


# ---- AC 5: a JSON body, never a query string, never a path segment ----------------------------


def test_the_route_takes_no_path_parameter_and_is_post_only(
    clone: Path, tmp_harnessed_home: Path
) -> None:
    app = control.create_app(tmp_harnessed_home)
    routes = [r for r in app.routes if getattr(r, "path", "") == PROVIDERS]
    assert len(routes) == 1
    assert "{" not in routes[0].path
    assert routes[0].methods == {"POST"}


def test_a_credential_sent_in_the_query_string_is_refused_and_never_written(
    accepted: Path, client: TestClient
) -> None:
    response = client.post(PROVIDERS, params={"provider": PROVIDER, "credential": TYPED})
    evidence(
        "query-refused.txt",
        f"url {response.request.url}\nstatus {response.status_code}\nbody {response.text}\n"
        f".env exists: {(accepted / '.env').exists()}\n",
    )
    assert response.status_code == 400
    assert TYPED not in response.text
    assert not (accepted / ".env").exists()


def test_a_credential_sent_as_a_path_segment_is_a_404_and_never_written(
    accepted: Path, client: TestClient
) -> None:
    response = client.post(f"{PROVIDERS}/{PROVIDER}/{TYPED}", json={})
    assert response.status_code in {404, 405}
    assert not (accepted / ".env").exists()


def test_the_control_client_sends_provider_and_credential_as_a_json_body() -> None:
    source = CONTROL_CLIENT.read_text(encoding="utf-8")
    assert 'PROVIDERS_ROUTE = "/control/v1/providers"' in source
    assert "postControl(PROVIDERS_ROUTE, { provider, credential }, signal)" in source
    assert "body: JSON.stringify(body)" in source
    # The route is a constant: nothing is ever interpolated into it.
    assert not re.search(r"PROVIDERS_ROUTE\s*\+|`\$\{PROVIDERS_ROUTE\}", source)


# ---- AC 3: the browser form ------------------------------------------------------------------

#: Every browser storage API. The form holds the typed value in component state and nowhere else.
BROWSER_STORAGE = ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "caches.")


def opening_tags(source: str, name: str) -> list[str]:
    """Every JSX opening tag ``<name ...>``, read to the ``>`` outside any ``{...}`` expression.

    A regex to the first ``>`` stops inside an arrow function's ``=>``, so this counts braces.
    """
    tags = []
    for match in re.finditer(rf"<{name}\b", source):
        depth = 0
        for end in range(match.end(), len(source)):
            char = source[end]
            depth += {"{": 1, "}": -1}.get(char, 0)
            if char == ">" and depth == 0:
                tags.append(source[match.start() : end + 1])
                break
    return tags


def test_the_form_is_autocomplete_off_and_the_field_is_a_password() -> None:
    source = PROVIDER_CARD.read_text(encoding="utf-8")
    forms = opening_tags(source, "form")
    inputs = opening_tags(source, "input")
    evidence(
        "form-attributes.txt",
        "forms:\n" + "\n".join(forms) + "\n\ninputs:\n" + "\n".join(inputs) + "\n",
    )
    assert len(forms) == 1
    assert 'autoComplete="off"' in forms[0]
    assert len(inputs) == 1
    assert 'type="password"' in inputs[0]
    assert 'autoComplete="off"' in inputs[0]
    # The field is controlled, so clearing the state clears what the browser shows.
    assert "value={credential}" in inputs[0]


def test_the_form_submits_through_the_control_client_and_clears_the_field_on_any_answer() -> None:
    source = PROVIDER_CARD.read_text(encoding="utf-8")
    assert "configureProvider(" in source
    assert "fetch(" not in source
    # The one clear sits after the awaited answer and before any branch on its state.
    answered = source.index("await configureProvider(")
    cleared = source.index('setCredential("")', answered)
    branched = source.index("result.state", answered)
    assert cleared < branched


@pytest.mark.parametrize("path", [PROVIDER_CARD, CONTROL_CLIENT])
def test_0_browser_storage_is_used(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert [api for api in BROWSER_STORAGE if api in source] == []
    assert "URLSearchParams" not in source
    assert "?credential" not in source
