from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("vehicles", "0012_dealer_inventory_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="dealer",
            name="inventory_type",
            field=models.CharField(
                choices=[
                    ("used", "Used cars"),
                    ("mixed", "Mixed new and used cars"),
                    ("new", "New cars only"),
                ],
                db_index=True,
                default="used",
                max_length=10,
            ),
        ),
    ]
