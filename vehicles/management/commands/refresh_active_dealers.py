from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from vehicles.models import Dealer, DealerInventorySnapshot
from vehicles.services.dealer_intelligence import refresh_dealer


class Command(BaseCommand):
    help = "Refresh every active tracked dealer, at most once per local calendar day."

    def handle(self, *args, **options):
        observed_at = timezone.now()
        today = timezone.localdate(observed_at)
        refreshed = skipped = 0
        failures = []

        for dealer in Dealer.objects.filter(is_active=True).order_by("pk"):
            already_completed = dealer.inventory_snapshots.filter(
                status=DealerInventorySnapshot.Status.COMPLETED,
                observed_at__date=today,
            ).exists()
            if already_completed:
                skipped += 1
                self.stdout.write(f"Skipping {dealer.name} ({dealer.pk}): already refreshed today.")
                continue

            try:
                snapshot = refresh_dealer(dealer, observed_at=observed_at)
            except Exception as exc:  # Keep independent dealer refreshes isolated.
                failures.append((dealer, str(exc)))
                self.stderr.write(self.style.ERROR(f"Failed {dealer.name} ({dealer.pk}): {exc}"))
                continue

            refreshed += 1
            self.stdout.write(self.style.SUCCESS(
                f"Refreshed {dealer.name} ({dealer.pk}): snapshot {snapshot.pk}, "
                f"{snapshot.inventory_count} active."
            ))

        summary = f"Dealer refresh finished: {refreshed} refreshed, {skipped} skipped, {len(failures)} failed."
        if failures:
            raise CommandError(summary)
        self.stdout.write(self.style.SUCCESS(summary))
