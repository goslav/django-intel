from django import forms

from .models import Listing, MarketProfile, SourceSearch


class MarketProfileForm(forms.ModelForm):
    class Meta:
        model = MarketProfile
        fields = (
            "name",
            "source",
            "make",
            "model",
            "generation",
            "facelift_status",
            "engine_family",
            "minimum_year",
            "maximum_year",
            "minimum_mileage",
            "maximum_mileage",
            "fuel",
            "transmission",
            "minimum_power_kw",
            "maximum_power_kw",
            "source_search_url",
            "notes",
            "is_active",
        )
        widgets = {"notes": forms.Textarea(attrs={"rows": 4})}


class ListingClassificationForm(forms.ModelForm):
    class Meta:
        model = Listing
        fields = ("generation", "facelift_status", "engine_family", "transmission_category")


class SourceSearchForm(forms.ModelForm):
    class Meta:
        model = SourceSearch
        fields = ("name", "source", "search_url", "is_active", "maximum_pages", "request_delay_seconds")
