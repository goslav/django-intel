import re
from urllib.parse import urlparse

from django.core.management.base import BaseCommand

from vehicles.models import Listing
from vehicles.sources.polovni_automobili.search_results import (
    canonicalize_public_ad_url, public_ad_url_fallback,
)


class Command(BaseCommand):
    help = "Repair malformed Polovni Automobili public listing URLs without network access."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        inspected = valid = repaired = unable = 0
        for listing in Listing.objects.filter(source="polovniautomobili").iterator():
            inspected += 1
            try:
                listing_id, canonical = canonicalize_public_ad_url(
                    listing.source_url, expected_listing_id=listing.external_id
                )
                if listing.source_url == canonical:
                    valid += 1
                    continue
                repaired_url = canonical
            except ValueError:
                path = urlparse(listing.source_url or "").path
                match = re.search(r"/auto-oglasi/(\d+)(?:/|$)", path)
                candidate_id = match.group(1) if match else str(listing.external_id)
                try:
                    repaired_url = public_ad_url_fallback(candidate_id)
                except ValueError:
                    unable += 1
                    continue
            repaired += 1
            if not options["dry_run"]:
                listing.source_url = repaired_url
                listing.save(update_fields=("source_url", "updated_at"))
        self.stdout.write(f"Records inspected: {inspected}")
        self.stdout.write(f"Already valid: {valid}")
        self.stdout.write(f"Repaired: {repaired}")
        self.stdout.write(f"Unable to repair: {unable}")
        if options["dry_run"]:
            self.stdout.write("Dry run complete; no records were changed.")
