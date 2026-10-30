"""Provider name to constructed adapter, and the one place a key is checked.

FR-9. Two tables and no vendor branch: :data:`OFFLINE_FACTORIES` for the providers that never
touch a network, and the dialect of the endpoint's :class:`~harnessed.providers.quirks.
ProviderCaps` for everything else. The credential check is a single table lookup against
``config.PROVIDER_KEY_VARS``, so a provider that needs no key never reads one, and a provider
that needs one fails at construction with a sentence rather than at request time with a 401.

The environment is reached only through ``config.py`` (All AI agents MUST rule 3): this module
takes an already-resolved :class:`~harnessed.config.Settings` and reads no variable itself.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Final, cast

from harnessed.config import MODEL_VAR, PROVIDER_KEY_VARS, Settings, secret_values
from harnessed.errors import ConfigError, ProviderError
from harnessed.providers import quirks
from harnessed.providers.base import Provider

__all__ = [
    "OFFLINE_FACTORIES",
    "STALE_MODEL_STATUSES",
    "ProviderState",
    "create",
    "credential_for",
    "json_payload",
    "key_var_for",
    "model_for",
    "provider_error",
    "requires_credential",
    "states",
]

#: The statuses at which a rejected request is plausibly about the model id rather than about the
#: endpoint. A 503 is the endpoint's problem, and telling an operator to change their model id
#: would send them to fix something that is not broken.
STALE_MODEL_STATUSES: Final[frozenset[int]] = frozenset({400, 404})


def key_var_for(provider: str) -> str | None:
    """The env var holding ``provider``'s key, ``None`` if it needs none, ``ConfigError`` if the
    name is unknown. The lookup never lets a ``KeyError`` escape."""
    try:
        return PROVIDER_KEY_VARS[provider]
    except KeyError as exc:
        raise ConfigError(
            f"unknown provider {provider!r}. Known providers: "
            f"{', '.join(sorted(PROVIDER_KEY_VARS))}."
        ) from exc


def requires_credential(provider: str) -> bool:
    """Whether constructing ``provider`` needs an API key at all."""
    return key_var_for(provider) is not None


def credential_for(settings: Settings, provider: str | None = None) -> str | None:
    """The key for ``provider``, or ``None`` when it needs none.

    This is the only credential check in the repository. For a keyless provider it returns
    before touching ``settings``, so the offline path reads no key and raises no error; for a
    live one it delegates to ``Settings.require_api_key``, which raises
    ``MissingCredentialError`` naming the variable and the line to add to ``.env``.
    """
    name = settings.require_provider() if provider is None else provider
    if not requires_credential(name):
        return None
    return settings.require_api_key(name)


def model_for(settings: Settings, provider: str | None = None) -> str:
    """The model id ``provider``'s requests carry: ``HARNESSED_MODEL``, then the table, then an
    error naming both.

    This is the model's twin of :func:`credential_for` and it exists for the same reason: the
    resolution happens once, in the package that owns the table, so no call site can invent a
    third arm. The arm that used to be third was ``settings.model or provider``, which sends a
    provider's own name as a model id - a 404 at every vendor, every time, and invisible offline
    because ``fake`` ignores the field.

    A provider the table has no entry for raises rather than guessing. That is FR-27's shape
    applied to the model: an unverified guess still reaches the endpoint, so a refusal that names
    the provider and the variable is the cheaper failure.
    """
    name = settings.require_provider() if provider is None else provider
    if settings.model:
        return settings.model
    default = quirks.default_model_for(name)
    if default is None:
        raise ConfigError(
            f"no model is configured for provider {name!r}, and no default is known for it. "
            f"Set {MODEL_VAR} to a model id that provider serves, in a .env file in this "
            f"directory or in your environment, which wins over the file: "
            f"{MODEL_VAR}=<model id>"
        )
    return default


@dataclass(frozen=True, slots=True)
class ProviderState:
    """One registered provider as the Models page shows it (FR-89, decision 55).

    ``configured`` is a fact about a variable's **name**: the key variable is set, or the provider
    takes none. No field here can hold a key, so a row cannot leak one however it is rendered.
    ``default_model`` is the table's confirmed id, ``None`` where the table knows none; it is not
    ``HARNESSED_MODEL``, which names what a run uses and not what the provider defaults to.
    """

    name: str
    configured: bool
    key_var: str | None
    default_model: str | None


def states(settings: Settings) -> tuple[ProviderState, ...]:
    """Every registered provider, in the table's order, read off resolved ``settings``.

    ``config.py`` stays the only reader of the environment (MUST rule 3): this asks
    ``Settings.api_key_for`` whether a key is there and keeps only the answer. A keyless provider
    is configured by construction, because there is nothing for an operator to set; an empty
    value is not a key, the same test ``Settings.require_api_key`` applies.
    """
    return tuple(
        ProviderState(
            name=name,
            configured=var is None or bool(settings.api_key_for(name)),
            key_var=var,
            default_model=quirks.default_model_for(name),
        )
        for name, var in PROVIDER_KEY_VARS.items()
    )


def json_payload(rows: tuple[ProviderState, ...]) -> dict[str, object]:
    """The one wire shape for :func:`states`: ``{"providers": [asdict(row), ...]}``.

    ``GET /v1/providers`` and ``harnessed providers list --json`` both print this, so the body and
    the line are the same bytes (FR-37).
    """
    return {"providers": [asdict(row) for row in rows]}


def provider_error(*, provider: str, model: str, status: int | None, message: str) -> ProviderError:
    """A vendor status error, restated as the one error type the CLI knows how to render.

    The vendor's own message is carried rather than dropped: a 4xx from a real endpoint is the
    most likely failure a new operator meets, and the endpoint is the only party that knows why
    it refused. It is scrubbed of every value :func:`~harnessed.config.secret_values` knows about
    first, because an SDK is free to quote the request back and this string is printed.

    When the status is one an endpoint uses to reject a model id, the message says the configured
    id may be stale. ``quirks.PROVIDER_DEFAULT_MODELS`` goes out of date as vendors retire ids,
    and this sentence is what points the next reader at that table instead of at the loop.
    """
    for secret in secret_values():
        message = message.replace(secret, "<redacted>")
    detail = f"provider {provider!r} refused the request for model {model!r}"
    answered = "no status" if status is None else str(status)
    stale = ""
    if status in STALE_MODEL_STATUSES and "model" in message.lower():
        stale = (
            f" That model id may be stale or unsupported for this provider - vendors retire them. "
            f"Set {MODEL_VAR} to one it serves, or update PROVIDER_DEFAULT_MODELS in "
            f"providers/quirks.py."
        )
    return ProviderError(f"{detail}: the endpoint answered {answered}: {message}{stale}")


def _build_fake(settings: Settings) -> Provider:
    """FakeProvider, imported late because it is built by a later ticket and needs no key.

    The import is dynamic so this module keeps working, and mypy keeps passing, in a checkout
    where ``providers/fake.py`` does not exist yet.
    """
    try:
        module = importlib.import_module("harnessed.providers.fake")
    except ModuleNotFoundError as exc:  # pragma: no cover - disappears when FR-2 lands
        raise ProviderError(
            "FakeProvider is not built yet (FR-2). Until it lands, set "
            "HARNESSED_PROVIDER to a live provider and its key, or run the offline "
            "test selection."
        ) from exc
    return cast(Provider, module.FakeProvider(settings))


#: Providers with no endpoint at all. Everything else is constructed from its endpoint's caps.
OFFLINE_FACTORIES: Final[dict[str, Callable[[Settings], Provider]]] = {"fake": _build_fake}


def _build_openai_compat(
    settings: Settings, provider: str, base_url: str | None, max_retries: int | None
) -> Provider:
    from harnessed.providers.openai_compat import OpenAICompatAdapter

    return OpenAICompatAdapter(
        settings=settings, provider=provider, base_url=base_url, max_retries=max_retries
    )


def _build_anthropic(
    settings: Settings, provider: str, base_url: str | None, max_retries: int | None
) -> Provider:
    from harnessed.providers.anthropic import AnthropicAdapter

    return AnthropicAdapter(
        settings=settings, provider=provider, base_url=base_url, max_retries=max_retries
    )


#: One adapter per dialect, not one per vendor. Both are imported lazily so that importing the
#: registry, or running the offline selection, never pulls in a vendor SDK.
_ADAPTERS: Final[
    dict[quirks.Dialect, Callable[[Settings, str, str | None, int | None], Provider]]
] = {
    "openai": _build_openai_compat,
    "anthropic": _build_anthropic,
}


def create(settings: Settings, *, max_retries: int | None = None) -> Provider:
    """Construct the configured provider.

    Raises ``ProviderNotConfiguredError`` when nothing was ever named (FR-27), ``ConfigError``
    for an unknown name, and ``MissingCredentialError`` for a live provider whose key is unset.
    Never a ``KeyError``.

    The FR-27 check is asked here as well as in ``providers.build_provider`` rather than instead
    of it: this is the function every other caller of the registry reaches, and a second door
    into the same table is exactly how a fallback gets reintroduced by accident.
    """
    name = settings.require_provider()
    offline = OFFLINE_FACTORIES.get(name)
    if offline is not None:
        return offline(settings)

    key_var_for(name)  # a typo'd name is a ConfigError before any endpoint is resolved
    base_url = settings.base_url or quirks.base_url_for(name)
    caps = quirks.caps_for(base_url)
    return _ADAPTERS[caps.dialect](settings, name, base_url, max_retries)
