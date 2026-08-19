"""Reusable queries and calculations for marketplace comparison."""

from dataclasses import dataclass
from decimal import Decimal
from statistics import median
import re

from django.db.models import DateTimeField, DecimalField, IntegerField, OuterRef, QuerySet, Subquery
from django.utils import timezone

from vehicles.models import Listing, ListingSnapshot, MarketProfile


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
    average_days_on_market: Decimal | None
    publication_date_count: int


@dataclass(frozen=True)
class ProfileMarketSummary:
    count: int
    minimum_price: Decimal
    maximum_price: Decimal
    average_price: Decimal
    median_price: Decimal
    price_percentile_25: Decimal
    price_percentile_75: Decimal
    minimum_mileage: int
    maximum_mileage: int
    median_mileage: Decimal
    average_days_on_market: Decimal | None
    publication_date_count: int


@dataclass(frozen=True)
class RankedComparable:
    listing: Listing
    similarity_score: Decimal
    year_difference: int
    mileage_difference: int
    power_difference: int | None
    matching_fields: tuple[str, ...]
    mismatches: tuple[str, ...]


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
        latest_mileage=Subquery(
            latest.values("mileage")[:1],
            output_field=IntegerField(),
        ),
    )


def listings_for_profile(profile: MarketProfile) -> QuerySet[Listing]:
    """Return current active advertisements matching a saved profile."""
    queryset = with_latest_snapshot(
        Listing.objects.filter(
            source=profile.source,
            status=Listing.Status.ACTIVE,
            make__iexact=profile.make,
            model__iexact=profile.model,
        )
    ).filter(latest_price__isnull=False)
    if profile.generation:
        queryset = queryset.filter(generation__iexact=profile.generation)
    if profile.facelift_status:
        queryset = queryset.filter(facelift_status=profile.facelift_status)
    if profile.engine_family:
        queryset = queryset.filter(engine_family__iexact=profile.engine_family)
    if profile.minimum_year is not None:
        queryset = queryset.filter(year__gte=profile.minimum_year)
    if profile.maximum_year is not None:
        queryset = queryset.filter(year__lte=profile.maximum_year)
    if profile.minimum_mileage is not None:
        queryset = queryset.filter(latest_mileage__gte=profile.minimum_mileage)
    if profile.maximum_mileage is not None:
        queryset = queryset.filter(latest_mileage__lte=profile.maximum_mileage)
    if profile.fuel:
        queryset = queryset.filter(fuel__iexact=profile.fuel)
    if profile.transmission:
        queryset = queryset.filter(transmission_category__iexact=profile.transmission)
    if profile.minimum_power_kw is not None:
        queryset = queryset.filter(power_kw__gte=profile.minimum_power_kw)
    if profile.maximum_power_kw is not None:
        queryset = queryset.filter(power_kw__lte=profile.maximum_power_kw)
    return queryset


def listing_matches_profile(listing: Listing, profile: MarketProfile) -> bool:
    """Match an annotated listing in memory for profile-list counts."""
    mileage = getattr(listing, "latest_mileage", None)
    return (
        listing.status == Listing.Status.ACTIVE
        and listing.source == profile.source
        and listing.make.casefold() == profile.make.casefold()
        and listing.model.casefold() == profile.model.casefold()
        and (not profile.generation or listing.generation.casefold() == profile.generation.casefold())
        and (not profile.facelift_status or listing.facelift_status == profile.facelift_status)
        and (not profile.engine_family or listing.engine_family.casefold() == profile.engine_family.casefold())
        and (profile.minimum_year is None or listing.year >= profile.minimum_year)
        and (profile.maximum_year is None or listing.year <= profile.maximum_year)
        and getattr(listing, "latest_price", None) is not None
        and mileage is not None
        and (profile.minimum_mileage is None or mileage >= profile.minimum_mileage)
        and (profile.maximum_mileage is None or mileage <= profile.maximum_mileage)
        and (not profile.fuel or listing.fuel.casefold() == profile.fuel.casefold())
        and (
            not profile.transmission
            or listing.transmission_category.casefold() == profile.transmission.casefold()
        )
        and (profile.minimum_power_kw is None or (listing.power_kw is not None and listing.power_kw >= profile.minimum_power_kw))
        and (profile.maximum_power_kw is None or (listing.power_kw is not None and listing.power_kw <= profile.maximum_power_kw))
    )


def percentile(values, proportion: Decimal) -> Decimal:
    """Return a linearly interpolated percentile using the (n - 1) rank."""
    ordered = sorted(Decimal(value) for value in values)
    if not ordered:
        raise ValueError("A percentile requires at least one value.")
    rank = Decimal(len(ordered) - 1) * proportion
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - Decimal(lower)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _publication_age_summary(items, as_of=None):
    as_of = as_of or timezone.localdate()
    ages = [max((as_of - item.published_at).days, 0) for item in items if item.published_at]
    if not ages:
        return None, 0
    return Decimal(sum(ages)) / Decimal(len(ages)), len(ages)


