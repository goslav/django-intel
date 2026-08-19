"""Cross-dealer comparisons built exclusively from immutable dealer snapshots."""

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from statistics import median

from django.utils import timezone

from vehicles.models import Dealer, DealerInventorySnapshot
from vehicles.services.vehicle_classification import classify_text


@dataclass(frozen=True)
class ComparisonFilters:
    make: str = ""
    model: str = ""
    dealer_ids: tuple[int, ...] = ()
    minimum_year: int | None = None
    maximum_year: int | None = None
    fuel: str = ""
    transmission: str = ""


def _classification(listing):
    return classify_text(
        make=listing.make, model=listing.model, title=listing.title,
        transmission=listing.transmission,
    )


def listing_matches(listing, filters: ComparisonFilters):
    classification = _classification(listing)
    if filters.make and classification.make.casefold() != filters.make.casefold():
        return False
    if filters.model and classification.model.casefold() != filters.model.casefold():
        return False
    if filters.minimum_year is not None and listing.year < filters.minimum_year:
        return False
    if filters.maximum_year is not None and listing.year > filters.maximum_year:
        return False
    if filters.fuel and listing.fuel.casefold() != filters.fuel.casefold():
        return False
    if filters.transmission and classification.transmission_category != filters.transmission:
        return False
    return True


def _relevant_ids(dealer):
    return set(dealer.listing_memberships.filter(is_relevant=True).values_list("listing_id", flat=True))


def _matching_items(snapshot, relevant_ids, filters):
    if snapshot is None:
        return []
    return [
        item for item in snapshot.inventory_items.filter(
            listing_id__in=relevant_ids,
        ).select_related("listing")
        if listing_matches(item.listing, filters)
    ]


def _age(item, membership_dates, as_of):
    start = item.listing.published_at or timezone.localdate(membership_dates[item.listing_id])
    return max((as_of - start).days, 0)


def _window_changes(dealer, latest, baseline, relevant_ids, filters, cutoff):
    snapshots = list(dealer.inventory_snapshots.filter(
        status=DealerInventorySnapshot.Status.COMPLETED,
        observed_at__gt=cutoff,
        observed_at__lte=latest.observed_at,
    ).order_by("observed_at", "pk"))
    if baseline:
        snapshots.insert(0, baseline)
    captures = []
    latest_items = {}
    for snapshot in snapshots:
        items = _matching_items(snapshot, relevant_ids, filters)
        capture = {item.listing_id: item for item in items}
        captures.append(capture)
        latest_items.update(capture)
    new_ids, disappeared_ids, reduced_ids = set(), set(), set()
    for previous, current in zip(captures, captures[1:]):
        previous_ids, current_ids = set(previous), set(current)
        new_ids.update(current_ids - previous_ids)
        disappeared_ids.update(previous_ids - current_ids)
        reduced_ids.update(
            listing_id for listing_id in previous_ids & current_ids
            if current[listing_id].asking_price < previous[listing_id].asking_price
        )
    return new_ids, disappeared_ids, reduced_ids, latest_items


def comparison_rows(filters: ComparisonFilters):
    dealers = Dealer.objects.filter(is_active=True).order_by("name", "pk")
    if filters.dealer_ids:
        dealers = dealers.filter(pk__in=filters.dealer_ids)
    rows = []
    for dealer in dealers:
        latest = dealer.inventory_snapshots.filter(
            status=DealerInventorySnapshot.Status.COMPLETED,
        ).order_by("-observed_at", "-pk").first()
        if latest is None:
            continue
        relevant_ids = _relevant_ids(dealer)
        current = _matching_items(latest, relevant_ids, filters)
        if not current:
            continue
        cutoff = latest.observed_at - timedelta(days=30)
        baseline = dealer.inventory_snapshots.filter(
            status=DealerInventorySnapshot.Status.COMPLETED,
            observed_at__lte=cutoff,
        ).order_by("-observed_at", "-pk").first()
        opening = _matching_items(baseline, relevant_ids, filters)
        current_prices = {item.listing_id: item.asking_price for item in current}
        new_ids, disappeared_ids, reduced_ids, window_items = _window_changes(
            dealer, latest, baseline, relevant_ids, filters, cutoff,
        )
        as_of = timezone.localdate(latest.observed_at)
        membership_dates = dict(dealer.listing_memberships.filter(
            listing_id__in=relevant_ids,
        ).values_list("listing_id", "first_seen_at"))
        rows.append({
            "dealer": dealer,
            "latest": latest,
            "baseline": baseline,
            "stock_count": len(current),
            "median_price": Decimal(str(median(current_prices.values()))),
            "median_mileage": Decimal(str(median(item.mileage for item in current))),
            "median_ad_age": Decimal(str(median(_age(item, membership_dates, as_of) for item in current))),
            "new_count": len(new_ids),
            "disappeared_count": len(disappeared_ids),
            "reduction_count": len(reduced_ids),
            "opening_stock_count": len(opening) if baseline else None,
            "apparent_turnover": (
                Decimal(len(disappeared_ids)) * Decimal("100") / Decimal(len(opening))
                if baseline and opening else None
            ),
            "change_new_ids": new_ids,
            "change_disappeared_ids": disappeared_ids,
            "change_reduced_ids": reduced_ids,
            "window_items": window_items,
        })
    return rows


