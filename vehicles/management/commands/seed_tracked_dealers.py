from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from vehicles.models import Dealer


TRACKED_DEALERS = (
    ("KIA CENTAR BEOGRAD", "Service-Maxx", "https://www.polovniautomobili.com/Service-Maxx", Dealer.InventoryType.MIXED),
    ("AK Kompresor", "akkompresor", "https://www.polovniautomobili.com/akkompresor", Dealer.InventoryType.NEW),
    ("Auto Nena Still Peugeot", "auto-nena-still-peugeot", "https://www.polovniautomobili.com/auto-nena-still-peugeot", Dealer.InventoryType.MIXED),
    ("Autoland", "autoland", "https://www.polovniautomobili.com/autoland", Dealer.InventoryType.USED),
    ("Autoto Srbija / Grand Motors", "autoto-srbija-grand-motors-doo", "https://www.polovniautomobili.com/autoto-srbija-grand-motors-doo", Dealer.InventoryType.USED),
    ("Delta Polovni Automobili", "delta-polovni-automobili", "https://www.polovniautomobili.com/delta-polovni-automobili", Dealer.InventoryType.USED),
    ("Ehom Auto", "ehom-auto", "https://www.polovniautomobili.com/ehom-auto", Dealer.InventoryType.USED),
    ("Emil Frey Auto Centar", "emil-frey-auto-centar", "https://www.polovniautomobili.com/emil-frey-auto-centar", Dealer.InventoryType.USED),
    ("French Concept", "french-concept", "https://www.polovniautomobili.com/french-concept", Dealer.InventoryType.USED),
    ("Stojanov", "stojanov", "https://www.polovniautomobili.com/stojanov", Dealer.InventoryType.USED),
    ("Porsche Inter Auto", "porsche-inter-auto-s", "https://www.polovniautomobili.com/porsche-inter-auto-s", Dealer.InventoryType.USED),
)


class Command(BaseCommand):
    help = "Create or update the configured Polovni Automobili tracked dealers."

    @transaction.atomic
    def handle(self, *args, **options):
        created_count = 0
        for name, external_id, api_url, inventory_type in TRACKED_DEALERS:
            dealer = Dealer.objects.filter(source="polovniautomobili").filter(
                Q(external_id=external_id) | Q(api_url=api_url)
            ).order_by("pk").first()
            if dealer is None:
                dealer = Dealer(source="polovniautomobili", external_id=external_id)
                created_count += 1

            dealer.name = name
            dealer.external_id = external_id
            dealer.api_url = api_url
            dealer.inventory_type = inventory_type
            dealer.is_active = True
            dealer.save()

        updated_count = len(TRACKED_DEALERS) - created_count
        self.stdout.write(self.style.SUCCESS(
            f"{len(TRACKED_DEALERS)} tracked dealers are ready "
            f"({created_count} created, {updated_count} updated)."
        ))
