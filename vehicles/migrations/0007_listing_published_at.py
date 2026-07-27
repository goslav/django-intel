from datetime import date

from django.db import migrations, models


def backfill_published_at(apps, schema_editor):
    Listing = apps.get_model("vehicles", "Listing")
    ListingSnapshot = apps.get_model("vehicles", "ListingSnapshot")
    snapshots = ListingSnapshot.objects.order_by("listing_id", "-observed_at", "-pk")
    seen = set()
    for snapshot in snapshots.iterator():
        if snapshot.listing_id in seen:
            continue
        seen.add(snapshot.listing_id)
        raw_value = (snapshot.raw_data or {}).get("publish_date")
        if not raw_value:
            continue
        try:
            published_at = date.fromisoformat(str(raw_value)[:10])
        except ValueError:
            continue
        Listing.objects.filter(pk=snapshot.listing_id).update(published_at=published_at)


class Migration(migrations.Migration):
    dependencies = [("vehicles", "0006_sourcesearchrunlisting_external_id_and_more")]

    operations = [
        migrations.AddField(
            model_name="listing",
            name="published_at",
            field=models.DateField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(backfill_published_at, migrations.RunPython.noop),
    ]
