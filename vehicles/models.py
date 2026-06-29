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