import httpx

from django.core.management.base import BaseCommand, CommandError

from vehicles.models import Dealer
from vehicles.sources.polovni_automobili.detail_record import description_flags, fetch_detail
from vehicles.sources.polovni_automobili.search_results import CollectionError, USER_AGENT


class Command(BaseCommand):
    help = "Refresh transient Opis classification flags for a dealer's current ads."

    def add_arguments(self, parser):
        parser.add_argument("dealer_id", type=int)

    def handle(self, *args, **options):
        try:
            dealer = Dealer.objects.get(pk=options["dealer_id"])
        except Dealer.DoesNotExist as exc:
            raise CommandError("Dealer does not exist.") from exc

        memberships = dealer.listing_memberships.filter(
            is_currently_present=True,
        ).select_related("listing").order_by("pk")
        scanned = flagged = 0
        failures = []
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(20, connect=10),
            transport=httpx.HTTPTransport(retries=2),
        ) as client:
            for membership in memberships:
                try:
                    detail = fetch_detail(membership.listing.external_id, client=client)
                except (CollectionError, httpx.HTTPError) as exc:
                    failures.append(f"{membership.listing.external_id}: {exc}")
                    continue
                flags = description_flags(detail.description)
                membership.description_flags = flags
                membership.save(update_fields=("description_flags",))
                scanned += 1
                flagged += bool(flags)

        self.stdout.write(self.style.SUCCESS(
            f"Opis scan complete: {scanned} scanned, {flagged} flagged, {len(failures)} failed."
        ))
        for failure in failures:
            self.stderr.write(self.style.WARNING(failure))
