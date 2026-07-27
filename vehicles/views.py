from decimal import Decimal, InvalidOperation

from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Abs
from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ListingClassificationForm, MarketProfileForm, SourceSearchForm
from .models import Listing, MarketProfile, SourceSearch, SourceSearchRun, Vehicle
from .services.source_search_refresh import refresh_source_search
from .sources.polovni_automobili.search_results import CollectionError
from .sources.polovni_automobili.catalog import CAR_BRANDS
from .services.market_analysis import (
    DEFAULT_MILEAGE_TOLERANCE,
    DEFAULT_YEAR_TOLERANCE,
    MAX_MILEAGE_TOLERANCE,
    MAX_YEAR_TOLERANCE,
    comparable_listings,
    price_history,
    summarize_market,
    summarize_profile_market,
    with_latest_snapshot,
    listing_matches_profile,
    listings_for_profile,
    rank_profile_comparables,
)


def vehicle_list(request):
    vehicles = Vehicle.objects.all().order_by("-id")

    return render(request, "vehicles/vehicle_list.html", {
        "vehicles": vehicles,
    })


SORT_OPTIONS = {
    "newest_published": (F("published_at").desc(nulls_last=True), "-pk"),
    "oldest_published": (F("published_at").asc(nulls_last=True), "pk"),
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

    target_year = _non_negative_number(request.GET.get("year"), int)
    numeric_filters = (
        ("min_year", "year__gte", int),
        ("max_year", "year__lte", int),
        ("min_mileage", "mileage__gte", int),
        ("max_mileage", "mileage__lte", int),
        ("min_price", "latest_price__gte", Decimal),
        ("max_price", "latest_price__lte", Decimal),
    )
    for parameter, lookup, converter in numeric_filters:
        if target_year is not None and parameter in {"min_year", "max_year"}:
            continue
        value = _non_negative_number(request.GET.get(parameter), converter)
        if value is not None:
            listings = listings.filter(**{lookup: value})

    year_window = None
    if target_year is not None:
        narrow = listings.filter(
            year__gte=max(0, target_year - 1), year__lte=target_year + 1,
        )
        expanded = narrow.count() < MINIMUM_COMPARABLE_YEAR_POPULATION
        tolerance = 2 if expanded else 1
        listings = listings.filter(
            year__gte=max(0, target_year - tolerance), year__lte=target_year + tolerance,
        ).annotate(target_year_distance=Abs(F("year") - Value(target_year)))
        year_window = {
            "target": target_year,
            "minimum": max(0, target_year - tolerance),
            "maximum": target_year + tolerance,
            "expanded": expanded,
            "minimum_population": MINIMUM_COMPARABLE_YEAR_POPULATION,
        }

    selected_sort = request.GET.get("sort", "newest_observed")
    if selected_sort not in SORT_OPTIONS:
        selected_sort = "newest_observed"
    ordering = SORT_OPTIONS[selected_sort]
    if target_year is not None:
        ordering = ("target_year_distance", *ordering)
    listings = listings.order_by(*ordering)

    comparable_parameters = ("make", "model", "fuel", "transmission", "year")
    comparable_summary = None
    if any(request.GET.get(parameter, "").strip() for parameter in comparable_parameters):
        comparable_summary = summarize_profile_market(list(listings))

    paginator = Paginator(listings, 20)
    page = paginator.get_page(request.GET.get("page"))
    query_parameters = request.GET.copy()
    query_parameters.pop("page", None)

    active = Listing.objects.filter(status=Listing.Status.ACTIVE)
    choice_fields = {
        field: active.order_by(field).values_list(field, flat=True).distinct()
        for field in ("source", "make", "model", "fuel", "transmission")
    }
    choice_fields["make"] = sorted(
        {value for value in choice_fields["make"] if value} | set(CAR_BRANDS),
        key=str.casefold,
    )
    choice_fields["fuel"] = sorted(
        {value for value in choice_fields["fuel"] if value}
        | {"diesel", "petrol", "hybrid"}
    )
    return render(
        request,
        "vehicles/market_list.html",
        {
            "page": page,
            "choices": choice_fields,
            "selected_sort": selected_sort,
            "querystring": query_parameters.urlencode(),
            "comparable_summary": comparable_summary,
            "year_window": year_window,
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
        request.GET.get("year_tolerance"), DEFAULT_YEAR_TOLERANCE, MAX_YEAR_TOLERANCE,
        "Year tolerance", errors,
    )
    mileage_tolerance = _bounded_tolerance(
        request.GET.get("mileage_tolerance"), DEFAULT_MILEAGE_TOLERANCE,
        MAX_MILEAGE_TOLERANCE, "Mileage tolerance", errors,
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
            "listing": selected, "comparables": comparables, "summary": summary,
            "history": price_history(selected), "year_tolerance": year_tolerance,
            "mileage_tolerance": mileage_tolerance,
            "same_transmission": same_transmission, "tolerance_errors": errors,
        },
    )


PROFILE_SORT_OPTIONS = {
    "lowest_price": ("latest_price", "pk"),
    "highest_price": ("-latest_price", "pk"),
    "lowest_mileage": ("latest_mileage", "pk"),
    "highest_mileage": ("-latest_mileage", "pk"),
    "newest_year": ("-year", "pk"),
    "oldest_year": ("year", "pk"),
    "recently_seen": ("-latest_observed_at", "pk"),
    "newest_published": (F("published_at").desc(nulls_last=True), "-pk"),
    "oldest_published": (F("published_at").asc(nulls_last=True), "pk"),
}
MINIMUM_COMPARABLE_YEAR_POPULATION = 10


def market_profile_list(request):
    profiles = list(MarketProfile.objects.all())
    candidates = list(
        with_latest_snapshot(Listing.objects.filter(status=Listing.Status.ACTIVE)).filter(
            latest_price__isnull=False
        )
    )
    for profile in profiles:
        profile.matching_count = sum(
            listing_matches_profile(listing, profile) for listing in candidates
        )
    return render(request, "vehicles/market_profile_list.html", {"profiles": profiles})


def market_profile_create(request):
    form = MarketProfileForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        profile = form.save()
        messages.success(request, f'Market profile "{profile.name}" was created.')
        return redirect("market_profiles:detail", profile_id=profile.pk)
    return render(
        request,
        "vehicles/market_profile_form.html",
        {"form": form, "heading": "New market profile", "submit_label": "Create profile"},
    )


def market_profile_edit(request, profile_id):
    profile = get_object_or_404(MarketProfile, pk=profile_id)
    form = MarketProfileForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        profile = form.save()
        messages.success(request, f'Market profile "{profile.name}" was updated.')
        return redirect("market_profiles:detail", profile_id=profile.pk)
    return render(
        request,
        "vehicles/market_profile_form.html",
        {
            "form": form,
            "profile": profile,
            "heading": "Edit market profile",
            "submit_label": "Save changes",
        },
    )


def market_profile_detail(request, profile_id):
    profile = get_object_or_404(MarketProfile, pk=profile_id)
    selected_sort = request.GET.get("sort", "best_match")
    if selected_sort != "best_match" and selected_sort not in PROFILE_SORT_OPTIONS:
        selected_sort = "best_match"
    queryset = listings_for_profile(profile)
    all_listings = list(queryset.order_by("-latest_observed_at", "pk"))
    target = None
    target_id = request.GET.get("target")
    if target_id:
        target = next((listing for listing in all_listings if str(listing.pk) == target_id), None)
    target = target or (all_listings[0] if all_listings else None)
    ranked_comparables = rank_profile_comparables(target, profile, all_listings) if target else []
    if selected_sort == "best_match":
        matching_listings = ([target] + [item.listing for item in ranked_comparables]) if target else []
    else:
        matching_listings = list(queryset.order_by(*PROFILE_SORT_OPTIONS[selected_sort]))
    summary = summarize_profile_market(all_listings)
    paginator = Paginator(matching_listings, 20)
    page = paginator.get_page(request.GET.get("page"))
    query_parameters = request.GET.copy()
    query_parameters.pop("page", None)
    return render(
        request,
        "vehicles/market_profile_detail.html",
        {
            "profile": profile,
            "summary": summary,
            "page": page,
            "selected_sort": selected_sort,
            "target": target,
            "target_choices": all_listings,
            "closest_comparables": ranked_comparables[:10],
            "querystring": query_parameters.urlencode(),
        },
    )


def classification_review(request):
    if request.method == "POST":
        listing = get_object_or_404(Listing, pk=request.POST.get("listing_id"))
        form = ListingClassificationForm(request.POST, instance=listing)
        if form.is_valid():
            listing = form.save(commit=False)
            listing.classification_method = Listing.ClassificationMethod.MANUAL
            listing.classification_confidence = Decimal("1.00")
            listing.classification_manually_overridden = True
            listing.classification_notes = "Manually classified in the review queue."
            listing.save()
            messages.success(request, f'Classification updated for "{listing}".')
            return redirect("classification_review")
        messages.error(request, "Please correct the classification values.")

    listings = Listing.objects.filter(
        make__iexact="Peugeot",
    ).filter(
        Q(model__iexact="2008") | Q(model__iexact="3008")
    ).filter(
        Q(generation="")
        | Q(engine_family="")
        | Q(transmission_category=Listing.TransmissionCategory.UNKNOWN)
        | Q(classification_confidence__lt=Decimal("0.80"))
        | Q(model__iexact="3008", facelift_status=Listing.FaceliftStatus.UNKNOWN)
    ).order_by("-last_seen_at", "pk")
    page = Paginator(listings, 20).get_page(request.GET.get("page"))
    return render(request, "vehicles/classification_review.html", {"page": page})


def source_search_list(request):
    searches = SourceSearch.objects.annotate(current_count=Count("memberships", filter=Q(memberships__is_currently_present=True)))
    return render(request, "vehicles/source_search_list.html", {"searches": searches})


def source_search_create(request):
    form = SourceSearchForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        source_search = form.save()
        messages.success(request, "Source search created.")
        return redirect("source_search_detail", search_id=source_search.pk)
    return render(request, "vehicles/source_search_form.html", {"form": form, "heading": "New source search"})


def source_search_edit(request, search_id):
    source_search = get_object_or_404(SourceSearch, pk=search_id)
    form = SourceSearchForm(request.POST or None, instance=source_search)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Source search updated.")
        return redirect("source_search_detail", search_id=source_search.pk)
    return render(request, "vehicles/source_search_form.html", {"form": form, "heading": "Edit source search", "source_search": source_search})


def source_search_detail(request, search_id):
    source_search = get_object_or_404(SourceSearch, pk=search_id)
    if request.method == "POST":
        if not settings.DEBUG:
            messages.error(request, "Browser refresh is available only in development.")
        else:
            try:
                result = refresh_source_search(source_search)
                messages.success(request, f"Refresh finished with status {result.run.status}.")
            except CollectionError as exc:
                messages.error(request, str(exc))
        return redirect("source_search_detail", search_id=source_search.pk)
    runs = source_search.runs.all()[:20]
    latest_completed = source_search.runs.filter(status=SourceSearchRun.Status.COMPLETED).first()
    latest_metrics = None
    if latest_completed:
        latest_metrics = {
            "new_listings": latest_completed.listings_created,
            "price_changes": max(latest_completed.snapshots_created - latest_completed.listings_created, 0),
            "missing": latest_completed.missing_advertisements_detected,
            "unknown": latest_completed.listing_results.filter(
                Q(listing__generation="")
                | Q(listing__engine_family="")
                | Q(listing__facelift_status=Listing.FaceliftStatus.UNKNOWN)
            ).count(),
        }
    relevant_profiles = MarketProfile.objects.filter(make__iexact="Peugeot", model__iexact="3008", engine_family__iexact="1.5 BlueHDi", transmission__iexact="automatic")
    return render(request, "vehicles/source_search_detail.html", {
        "source_search": source_search, "runs": runs,
        "current_count": source_search.memberships.filter(is_currently_present=True).count(),
        "relevant_profiles": relevant_profiles, "debug": settings.DEBUG,
        "latest_metrics": latest_metrics,
    })


def source_search_run_detail(request, run_id):
    run = get_object_or_404(SourceSearchRun.objects.select_related("source_search"), pk=run_id)
    results = run.listing_results.select_related("listing").order_by("listing__external_id")
    return render(request, "vehicles/source_search_run_detail.html", {"run": run, "results": results})
