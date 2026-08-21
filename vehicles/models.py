from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from urllib.parse import urlparse


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

    class FaceliftStatus(models.TextChoices):
        PRE_FACELIFT = "pre_facelift", "Pre-facelift"
        FACELIFT = "facelift", "Facelift"
        NOT_APPLICABLE = "not_applicable", "Not applicable"
        UNKNOWN = "unknown", "Unknown"

    class ClassificationMethod(models.TextChoices):
        SOURCE_DATA = "source_data", "Source data"
        RULE_BASED = "rule_based", "Rule based"
        MANUAL = "manual", "Manual"
        UNKNOWN = "unknown", "Unknown"

    class TransmissionCategory(models.TextChoices):
        AUTOMATIC = "automatic", "Automatic"
        MANUAL = "manual", "Manual"
        UNKNOWN = "unknown", "Unknown"

    source = models.CharField(max_length=50, db_index=True)
    external_id = models.CharField(max_length=255)
    source_url = models.URLField(blank=True)
    thumbnail_url = models.URLField(blank=True)
    title = models.CharField(max_length=500, blank=True)
    make = models.CharField(max_length=100, db_index=True)
    model = models.CharField(max_length=100, db_index=True)
    year = models.PositiveIntegerField(db_index=True)
    mileage = models.PositiveIntegerField()
    fuel = models.CharField(max_length=50)
    transmission = models.CharField(max_length=50, blank=True)
    generation = models.CharField(max_length=100, blank=True)
    facelift_status = models.CharField(max_length=20, choices=FaceliftStatus.choices, default=FaceliftStatus.UNKNOWN)
    engine_family = models.CharField(max_length=100, blank=True)
    power_kw = models.PositiveIntegerField(null=True, blank=True)
    transmission_category = models.CharField(max_length=20, choices=TransmissionCategory.choices, default=TransmissionCategory.UNKNOWN)
    classification_method = models.CharField(max_length=20, choices=ClassificationMethod.choices, default=ClassificationMethod.UNKNOWN)
    classification_confidence = models.DecimalField(max_digits=3, decimal_places=2, default=0, validators=(MinValueValidator(0), MaxValueValidator(1)))
    classification_notes = models.TextField(blank=True)
    classification_manually_overridden = models.BooleanField(default=False, db_index=True)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField(db_index=True)
    published_at = models.DateField(null=True, blank=True, db_index=True)
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

    @property
    def days_on_market(self):
        if not self.published_at:
            return None
        return max((timezone.localdate() - self.published_at).days, 0)


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


class MarketProfile(models.Model):
    name = models.CharField(max_length=150)
    source = models.CharField(max_length=50, default="polovniautomobili", db_index=True)
    make = models.CharField(max_length=100)
    model = models.CharField(max_length=100)
    generation = models.CharField(max_length=100, blank=True)
    facelift_status = models.CharField(max_length=20, choices=Listing.FaceliftStatus.choices, blank=True)
    engine_family = models.CharField(max_length=100, blank=True)
    minimum_year = models.PositiveIntegerField(null=True, blank=True)
    maximum_year = models.PositiveIntegerField(null=True, blank=True)
    minimum_mileage = models.PositiveIntegerField(null=True, blank=True)
    maximum_mileage = models.PositiveIntegerField(null=True, blank=True)
    fuel = models.CharField(max_length=50, blank=True)
    transmission = models.CharField(max_length=50, blank=True)
    minimum_power_kw = models.PositiveIntegerField(null=True, blank=True)
    maximum_power_kw = models.PositiveIntegerField(null=True, blank=True)
    source_search_url = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "pk")

    def clean(self):
        errors = {}
        for field in ("name", "source", "make", "model"):
            if not (getattr(self, field, "") or "").strip():
                errors[field] = "This field cannot contain only whitespace."

        maximum_plausible_year = date.today().year + 2
        for field in ("minimum_year", "maximum_year"):
            value = getattr(self, field, None)
            if value is not None and not 1886 <= value <= maximum_plausible_year:
                errors[field] = f"Enter a year between 1886 and {maximum_plausible_year}."
        if (
            self.minimum_year is not None
            and self.maximum_year is not None
            and self.minimum_year > self.maximum_year
        ):
            errors["maximum_year"] = "Maximum year must be greater than or equal to minimum year."

        for field in ("minimum_mileage", "maximum_mileage"):
            value = getattr(self, field, None)
            if value is not None and not 0 <= value <= 5_000_000:
                errors[field] = "Enter mileage between 0 and 5,000,000 km."
        if (
            self.minimum_mileage is not None
            and self.maximum_mileage is not None
            and self.minimum_mileage > self.maximum_mileage
        ):
            errors["maximum_mileage"] = (
                "Maximum mileage must be greater than or equal to minimum mileage."
            )
        for field in ("minimum_power_kw", "maximum_power_kw"):
            value = getattr(self, field, None)
            if value is not None and not 0 <= value <= 2_000:
                errors[field] = "Enter power between 0 and 2,000 kW."
        if (
            self.minimum_power_kw is not None
            and self.maximum_power_kw is not None
            and self.minimum_power_kw > self.maximum_power_kw
        ):
            errors["maximum_power_kw"] = "Maximum power must be greater than or equal to minimum power."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


