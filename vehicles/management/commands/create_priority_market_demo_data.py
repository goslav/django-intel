from datetime import datetime, timezone
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from vehicles.models import ImportRun, Listing, ListingSnapshot
from vehicles.services.vehicle_classification import classify_listing


DEMO_SOURCE = "demo-priority-market"


class Command(BaseCommand):
    help = "Create deterministic classified Peugeot priority-market demo data."

    @transaction.atomic
    def handle(self, *args, **options):
        observed = (
            datetime(2026, 6, 1, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 10, tzinfo=timezone.utc),
        )
        runs = []
        for timestamp in observed:
            run = ImportRun.objects.filter(source=DEMO_SOURCE, snapshot_at=timestamp).first()
            runs.append(run or ImportRun.objects.create(
                source=DEMO_SOURCE, snapshot_at=timestamp, completed_at=timestamp,
                status=ImportRun.Status.COMPLETED,
            ))
        rows = (
            ("demo-priority-3008-pre-diesel-auto", "3008", "Peugeot 3008 II pre-facelift 1.5 BlueHDi EAT8", 2019, 120000, "diesel", "EAT8", "22500", "21500", "active"),
            ("demo-priority-3008-face-petrol-auto", "3008", "Peugeot 3008 II facelift 1.2 PureTech BVA8", 2021, 76000, "petrol", "BVA8", "26800", "25900", "active"),
            ("demo-priority-3008-face-diesel-manual", "3008", "Peugeot 3008 II facelift BlueHDi 130 BVM6", 2022, 68000, "diesel", "BVM6", "27900", "26900", "active"),
            ("demo-priority-3008-unknown", "3008", "Peugeot 3008 1.5 HDi 130 automatic", 2021, 101000, "diesel", "automatic", "23000", "22000", "active"),
            ("demo-priority-2008-first", "2008", "Peugeot 2008 I 1.2 PureTech manual", 2018, 94000, "petrol", "manual", "14500", "13900", "active"),
            ("demo-priority-2008-second-diesel", "2008", "Peugeot 2008 II 1.5 BlueHDi EAT8", 2021, 72000, "diesel", "EAT8", "22500", "21500", "active"),
            ("demo-priority-2008-second-petrol", "2008", "Peugeot 2008 II PureTech 130 BVM6", 2022, 54000, "petrol", "BVM6", "21900", "20900", "active"),
            ("demo-priority-2008-unknown", "2008", "Peugeot 2008 1.5 BlueHDi", 2020, 110000, "diesel", "", "18000", "17500", "active"),
            ("demo-priority-removed", "3008", "Peugeot 3008 II pre-facelift 1.2 PureTech manual", 2019, 150000, "petrol", "manual", "17500", "16900", "removed"),
        )
        for external_id, model, title, year, mileage, fuel, transmission, old_price, price, status in rows:
            listing, _ = Listing.objects.update_or_create(
                source=DEMO_SOURCE, external_id=external_id,
                defaults={
                    "source_url": f"https://example.test/{external_id}", "title": title,
                    "make": "Peugeot", "model": model, "year": year,
                    "mileage": mileage, "fuel": fuel, "transmission": transmission,
                    "first_seen_at": observed[0], "last_seen_at": observed[1], "status": status,
                },
            )
            classify_listing(listing)
            for index, value in enumerate((old_price, price)):
                ListingSnapshot.objects.update_or_create(
                    listing=listing, observed_at=observed[index],
                    defaults={"import_run": runs[index], "asking_price": Decimal(value), "mileage": mileage - index * 1000, "raw_data": {"demo": True}},
                )
        self.stdout.write(self.style.SUCCESS("Priority market demo data is ready."))
