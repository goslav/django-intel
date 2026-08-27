import time
from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from vehicles.models import (
    ImportRun, Listing, ListingSnapshot, SourceSearch, SourceSearchMembership,
    SourceSearchRun, SourceSearchRunListing,
)
from vehicles.services.vehicle_classification import classify_listing
from vehicles.sources.polovni_automobili.detail_record import fetch_detail
from vehicles.sources.polovni_automobili.search_results import (
    CollectionError, DiscoveryResult,
    discover_advertisements, resolve_public_ad_url,
)


@dataclass(frozen=True)
class RefreshResult:
    run: SourceSearchRun | None
    discovery: DiscoveryResult
    unknown_classifications: int


def _upsert_detail(source_search, run, import_run, advertisement, observation, detail):
    observed_at = run.started_at
    existing_listing = Listing.objects.filter(
        source=source_search.source, external_id=detail.external_id
    ).first()
    public_url, url_source = resolve_public_ad_url(
        advertisement.public_url, detail.external_id,
        existing_listing.source_url if existing_listing else "",
    )
    defaults = {
        "source_url": public_url,
        "thumbnail_url": advertisement.thumbnail_url or (
            existing_listing.thumbnail_url if existing_listing else ""
        ),
        "title": " ".join(part for part in (detail.brand, detail.model, detail.mark) if part),
        "make": detail.brand, "model": detail.model, "year": detail.year,
        "mileage": detail.mileage, "fuel": detail.fuel, "transmission": detail.gearbox,
        "vin": "".join(detail.vin.upper().split()),
        "power_kw": detail.power_kw, "first_seen_at": observed_at,
        "last_seen_at": observed_at, "published_at": detail.publish_date,
        "status": Listing.Status.ACTIVE,
    }
    listing, created = Listing.objects.get_or_create(
        source=source_search.source, external_id=detail.external_id, defaults=defaults,
    )
    updated = False
    if not created:
        for field, value in defaults.items():
            if field == "first_seen_at":
                continue
            if getattr(listing, field) != value:
                setattr(listing, field, value)
                updated = True
        if updated:
            listing.save()
    if listing.vin:
        original = Listing.objects.filter(vin=listing.vin).exclude(pk=listing.pk).order_by("first_seen_at", "pk").first()
        if original:
            listing.repeated_listing_of = original.repeated_listing_of or original
            listing.repeat_detection_method = Listing.RepeatDetectionMethod.VIN
            listing.repeat_detected_at = observed_at
            listing.save(update_fields=("repeated_listing_of", "repeat_detection_method", "repeat_detected_at", "updated_at"))
    classify_listing(listing)
    latest_snapshot = listing.snapshots.order_by("-observed_at", "-pk").first()
    snapshot_created = not latest_snapshot or (
        latest_snapshot.asking_price != detail.price or latest_snapshot.mileage != detail.mileage
    )
    if snapshot_created:
        ListingSnapshot.objects.get_or_create(
            listing=listing, observed_at=observed_at,
            defaults={
            "import_run": import_run, "asking_price": detail.price,
            "mileage": detail.mileage, "vin": "".join(detail.vin.upper().split()),
            "raw_data": {
                "mark": detail.mark, "engine_volume": detail.engine_volume,
                "source_fuel": detail.source_fuel,
                "power_kw": detail.power_kw, "horsepower": detail.horsepower,
                "chassis": detail.chassis, "chassis_id": detail.chassis_id,
                "price_currency": detail.price_currency,
                "publish_date": detail.publish_date.isoformat() if detail.publish_date else "",
                "renew_date": detail.renew_date.isoformat() if detail.renew_date else "",
                "condition_new": detail.condition_new,
                "status": detail.status,
                "vin": detail.vin,
            },
            },
        )
    observation.listing = listing
    observation.detail_fetch_status = SourceSearchRunListing.DetailStatus.COMPLETED
    observation.error_message = "" if url_source == "search_result" else "Public URL used numeric ID fallback."
    observation.save(update_fields=("listing", "detail_fetch_status", "error_message"))
    SourceSearchMembership.objects.update_or_create(
        source_search=source_search, listing=listing,
        defaults={"last_seen_at": observed_at, "is_currently_present": True},
        create_defaults={"first_seen_at": observed_at, "last_seen_at": observed_at, "is_currently_present": True},
    )
    return listing, created, updated, snapshot_created


