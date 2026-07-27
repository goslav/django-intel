"""CSV listing ingestion.

The CSV must contain this header (column order is not significant):

source,external_id,source_url,title,make,model,year,mileage,fuel,
transmission,asking_price,observed_at

``source_url``, ``title``, and ``transmission`` may be empty. All other values
are required. ``year`` and ``mileage`` are non-negative integers,
``asking_price`` is a non-negative decimal, and ``observed_at`` is an ISO-8601
datetime. Naive datetimes are interpreted in Django's configured time zone.
Every row must have the source supplied to :func:`import_listings`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from vehicles.models import ImportRun, Listing, ListingSnapshot


CSV_COLUMNS = (
    "source",
    "external_id",
    "source_url",
    "title",
    "make",
    "model",
    "year",
    "mileage",
    "fuel",
    "transmission",
    "asking_price",
    "observed_at",
)
REQUIRED_VALUES = (
    "source",
    "external_id",
    "make",
    "model",
    "year",
    "mileage",
    "fuel",
    "asking_price",
    "observed_at",
)


@dataclass(frozen=True)
class ImportResult:
    import_run: ImportRun
    snapshots_created: int
    errors: tuple[str, ...]


class RowValidationError(ValueError):
    pass


def _parse_non_negative_integer(value: str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RowValidationError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise RowValidationError(f"{field} must not be negative")
    return parsed


def _parse_price(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise RowValidationError("asking_price must be a decimal number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise RowValidationError("asking_price must be a non-negative decimal number")
    return parsed


def _parse_observed_at(value: str) -> datetime:
    parsed = parse_datetime(value)
    if parsed is None:
        raise RowValidationError("observed_at must be an ISO-8601 datetime")
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _validate_row(row: dict[str, str | None], expected_source: str) -> dict:
    cleaned = {key: (row.get(key) or "").strip() for key in CSV_COLUMNS}
    missing = [field for field in REQUIRED_VALUES if not cleaned[field]]
    if missing:
        raise RowValidationError(f"missing required value(s): {', '.join(missing)}")
    if cleaned["source"] != expected_source:
        raise RowValidationError(
            f"source {cleaned['source']!r} does not match --source {expected_source!r}"
        )
    cleaned["year"] = _parse_non_negative_integer(cleaned["year"], "year")
    cleaned["mileage"] = _parse_non_negative_integer(cleaned["mileage"], "mileage")
    cleaned["asking_price"] = _parse_price(cleaned["asking_price"])
    cleaned["observed_at"] = _parse_observed_at(cleaned["observed_at"])
    return cleaned


def import_listings(csv_path: str | Path, source: str, is_full_snapshot: bool = False) -> ImportResult:
    """Import a CSV file and return its persisted run plus row-level errors.

    A run containing validation errors is marked failed. Valid rows from that
    file are still retained, but removal detection is never performed for it.
    A repeated observation for the same listing and ``observed_at`` is
    idempotent across import runs and does not add another snapshot.
    """
    source = source.strip()
    if not source:
        raise ValueError("source must not be empty")

    raw_rows: list[dict[str, str | None]] = []
    errors: list[str] = []
    snapshot_at = timezone.now()

    try:
        with Path(csv_path).open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            missing_columns = [column for column in CSV_COLUMNS if column not in (reader.fieldnames or [])]
            if missing_columns:
                errors.append(f"CSV header is missing column(s): {', '.join(missing_columns)}")
            else:
                raw_rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        errors.append(f"Unable to read CSV: {exc}")

    validated_rows: list[tuple[int, dict]] = []
    seen_keys: set[tuple[str, str]] = set()
    for row_number, row in enumerate(raw_rows, start=2):
        try:
            cleaned = _validate_row(row, source)
            key = (cleaned["source"], cleaned["external_id"])
            if key in seen_keys:
                raise RowValidationError("duplicate source/external_id in this CSV")
            seen_keys.add(key)
            validated_rows.append((row_number, cleaned))
        except RowValidationError as exc:
            errors.append(f"Row {row_number}: {exc}")

    if validated_rows:
        snapshot_at = max(row["observed_at"] for _, row in validated_rows)

    import_run = ImportRun.objects.create(
        source=source,
        snapshot_at=snapshot_at,
        is_full_snapshot=is_full_snapshot,
    )
    created_count = 0
    updated_count = 0
    removed_count = 0
    snapshots_created = 0
    observed_listing_ids: set[int] = set()

    try:
        with transaction.atomic():
            for row_number, row in validated_rows:
                listing_defaults = {
                    "source_url": row["source_url"],
                    "title": row["title"],
                    "make": row["make"],
                    "model": row["model"],
                    "year": row["year"],
                    "mileage": row["mileage"],
                    "fuel": row["fuel"],
                    "transmission": row["transmission"],
                    "first_seen_at": row["observed_at"],
                    "last_seen_at": row["observed_at"],
                    "status": Listing.Status.ACTIVE,
                }
                listing, created = Listing.objects.get_or_create(
                    source=source,
                    external_id=row["external_id"],
                    defaults=listing_defaults,
                )
                if created:
                    created_count += 1
                else:
                    changed = False
                    for field, value in listing_defaults.items():
                        if field == "first_seen_at":
                            continue
                        if getattr(listing, field) != value:
                            setattr(listing, field, value)
                            changed = True
                    if changed:
                        listing.save()
                        updated_count += 1

                observed_listing_ids.add(listing.pk)
                snapshot_exists = ListingSnapshot.objects.filter(
                    listing=listing,
                    observed_at=row["observed_at"],
                ).exists()
                if not snapshot_exists:
                    ListingSnapshot.objects.create(
                        listing=listing,
                        import_run=import_run,
                        observed_at=row["observed_at"],
                        asking_price=row["asking_price"],
                        mileage=row["mileage"],
                        raw_data={key: value for key, value in row.items() if key not in {"asking_price", "observed_at"}},
                    )
                    snapshots_created += 1

            if is_full_snapshot and not errors:
                missing = Listing.objects.filter(source=source).exclude(pk__in=observed_listing_ids)
                removed_count = missing.exclude(status=Listing.Status.REMOVED).update(
                    status=Listing.Status.REMOVED
                )
    except Exception as exc:
        errors.append(f"Import failed: {exc}")
        created_count = 0
        updated_count = 0
        removed_count = 0
        snapshots_created = 0

    import_run.status = ImportRun.Status.FAILED if errors else ImportRun.Status.COMPLETED
    import_run.completed_at = timezone.now()
    import_run.processed_count = len(raw_rows)
    import_run.created_count = created_count
    import_run.updated_count = updated_count
    import_run.removed_count = removed_count
    import_run.error_count = len(errors)
    import_run.error_summary = "\n".join(errors)
    import_run.save()

    return ImportResult(import_run, snapshots_created, tuple(errors))
