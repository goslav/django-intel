from django.core.management.base import BaseCommand
from django.db import transaction

from vehicles.models import Listing, MarketProfile


class Command(BaseCommand):
    help = "Create the dealership's initial Peugeot priority market catalogue."

    @transaction.atomic
    def handle(self, *args, **options):
        profiles = []
        for facelift, facelift_label in (
            (Listing.FaceliftStatus.PRE_FACELIFT, "pre-facelift"),
            (Listing.FaceliftStatus.FACELIFT, "facelift"),
        ):
            for engine in ("1.5 BlueHDi", "1.2 PureTech"):
                for transmission in ("automatic", "manual"):
                    profiles.append((
                        f"Peugeot 3008 II {facelift_label} - {engine} - {transmission}",
                        "3008", "II", facelift, engine, transmission,
                    ))
        for engine in ("1.5 BlueHDi", "1.2 PureTech"):
            for transmission in ("manual", "automatic"):
                profiles.append((
                    f"Peugeot 2008 II - {engine} - {transmission}",
                    "2008", "II", Listing.FaceliftStatus.NOT_APPLICABLE,
                    engine, transmission,
                ))

        for name, model, generation, facelift, engine, transmission in profiles:
            MarketProfile.objects.update_or_create(
                name=name,
                source="polovniautomobili",
                defaults={
                    "make": "Peugeot", "model": model, "generation": generation,
                    "facelift_status": facelift, "engine_family": engine,
                    "transmission": transmission, "fuel": "",
                    "minimum_year": None, "maximum_year": None,
                    "minimum_mileage": None, "maximum_mileage": None,
                    "minimum_power_kw": None, "maximum_power_kw": None,
                    "notes": "Priority purchasing segment.", "is_active": True,
                },
            )
        self.stdout.write(self.style.SUCCESS(f"{len(profiles)} priority market profiles are ready."))
