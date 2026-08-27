"""Dealer API ingestion and daily inventory intelligence."""

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import os
from statistics import median
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from vehicles.models import (
    Dealer, DealerInventorySnapshot, DealerListingMembership, DealerSnapshotListing,
    ImportRun, Listing, ListingSnapshot,
)
from vehicles.sources.polovni_automobili.detail_record import description_flags, fetch_detail
from vehicles.sources.polovni_automobili.search_results import (
    CollectionError, USER_AGENT, canonicalize_public_ad_url,
)


class DealerAPIError(Exception):
    pass


REQUIRED_FIELDS = ("external_id", "make", "model", "year", "mileage", "fuel", "asking_price")


def fetch_dealer_inventory(dealer: Dealer) -> list[dict]:
    if urlparse(dealer.api_url).hostname in {"polovniautomobili.com", "www.polovniautomobili.com"}:
        return _fetch_polovni_inventory(dealer.api_url)
    headers = {"Accept": "application/json"}
    if dealer.api_token_env_var:
        token = os.environ.get(dealer.api_token_env_var)
        if not token:
            raise DealerAPIError(f"Environment variable {dealer.api_token_env_var} is not set.")
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(dealer.api_url, headers=headers, timeout=30, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DealerAPIError(f"Dealer API request failed: {exc}") from exc
    records = payload.get("vehicles") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise DealerAPIError('Expected a JSON array or an object containing a "vehicles" array.')
    return records


def _next_dealer_page_url(soup, current_url, dealer_url):
    """Return the next storefront page advertised by its pagination links."""
    current = urlparse(current_url)
    dealer = urlparse(dealer_url)
    current_page = int(parse_qs(current.query).get("page", ["1"])[0])
    candidates = []
    for anchor in soup.find_all("a", href=True):
        candidate_url = urljoin(current_url, anchor["href"])
        candidate = urlparse(candidate_url)
        if candidate.hostname != dealer.hostname or candidate.path.rstrip("/") != dealer.path.rstrip("/"):
            continue
        try:
            candidate_page = int(parse_qs(candidate.query).get("page", [""])[0])
        except ValueError:
            continue
        if candidate_page > current_page:
            priority = 0 if "next" in anchor.get("rel", []) else 1
            candidates.append((priority, candidate_page, candidate_url))
    return min(candidates, default=(None, None, None))[2]


def _fetch_polovni_inventory(dealer_url: str) -> list[dict]:
    """Collect a public Polovni dealer storefront through its existing detail API."""
    parsed = urlparse(dealer_url)
    if parsed.scheme != "https" or parsed.hostname not in {"polovniautomobili.com", "www.polovniautomobili.com"}:
        raise DealerAPIError("Polovni dealer URL must use HTTPS on polovniautomobili.com.")
    if len([part for part in parsed.path.split("/") if part]) != 1:
        raise DealerAPIError("Expected a Polovni dealer storefront URL.")
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(30, connect=10), follow_redirects=True, transport=httpx.HTTPTransport(retries=2))
    advertisements = {}
    try:
        page_url = dealer_url
        visited_pages = set()
        while page_url and page_url not in visited_pages:
            visited_pages.add(page_url)
            try:
                response = client.get(page_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise DealerAPIError(f"Dealer storefront request failed: {exc}") from exc
            lowered = response.text.casefold()
            if any(marker in lowered for marker in ("captcha", "cf-chl-", "verify you are human", "access denied")):
                raise DealerAPIError("Dealer storefront returned an access challenge.")
            page_ids = set()
            known_before_page = set(advertisements)
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.find_all("a", href=True):
                try:
                    listing_id, public_url = canonicalize_public_ad_url(anchor["href"])
                except ValueError:
                    continue
                page_ids.add(listing_id)
                advertisements.setdefault(listing_id, public_url)
            if not page_ids:
                if len(visited_pages) == 1:
                    raise DealerAPIError("No vehicle ads were found on the dealer storefront.")
                break
            if len(visited_pages) > 1 and page_ids.issubset(known_before_page):
                break
            page_url = _next_dealer_page_url(soup, page_url, dealer_url)

        records = []
        for listing_id, public_url in advertisements.items():
            try:
                detail = fetch_detail(listing_id, client=client)
            except CollectionError as exc:
                if str(exc) == "Detail response has no asking price.":
                    continue
                raise DealerAPIError(f"Could not fetch advertisement {listing_id}: {exc}") from exc
            except Exception as exc:
                raise DealerAPIError(f"Could not fetch advertisement {listing_id}: {exc}") from exc
            records.append({
                "external_id": detail.external_id, "source_url": public_url,
                "title": " ".join(part for part in (detail.brand, detail.model, detail.mark) if part),
                "make": detail.brand, "model": detail.model, "year": detail.year,
                "mileage": detail.mileage, "fuel": detail.fuel or "unknown", "transmission": detail.gearbox,
                "asking_price": str(detail.price),
                "published_at": detail.publish_date.isoformat() if detail.publish_date else None,
                "vin": detail.vin,
                "description_flags": description_flags(detail.description),
            })
        return records
    finally:
        client.close()


def _datetime(value, fallback):
    if not value:
        return fallback
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise DealerAPIError(f"Invalid datetime: {value}")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _normalize(record):
    missing = [field for field in REQUIRED_FIELDS if record.get(field) in (None, "")]
    if missing:
        raise DealerAPIError(f"Vehicle is missing required fields: {', '.join(missing)}")
    try:
        return {
            "external_id": str(record["external_id"]), "make": str(record["make"]).strip(),
            "model": str(record["model"]).strip(), "year": int(record["year"]),
            "mileage": int(record["mileage"]), "fuel": str(record["fuel"]).strip(),
            "asking_price": Decimal(str(record["asking_price"])),
            "title": str(record.get("title", "")).strip(), "source_url": str(record.get("source_url", "")).strip(),
            "thumbnail_url": str(record.get("thumbnail_url", "")).strip(),
            "transmission": str(record.get("transmission", "")).strip(),
            "vin": "".join(str(record.get("vin", "")).upper().split()),
            "published_at": parse_date(str(record["published_at"])) if record.get("published_at") else None,
            "description_flags": list(record.get("description_flags") or []),
            "raw": record,
        }
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise DealerAPIError(f"Vehicle {record.get('external_id', '?')} has invalid numeric data.") from exc


@transaction.atomic
def ingest_dealer_inventory(dealer: Dealer, records: list[dict], *, observed_at=None):
    observed_at = observed_at or timezone.now()
    if timezone.is_naive(observed_at):
        observed_at = timezone.make_aware(observed_at)
    normalized = [_normalize(record) for record in records]
    ids = [item["external_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise DealerAPIError("The API response contains duplicate external_id values.")

    snapshot = DealerInventorySnapshot.objects.create(dealer=dealer, observed_at=observed_at)
    import_run = ImportRun.objects.create(source=dealer.source, snapshot_at=observed_at, is_full_snapshot=False)
    present_listing_ids, prices, ages = set(), [], []
    for item in normalized:
        listing, created = Listing.objects.get_or_create(
            source=dealer.source, external_id=item["external_id"],
            defaults={"first_seen_at": observed_at, "last_seen_at": observed_at, "status": Listing.Status.ACTIVE,
                      **{key: item[key] for key in ("make", "model", "year", "mileage", "fuel", "title", "source_url", "thumbnail_url", "transmission", "published_at", "vin")}},
        )
        for field in ("make", "model", "year", "mileage", "fuel", "title", "source_url", "thumbnail_url", "transmission", "published_at", "vin"):
            setattr(listing, field, item[field])
        if item["vin"]:
            original = Listing.objects.filter(vin=item["vin"]).exclude(pk=listing.pk).order_by("first_seen_at", "pk").first()
            if original:
                listing.repeated_listing_of = original.repeated_listing_of or original
                listing.repeat_detection_method = Listing.RepeatDetectionMethod.VIN
                listing.repeat_detected_at = observed_at
        listing.last_seen_at, listing.status = observed_at, Listing.Status.ACTIVE
        listing.save()
        listing_snapshot, _ = ListingSnapshot.objects.update_or_create(
            listing=listing, observed_at=observed_at,
            defaults={"import_run": import_run, "asking_price": item["asking_price"], "mileage": item["mileage"], "vin": item["vin"], "raw_data": item["raw"]},
        )
        membership, _ = DealerListingMembership.objects.get_or_create(
            dealer=dealer, listing=listing,
            defaults={"first_seen_at": observed_at, "last_seen_at": observed_at},
        )
        membership.last_seen_at, membership.is_currently_present, membership.disappeared_at = observed_at, True, None
        membership.description_flags = item["description_flags"]
        membership.save()
        DealerSnapshotListing.objects.create(
            snapshot=snapshot, listing=listing, asking_price=item["asking_price"],
            mileage=item["mileage"], vin=item["vin"], status=Listing.Status.ACTIVE,
            observed_at=observed_at, raw_data=item["raw"],
        )
        present_listing_ids.add(listing.pk)
        prices.append(item["asking_price"])
        start_date = item["published_at"] or timezone.localdate(membership.first_seen_at)
        ages.append(max((timezone.localdate(observed_at) - start_date).days, 0))

    disappeared = dealer.listing_memberships.filter(is_currently_present=True).exclude(listing_id__in=present_listing_ids)
    disappeared_count = disappeared.update(is_currently_present=False, disappeared_at=observed_at)
    bracket_size = Decimal(dealer.price_bracket_size)
    brackets = Counter((price // bracket_size) * bracket_size for price in prices)
    dominant_low = min((low for low, count in brackets.items() if count == max(brackets.values())), default=None)
    snapshot.inventory_count = len(normalized)
    snapshot.median_price = Decimal(str(median(prices))) if prices else None
    snapshot.dominant_price_bracket_low = dominant_low
    snapshot.dominant_price_bracket_high = dominant_low + bracket_size if dominant_low is not None else None
    snapshot.dominant_price_bracket_count = brackets.get(dominant_low, 0)
    snapshot.average_active_days = Decimal(sum(ages)) / Decimal(len(ages)) if ages else None
    snapshot.disappeared_count = disappeared_count
    snapshot.status = DealerInventorySnapshot.Status.COMPLETED
    snapshot.save()
    import_run.processed_count = len(normalized)
    import_run.created_count = sum(1 for item in snapshot.inventory_items.all() if item.listing.first_seen_at == observed_at)
    import_run.updated_count = len(normalized) - import_run.created_count
    import_run.completed_at, import_run.status = timezone.now(), ImportRun.Status.COMPLETED
    import_run.save()
    return snapshot


def refresh_dealer(dealer: Dealer, *, observed_at=None):
    return ingest_dealer_inventory(dealer, fetch_dealer_inventory(dealer), observed_at=observed_at)
