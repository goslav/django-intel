from datetime import datetime, timezone
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from vehicles.models import ImportRun, Listing, ListingSnapshot, MarketProfile
from vehicles.services.vehicle_classification import classify_listing


DEMO_SOURCE = "demo-polovniautomobili"


class Command(BaseCommand):
    help = "Create deterministic market-profile demo listings and snapshots."

    @transaction.atomic
    def handle(self, *args, **options):
        observations = (
            datetime(2026, 7, 1, 10, tzinfo=timezone.utc),
            datetime(2026, 7, 15, 10, tzinfo=timezone.utc),
        )
        runs = []
        for observed_at in observations:
            run = ImportRun.objects.filter(
                source=DEMO_SOURCE, snapshot_at=observed_at
            ).order_by("pk").first()
            if run is None:
                run = ImportRun.objects.create(
                    source=DEMO_SOURCE,
                    snapshot_at=observed_at,
                    completed_at=observed_at,
                    status=ImportRun.Status.COMPLETED,
                )
            runs.append(run)

        rows = (
            ("demo-3008-1", "Peugeot", "3008", 2020, 145000, "diesel", "automatic", "20900", "19900", "active"),
            ("demo-3008-2", "Peugeot", "3008", 2021, 98000, "diesel", "automatic", "23900", "22900", "active"),
            ("demo-3008-3", "Peugeot", "3008", 2023, 61000, "diesel", "automatic", "28500", "27900", "active"),
            ("demo-3008-petrol", "Peugeot", "3008", 2022, 72000, "petrol", "automatic", "25200", "24900", "active"),
            ("demo-3008-removed", "Peugeot", "3008", 2020, 158000, "diesel", "manual", "18500", "17900", "removed"),
            ("demo-2008-1", "Peugeot", "2008", 2021, 87000, "diesel", "manual", "17800", "16900", "active"),
            ("demo-2008-2", "Peugeot", "2008", 2023, 42000, "petrol", "automatic", "22400", "21900", "active"),
            ("demo-c5-1", "Citroen", "C5 Aircross", 2020, 125000, "diesel", "automatic", "21500", "20500", "active"),
            ("demo-c5-2", "Citroen", "C5 Aircross", 2022, 68000, "diesel", "automatic", "26900", "25900", "active"),
        )
        for external_id, make, model, year, mileage, fuel, transmission, old_price, price, status in rows:
            listing, _ = Listing.objects.update_or_create(
                source=DEMO_SOURCE,
                external_id=external_id,
                defaults={
                    "source_url": f"https://example.test/demo/{external_id}",
                    "title": f"DEMO {make} {model} {year}",
                    "make": make,
                    "model": model,
                    "year": year,
                    "mileage": mileage + 2000,
                    "fuel": fuel,
                    "transmission": transmission,
                    "first_seen_at": observations[0],
                    "last_seen_at": observations[1],
                    "status": status,
                },
            )
            classify_listing(listing)
            for index, asking_price in enumerate((old_price, price)):
                ListingSnapshot.objects.update_or_create(
                    listing=listing,
                    observed_at=observations[index],
                    defaults={
                        "import_run": runs[index],
                        "asking_price": Decimal(asking_price),
                        "mileage": mileage + (1000 if index == 0 else 0),
                        "raw_data": {"demo": True},
                    },
                )

        profiles = (
            ("DEMO Peugeot 3008 diesel automatic", "Peugeot", "3008", 2020, 2023, 60000, 160000, "diesel", "automatic"),
            ("DEMO Peugeot 2008", "Peugeot", "2008", 2020, 2024, None, 120000, "", ""),
            ("DEMO Citroen C5 Aircross", "Citroen", "C5 Aircross", 2020, 2023, 50000, 150000, "diesel", "automatic"),
        )
        for name, make, model, min_year, max_year, min_mileage, max_mileage, fuel, transmission in profiles:
            MarketProfile.objects.update_or_create(
                name=name,
                source=DEMO_SOURCE,
                defaults={
                    "make": make,
                    "model": model,
                    "minimum_year": min_year,
                    "maximum_year": max_year,
                    "minimum_mileage": min_mileage,
                    "maximum_mileage": max_mileage,
                    "fuel": fuel,
                    "transmission": transmission,
                    "notes": "Created by create_market_demo_data.",
                    "is_active": True,
                },
            )
        self.stdout.write(self.style.SUCCESS("Market demo data is ready."))
