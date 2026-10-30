"""What a provider charges, read from its own catalog, and the rows that reading left behind.

FR-73, decision 53, MUST rule 28. :func:`fetch` is the **only** code path in the package that asks a
provider for a price, and ``cli/prices_cmd.py`` is the only module that names it (asserted by
``tests/test_pricing.py``). Nothing at runtime asks: a run, a page and a console message read
:func:`stored`, which is a query over ``prices`` (migration 011), so the harness works with no
network because it never needs one for a figure.

**There is no fallback table.** This module holds 0 literal prices. A provider whose listing
publishes none leaves its models unpriced, and the refresh says so by name; a guess rendered as a
dollar figure is exactly the failure this project exists to prevent.

The catalog is each provider's own models listing, the same endpoint its adapter already talks to.
A listing *publishes* a catalog when its entries carry a ``pricing`` object, which is data the
provider answers with, not a table kept here. The live run of 2026-09-23 found OpenRouter and Groq
publishing one (per-token decimal strings under ``prompt``, ``completion`` and
``input_cache_read``) and DeepSeek not; Anthropic and OpenAI were not configured on that machine.

Every rate is stored as a decimal string **per million tokens**, converted with
:class:`decimal.Decimal` and never through a float. A model is priced only when both its input and
output rates are usable; a variable price (OpenRouter's routers publish ``-1``) or a missing output
rate leaves it unpriced rather than half-priced. Tiered ``overrides`` are not read: the base rate
is the one stored.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Final
from urllib.request import Request, urlopen

from harnessed import clock
from harnessed.config import Settings
from harnessed.errors import ProviderError
from harnessed.providers import quirks, registry

__all__ = [
    "CURRENCY",
    "FETCH_TIMEOUT_S",
    "TOKENS_PER_MILLION",
    "Catalog",
    "Price",
    "ProviderError",
    "catalog_url",
    "configured",
    "fetch",
    "json_payload",
    "parse",
    "stored",
    "upserts",
]

#: The one currency stored (decision 53). A second is a new decision, not a column default.
CURRENCY: Final = "USD"

#: Rates are published per token and stored per million tokens.
TOKENS_PER_MILLION: Final = 1_000_000

#: How long one catalog read may take before the refresh names the provider as unreachable.
FETCH_TIMEOUT_S: Final = 30

#: Where each dialect keeps its models listing, relative to the provider's base URL.
_LISTING_PATHS: Final[dict[quirks.Dialect, str]] = {"openai": "/models", "anthropic": "/v1/models"}

#: The catalog's own names for the three rates a turn can need.
_INPUT: Final = "prompt"
_OUTPUT: Final = "completion"
_CACHED_INPUT: Final = "input_cache_read"

_UPSERT: Final = (
    "INSERT INTO prices "
    "(provider, model, input, output, cached_input, currency, source_url, fetched_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (provider, model) DO UPDATE SET "
    "input = excluded.input, output = excluded.output, cached_input = excluded.cached_input, "
    "currency = excluded.currency, source_url = excluded.source_url, "
    "fetched_at = excluded.fetched_at"
)

_SELECT: Final = (
    "SELECT provider, model, input, output, cached_input, currency, source_url, fetched_at "
    "FROM prices ORDER BY provider, model"
)

_decoder: Final = json.JSONDecoder()


@dataclass(frozen=True, slots=True)
class Price:
    """One model's published rates, per million tokens, every one a decimal string.

    ``fetched_at`` is when this row's catalog was read. It is the row's, and a reader shows it as
    such: never the time the row is shown.
    """

    provider: str
    model: str
    input: str
    output: str
    cached_input: str | None
    currency: str
    source_url: str
    fetched_at: str


@dataclass(frozen=True, slots=True)
class Catalog:
    """One provider's listing, read once: how many models it listed and which it priced."""

    provider: str
    source_url: str
    fetched_at: str
    listed: int
    #: Whether any entry carried a ``pricing`` object at all. ``False`` is "publishes no catalog".
    publishes: bool
    prices: tuple[Price, ...]

    @property
    def unpriced(self) -> int:
        """Models the listing named that carry no usable input and output rate."""
        return self.listed - len(self.prices)


def configured(settings: Settings) -> tuple[str, ...]:
    """The providers whose key is set, in the table's order: the ones a bare refresh asks.

    A provider that needs no key (``fake``, a local server) has no catalog to publish and is only
    asked when named with ``--provider``.
    """
    return tuple(
        name
        for name in quirks.PROVIDER_BASE_URLS
        if registry.requires_credential(name) and settings.api_key_for(name)
    )


