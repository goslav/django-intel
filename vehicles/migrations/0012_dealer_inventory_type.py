from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vehicles", "0011_dealersnapshotlisting_observed_at_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="dealer",
            name="inventory_type",
            field=models.CharField(
                choices=[("used", "Used cars"), ("new", "New cars only")],
                db_index=True,
                default="used",
                max_length=10,
            ),
        ),
    ]
