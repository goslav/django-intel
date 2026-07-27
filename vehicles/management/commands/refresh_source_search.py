from django.core.management.base import BaseCommand, CommandError

from vehicles.models import SourceSearch
from vehicles.services.source_search_refresh import refresh_source_search
from vehicles.sources.polovni_automobili.search_results import CollectionError


class Command(BaseCommand):
    help = "Refresh one configured Polovni Automobili source search."

    def add_arguments(self, parser):
        parser.add_argument("source_search_id", type=int)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--max-pages", type=int)
        parser.add_argument("--request-delay", type=float)

    def handle(self, *args, **options):
        try:
            source_search = SourceSearch.objects.get(pk=options["source_search_id"])
            result = refresh_source_search(
                source_search, maximum_pages=options["max_pages"],
                delay_seconds=options["request_delay"], dry_run=options["dry_run"],
            )
        except SourceSearch.DoesNotExist as exc:
            raise CommandError("Source search does not exist.") from exc
        except CollectionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Pages processed: {result.discovery.pages_completed}")
        self.stdout.write(f"IDs discovered: {len(result.discovery.advertisements)}")
        if options["dry_run"]:
            for advertisement in result.discovery.advertisements:
                self.stdout.write(f"{advertisement.external_id} {advertisement.public_url}")
            self.stdout.write("Dry run complete; no database changes were made.")
            return
        run = result.run
        self.stdout.write(f"Detail requests completed: {run.detail_records_completed}")
        self.stdout.write(f"Listings created/updated: {run.listings_created}/{run.listings_updated}")
        self.stdout.write(f"Snapshots created: {run.snapshots_created}")
        self.stdout.write(f"Unknown classifications: {result.unknown_classifications}")
        self.stdout.write(f"Missing advertisements: {run.missing_advertisements_detected}")
        self.stdout.write(f"Errors: {run.error_count}")
        self.stdout.write(f"Final status: {run.status}")