def catalog_url(provider: str, base_url: str | None = None) -> str:
    """The models listing for ``provider``: its base URL, then its dialect's listing path."""
    base = base_url or quirks.base_url_for(provider)
    if base is None:
        raise ProviderError(f"provider {provider!r} has no endpoint, so it has no catalog to read.")
    return base.rstrip("/") + _LISTING_PATHS[quirks.caps_for(base).dialect]


def _headers(dialect: quirks.Dialect, key: str | None) -> dict[str, str]:
    headers = {"User-Agent": "harnessed", "Accept": "application/json"}
    if key is None:
        return headers
    if dialect == "anthropic":
        return {**headers, "x-api-key": key, "anthropic-version": "2023-06-01"}
    return {**headers, "Authorization": f"Bearer {key}"}


def fetch(settings: Settings, provider: str) -> Catalog:
    """Ask ``provider`` for its catalog, once. The one network read of a price in the package.

    The key comes from ``registry.credential_for``, the one credential check, so an unconfigured
    provider is refused naming its variable before anything is sent. A refusal or an unreachable
    endpoint is a :class:`ProviderError` naming the provider and the URL; the key is never in it.
    """
    key = registry.credential_for(settings, provider)
    base = settings.base_url if provider == settings.provider else None
    url = catalog_url(provider, base)
    dialect = quirks.caps_for(url).dialect
    request = Request(url, headers=_headers(dialect, key))
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            body: bytes = response.read()
    except urllib.error.HTTPError as exc:
        raise ProviderError(
            f"provider {provider!r} refused the catalog request at {url}: "
            f"the endpoint answered {exc.code}."
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProviderError(f"could not reach provider {provider!r} at {url}: {exc}.") from exc
    return parse(provider, body, source_url=url, fetched_at=clock.isoformat(clock.now()))


def parse(provider: str, body: bytes, *, source_url: str, fetched_at: str) -> Catalog:
    """A models listing into a :class:`Catalog`. Pure: the offline tests call it on recordings."""
    try:
        listing: object = _decoder.decode(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProviderError(f"provider {provider!r} answered {source_url} with no JSON.") from exc
    entries = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(entries, list):
        raise ProviderError(
            f"provider {provider!r} answered {source_url} with something that is not a models "
            "listing: no data list."
        )
    listed = 0
    publishes = False
    prices: dict[str, Price] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        listed += 1
        rates = entry.get("pricing")
        if not isinstance(rates, dict):
            continue
        publishes = True
        input_rate = _per_million(rates.get(_INPUT))
        output_rate = _per_million(rates.get(_OUTPUT))
        if input_rate is None or output_rate is None:
            continue
        prices[entry["id"]] = Price(
            provider=provider,
            model=entry["id"],
            input=input_rate,
            output=output_rate,
            cached_input=_per_million(rates.get(_CACHED_INPUT)),
            currency=CURRENCY,
            source_url=source_url,
            fetched_at=fetched_at,
        )
    return Catalog(provider, source_url, fetched_at, listed, publishes, tuple(prices.values()))


def _per_million(published: object) -> str | None:
    """A per-token decimal string as a per-million one, or ``None`` when it is not a usable rate.

    A negative rate is how a catalog says "it depends"; it is not a price, and neither is a number
    sent as a JSON number, which has already been through a float on the provider's side.
    """
    if not isinstance(published, str):
        return None
    try:
        rate = Decimal(published)
    except InvalidOperation:
        return None
    if not rate.is_finite() or rate.is_signed():
        return None
    return format((rate * TOKENS_PER_MILLION).normalize(), "f")


def upserts(catalog: Catalog) -> tuple[tuple[str, tuple[object, ...]], ...]:
    """The statements that store ``catalog``: one upsert per priced model, keyed ``(provider,
    model)``. A model the catalog no longer prices keeps its last row and that row's own
    ``fetched_at``, which is what says how old it is."""
    return tuple(
        (
            _UPSERT,
            (
                price.provider,
                price.model,
                price.input,
                price.output,
                price.cached_input,
                price.currency,
                price.source_url,
                price.fetched_at,
            ),
        )
        for price in catalog.prices
    )


def stored(conn: sqlite3.Connection) -> tuple[Price, ...]:
    """Every stored row, by provider then model: the read behind ``GET /v1/prices`` and ``harnessed
    prices list``. It asks no provider anything."""
    return tuple(Price(*row) for row in conn.execute(_SELECT).fetchall())


def json_payload(rows: tuple[Price, ...]) -> dict[str, object]:
    """The one wire shape for :func:`stored`: ``{"prices": [asdict(row), ...]}``.

    Every rate is the stored string (MUST rule 29) and ``cached_input`` is ``null`` where the
    catalog published no cache rate.
    """
    return {"prices": [asdict(row) for row in rows]}