def refresh_source_search(source_search, *, maximum_pages=None, delay_seconds=None, discovery_client=None, detail_client=None, dry_run=False):
    maximum_pages = maximum_pages or source_search.maximum_pages
    delay_seconds = float(source_search.request_delay_seconds if delay_seconds is None else delay_seconds)
    if dry_run:
        discovery = discover_advertisements(
            source_search.search_url, maximum_pages=maximum_pages,
            delay_seconds=delay_seconds, client=discovery_client,
        )
        return RefreshResult(None, discovery, 0)

    source_search.last_refresh_started_at = timezone.now()
    source_search.last_refresh_status = SourceSearch.RefreshStatus.RUNNING
    source_search.last_error_summary = ""
    source_search.save(update_fields=("last_refresh_started_at", "last_refresh_status", "last_error_summary", "updated_at"))
    run = SourceSearchRun.objects.create(source_search=source_search)
    try:
        discovery = discover_advertisements(
            source_search.search_url, maximum_pages=maximum_pages,
            delay_seconds=delay_seconds, client=discovery_client,
        )
    except CollectionError as exc:
        run.completed_at = timezone.now()
        run.status = SourceSearchRun.Status.FAILED
        run.result_pages_requested = 1
        run.error_count = 1
        run.error_summary = str(exc)
        run.save()
        source_search.last_refresh_completed_at = run.completed_at
        source_search.last_refresh_status = SourceSearch.RefreshStatus.FAILED
        source_search.last_error_summary = str(exc)
        source_search.save(update_fields=("last_refresh_completed_at", "last_refresh_status", "last_error_summary", "updated_at"))
        raise
    import_run = ImportRun.objects.create(source=source_search.source, snapshot_at=run.started_at)
    run.result_pages_requested = discovery.pages_requested
    run.result_pages_completed = discovery.pages_completed
    run.advertisement_ids_discovered = len(discovery.advertisements)
    errors = []
    observed_ids = []
    unknown = 0
    for index, advertisement in enumerate(discovery.advertisements):
        observation = SourceSearchRunListing.objects.create(
            source_search_run=run,
            external_id=advertisement.external_id,
            public_advertisement_url=advertisement.public_url,
        )
        existing_listing = Listing.objects.filter(
            source=source_search.source, external_id=advertisement.external_id
        ).first()
        if existing_listing:
            observation.listing = existing_listing
            observation.save(update_fields=("listing",))
            fields_to_update = []
            if existing_listing.source_url != advertisement.public_url:
                existing_listing.source_url = advertisement.public_url
                fields_to_update.append("source_url")
            if advertisement.thumbnail_url and existing_listing.thumbnail_url != advertisement.thumbnail_url:
                existing_listing.thumbnail_url = advertisement.thumbnail_url
                fields_to_update.append("thumbnail_url")
            if fields_to_update:
                existing_listing.save(update_fields=(*fields_to_update, "updated_at"))
        try:
            detail = fetch_detail(advertisement.external_id, client=detail_client)
            with transaction.atomic():
                listing, created, updated, snapshot_created = _upsert_detail(
                    source_search, run, import_run, advertisement, observation, detail,
                )
            observed_ids.append(listing.pk)
            run.detail_records_completed += 1
            run.listings_created += int(created)
            run.listings_updated += int(updated)
            run.snapshots_created += int(snapshot_created)
            if listing.facelift_status == Listing.FaceliftStatus.UNKNOWN:
                unknown += 1
        except CollectionError as exc:
            errors.append(f"Advertisement {advertisement.external_id}: {exc}")
            observation.detail_fetch_status = SourceSearchRunListing.DetailStatus.FAILED
            observation.error_message = str(exc)
            observation.save(update_fields=("detail_fetch_status", "error_message"))
            if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                break
        if delay_seconds and index + 1 < len(discovery.advertisements):
            time.sleep(delay_seconds)

    complete = discovery.is_complete and not errors and run.detail_records_completed == len(discovery.advertisements)
    if complete:
        missing = source_search.memberships.filter(is_currently_present=True).exclude(listing_id__in=observed_ids)
        run.missing_advertisements_detected = missing.count()
        missing.update(is_currently_present=False)
    run.error_count = len(errors)
    run.error_summary = "\n".join(errors)
    run.status = SourceSearchRun.Status.COMPLETED if complete else SourceSearchRun.Status.PARTIAL
    if not run.detail_records_completed and errors:
        run.status = SourceSearchRun.Status.FAILED
    run.completed_at = timezone.now()
    run.save()
    import_run.status = ImportRun.Status.COMPLETED if complete else ImportRun.Status.FAILED
    import_run.completed_at = run.completed_at
    import_run.processed_count = len(discovery.advertisements)
    import_run.created_count = run.listings_created
    import_run.updated_count = run.listings_updated
    import_run.error_count = run.error_count
    import_run.error_summary = run.error_summary
    import_run.save()
    source_search.last_refresh_completed_at = run.completed_at
    source_search.last_refresh_status = run.status
    source_search.last_error_summary = run.error_summary
    source_search.save(update_fields=("last_refresh_completed_at", "last_refresh_status", "last_error_summary", "updated_at"))
    return RefreshResult(run, discovery, unknown)
