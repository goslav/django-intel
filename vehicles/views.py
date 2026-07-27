from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, render

from .models import Listing, Vehicle
from .services.market_analysis import (
    DEFAULT_MILEAGE_TOLERANCE,
    DEFAULT_YEAR_TOLERANCE,
    MAX_MILEAGE_TOLERANCE,
    MAX_YEAR_TOLERANCE,
    comparable_listings,
    price_history,
    summarize_market,
    with_latest_snapshot,
)


def vehicle_list(request):
    vehicles = Vehicle.objects.all().order_by("-id")

    return render(request, "vehicles/vehicle_list.html", {
        "vehicles": vehicles,
    })


SORT_OPTIONS = {
    "newest_observed": ("-latest_observed_at", "-pk"),
    "oldest_observed": ("latest_observed_at", "pk"),
    "lowest_price": ("latest_price", "pk"),
    "highest_price": ("-latest_price", "pk"),
    "lowest_mileage": ("mileage", "pk"),
    "highest_mileage": ("-mileage", "pk"),
    "newest_year": ("-year", "pk"),
    "oldest_year": ("year", "pk"),
}


def home(request):
    return render(
        request,
        "vehicles/home.html",
        {
            "active_listing_count": Listing.objects.filter(
                status=Listing.Status.ACTIVE
            ).count(),
            "vehicle_count": Vehicle.objects.count(),
        },
    )


def _non_negative_number(value, converter):
    if value in (None, ""):
        return None
    try:
        parsed = converter(value)
    except (ValueError, InvalidOperation):
        return None
    return parsed if parsed >= 0 else None


def market_list(request):
    listings = with_latest_snapshot(
        Listing.objects.filter(status=Listing.Status.ACTIVE)
    ).filter(latest_price__isnull=False)

    for field in ("source", "make", "model", "fuel", "transmission"):
        value = request.GET.get(field, "").strip()
        if value:
            listings = listings.filter(**{f"{field}__iexact": value})

    numeric_filters = (
        ("min_year", "year__gte", int),
        ("max_year", "year__lte", int),
        ("min_mileage", "mileage__gte", int),
        ("max_mileage", "mileage__lte", int),
        ("min_price", "latest_price__gte", Decimal),
        ("max_price", "latest_price__lte", Decimal),
    )
    for parameter, lookup, converter in numeric_filters:
        value = _non_negative_number(request.GET.get(parameter), converter)
        if value is not None:
            listings = listings.filter(**{lookup: value})

    selected_sort = request.GET.get("sort", "newest_observed")
    if selected_sort not in SORT_OPTIONS:
        selected_sort = "newest_observed"
    listings = listings.order_by(*SORT_OPTIONS[selected_sort])

    paginator = Paginator(listings, 20)
    page = paginator.get_page(request.GET.get("page"))
    query_parameters = request.GET.copy()
    query_parameters.pop("page", None)

    active = Listing.objects.filter(status=Listing.Status.ACTIVE)
    choice_fields = {
        field: active.order_by(field).values_list(field, flat=True).distinct()
        for field in ("source", "make", "model", "fuel", "transmission")
    }
    return render(
        request,
        "vehicles/market_list.html",
        {
            "page": page,
            "choices": choice_fields,
            "selected_sort": selected_sort,
            "querystring": query_parameters.urlencode(),
        },
    )


def _bounded_tolerance(raw_value, default, maximum, label, errors):
    if raw_value in (None, ""):
        return default
    try:
        value = int(raw_value)
    except ValueError:
        errors.append(f"{label} must be a whole number.")
        return default
    if value < 0 or value > maximum:
        errors.append(f"{label} must be between 0 and {maximum:,}.")
        return default
    return value


def market_detail(request, listing_id):
    selected = get_object_or_404(
        with_latest_snapshot(Listing.objects.all()),
        pk=listing_id,
    )
    errors = []
    year_tolerance = _bounded_tolerance(
        request.GET.get("year_tolerance"),
        DEFAULT_YEAR_TOLERANCE,
        MAX_YEAR_TOLERANCE,
        "Year tolerance",
        errors,
    )
    mileage_tolerance = _bounded_tolerance(
        request.GET.get("mileage_tolerance"),
        DEFAULT_MILEAGE_TOLERANCE,
        MAX_MILEAGE_TOLERANCE,
        "Mileage tolerance",
        errors,
    )
    same_transmission = request.GET.get("same_transmission") == "1"
    comparables = list(
        comparable_listings(
            selected,
            year_tolerance=year_tolerance,
            mileage_tolerance=mileage_tolerance,
            same_transmission=same_transmission,
        ).order_by("latest_price", "pk")
    )
    summary = summarize_market(comparables, selected.latest_price)

    return render(
        request,
        "vehicles/market_detail.html",
        {
            "listing": selected,
            "comparables": comparables,
            "summary": summary,
            "history": price_history(selected),
            "year_tolerance": year_tolerance,
            "mileage_tolerance": mileage_tolerance,
            "same_transmission": same_transmission,
            "tolerance_errors": errors,
        },
    )
