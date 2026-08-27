import httpx
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from vehicles.models import DealerListingMembership, Listing
from vehicles.sources.polovni_automobili.detail_record import fetch_detail
from vehicles.sources.polovni_automobili.search_results import USER_AGENT


class Command(BaseCommand):
    help = "Fetch current Polovni dealer ads and backfill explicitly supplied VINs."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument("--retry-errors", action="store_true")

    def handle(self, *args, **options):
        listing_ids = DealerListingMembership.objects.filter(
            is_currently_present=True,
            listing__source="polovniautomobili",
        ).values_list("listing_id", flat=True).distinct()
        listings = Listing.objects.filter(pk__in=listing_ids, vin_checked_at__isnull=True)
        if options["retry_errors"]:
            listings = listings.exclude(vin_lookup_error="")
        else:
            listings = listings.filter(vin_lookup_error="")
        listings = listings.order_by("pk")
        if options["limit"]:
            listings = listings[:options["limit"]]
        checked = found = repeated = failed = 0
        client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30, connect=10),
            follow_redirects=True,
            transport=httpx.HTTPTransport(retries=2),
        )
        try:
            for listing in listings.iterator():
                checked += 1
                try:
                    detail = fetch_detail(listing.external_id, client=client)
                except Exception as exc:
                    failed += 1
                    listing.vin_lookup_error = str(exc)[:255]
                    listing.save(update_fields=("vin_lookup_error", "updated_at"))
                    self.stderr.write(f"Failed {listing.external_id}: {exc}")
                    continue
                with transaction.atomic():
                    listing.vin = detail.vin
                    listing.vin_checked_at = timezone.now()
                    listing.vin_lookup_error = ""
                    fields = ["vin", "vin_checked_at", "vin_lookup_error", "updated_at"]
                    if detail.vin:
                        found += 1
                        original = Listing.objects.filter(vin=detail.vin).exclude(pk=listing.pk).order_by("first_seen_at", "pk").first()
                        if original:
                            listing.repeated_listing_of = original.repeated_listing_of or original
                            listing.repeat_detection_method = Listing.RepeatDetectionMethod.VIN
                            listing.repeat_detected_at = listing.last_seen_at
                            fields.extend(("repeated_listing_of", "repeat_detection_method", "repeat_detected_at"))
                            repeated += 1
                    listing.save(update_fields=fields)
                if detail.vin:
                    self.stdout.write(f"VIN {listing.external_id}: {detail.vin}")
        finally:
            client.close()
        self.stdout.write(self.style.SUCCESS(
            f"VIN backfill finished: {checked} checked, {found} found, "
            f"{repeated} repeated ads flagged, {failed} failed."
        ))