def validate_polovni_search_url(value):
    parsed = urlparse(value)
    if parsed.scheme != "https":
        raise ValidationError("The search URL must use HTTPS.")
    if parsed.hostname not in {"polovniautomobili.com", "www.polovniautomobili.com"}:
        raise ValidationError("Use a polovniautomobili.com search URL.")
    if __import__("re").search(r"/auto-oglasi/\d+(?:/|$)", parsed.path):
        raise ValidationError("Individual advertisement URLs are not valid searches.")
    if not (parsed.path.startswith("/automobili") or parsed.path.startswith("/auto-oglasi/pretraga")):
        raise ValidationError("The URL must be a passenger-car search page.")


class SourceSearch(models.Model):
    class RefreshStatus(models.TextChoices):
        NEVER = "never", "Never run"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=150)
    source = models.CharField(max_length=50, default="polovniautomobili")
    search_url = models.URLField(max_length=2000, validators=(validate_polovni_search_url,))
    is_active = models.BooleanField(default=True)
    maximum_pages = models.PositiveSmallIntegerField(default=2, validators=(MinValueValidator(1), MaxValueValidator(25)))
    request_delay_seconds = models.DecimalField(max_digits=5, decimal_places=2, default=2, validators=(MinValueValidator(0), MaxValueValidator(60)))
    last_refresh_started_at = models.DateTimeField(null=True, blank=True)
    last_refresh_completed_at = models.DateTimeField(null=True, blank=True)
    last_refresh_status = models.CharField(max_length=10, choices=RefreshStatus.choices, default=RefreshStatus.NEVER)
    last_error_summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "pk")

    def clean(self):
        super().clean()
        if self.source != "polovniautomobili":
            raise ValidationError({"source": "Only polovniautomobili is supported by this proof of concept."})

    def __str__(self):
        return self.name


class SourceSearchRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    source_search = models.ForeignKey(SourceSearch, on_delete=models.CASCADE, related_name="runs")
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    result_pages_requested = models.PositiveIntegerField(default=0)
    result_pages_completed = models.PositiveIntegerField(default=0)
    advertisement_ids_discovered = models.PositiveIntegerField(default=0)
    detail_records_completed = models.PositiveIntegerField(default=0)
    listings_created = models.PositiveIntegerField(default=0)
    listings_updated = models.PositiveIntegerField(default=0)
    snapshots_created = models.PositiveIntegerField(default=0)
    missing_advertisements_detected = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True)

    class Meta:
        ordering = ("-started_at", "-pk")


class SourceSearchRunListing(models.Model):
    class DetailStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    source_search_run = models.ForeignKey(SourceSearchRun, on_delete=models.CASCADE, related_name="listing_results")
    external_id = models.CharField(max_length=255)
    listing = models.ForeignKey(Listing, null=True, blank=True, on_delete=models.SET_NULL, related_name="source_search_run_results")
    public_advertisement_url = models.URLField(max_length=2000)
    detail_fetch_status = models.CharField(max_length=10, choices=DetailStatus.choices, default=DetailStatus.PENDING)
    error_message = models.TextField(blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("source_search_run", "external_id"), name="unique_external_id_per_source_search_run"),
            models.UniqueConstraint(fields=("source_search_run", "listing"), name="unique_listing_per_source_search_run"),
        )


