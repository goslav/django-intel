from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("vehicles", "0007_listing_published_at")]

    operations = [
        migrations.AddField(
            model_name="listing",
            name="thumbnail_url",
            field=models.URLField(blank=True),
        ),
    ]
