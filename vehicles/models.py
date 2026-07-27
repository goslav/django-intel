from django.db import models


class Vehicle(models.Model):
    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    year = models.PositiveIntegerField()
    mileage = models.PositiveIntegerField()
    fuel_type = models.CharField(max_length=50)
    transmission = models.CharField(max_length=50, blank=True)
    purchase_price = models.DecimalField(max_digits=10, decimal_places=2)
    expected_sale_price = models.DecimalField(max_digits=10, decimal_places=2)
    source_url = models.URLField(blank=True)
    notes = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def estimated_profit(self):
        return self.expected_sale_price - self.purchase_price

    def __str__(self):
        return f"{self.make} {self.model} {self.year}"


class Listing(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        REMOVED = "removed", "Removed"
        UNKNOWN = "unknown", "Unknown"

    source = models.CharField(max_length=50, db_index=True)
    external_id = models.CharField(max_length=255)
    source_url = models.URLField(blank=True)
    title = models.CharField(max_length=500, blank=True)
    make = models.CharField(max_length=100, db_index=True)
    model = models.CharField(max_length=100, db_index=True)
    year = models.PositiveIntegerField(db_index=True)
    mileage = models.PositiveIntegerField()
    fuel = models.CharField(max_length=50)
    transmission = models.CharField(max_length=50, blank=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(db_index=True)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.UNKNOWN,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("source", "external_id"),
                name="unique_listing_source_external_id",
            ),
        ]
        indexes = [
            models.Index(fields=("source", "status"), name="listing_source_status_idx"),
            models.Index(fields=("make", "model", "year"), name="listing_vehicle_idx"),
            models.Index(fields=("source", "last_seen_at"), name="listing_source_seen_idx"),
        ]

    def __str__(self):
        return f"{self.make} {self.model} ({self.source}:{self.external_id})"


class ImportRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    source = models.CharField(max_length=50, db_index=True)
    snapshot_at = models.DateTimeField(db_index=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_full_snapshot = models.BooleanField(default=False)
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.RUNNING,
        db_index=True,
    )
    processed_count = models.PositiveIntegerField(default=0)
    created_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    removed_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True)

    class Meta:
        ordering = ("-started_at",)

    def __str__(self):
        return f"{self.source} import at {self.snapshot_at}"


class ListingSnapshot(models.Model):
    listing = models.ForeignKey(
        Listing,
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    import_run = models.ForeignKey(
        ImportRun,
        on_delete=models.CASCADE,
        related_name="snapshots",
    )
    observed_at = models.DateTimeField(db_index=True)
    asking_price = models.DecimalField(max_digits=12, decimal_places=2)
    mileage = models.PositiveIntegerField()
    raw_data = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ("-observed_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("listing", "import_run"),
                name="unique_listing_snapshot_per_import",
            ),
            models.UniqueConstraint(
                fields=("listing", "observed_at"),
                name="unique_listing_observed_at",
            ),
        ]
        indexes = [
            models.Index(fields=("listing", "observed_at"), name="snapshot_listing_seen_idx"),
        ]

    def __str__(self):
        return f"{self.listing} at {self.observed_at}"