class SourceSearchMembership(models.Model):
    source_search = models.ForeignKey(SourceSearch, on_delete=models.CASCADE, related_name="memberships")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="source_search_memberships")
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    is_currently_present = models.BooleanField(default=True)

    class Meta:
        constraints = (models.UniqueConstraint(fields=("source_search", "listing"), name="unique_source_search_listing_membership"),)


class Dealer(models.Model):
    """A dealer whose complete inventory is collected from an API each day."""

    class InventoryType(models.TextChoices):
        USED = "used", "Used cars"
        MIXED = "mixed", "Mixed new and used cars"
        NEW = "new", "New cars only"

    name = models.CharField(max_length=200)
    source = models.CharField(max_length=50, db_index=True)
    external_id = models.CharField(max_length=255)
    api_url = models.URLField(max_length=2000)
    api_token_env_var = models.CharField(max_length=100, blank=True)
    price_bracket_size = models.PositiveIntegerField(default=5000)
    inventory_type = models.CharField(
        max_length=10, choices=InventoryType.choices, default=InventoryType.USED,
        db_index=True,
    )
    is_qa = models.BooleanField(default=False, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "pk")
        constraints = (
            models.UniqueConstraint(fields=("source", "external_id"), name="unique_dealer_source_external_id"),
        )

    def __str__(self):
        return self.name


class DealerInventorySnapshot(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    dealer = models.ForeignKey(Dealer, on_delete=models.CASCADE, related_name="inventory_snapshots")
    observed_at = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    inventory_count = models.PositiveIntegerField(default=0)
    median_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    dominant_price_bracket_low = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    dominant_price_bracket_high = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    dominant_price_bracket_count = models.PositiveIntegerField(default=0)
    average_active_days = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    disappeared_count = models.PositiveIntegerField(default=0)
    error_summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    listings = models.ManyToManyField(Listing, through="DealerSnapshotListing", related_name="dealer_snapshots")

    class Meta:
        ordering = ("-observed_at", "-pk")
        constraints = (
            models.UniqueConstraint(fields=("dealer", "observed_at"), name="unique_dealer_snapshot_time"),
        )

    def __str__(self):
        return f"{self.dealer} at {self.observed_at}"

    def save(self, *args, **kwargs):
        if self.pk:
            persisted = type(self).objects.filter(pk=self.pk).first()
            if persisted and persisted.status == self.Status.COMPLETED:
                tracked = (
                    "dealer_id", "observed_at", "status", "inventory_count", "median_price",
                    "dominant_price_bracket_low", "dominant_price_bracket_high",
                    "dominant_price_bracket_count", "average_active_days", "disappeared_count",
                    "error_summary",
                )
                if any(getattr(self, field) != getattr(persisted, field) for field in tracked):
                    raise ValidationError("Completed dealer snapshots are immutable.")
        return super().save(*args, **kwargs)


class DealerSnapshotListing(models.Model):
    snapshot = models.ForeignKey(DealerInventorySnapshot, on_delete=models.CASCADE, related_name="inventory_items")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="dealer_inventory_items")
    asking_price = models.DecimalField(max_digits=12, decimal_places=2)
    mileage = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Listing.Status.choices, default=Listing.Status.ACTIVE)
    observed_at = models.DateTimeField(db_index=True)
    raw_data = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("snapshot", "listing"), name="unique_listing_per_dealer_snapshot"),
        )

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Captured dealer snapshot listings are immutable.")
        return super().save(*args, **kwargs)


class DealerListingMembership(models.Model):
    dealer = models.ForeignKey(Dealer, on_delete=models.CASCADE, related_name="listing_memberships")
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name="dealer_memberships")
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    disappeared_at = models.DateTimeField(null=True, blank=True, db_index=True)
    is_currently_present = models.BooleanField(default=True, db_index=True)
    is_relevant = models.BooleanField(default=True, db_index=True)
    description_flags = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=("dealer", "listing"), name="unique_dealer_listing_membership"),
        )
