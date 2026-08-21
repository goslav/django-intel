from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html import unescape
import re
import unicodedata

import httpx
from django.utils.dateparse import parse_date, parse_datetime

from .search_results import CollectionError, USER_AGENT


DETAIL_URL = "https://core.polovniautomobili.com/api/v1/classifieds/car/{listing_id}"


@dataclass(frozen=True)
class DetailRecord:
    external_id: str
    brand: str
    model: str
    mark: str
    year: int
    mileage: int
    fuel: str
    source_fuel: str
    gearbox: str
    engine_volume: str
    power_kw: int | None
    horsepower: int | None
    chassis: str
    chassis_id: str
    price: Decimal
    price_currency: str
    publish_date: date | None
    renew_date: date | None
    condition_new: bool
    status: str
    description: str


def description_flags(description: str) -> list[str]:
    """Classify Opis text without retaining it."""
    plain = re.sub(r"<[^>]+>", " ", unescape(str(description or "")))
    normalized = unicodedata.normalize("NFKD", plain).encode("ascii", "ignore").decode().casefold()
    normalized = re.sub(r"\s+", " ", normalized)
    flags = []
    if re.search(r"\b(usluzna|komisiona)\s+prodaja\b|\bprodaja\s+za\s+racun\s+vlasnika\b", normalized):
        flags.append("Service/commission sale wording")
    if re.search(r"\b(novo|nekorisceno)\s+vozilo\b|\bnova\s+vozila\b|\bvozilo\s+(je\s+)?nekorisceno\b", normalized):
        flags.append("New/unused vehicle wording")
    return flags


def _text(value):
    if isinstance(value, dict):
        return str(value.get("name") or value.get("value") or "").strip()
    return str(value or "").strip()


def _source_date(value) -> date | None:
    text = _text(value)
    if not text:
        return None
    parsed_date = parse_date(text)
    if parsed_date:
        return parsed_date
    parsed_datetime = parse_datetime(text)
    return parsed_datetime.date() if parsed_datetime else None


def normalize_fuel(value: str) -> str:
    source_value = _text(value)
    normalized = source_value.casefold()
    if normalized in {"dizel", "diesel", "disel"}:
        return "diesel"
    if normalized in {"benzin", "petrol", "gasoline"}:
        return "petrol"
    if "hibrid" in normalized or "hybrid" in normalized:
        return "hybrid"
    return normalized


def normalize_detail(payload: dict, expected_id: str) -> DetailRecord:
    if not isinstance(payload, dict):
        raise CollectionError("Detail response is not a JSON object.")
    external_id = str(payload.get("id") or "")
    if external_id != str(expected_id):
        raise CollectionError("Detail response advertisement ID did not match the requested ID.")
    if payload.get("price") in (None, ""):
        raise CollectionError("Detail response has no asking price.")
    try:
        year = int(payload["year"])
        mileage = int(str(payload["mileage"]).replace(".", "").replace(",", ""))
        raw_price = str(payload["price"]).strip()
        if "," in raw_price and "." in raw_price:
            raw_price = raw_price.replace(".", "").replace(",", ".")
        elif "," in raw_price:
            raw_price = raw_price.replace(",", ".")
        price = Decimal(raw_price)
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise CollectionError("Detail response has invalid year, mileage, or price.") from exc
    power = payload.get("power")
    horsepower = payload.get("horsePower")
    return DetailRecord(
        external_id, _text(payload.get("brand")), _text(payload.get("model")),
        _text(payload.get("mark")), year, mileage, normalize_fuel(payload.get("fuel")),
        _text(payload.get("fuel")),
        _text(payload.get("gearBox")), _text(payload.get("engineVolume")),
        int(power) if str(power or "").isdigit() else None,
        int(horsepower) if str(horsepower or "").isdigit() else None,
        _text(payload.get("chassis")), _text(payload.get("chassisId")), price,
        _text(payload.get("priceCurrency")), _source_date(payload.get("publishDate")),
        _source_date(payload.get("renewDate")), bool(payload.get("conditionNew")),
        _text(payload.get("status")),
        str(payload.get("description") or ""),
    )


def fetch_detail(listing_id: str, *, client=None) -> DetailRecord:
    owns_client = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(20, connect=10), transport=httpx.HTTPTransport(retries=2))
    try:
        response = client.get(DETAIL_URL.format(listing_id=listing_id))
        if response.status_code in (403, 429):
            raise CollectionError(f"Detail fetch stopped safely after HTTP {response.status_code}.")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise CollectionError("Detail response contained invalid JSON.") from exc
        return normalize_detail(payload, listing_id)
    except httpx.HTTPError as exc:
        raise CollectionError(f"Detail request failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()
