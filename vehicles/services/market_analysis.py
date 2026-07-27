"""Reusable queries and calculations for marketplace comparison."""

from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from django.db.models import DateTimeField, DecimalField, OuterRef, QuerySet, Subquery

from vehicles.models import Listing, ListingSnapshot


DEFAULT_YEAR_TOLERANCE = 1
MAX_YEAR_TOLERANCE = 5
DEFAULT_MILEAGE_TOLERANCE = 30_000
MAX_MILEAGE_TOLERANCE = 200_000


@dataclass(frozen=True)
class MarketSummary:
    count: int
    minimum: Decimal
    maximum: Decimal
    average: Decimal
    median: Decimal
    selected_difference: Decimal | None
    selected_difference_percentage: Decimal | None


def with_latest_snapshot(queryset: QuerySet[Listing]) -> QuerySet[Listing]:
    """Annotate listings with price and time from their latest snapshot."""
    latest = ListingSnapshot.objects.filter(listing=OuterRef("pk")).order_by(
        "-observed_at", "-pk"
    )
    return queryset.annotate(
        latest_price=Subquery(
            latest.values("asking_price")[:1],
            output_field=DecimalField(max_digits=12, decimal_places=2),
        ),
        latest_observed_at=Subquery(
            latest.values("observed_at")[:1],
            output_field=DateTimeField(),
        ),
    )


def comparable_listings(
    selected: Listing,
    *,
    year_tolerance: int = DEFAULT_YEAR_TOLERANCE,
    mileage_tolerance: int = DEFAULT_MILEAGE_TOLERANCE,
    same_transmission: bool = False,
) -> QuerySet[Listing]:
    """Return active listings matching the documented comparison rules."""
    queryset = Listing.objects.filter(
        status=Listing.Status.ACTIVE,
        make__iexact=selected.make,
        model__iexact=selected.model,
        fuel__iexact=selected.fuel,
        year__gte=max(0, selected.year - year_tolerance),
        year__lte=selected.year + year_tolerance,
        mileage__gte=max(0, selected.mileage - mileage_tolerance),
        mileage__lte=selected.mileage + mileage_tolerance,
    ).exclude(pk=selected.pk)
    if same_transmission:
        queryset = queryset.filter(transmission__iexact=selected.transmission)
    return with_latest_snapshot(queryset).filter(latest_price__isnull=False)


def summarize_market(comparables, selected_price: Decimal | None) -> MarketSummary | None:
    """Calculate asking-price statistics from latest annotated prices."""
    prices = [item.latest_price for item in comparables if item.latest_price is not None]
    if not prices:
        return None

    median_price = median(prices)
    average_price = sum(prices, Decimal("0")) / Decimal(len(prices))
    difference = selected_price - median_price if selected_price is not None else None
    percentage = None
    if difference is not None and median_price:
        percentage = difference / median_price * Decimal("100")

    return MarketSummary(
        count=len(prices),
        minimum=min(prices),
        maximum=max(prices),
        average=average_price,
        median=median_price,
        selected_difference=difference,
        selected_difference_percentage=percentage,
    )


def price_history(listing: Listing) -> list[dict]:
    """Return chronological snapshots with changes from the preceding price."""
    snapshots = listing.snapshots.select_related("import_run").order_by("observed_at", "pk")
    history = []
    previous_price = None
    for snapshot in snapshots:
        difference = None
        if previous_price is not None:
            difference = snapshot.asking_price - previous_price
        history.append({"snapshot": snapshot, "price_difference": difference})
        previous_price = snapshot.asking_price
    return history
