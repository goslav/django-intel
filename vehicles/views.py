from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from statistics import median

from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Value
from django.db.models.functions import Abs
from django.conf import settings
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import ListingClassificationForm, MarketProfileForm, SourceSearchForm
from .models import Dealer, DealerInventorySnapshot, DealerListingMembership, DealerSnapshotListing, Listing, MarketProfile, SourceSearch, SourceSearchRun, Vehicle
from .services.source_search_refresh import refresh_source_search
from .services.dealer_comparison import (
    ComparisonFilters, comparison_rows, dealer_drilldown, filter_options,
    model_frequency_rows,
)
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
    dealer_membership = selected.dealer_memberships.select_related("dealer").first()
    return render(
        request,
        "vehicles/market_detail.html",
        {
            "listing": selected, "comparables": comparables, "summary": summary,
            "history": price_history(selected), "year_tolerance": year_tolerance,
            "mileage_tolerance": mileage_tolerance,
            "same_transmission": same_transmission, "tolerance_errors": errors,
            "dealer_membership": dealer_membership,
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


def dealer_list(request):
    dealers = Dealer.objects.prefetch_related("inventory_snapshots")
    for dealer in dealers:
        dealer.latest_snapshot = dealer.inventory_snapshots.first()
    return render(request, "vehicles/dealer_list.html", {"dealers": dealers})


def dealer_activity(request):
    try:
        period_days = int(request.GET.get("period", 30))
    except (TypeError, ValueError):
        period_days = 30
    if period_days not in (7, 30):
        period_days = 30

    latest_snapshot = DealerInventorySnapshot.objects.filter(
        status=DealerInventorySnapshot.Status.COMPLETED,
    ).order_by("-observed_at", "-pk").first()
    as_of = latest_snapshot.observed_at if latest_snapshot else timezone.now()
    cutoff = as_of - timedelta(days=period_days)
    rows = []
    for dealer in Dealer.objects.filter(is_active=True):
        completed = dealer.inventory_snapshots.filter(
            status=DealerInventorySnapshot.Status.COMPLETED,
        ).order_by("observed_at", "pk")
        first_snapshot = completed.first()
        latest = completed.last()
        monitoring_started_at = first_snapshot.observed_at if first_snapshot else as_of
        additions_since = max(cutoff, monitoring_started_at)
        additions = dealer.listing_memberships.filter(
            first_seen_at__gt=additions_since,
            first_seen_at__lte=as_of,
        ).count()
        disappearances = dealer.listing_memberships.filter(
            disappeared_at__gt=cutoff,
            disappeared_at__lte=as_of,
        ).count()
        observed_days = max((timezone.localdate(as_of) - timezone.localdate(additions_since)).days + 1, 1)
        rows.append({
            "dealer": dealer,
            "latest": latest,
            "additions": additions,
            "disappearances": disappearances,
            "total_activity": additions + disappearances,
            "net_change": additions - disappearances,
            "additions_per_week": Decimal(additions * 7) / Decimal(observed_days),
            "observed_days": observed_days,
        })
    rows.sort(key=lambda row: (
        -row["total_activity"], -row["additions"], -row["disappearances"],
        row["dealer"].name.casefold(),
    ))
    return render(request, "vehicles/dealer_activity.html", {
        "rows": rows,
        "period_days": period_days,
        "as_of": as_of,
        "total_additions": sum(row["additions"] for row in rows),
        "total_disappearances": sum(row["disappearances"] for row in rows),
        "active_dealer_count": sum(bool(row["total_activity"]) for row in rows),
    })


def _comparison_filters(request):
    def optional_int(name):
        try:
            return int(request.GET[name]) if request.GET.get(name) else None
        except (TypeError, ValueError):
            return None
    dealer_ids = []
    for value in request.GET.getlist("dealer"):
        try:
            dealer_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ComparisonFilters(
        make=request.GET.get("make", "").strip(),
        model=request.GET.get("model", "").strip(),
        dealer_ids=tuple(dict.fromkeys(dealer_ids)),
        minimum_year=optional_int("year_min"),
        maximum_year=optional_int("year_max"),
        fuel=request.GET.get("fuel", "").strip(),
        transmission=request.GET.get("transmission", "").strip(),
    )


def dealer_model_comparison(request):
    filters = _comparison_filters(request)
    options = filter_options()
    return render(request, "vehicles/dealer_model_comparison.html", {
        "filters": filters, "rows": comparison_rows(filters),
        "model_frequencies": model_frequency_rows(filters), **options,
        "querystring": request.GET.urlencode(),
        "transmissions": Listing.TransmissionCategory.choices,
    })


def dealer_model_comparison_detail(request, dealer_id):
    dealer = get_object_or_404(Dealer, pk=dealer_id, is_active=True)
    filters = _comparison_filters(request)
    row, vehicles = dealer_drilldown(dealer, filters)
    return render(request, "vehicles/dealer_model_comparison_detail.html", {
        "dealer": dealer, "filters": filters, "row": row, "vehicles": vehicles,
        "querystring": request.GET.urlencode(),
    })


def dealer_snapshot_history(request, dealer_id):
    dealer = get_object_or_404(Dealer, pk=dealer_id)
    snapshots = dealer.inventory_snapshots.filter(
        status=DealerInventorySnapshot.Status.COMPLETED
    ).order_by("-observed_at", "-pk")
    page = Paginator(snapshots, 50).get_page(request.GET.get("page"))
    return render(request, "vehicles/dealer_snapshot_history.html", {"dealer": dealer, "page": page})


def dealer_snapshot_detail(request, dealer_id, snapshot_id):
    dealer = get_object_or_404(Dealer, pk=dealer_id)
    snapshot = get_object_or_404(
        DealerInventorySnapshot, pk=snapshot_id, dealer=dealer,
        status=DealerInventorySnapshot.Status.COMPLETED,
    )
    items = snapshot.inventory_items.select_related("listing").order_by("listing__make", "listing__model", "pk")
    page = Paginator(items, 100).get_page(request.GET.get("page"))
    return render(request, "vehicles/dealer_snapshot_detail.html", {
        "dealer": dealer, "snapshot": snapshot, "page": page,
    })


def dealer_detail(request, dealer_id):
    dealer = get_object_or_404(Dealer, pk=dealer_id)
    if request.method == "POST":
        return_visibility = request.POST.get("return_visibility", "relevant")
        if return_visibility not in {"relevant", "flagged", "excluded", "all"}:
            return_visibility = "relevant"
        restore_listing_id = request.POST.get("restore_listing_id")
        listing_ids = [restore_listing_id] if restore_listing_id else request.POST.getlist("listing_ids")
        bulk_action = "restore" if restore_listing_id else request.POST.get("bulk_action")
        redirect_url = reverse("dealers:detail", kwargs={"dealer_id": dealer.pk})
        if return_visibility != "relevant":
            redirect_url = f"{redirect_url}?visibility={return_visibility}"
        if not listing_ids:
            messages.error(request, "Select at least one vehicle.")
            return redirect(redirect_url)
        if bulk_action not in {"exclude", "restore"}:
            messages.error(request, "Choose a valid bulk action.")
            return redirect(redirect_url)
        memberships = DealerListingMembership.objects.filter(
            dealer=dealer, listing_id__in=listing_ids,
        )
        changed = memberships.update(is_relevant=bulk_action == "restore")
        action = "restored to" if bulk_action == "restore" else "excluded from"
        messages.success(request, f"{changed} vehicle(s) were {action} market intelligence.")
        return redirect(redirect_url)
    snapshots = list(dealer.inventory_snapshots.filter(status=DealerInventorySnapshot.Status.COMPLETED)[:366])
    latest = snapshots[0] if snapshots else None
    comparison_options = snapshots[1:]
    comparison_snapshot = None
    requested_comparison = request.GET.get("compare_to")
    if requested_comparison:
        comparison_snapshot = next((snapshot for snapshot in comparison_options if str(snapshot.pk) == requested_comparison), None)
    elif comparison_options:
        comparison_snapshot = comparison_options[0]
    selected_snapshot = None
    requested_snapshot = request.GET.get("snapshot")
    if requested_snapshot:
        selected_snapshot = next((snapshot for snapshot in snapshots if str(snapshot.pk) == requested_snapshot), None)
    snapshot_changes = None
    new_listing_ids, reduced_listing_ids, increased_listing_ids = set(), set(), set()
    disappeared_listing_ids = set()
    if latest and comparison_snapshot:
        current_prices = dict(latest.inventory_items.values_list("listing_id", "asking_price"))
        previous_prices = dict(comparison_snapshot.inventory_items.values_list("listing_id", "asking_price"))
        current_ids, previous_ids = set(current_prices), set(previous_prices)
        new_listing_ids = current_ids - previous_ids
        disappeared_listing_ids = previous_ids - current_ids
        common_ids = current_ids & previous_ids
        reduced_listing_ids = {listing_id for listing_id in common_ids if current_prices[listing_id] < previous_prices[listing_id]}
        increased_listing_ids = {listing_id for listing_id in common_ids if current_prices[listing_id] > previous_prices[listing_id]}
        snapshot_changes = {
            "new": len(new_listing_ids), "disappeared": len(disappeared_listing_ids),
            "reduced": len(reduced_listing_ids), "increased": len(increased_listing_ids),
            "inventory_change": latest.inventory_count - comparison_snapshot.inventory_count,
            "median_change": latest.median_price - comparison_snapshot.median_price
            if latest.median_price is not None and comparison_snapshot.median_price is not None else None,
        }
    sort_options = {"mileage_asc", "mileage_desc", "active_days_asc", "active_days_desc"}
    selected_sort = request.GET.get("sort", "mileage_asc")
    if selected_sort not in sort_options:
        selected_sort = "mileage_asc"
    try:
        page_size = int(request.GET.get("page_size", 100))
    except (TypeError, ValueError):
        page_size = 100
    if page_size not in (50, 100, 200):
        page_size = 100
    visibility = request.GET.get("visibility", "relevant")
    if visibility not in {"relevant", "flagged", "excluded", "all"}:
        visibility = "relevant"
    inventory = latest.inventory_items.select_related("listing") if latest else []
    if latest and visibility == "relevant":
        inventory = inventory.filter(
            listing__dealer_memberships__dealer=dealer,
            listing__dealer_memberships__is_relevant=True,
        )
    elif latest and visibility == "excluded":
        inventory = inventory.filter(
            listing__dealer_memberships__dealer=dealer,
            listing__dealer_memberships__is_relevant=False,
        )
    elif latest and visibility == "flagged":
        inventory = inventory.exclude(
            listing__dealer_memberships__dealer=dealer,
            listing__dealer_memberships__description_flags=[],
        )
    inventory = list(inventory)
    inventory_memberships = {membership.listing_id: membership for membership in dealer.listing_memberships.filter(
        listing_id__in=[item.listing_id for item in inventory]
    )}
    as_of = timezone.localdate(latest.observed_at) if latest else timezone.localdate()
    for item in inventory:
        membership = inventory_memberships[item.listing_id]
        active_since = item.listing.published_at or timezone.localdate(membership.first_seen_at)
        item.active_days = max((as_of - active_since).days, 0)
        item.description_flags = membership.description_flags
    sort_field = "mileage" if selected_sort.startswith("mileage") else "active_days"
    inventory.sort(key=lambda item: (getattr(item, sort_field), item.pk), reverse=selected_sort.endswith("desc"))
    inventory_page = Paginator(inventory, page_size).get_page(request.GET.get("page"))
    query_parameters = request.GET.copy()
    query_parameters.pop("page", None)
    recently_disappeared_memberships = list(
        dealer.listing_memberships.filter(is_currently_present=False)
        .select_related("listing").order_by("-disappeared_at")[:30]
    )
    last_captured_items = {}
    for item in DealerSnapshotListing.objects.filter(
            snapshot__dealer=dealer,
            listing_id__in=[membership.listing_id for membership in recently_disappeared_memberships],
        ).select_related("listing").order_by("listing_id", "-observed_at", "-pk"):
        last_captured_items.setdefault(item.listing_id, item)
    recently_disappeared = [
        {"membership": membership, "captured_item": last_captured_items.get(membership.listing_id)}
        for membership in recently_disappeared_memberships
    ]
    recently_added_memberships = list(
        dealer.listing_memberships.select_related("listing").order_by("-first_seen_at")[:50]
    )
    latest_added_items = {}
    for item in DealerSnapshotListing.objects.filter(
        snapshot__dealer=dealer,
        listing_id__in=[membership.listing_id for membership in recently_added_memberships],
    ).select_related("listing").order_by("listing_id", "-observed_at", "-pk"):
        latest_added_items.setdefault(item.listing_id, item)
    recently_added = [
        {"membership": membership, "captured_item": latest_added_items.get(membership.listing_id)}
        for membership in recently_added_memberships
    ]
    replenishment = None
    if latest:
        monitoring_started_at = snapshots[-1].observed_at
        additions_7d = dealer.listing_memberships.filter(
            first_seen_at__gt=max(latest.observed_at - timedelta(days=7), monitoring_started_at)
        ).count()
        additions_30d = dealer.listing_memberships.filter(
            first_seen_at__gt=max(latest.observed_at - timedelta(days=30), monitoring_started_at)
        ).count()
        monitored_days = max(
            (timezone.localdate(latest.observed_at) - timezone.localdate(monitoring_started_at)).days + 1,
            1,
        )
        rate_window_days = min(monitored_days, 30)
        replenishment = {
            "additions_7d": additions_7d,
            "additions_30d": additions_30d,
            "average_per_week": Decimal(additions_30d * 7) / Decimal(rate_window_days),
            "rate_window_days": rate_window_days,
        }
    excluded_count = dealer.listing_memberships.filter(is_relevant=False, is_currently_present=True).count()
    description_flagged_count = dealer.listing_memberships.exclude(description_flags=[]).filter(is_currently_present=True).count()
    relevant_items = list(latest.inventory_items.filter(
        listing__dealer_memberships__dealer=dealer,
        listing__dealer_memberships__is_relevant=True,
    ).select_related("listing")) if latest else []
    relevant_listing_ids = {item.listing_id for item in relevant_items}
    intelligence_summary = None
    top_models = []
    price_brackets = []
    if relevant_items:
        prices = [item.asking_price for item in relevant_items]
        bracket_size = Decimal(dealer.price_bracket_size)
        brackets = Counter((price // bracket_size) * bracket_size for price in prices)
        bracket_count = max(brackets.values())
        bracket_low = min(low for low, count in brackets.items() if count == bracket_count)
        membership_dates = dict(dealer.listing_memberships.filter(
            listing_id__in=[item.listing_id for item in relevant_items]
        ).values_list("listing_id", "first_seen_at"))
        as_of = timezone.localdate(latest.observed_at)
        ages = [max((as_of - (item.listing.published_at or timezone.localdate(membership_dates[item.listing_id]))).days, 0) for item in relevant_items]
        intelligence_summary = {
            "count": len(relevant_items), "median_price": Decimal(str(median(prices))),
            "bracket_low": bracket_low, "bracket_high": bracket_low + bracket_size,
            "bracket_count": bracket_count,
            "average_active_days": Decimal(sum(ages)) / Decimal(len(ages)),
        }
        model_counts = Counter(f"{item.listing.make} {item.listing.model}" for item in relevant_items)
        top_models = model_counts.most_common(5)
        price_brackets = [
            {"low": low, "high": low + bracket_size, "count": count}
            for low, count in sorted(brackets.items())
        ]
    stock_age_buckets = {"0–30 days": 0, "31–60 days": 0, "61–90 days": 0, "90+ days": 0}
    model_ages = {}
    for item in relevant_items:
        age = max((timezone.localdate(latest.observed_at) - (item.listing.published_at or timezone.localdate(membership_dates[item.listing_id]))).days, 0)
        if age <= 30:
            stock_age_buckets["0–30 days"] += 1
        elif age <= 60:
            stock_age_buckets["31–60 days"] += 1
        elif age <= 90:
            stock_age_buckets["61–90 days"] += 1
        else:
            stock_age_buckets["90+ days"] += 1
        model_ages.setdefault(f"{item.listing.make} {item.listing.model}", []).append(age)
    oldest_models = sorted(
        ((model, Decimal(sum(ages)) / Decimal(len(ages)), len(ages)) for model, ages in model_ages.items()),
        key=lambda row: (-row[1], row[0]),
    )[:5]
    slow_stock_by_model = {}
    for item in relevant_items:
        age = max((timezone.localdate(latest.observed_at) - (item.listing.published_at or timezone.localdate(membership_dates[item.listing_id]))).days, 0)
        if age <= 90:
            continue
        observed_prices = list(item.listing.snapshots.order_by("observed_at", "pk").values_list("asking_price", flat=True))
        reductions = [previous - current for previous, current in zip(observed_prices, observed_prices[1:]) if current < previous]
        key = f"{item.listing.make} {item.listing.model}"
        slow_stock_by_model.setdefault(key, []).append({
            "listing": item.listing, "current_price": item.asking_price, "ad_age": age,
            "reduction_count": len(reductions),
            "total_reduction": sum(reductions, Decimal("0")),
        })
    slow_stock_groups = [
        {"model": model, "vehicles": sorted(vehicles, key=lambda vehicle: (-vehicle["ad_age"], vehicle["listing"].pk))}
        for model, vehicles in sorted(slow_stock_by_model.items())
    ]

    relevant_membership_ids = set(dealer.listing_memberships.filter(is_relevant=True).values_list("listing_id", flat=True))
    previous_relevant_items = list(comparison_snapshot.inventory_items.filter(
        listing_id__in=relevant_membership_ids
    ).select_related("listing")) if comparison_snapshot else []
    current_relevant_ids = {item.listing_id for item in relevant_items}
    previous_relevant_ids = {item.listing_id for item in previous_relevant_items}
    model_summary_data = {}
    for item in relevant_items:
        key = (item.listing.make, item.listing.model)
        row = model_summary_data.setdefault(key, {"prices": [], "ages": [], "current_ids": set(), "previous_ids": set()})
        row["prices"].append(item.asking_price)
        row["ages"].append(max((timezone.localdate(latest.observed_at) - (item.listing.published_at or timezone.localdate(membership_dates[item.listing_id]))).days, 0))
        row["current_ids"].add(item.listing_id)
    for item in previous_relevant_items:
        key = (item.listing.make, item.listing.model)
        row = model_summary_data.setdefault(key, {"prices": [], "ages": [], "current_ids": set(), "previous_ids": set()})
        row["previous_ids"].add(item.listing_id)
    model_summary = []
    for (make, model), row in model_summary_data.items():
        disappeared_count = len(row["previous_ids"] - current_relevant_ids) if comparison_snapshot else None
        previous_count = len(row["previous_ids"])
        model_summary.append({
            "make": make, "model": model, "current_count": len(row["current_ids"]),
            "previous_count": previous_count,
            "average_ad_age": Decimal(sum(row["ages"])) / Decimal(len(row["ages"])) if row["ages"] else None,
            "median_price": Decimal(str(median(row["prices"]))) if row["prices"] else None,
            "new_count": len((row["current_ids"] - previous_relevant_ids)) if comparison_snapshot else None,
            "disappeared_count": disappeared_count,
            "apparent_turnover": (
                Decimal(disappeared_count) * Decimal("100") / Decimal(previous_count)
                if disappeared_count is not None and previous_count else None
            ),
        })
    model_summary.sort(key=lambda row: (-row["current_count"], row["make"].casefold(), row["model"].casefold()))
    snapshot_history = []
    for index, snapshot in enumerate(snapshots):
        items = list(snapshot.inventory_items.filter(listing_id__in=relevant_membership_ids).select_related("listing"))
        prices = [item.asking_price for item in items]
        ages = []
        first_seen = dict(dealer.listing_memberships.filter(listing_id__in=[item.listing_id for item in items]).values_list("listing_id", "first_seen_at"))
        for item in items:
            start = item.listing.published_at or timezone.localdate(first_seen[item.listing_id])
            ages.append(max((timezone.localdate(snapshot.observed_at) - start).days, 0))
        older = snapshots[index + 1] if index + 1 < len(snapshots) else None
        current_ids = {item.listing_id for item in items}
        older_ids = set(older.inventory_items.filter(listing_id__in=relevant_membership_ids).values_list("listing_id", flat=True)) if older else set()
        snapshot_history.append({
            "snapshot": snapshot, "relevant_count": len(items),
            "median_price": Decimal(str(median(prices))) if prices else None,
            "average_ad_age": Decimal(sum(ages)) / Decimal(len(ages)) if ages else None,
            "new_count": len(current_ids - older_ids) if older else None,
            "disappeared_count": len(older_ids - current_ids) if older else None,
            "items": items if selected_snapshot and selected_snapshot.pk == snapshot.pk else [],
        })
    selected_snapshot_detail = next((row for row in snapshot_history if selected_snapshot and row["snapshot"].pk == selected_snapshot.pk), None)
    period_summaries = []
    current_history = snapshot_history[0] if snapshot_history else None
    current_date = timezone.localdate(latest.observed_at) if latest else None
    for days in (30, 60, 90):
        target_date = current_date - timedelta(days=days) if current_date else None
        baseline = next(
            (row for row in snapshot_history if timezone.localdate(row["snapshot"].observed_at) <= target_date),
            None,
        ) if target_date else None
        period_summaries.append({
            "days": days, "baseline": baseline,
            "inventory_change": current_history["relevant_count"] - baseline["relevant_count"] if current_history and baseline else None,
            "median_change": current_history["median_price"] - baseline["median_price"] if current_history and baseline and current_history["median_price"] is not None and baseline["median_price"] is not None else None,
            "ad_age_change": current_history["average_ad_age"] - baseline["average_ad_age"] if current_history and baseline and current_history["average_ad_age"] is not None and baseline["average_ad_age"] is not None else None,
        })
    return render(request, "vehicles/dealer_detail.html", {
        "dealer": dealer, "latest": latest, "snapshots": snapshots,
        "inventory_page": inventory_page, "selected_sort": selected_sort,
        "page_size": page_size, "querystring": query_parameters.urlencode(),
        "recently_disappeared": recently_disappeared,
        "recently_added": recently_added, "replenishment": replenishment,
        "visibility": visibility,
        "excluded_count": excluded_count, "description_flagged_count": description_flagged_count,
        "intelligence_summary": intelligence_summary,
        "relevant_listing_ids": relevant_listing_ids,
        "previous_snapshot": comparison_snapshot, "snapshot_changes": snapshot_changes,
        "comparison_snapshot": comparison_snapshot, "comparison_options": comparison_options,
        "new_listing_ids": new_listing_ids, "reduced_listing_ids": reduced_listing_ids,
        "increased_listing_ids": increased_listing_ids,
        "top_models": top_models, "price_brackets": price_brackets,
        "comparison_new_vehicles": Listing.objects.filter(pk__in=new_listing_ids),
        "comparison_disappeared_items": comparison_snapshot.inventory_items.filter(
            listing_id__in=disappeared_listing_ids,
        ).select_related("listing") if comparison_snapshot else [],
        "stock_age_buckets": stock_age_buckets, "oldest_models": oldest_models,
        "slow_stock_groups": slow_stock_groups,
        "snapshot_history": snapshot_history, "selected_snapshot_detail": selected_snapshot_detail,
        "model_summary": model_summary,
        "period_summaries": period_summaries,
    })
