from django import template
from decimal import Decimal, InvalidOperation

from vehicles.sources.polovni_automobili.search_results import canonicalize_public_ad_url


register = template.Library()


@register.filter
def price_sr(value):
    """Format whole-euro prices with a dot thousands separator."""
    if value in (None, ""):
        return ""
    try:
        return f"{Decimal(value):,.0f}".replace(",", ".")
    except (InvalidOperation, TypeError, ValueError):
        return ""


@register.filter
def pa_public_url(listing):
    if not listing or listing.source != "polovniautomobili":
        return ""
    try:
        _, public_url = canonicalize_public_ad_url(
            listing.source_url, expected_listing_id=listing.external_id
        )
        return public_url
    except ValueError:
        return ""
