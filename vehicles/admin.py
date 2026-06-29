from django.contrib import admin
from .models import Vehicle


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