def summarize_profile_market(listings, *, as_of=None) -> ProfileMarketSummary | None:
    items = list(listings)
    if not items:
        return None
    prices = [item.latest_price for item in items]
    mileages = [item.latest_mileage for item in items]
    average_days, publication_count = _publication_age_summary(items, as_of)
    return ProfileMarketSummary(
        count=len(items),
        minimum_price=min(prices),
        maximum_price=max(prices),
        average_price=sum(prices, Decimal("0")) / Decimal(len(prices)),
        median_price=percentile(prices, Decimal("0.5")),
        price_percentile_25=percentile(prices, Decimal("0.25")),
        price_percentile_75=percentile(prices, Decimal("0.75")),
        minimum_mileage=min(mileages),
        maximum_mileage=max(mileages),
        median_mileage=percentile(mileages, Decimal("0.5")),
        average_days_on_market=average_days,
        publication_date_count=publication_count,
    )


def _trim_tokens(title: str) -> set[str]:
    ignored = {"peugeot", "citroen", "diesel", "dizel", "automatic", "automatski"}
    return {token for token in re.findall(r"[a-z0-9]+", title.casefold()) if len(token) > 1 and token not in ignored}


def rank_profile_comparables(target: Listing, profile: MarketProfile, listings) -> list[RankedComparable]:
    """Rank strict-profile matches by technical similarity, never by asking price."""
    candidates = [listing for listing in listings if listing.pk != target.pk]
    newest_observed = max(
        (listing.latest_observed_at for listing in candidates if listing.latest_observed_at),
        default=target.latest_observed_at,
    )
    target_tokens = _trim_tokens(target.title)
    ranked = []
    required_fields = (
        ("generation", target.generation),
        ("facelift", target.facelift_status if target.facelift_status != Listing.FaceliftStatus.UNKNOWN else ""),
        ("engine", target.engine_family),
        ("transmission", target.transmission_category if target.transmission_category != Listing.TransmissionCategory.UNKNOWN else ""),
        ("fuel", target.fuel),
    )
    for candidate in candidates:
        if candidate.make.casefold() != target.make.casefold() or candidate.model.casefold() != target.model.casefold():
            continue
        matching = ["make", "model"]
        mismatches = []
        incompatible = False
        for label, target_value in required_fields:
            candidate_value = {
                "facelift": candidate.facelift_status,
                "engine": candidate.engine_family,
                "transmission": candidate.transmission_category,
            }.get(label, getattr(candidate, label, ""))
            if target_value:
                if not candidate_value or str(candidate_value).casefold() in {"", "unknown"}:
                    mismatches.append(f"{label} unknown")
                elif str(candidate_value).casefold() != str(target_value).casefold():
                    incompatible = True
                    break
                else:
                    matching.append(label)
        if incompatible:
            continue
        year_difference = abs(candidate.year - target.year)
        mileage_difference = abs(candidate.latest_mileage - target.latest_mileage)
        power_difference = None
        if candidate.power_kw is not None and target.power_kw is not None:
            power_difference = abs(candidate.power_kw - target.power_kw)
            matching.append("power") if power_difference == 0 else mismatches.append("power differs")
        else:
            mismatches.append("power unknown")
        token_overlap = len(target_tokens & _trim_tokens(candidate.title))
        stale_days = 0
        if newest_observed and candidate.latest_observed_at:
            stale_days = max((newest_observed - candidate.latest_observed_at).days, 0)
        score = (
            Decimal("100")
            - Decimal(year_difference * 12)
            - (Decimal(mileage_difference) / Decimal("10000") * Decimal("2"))
            - (Decimal(power_difference or 0) * Decimal("0.4"))
            - Decimal(len(mismatches) * 3)
            + Decimal(min(token_overlap, 5))
            - min(Decimal(stale_days) / Decimal("30") * Decimal("0.5"), Decimal("5"))
        )
        ranked.append(RankedComparable(
            candidate, score.quantize(Decimal("0.01")), year_difference,
            mileage_difference, power_difference, tuple(matching), tuple(mismatches),
        ))
    return sorted(ranked, key=lambda item: (-item.similarity_score, item.year_difference, item.mileage_difference, item.listing.pk))


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


def summarize_market(comparables, selected_price: Decimal | None, *, as_of=None) -> MarketSummary | None:
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
    average_days, publication_count = _publication_age_summary(comparables, as_of)

    return MarketSummary(
        count=len(prices),
        minimum=min(prices),
        maximum=max(prices),
        average=average_price,
        median=median_price,
        selected_difference=difference,
        selected_difference_percentage=percentage,
        average_days_on_market=average_days,
        publication_date_count=publication_count,
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
