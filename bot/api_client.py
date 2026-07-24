"""HTTP layer for the external API the bot talks to.

Every outbound API call lives in this module so the handlers in main.py
stay small and never deal with URLs, status codes, or JSON shapes. To
integrate a different API, point config at its base URL and rewrite the
fetch function — the rest of the bot only sees plain dicts and the two
exception types below.
"""

import logging
from typing import Any

import httpx

from bot import config

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """The API could not be reached or returned an unexpected response."""


class NotFoundError(ApiError):
    """The API answered, but has no data for the requested query."""


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """One shared client per process: reuses connections between requests."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=config.OPENFDA_BASE_URL,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": "medik-bot/1.0"},
        )
    return _client


async def aclose() -> None:
    """Close the shared client; the bot calls this on shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _first(record: dict[str, Any], field: str) -> str | None:
    """openFDA wraps almost every field in a list; unwrap the first entry."""
    value = record.get(field)
    if isinstance(value, list) and value:
        return str(value[0])
    return None


async def fetch_drug_label(name: str) -> dict[str, str | None]:
    """Look up an FDA drug label by brand or generic name.

    Returns a flat dict of just the fields the bot displays.
    Raises NotFoundError when the FDA has no label for the name, and
    ApiError for network failures or responses we don't understand.
    """
    term = name.replace('"', " ").strip()
    params: dict[str, str | int] = {
        "search": f'openfda.brand_name:"{term}" OR openfda.generic_name:"{term}"',
        "limit": 1,
    }
    if config.OPENFDA_API_KEY:
        params["api_key"] = config.OPENFDA_API_KEY

    try:
        response = await _get_client().get("/drug/label.json", params=params)
    except httpx.HTTPError as exc:
        logger.warning("openFDA request failed: %s", exc)
        raise ApiError("could not reach openFDA") from exc

    # openFDA reports "no matches" as HTTP 404 with a JSON error body.
    if response.status_code == 404:
        raise NotFoundError(term)
    if response.status_code != 200:
        logger.warning(
            "openFDA returned HTTP %s: %s", response.status_code, response.text[:200]
        )
        raise ApiError(f"openFDA returned HTTP {response.status_code}")

    try:
        record = response.json()["results"][0]
    except (ValueError, LookupError) as exc:
        raise ApiError("unexpected openFDA response shape") from exc

    openfda = record.get("openfda", {})
    return {
        "brand_name": _first(openfda, "brand_name"),
        "generic_name": _first(openfda, "generic_name"),
        "purpose": _first(record, "purpose"),
        "indications": _first(record, "indications_and_usage"),
        "warnings": _first(record, "warnings"),
        "dosage": _first(record, "dosage_and_administration"),
    }
