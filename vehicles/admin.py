from django.contrib import admin
from .models import ImportRun, Listing, ListingSnapshot, MarketProfile, SourceSearch, SourceSearchMembership, SourceSearchRun, SourceSearchRunListing, Vehicle


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = (
        "make",
        "model",
        "year",
        "mileage",
        "fuel_type",
        "purchase_price",
        "expected_sale_price",
        "created_at",
    )
    search_fields = ("make", "model", "fuel_type")
    list_filter = ("make", "fuel_type", "year")


class ListingSnapshotInline(admin.TabularInline):
    model = ListingSnapshot
    extra = 0
    fields = ("observed_at", "asking_price", "mileage", "import_run")
    readonly_fields = fields
    ordering = ("-observed_at",)
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "external_id",
        "make",
        "model",
        "year",
        "mileage",
        "generation",
        "facelift_status",
        "engine_family",
        "classification_method",
        "classification_manually_overridden",
        "status",
        "last_seen_at",
    )
    search_fields = ("external_id", "title", "make", "model", "generation", "engine_family", "source_url")
    list_filter = ("source", "status", "generation", "facelift_status", "engine_family", "transmission_category", "classification_method", "classification_manually_overridden", "fuel", "year")
    readonly_fields = ("first_seen_at", "last_seen_at", "created_at", "updated_at")
    inlines = (ListingSnapshotInline,)
    date_hierarchy = "last_seen_at"


@admin.register(ListingSnapshot)
class ListingSnapshotAdmin(admin.ModelAdmin):
    list_display = ("listing", "source", "observed_at", "asking_price", "mileage", "import_run")
    search_fields = ("listing__external_id", "listing__make", "listing__model")
    list_filter = ("listing__source", "observed_at")
    readonly_fields = ("listing", "import_run", "observed_at", "asking_price", "mileage", "raw_data")
    list_select_related = ("listing", "import_run")
    date_hierarchy = "observed_at"

    @admin.display(ordering="listing__source")
    def source(self, obj):
        return obj.listing.source


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = (
        "source",
        "snapshot_at",
        "status",
        "is_full_snapshot",
        "processed_count",
        "created_count",
        "updated_count",
        "removed_count",
        "error_count",
    )
    search_fields = ("source", "error_summary")
    list_filter = ("source", "status", "is_full_snapshot")
    readonly_fields = (
        "source",
        "snapshot_at",
        "started_at",
        "completed_at",
        "is_full_snapshot",
        "status",
        "processed_count",
        "created_count",
        "updated_count",
        "removed_count",
        "error_count",
        "error_summary",
    )
    date_hierarchy = "snapshot_at"


@admin.register(MarketProfile)
class MarketProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "source", "make", "model", "generation", "facelift_status", "engine_family", "transmission", "is_active", "updated_at")
    search_fields = ("name", "source", "make", "model", "notes")
    list_filter = ("is_active", "source", "fuel", "transmission")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        ("Profile", {"fields": ("name", "is_active", "notes")}),
        ("Vehicle segment", {"fields": ("make", "model", "generation", "facelift_status", "engine_family", ("minimum_year", "maximum_year"), ("minimum_mileage", "maximum_mileage"), ("minimum_power_kw", "maximum_power_kw"), "fuel", "transmission")}),
        ("Source", {"fields": ("source", "source_search_url")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


@admin.register(SourceSearch)
class SourceSearchAdmin(admin.ModelAdmin):
    list_display = ("name", "source", "is_active", "maximum_pages", "last_refresh_status", "last_refresh_completed_at")
    list_filter = ("source", "is_active", "last_refresh_status")
    search_fields = ("name", "search_url")
    readonly_fields = ("last_refresh_started_at", "last_refresh_completed_at", "last_refresh_status", "last_error_summary", "created_at", "updated_at")


admin.site.register(SourceSearchRun)
admin.site.register(SourceSearchRunListing)
admin.site.register(SourceSearchMembership)
