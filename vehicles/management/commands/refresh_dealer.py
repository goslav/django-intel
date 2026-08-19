from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from vehicles.models import Dealer
from vehicles.services.dealer_intelligence import DealerAPIError, refresh_dealer


class Command(BaseCommand):
    help = "Fetch a dealer's complete API inventory and create a daily snapshot."

    def add_arguments(self, parser):
        parser.add_argument("dealer_id", type=int)
        parser.add_argument("--observed-at", help="Optional ISO-8601 snapshot time")

    def handle(self, *args, **options):
        try:
            dealer = Dealer.objects.get(pk=options["dealer_id"])
            observed_at = parse_datetime(options["observed_at"]) if options["observed_at"] else None
            if options["observed_at"] and observed_at is None:
                raise CommandError("--observed-at must be an ISO-8601 datetime.")
            snapshot = refresh_dealer(dealer, observed_at=observed_at)
        except Dealer.DoesNotExist as exc:
            raise CommandError("Dealer does not exist.") from exc
        except DealerAPIError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(
            f"Snapshot complete: {snapshot.inventory_count} active, "
            f"{snapshot.disappeared_count} disappeared, median {snapshot.median_price or '-'}"
        ))
