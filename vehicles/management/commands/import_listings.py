from django.core.management.base import BaseCommand, CommandError

from vehicles.services.import_listings import import_listings


class Command(BaseCommand):
    help = "Import marketplace listings from a UTF-8 CSV file."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--source", required=True)
        parser.add_argument("--full-snapshot", action="store_true")

    def handle(self, *args, **options):
        try:
            result = import_listings(
                options["csv_path"],
                source=options["source"],
                is_full_snapshot=options["full_snapshot"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        run = result.import_run
        self.stdout.write(
            " | ".join(
                (
                    f"Processed: {run.processed_count}",
                    f"Created: {run.created_count}",
                    f"Updated: {run.updated_count}",
                    f"Snapshots: {result.snapshots_created}",
                    f"Removed: {run.removed_count}",
                    f"Errors: {run.error_count}",
                )
            )
        )
        for error in result.errors:
            self.stderr.write(error)
        if run.error_count:
            raise CommandError(f"Import run {run.pk} failed with {run.error_count} error(s).")