def model_frequency_rows(filters: ComparisonFilters, *, limit=20):
    """Rank normalized make/model pairs in current relevant dealer inventory."""
    dealers = Dealer.objects.filter(is_active=True).order_by("name", "pk")
    if filters.dealer_ids:
        dealers = dealers.filter(pk__in=filters.dealer_ids)
    grouped = {}
    for dealer in dealers:
        latest = dealer.inventory_snapshots.filter(
            status=DealerInventorySnapshot.Status.COMPLETED,
        ).order_by("-observed_at", "-pk").first()
        if latest is None:
            continue
        relevant_ids = _relevant_ids(dealer)
        membership_dates = dict(dealer.listing_memberships.filter(
            listing_id__in=relevant_ids,
        ).values_list("listing_id", "first_seen_at"))
        current_items = _matching_items(latest, relevant_ids, filters)
        for item in current_items:
            classification = _classification(item.listing)
            key = (classification.make, classification.model)
            row = grouped.setdefault(key, {
                "make": classification.make, "model": classification.model,
                "stock_count": 0, "dealer_ids": set(), "ad_days": [],
                "opening_stock_count": 0, "disappeared_count": 0,
            })
            row["stock_count"] += 1
            row["dealer_ids"].add(dealer.pk)
            start = item.listing.published_at or timezone.localdate(membership_dates[item.listing_id])
            row["ad_days"].append(max((timezone.localdate(latest.observed_at) - start).days, 0))
        cutoff = latest.observed_at - timedelta(days=30)
        baseline = dealer.inventory_snapshots.filter(
            status=DealerInventorySnapshot.Status.COMPLETED,
            observed_at__lte=cutoff,
        ).order_by("-observed_at", "-pk").first()
        opening_items = _matching_items(baseline, relevant_ids, filters)
        for item in opening_items:
            classification = _classification(item.listing)
            key = (classification.make, classification.model)
            if key in grouped:
                grouped[key]["opening_stock_count"] += 1
        if baseline:
            _, disappeared_ids, _, window_items = _window_changes(
                dealer, latest, baseline, relevant_ids, filters, cutoff,
            )
            for listing_id in disappeared_ids:
                item = window_items[listing_id]
                classification = _classification(item.listing)
                key = (classification.make, classification.model)
                if key in grouped:
                    grouped[key]["disappeared_count"] += 1
    rows = [{
        "make": row["make"], "model": row["model"],
        "stock_count": row["stock_count"], "dealer_count": len(row["dealer_ids"]),
        "median_ad_days": Decimal(str(median(row["ad_days"]))),
        "opening_stock_count": row["opening_stock_count"],
        "disappeared_count": row["disappeared_count"],
        "apparent_turnover": (
            Decimal(row["disappeared_count"]) * Decimal("100") / Decimal(row["opening_stock_count"])
            if row["opening_stock_count"] else None
        ),
    } for row in grouped.values()]
    rows.sort(key=lambda row: (
        -row["stock_count"], -row["dealer_count"],
        row["make"].casefold(), row["model"].casefold(),
    ))
    return rows[:limit]


def dealer_drilldown(dealer, filters: ComparisonFilters):
    row = next((row for row in comparison_rows(
        replace(filters, dealer_ids=(dealer.pk,))
    )), None)
    if row is None:
        return None, []
    relevant_ids = _relevant_ids(dealer)
    current = _matching_items(row["latest"], relevant_ids, filters)
    current_by_id = {item.listing_id: item for item in current}
    disappeared_ids = row["change_disappeared_ids"] - set(current_by_id)
    all_ids = set(current_by_id) | disappeared_ids
    snapshot_start = row["baseline"].observed_at if row["baseline"] else row["latest"].observed_at - timedelta(days=30)
    price_histories = {}
    for snapshot in dealer.inventory_snapshots.filter(
        status=DealerInventorySnapshot.Status.COMPLETED,
        observed_at__gte=snapshot_start,
        observed_at__lte=row["latest"].observed_at,
    ).order_by("observed_at", "pk"):
        for item in snapshot.inventory_items.filter(listing_id__in=all_ids):
            price_histories.setdefault(item.listing_id, []).append(item.asking_price)
    as_of = timezone.localdate(row["latest"].observed_at)
    membership_dates = dict(dealer.listing_memberships.filter(
        listing_id__in=all_ids,
    ).values_list("listing_id", "first_seen_at"))
    vehicles = []
    for listing_id in all_ids:
        item = current_by_id.get(listing_id) or row["window_items"][listing_id]
        prices = price_histories.get(listing_id, [])
        reductions = [old - new for old, new in zip(prices, prices[1:]) if new < old]
        vehicles.append({
            "listing": item.listing,
            "mileage": item.mileage,
            "asking_price": item.asking_price,
            "ad_age": _age(item, membership_dates, as_of),
            "is_disappeared": listing_id not in current_by_id,
            "price_reduction_count": len(reductions),
            "total_price_reduction": sum(reductions, Decimal("0")),
        })
    vehicles.sort(key=lambda vehicle: (vehicle["is_disappeared"], vehicle["listing"].title.casefold(), vehicle["listing"].pk))
    return row, vehicles


def filter_options():
    latest_ids = []
    for dealer in Dealer.objects.filter(is_active=True):
        latest = dealer.inventory_snapshots.filter(
            status=DealerInventorySnapshot.Status.COMPLETED,
        ).order_by("-observed_at", "-pk").first()
        if latest:
            latest_ids.append(latest.pk)
    items = list(
        DealerInventorySnapshot.objects.filter(pk__in=latest_ids)
        .values_list("inventory_items__listing", flat=True)
    )
    from vehicles.models import Listing
    listings = Listing.objects.filter(pk__in=items)
    makes, models, fuels = set(), set(), set()
    for listing in listings:
        classification = _classification(listing)
        makes.add(classification.make)
        models.add(classification.model)
        fuels.add(listing.fuel)
    return {
        "makes": sorted(makes, key=str.casefold),
        "models": sorted(models, key=str.casefold),
        "fuels": sorted(fuels, key=str.casefold),
        "dealers": Dealer.objects.filter(is_active=True).order_by("name", "pk"),
    }
