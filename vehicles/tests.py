import csv
from datetime import date
from decimal import Decimal
from pathlib import Path
from io import StringIO
from tempfile import TemporaryDirectory

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from vehicles.models import ImportRun, Listing, ListingSnapshot, MarketProfile, Vehicle
from vehicles.services.import_listings import CSV_COLUMNS, import_listings
from vehicles.services.market_analysis import (
    comparable_listings,
    listings_for_profile,
    summarize_market,
    summarize_profile_market,
    rank_profile_comparables,
    with_latest_snapshot,
)
from vehicles.services.vehicle_classification import classify_listing, classify_text
from vehicles.templatetags.vehicle_links import price_sr


class ListingImportTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.csv_number = 0

    def row(self, external_id="listing-1", observed_at="2026-07-01T10:00:00Z", **changes):
        row = {
            "source": "example",
            "external_id": external_id,
            "source_url": f"https://example.test/{external_id}",
            "title": "Volkswagen Golf 1.6 TDI",
            "make": "Volkswagen",
            "model": "Golf",
            "year": "2018",
            "mileage": "125000",
            "fuel": "diesel",
            "transmission": "manual",
            "asking_price": "12900.00",
            "observed_at": observed_at,
        }
        row.update(changes)
        return row

    def write_csv(self, rows, columns=CSV_COLUMNS):
        self.csv_number += 1
        path = Path(self.temporary_directory.name) / f"import-{self.csv_number}.csv"
        with path.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def import_rows(self, rows, *, source="example", full=False):
        return import_listings(self.write_csv(rows), source, is_full_snapshot=full)

    def test_first_import_creates_listings_and_snapshots(self):
        result = self.import_rows([self.row(), self.row("listing-2")])

        self.assertEqual(Listing.objects.count(), 2)
        self.assertEqual(ListingSnapshot.objects.count(), 2)
        self.assertEqual(result.import_run.status, ImportRun.Status.COMPLETED)
        self.assertEqual(result.import_run.created_count, 2)
        self.assertEqual(result.snapshots_created, 2)

    def test_reimport_does_not_duplicate_listings(self):
        self.import_rows([self.row()])
        self.import_rows([self.row(observed_at="2026-07-02T10:00:00Z")])

        self.assertEqual(Listing.objects.count(), 1)

    def test_reimporting_same_snapshot_does_not_duplicate_snapshots(self):
        self.import_rows([self.row()])
        result = self.import_rows([self.row()])

        self.assertEqual(ImportRun.objects.count(), 2)
        self.assertEqual(ListingSnapshot.objects.count(), 1)
        self.assertEqual(result.snapshots_created, 0)

    def test_later_price_change_preserves_history(self):
        self.import_rows([self.row()])
        self.import_rows(
            [self.row(observed_at="2026-07-02T10:00:00Z", asking_price="11900.00")]
        )

        prices = list(
            ListingSnapshot.objects.order_by("observed_at").values_list("asking_price", flat=True)
        )
        self.assertEqual(prices, [Decimal("12900.00"), Decimal("11900.00")])

    def test_successful_full_snapshot_marks_missing_listing_removed(self):
        self.import_rows([self.row(), self.row("listing-2")])
        self.import_rows(
            [self.row(observed_at="2026-07-02T10:00:00Z")],
            full=True,
        )

        self.assertEqual(Listing.objects.get(external_id="listing-2").status, Listing.Status.REMOVED)

    def test_partial_snapshot_does_not_mark_missing_listing_removed(self):
        self.import_rows([self.row(), self.row("listing-2")])
        self.import_rows([self.row(observed_at="2026-07-02T10:00:00Z")])

        self.assertEqual(Listing.objects.get(external_id="listing-2").status, Listing.Status.ACTIVE)

    def test_failed_import_does_not_mark_missing_listing_removed(self):
        self.import_rows([self.row(), self.row("listing-2")])
        result = self.import_rows(
            [
                self.row(observed_at="2026-07-02T10:00:00Z"),
                self.row("broken", observed_at="not-a-date"),
            ],
            full=True,
        )

        self.assertEqual(result.import_run.status, ImportRun.Status.FAILED)
        self.assertEqual(Listing.objects.get(external_id="listing-2").status, Listing.Status.ACTIVE)

    def test_full_snapshot_does_not_affect_another_source(self):
        self.import_rows([self.row()])
        other = self.row(external_id="other-1", source="other")
        self.import_rows([other], source="other")

        self.import_rows([], full=True)

        self.assertEqual(Listing.objects.get(source="example").status, Listing.Status.REMOVED)
        self.assertEqual(Listing.objects.get(source="other").status, Listing.Status.ACTIVE)

    def test_invalid_rows_report_row_number_and_reason(self):
        result = self.import_rows([self.row(year="not-a-year")])

        self.assertEqual(result.import_run.status, ImportRun.Status.FAILED)
        self.assertEqual(result.import_run.error_count, 1)
        self.assertIn("Row 2: year must be an integer", result.import_run.error_summary)
        self.assertEqual(Listing.objects.count(), 0)


class ExistingVehicleTests(TestCase):
    def test_homepage_shows_navigation_and_counts(self):
        Vehicle.objects.create(
            make="Skoda",
            model="Octavia",
            year=2019,
            mileage=110000,
            fuel_type="diesel",
            purchase_price=Decimal("10000.00"),
            expected_sale_price=Decimal("12000.00"),
        )

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Serbian used-car market intelligence")
        self.assertContains(response, "1 saved")
        self.assertContains(response, reverse("market:listing_list"))

    def test_vehicle_model_profit_and_list_view_still_work(self):
        vehicle = Vehicle.objects.create(
            make="Skoda",
            model="Octavia",
            year=2019,
            mileage=110000,
            fuel_type="diesel",
            transmission="manual",
            purchase_price=Decimal("10000.00"),
            expected_sale_price=Decimal("12000.00"),
        )

        self.assertEqual(vehicle.estimated_profit(), Decimal("2000.00"))
        response = self.client.get(reverse("vehicles:vehicle_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Skoda")


class MarketBrowserTests(TestCase):
    def create_listing(
        self,
        external_id,
        *,
        make="Volkswagen",
        model="Golf",
        year=2018,
        mileage=120000,
        fuel="diesel",
        transmission="manual",
        price="12000.00",
        observed_at="2026-07-01T10:00:00Z",
        status=Listing.Status.ACTIVE,
        source="example",
        source_url="",
        published_at=None,
    ):
        observed = parse_datetime(observed_at)
        listing = Listing.objects.create(
            source=source,
            external_id=external_id,
            source_url=source_url,
            published_at=published_at,
            title=f"{make} {model} {external_id}",
            make=make,
            model=model,
            year=year,
            mileage=mileage,
            fuel=fuel,
            transmission=transmission,
            first_seen_at=observed,
            last_seen_at=observed,
            status=status,
        )
        self.add_snapshot(listing, price, observed_at, mileage)
        return listing

    def add_snapshot(self, listing, price, observed_at, mileage=None):
        observed = parse_datetime(observed_at)
        run = ImportRun.objects.create(
            source=listing.source,
            snapshot_at=observed,
            status=ImportRun.Status.COMPLETED,
            completed_at=observed,
        )
        return ListingSnapshot.objects.create(
            listing=listing,
            import_run=run,
            observed_at=observed,
            asking_price=Decimal(price),
            mileage=listing.mileage if mileage is None else mileage,
        )

    def test_browser_shows_active_and_excludes_removed_listings(self):
        self.create_listing("active")
        self.create_listing("removed", status=Listing.Status.REMOVED)

        response = self.client.get(reverse("market:listing_list"))

        self.assertContains(response, "Volkswagen Golf active")
        self.assertNotContains(response, "Volkswagen Golf removed")

    def test_browser_filters_work(self):
        self.create_listing("golf", price="12000", mileage=100000)
        self.create_listing("polo", model="Polo", price="9000", mileage=80000)

        response = self.client.get(
            reverse("market:listing_list"),
            {"model": "Golf", "min_price": "11000", "max_mileage": "110000"},
        )

        self.assertContains(response, "Volkswagen Golf golf")
        self.assertNotContains(response, "Volkswagen Polo polo")

    def test_comparable_search_accepts_vehicle_definition_and_summarizes(self):
        self.create_listing("match", year=2020, price="12000", mileage=100000)
        self.create_listing("other-year", year=2021, price="14000")
        response = self.client.get(reverse("market:listing_list"), {
            "make": "volkswagen", "model": "golf", "fuel": "diesel",
            "transmission": "manual", "year": "2020",
        })
        self.assertEqual(response.context["page"].paginator.count, 2)
        self.assertEqual(
            list(response.context["page"].object_list),
            [Listing.objects.get(external_id="match"), Listing.objects.get(external_id="other-year")],
        )
        self.assertEqual(response.context["comparable_summary"].median_price, Decimal("13000"))
        self.assertContains(response, "Comparable market summary")

    def test_low_three_year_population_expands_to_two_years_each_side(self):
        for index in range(9):
            self.create_listing(
                f"narrow-{index}", year=2021 + (index % 3) - 1,
                price=str(10000 + index * 100),
            )
        outer = self.create_listing("outer", year=2019, price="15000")
        self.create_listing("too-far", year=2018, price="9000")

        response = self.client.get(
            reverse("market:listing_list"), {"make": "Volkswagen", "model": "Golf", "year": "2021"},
        )

        results = list(response.context["page"].object_list)
        self.assertEqual(response.context["year_window"]["minimum"], 2019)
        self.assertEqual(response.context["year_window"]["maximum"], 2023)
        self.assertTrue(response.context["year_window"]["expanded"])
        self.assertEqual(len(results), 10)
        self.assertEqual([item.year for item in results[:3]], [2021, 2021, 2021])
        self.assertIn(outer, results)
        self.assertNotContains(response, "Volkswagen Golf too-far")

    def test_browser_always_offers_canonical_fuel_categories(self):
        response = self.client.get(reverse("market:listing_list"))
        self.assertContains(response, '<option value="hybrid"')

    def test_prices_use_dot_thousands_separator_without_decimals(self):
        self.create_listing("formatted", price="19950.00")
        response = self.client.get(reverse("market:listing_list"))
        self.assertEqual(price_sr(Decimal("19950.00")), "19.950")
        self.assertContains(response, "€19.950")
        self.assertNotContains(response, "19950.00")

    def test_browser_offers_searchable_polovni_automobili_brand_catalog(self):
        response = self.client.get(reverse("market:listing_list"))
        self.assertContains(response, 'type="search" name="make" list="brand-options"')
        self.assertIn("Aston Martin", response.context["choices"]["make"])
        self.assertIn("Xiaomi", response.context["choices"]["make"])
        self.assertIn("\u0160koda", response.context["choices"]["make"])

    def test_selected_catalog_brand_filters_listings(self):
        self.create_listing("bmw", make="BMW", model="X3")
        self.create_listing("audi", make="Audi", model="Q5")
        response = self.client.get(reverse("market:listing_list"), {"make": "bmw"})
        self.assertContains(response, "BMW X3 bmw")
        self.assertNotContains(response, "Audi Q5 audi")

    def test_browser_sorts_by_latest_price(self):
        expensive = self.create_listing("expensive", price="15000")
        cheap = self.create_listing("cheap", price="9000")

        response = self.client.get(reverse("market:listing_list"), {"sort": "lowest_price"})

        self.assertEqual(list(response.context["page"].object_list), [cheap, expensive])

    def test_browser_sorts_by_publication_date_with_unknown_dates_last(self):
        older = self.create_listing("older", published_at=date(2026, 5, 1))
        newer = self.create_listing("newer", published_at=date(2026, 7, 1))
        unknown = self.create_listing("unknown")

        newest = self.client.get(
            reverse("market:listing_list"), {"sort": "newest_published"}
        )
        oldest = self.client.get(
            reverse("market:listing_list"), {"sort": "oldest_published"}
        )

        self.assertEqual(list(newest.context["page"].object_list), [newer, older, unknown])
        self.assertEqual(list(oldest.context["page"].object_list), [older, newer, unknown])

    def test_browser_paginates_results(self):
        for number in range(21):
            self.create_listing(f"listing-{number}")

        response = self.client.get(reverse("market:listing_list"), {"page": 2})

        self.assertEqual(response.context["page"].paginator.per_page, 20)
        self.assertEqual(len(response.context["page"].object_list), 1)

    def test_detail_page_loads(self):
        listing = self.create_listing("selected")

        response = self.client.get(reverse("market:listing_detail", args=(listing.pk,)))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Volkswagen Golf selected")

    def test_internal_and_valid_external_listing_links_are_distinct(self):
        listing = self.create_listing(
            "123", source="polovniautomobili",
            source_url="https://www.polovniautomobili.com/auto-oglasi/123/volkswagen-golf",
        )
        response = self.client.get(reverse("market:listing_list"))
        self.assertContains(response, reverse("market:listing_detail", args=(listing.pk,)))
        self.assertContains(response, "Open original advertisement")
        self.assertContains(response, 'target="_blank" rel="noopener noreferrer"')

    def test_comparables_respect_make_and_model_case_insensitively(self):
        selected = self.create_listing("selected")
        match = self.create_listing("match", make="volkswagen", model="golf")
        self.create_listing("wrong-make", make="Skoda")
        self.create_listing("wrong-model", model="Polo")

        result = list(comparable_listings(selected))

        self.assertEqual(result, [match])

    def test_comparables_respect_year_tolerance(self):
        selected = self.create_listing("selected", year=2018)
        near = self.create_listing("near", year=2020)
        self.create_listing("far", year=2021)

        self.assertEqual(list(comparable_listings(selected, year_tolerance=2)), [near])

    def test_comparables_respect_mileage_tolerance(self):
        selected = self.create_listing("selected", mileage=100000)
        near = self.create_listing("near", mileage=145000)
        self.create_listing("far", mileage=150001)

        self.assertEqual(
            list(comparable_listings(selected, mileage_tolerance=50000)),
            [near],
        )

    def test_optional_transmission_matching(self):
        selected = self.create_listing("selected", transmission="manual")
        manual = self.create_listing("manual", transmission="MANUAL")
        automatic = self.create_listing("automatic", transmission="automatic")

        without_matching = set(comparable_listings(selected))
        with_matching = list(comparable_listings(selected, same_transmission=True))

        self.assertEqual(without_matching, {manual, automatic})
        self.assertEqual(with_matching, [manual])

    def test_selected_listing_is_not_its_own_comparable(self):
        selected = self.create_listing("selected")

        self.assertNotIn(selected, comparable_listings(selected))

    def test_latest_snapshot_is_used_as_current_price(self):
        listing = self.create_listing("selected", price="14000")
        self.add_snapshot(listing, "11000", "2026-07-02T10:00:00Z")

        annotated = with_latest_snapshot(Listing.objects.filter(pk=listing.pk)).get()

        self.assertEqual(annotated.latest_price, Decimal("11000"))
        response = self.client.get(reverse("market:listing_list"))
        self.assertContains(response, "€11.000")

    def test_market_statistics_are_correct_for_even_price_count(self):
        selected = self.create_listing("selected", price="12500")
        comparables = [
            self.create_listing("one", price="10000"),
            self.create_listing("two", price="12000"),
            self.create_listing("three", price="14000"),
            self.create_listing("four", price="20000"),
        ]
        annotated = list(with_latest_snapshot(Listing.objects.filter(pk__in=[item.pk for item in comparables])))

        summary = summarize_market(annotated, Decimal("12500"))

        self.assertEqual(summary.minimum, Decimal("10000"))
        self.assertEqual(summary.maximum, Decimal("20000"))
        self.assertEqual(summary.average, Decimal("14000"))
        self.assertEqual(summary.median, Decimal("13000"))
        self.assertEqual(summary.selected_difference, Decimal("-500"))

    def test_detail_has_clear_no_comparables_state(self):
        selected = self.create_listing("selected")

        response = self.client.get(reverse("market:listing_detail", args=(selected.pk,)))

        self.assertIsNone(response.context["summary"])
        self.assertContains(response, "No comparable active listings")

    def test_price_history_is_chronological_and_shows_changes(self):
        listing = self.create_listing(
            "selected",
            price="13000",
            observed_at="2026-07-02T10:00:00Z",
        )
        self.add_snapshot(listing, "14000", "2026-07-01T10:00:00Z", 121000)
        self.add_snapshot(listing, "12500", "2026-07-03T10:00:00Z", 119000)

        response = self.client.get(reverse("market:listing_detail", args=(listing.pk,)))
        history = response.context["history"]

        self.assertEqual(
            [item["snapshot"].asking_price for item in history],
            [Decimal("14000"), Decimal("13000"), Decimal("12500")],
        )
        self.assertEqual(
            [item["price_difference"] for item in history],
            [None, Decimal("-1000"), Decimal("-500")],
        )


class MarketProfileTests(TestCase):
    def profile(self, **changes):
        values = {
            "name": "Peugeot 3008 segment", "source": "example", "make": "Peugeot",
            "model": "3008", "minimum_year": 2020, "maximum_year": 2023,
            "minimum_mileage": 60000, "maximum_mileage": 160000,
            "fuel": "diesel", "transmission": "automatic",
        }
        values.update(changes)
        return MarketProfile.objects.create(**values)

    def listing(self, external_id, *, price="20000", observed_at="2026-07-01T10:00:00Z", snapshots=True, **changes):
        snapshot_mileage = changes.pop("snapshot_mileage", None)
        values = {
            "source": "example", "title": f"Listing {external_id}", "make": "Peugeot",
            "model": "3008", "year": 2021, "mileage": 100000, "fuel": "diesel",
            "transmission": "automatic", "transmission_category": "automatic",
            "engine_family": "1.5 BlueHDi", "status": Listing.Status.ACTIVE,
        }
        values.update(changes)
        observed = parse_datetime(observed_at)
        listing = Listing.objects.create(
            external_id=external_id, first_seen_at=observed, last_seen_at=observed, **values
        )
        if snapshots:
            run = ImportRun.objects.create(
                source=listing.source, snapshot_at=observed,
                completed_at=observed, status=ImportRun.Status.COMPLETED,
            )
            ListingSnapshot.objects.create(
                listing=listing, import_run=run, observed_at=observed,
                asking_price=Decimal(price),
                mileage=listing.mileage if snapshot_mileage is None else snapshot_mileage,
            )
        return listing

    def test_validation_rejects_reversed_ranges(self):
        with self.assertRaises(ValidationError) as years:
            self.profile(minimum_year=2023, maximum_year=2020).full_clean()
        self.assertIn("maximum_year", years.exception.message_dict)
        with self.assertRaises(ValidationError) as mileage:
            self.profile(name="Mileage", minimum_mileage=160000, maximum_mileage=60000).full_clean()
        self.assertIn("maximum_mileage", mileage.exception.message_dict)

    def test_case_insensitive_matching_and_inclusive_boundaries(self):
        profile = self.profile(make="PEUGEOT", model="3008")
        low = self.listing("low", make="peugeot", model="3008", year=2020, snapshot_mileage=60000)
        high = self.listing("high", year=2023, snapshot_mileage=160000)
        self.listing("outside", year=2024)
        self.assertEqual(set(listings_for_profile(profile)), {low, high})

    def test_optional_fuel_and_transmission_filters(self):
        broad = self.profile(fuel="", transmission="")
        diesel = self.listing("diesel")
        petrol = self.listing("petrol", fuel="petrol", transmission="manual")
        self.assertEqual(set(listings_for_profile(broad)), {diesel, petrol})
        broad.fuel, broad.transmission = "DIESEL", "AUTOMATIC"
        self.assertEqual(list(listings_for_profile(broad)), [diesel])

    def test_removed_and_snapshotless_listings_are_excluded(self):
        profile = self.profile()
        self.listing("removed", status=Listing.Status.REMOVED)
        self.listing("no-snapshot", snapshots=False)
        self.assertFalse(listings_for_profile(profile).exists())

    def test_latest_snapshot_supplies_price_and_mileage(self):
        profile = self.profile(maximum_mileage=90000)
        listing = self.listing("latest", price="25000", mileage=150000, snapshot_mileage=100000)
        observed = parse_datetime("2026-07-02T10:00:00Z")
        run = ImportRun.objects.create(source="example", snapshot_at=observed, status=ImportRun.Status.COMPLETED)
        ListingSnapshot.objects.create(
            listing=listing, import_run=run, observed_at=observed,
            asking_price=Decimal("22000"), mileage=85000,
        )
        current = listings_for_profile(profile).get()
        self.assertEqual(current.latest_price, Decimal("22000"))
        self.assertEqual(current.latest_mileage, 85000)

    def test_listing_can_match_multiple_profiles_without_duplication(self):
        first = self.profile()
        second = self.profile(name="Broad", fuel="", transmission="")
        listing = self.listing("shared")
        self.assertEqual(list(listings_for_profile(first)), [listing])
        self.assertEqual(list(listings_for_profile(second)), [listing])
        self.assertEqual(Listing.objects.count(), 1)

    def test_statistics_and_percentiles(self):
        profile = self.profile()
        for number, price in enumerate(("10000", "12000", "14000", "20000")):
            published_at = date(2026, 7, 1 + number) if number < 3 else None
            self.listing(
                str(number), price=price, snapshot_mileage=80000 + number * 10000,
                published_at=published_at,
            )
        summary = summarize_profile_market(
            list(listings_for_profile(profile)), as_of=date(2026, 7, 11)
        )
        self.assertEqual((summary.count, summary.minimum_price, summary.maximum_price), (4, Decimal("10000"), Decimal("20000")))
        self.assertEqual(summary.average_price, Decimal("14000"))
        self.assertEqual(summary.median_price, Decimal("13000"))
        self.assertEqual(summary.price_percentile_25, Decimal("11500"))
        self.assertEqual(summary.price_percentile_75, Decimal("15500"))
        self.assertEqual(summary.median_mileage, Decimal("95000"))
        self.assertEqual(summary.average_days_on_market, Decimal("9"))
        self.assertEqual(summary.publication_date_count, 3)

    def test_similarity_ranking_prefers_technical_and_distance_matches(self):
        profile = self.profile(fuel="", transmission="")
        target = self.listing("target", price="25000", year=2022, snapshot_mileage=100000,
                              generation="II", facelift_status="facelift", engine_family="1.5 BlueHDi",
                              transmission_category="automatic", power_kw=96)
        close = self.listing("close", price="24000", year=2022, snapshot_mileage=105000,
                             generation="II", facelift_status="facelift", engine_family="1.5 BlueHDi",
                             transmission_category="automatic", power_kw=96)
        distant = self.listing("distant", price="20000", year=2020, snapshot_mileage=160000,
                               generation="II", facelift_status="facelift", engine_family="1.5 BlueHDi",
                               transmission_category="automatic", power_kw=96)
        self.listing("cheap-wrong", price="10000", year=2022, snapshot_mileage=100000,
                     generation="II", facelift_status="pre_facelift", engine_family="1.2 PureTech",
                     transmission_category="automatic", power_kw=96)
        listings = list(listings_for_profile(profile))
        annotated_target = next(item for item in listings if item.pk == target.pk)
        ranked = rank_profile_comparables(annotated_target, profile, listings)
        self.assertEqual(ranked[0].listing, close)
        self.assertGreater(ranked[0].similarity_score, ranked[1].similarity_score)
        self.assertNotIn("cheap-wrong", [item.listing.external_id for item in ranked])
        self.assertEqual(summarize_profile_market(listings).count, 4)

    def test_empty_dashboard_has_clear_state(self):
        response = self.client.get(reverse("market_profiles:detail", args=(self.profile().pk,)))
        self.assertIsNone(response.context["summary"])
        self.assertContains(response, "statistics are unavailable")

    def test_dashboard_sorting_and_pagination(self):
        profile = self.profile(minimum_mileage=None, maximum_mileage=None)
        for number in range(21):
            self.listing(f"item-{number}", price=str(10000 + number), mileage=100000 + number)
        response = self.client.get(reverse("market_profiles:detail", args=(profile.pk,)), {"sort": "highest_price", "page": 2})
        self.assertEqual(response.context["page"].paginator.per_page, 20)
        self.assertEqual(len(response.context["page"].object_list), 1)
        first_page = self.client.get(reverse("market:listing_list"))
        self.assertContains(first_page, 'page=2')
        self.assertContains(first_page, 'aria-current="page"')
        first_page = self.client.get(reverse("market_profiles:detail", args=(profile.pk,)), {"sort": "highest_price"})
        self.assertEqual(first_page.context["page"].object_list[0].latest_price, Decimal("10020"))

    def test_create_and_edit_forms(self):
        data = {
            "name": "Saved profile", "source": "example", "make": "Peugeot", "model": "3008",
            "minimum_year": 2020, "maximum_year": 2023, "minimum_mileage": 60000,
            "maximum_mileage": 160000, "fuel": "diesel", "transmission": "automatic",
            "is_active": "on",
        }
        response = self.client.post(reverse("market_profiles:create"), data)
        profile = MarketProfile.objects.get(name="Saved profile")
        self.assertRedirects(response, reverse("market_profiles:detail", args=(profile.pk,)))
        data["name"] = "Updated profile"
        response = self.client.post(reverse("market_profiles:edit", args=(profile.pk,)), data)
        self.assertRedirects(response, reverse("market_profiles:detail", args=(profile.pk,)))
        profile.refresh_from_db()
        self.assertEqual(profile.name, "Updated profile")

    def test_demo_data_command_is_idempotent(self):
        call_command("create_market_demo_data", stdout=StringIO())
        counts = (Listing.objects.count(), ListingSnapshot.objects.count(), MarketProfile.objects.count())
        call_command("create_market_demo_data", stdout=StringIO())
        self.assertEqual((Listing.objects.count(), ListingSnapshot.objects.count(), MarketProfile.objects.count()), counts)
        self.assertGreaterEqual(counts[0], 9)
        self.assertGreaterEqual(counts[2], 3)


class VehicleClassificationTests(TestCase):
    def listing(self, title, *, model="3008", year=2021, transmission=""):
        observed = parse_datetime("2026-07-01T10:00:00Z")
        return Listing.objects.create(
            source="example", external_id=str(Listing.objects.count()), title=title,
            make="Peugeot", model=model, year=year, mileage=100000, fuel="diesel",
            transmission=transmission, first_seen_at=observed, last_seen_at=observed,
            status=Listing.Status.ACTIVE,
        )

    def test_2008_generations_remain_separate(self):
        first = classify_text(make="Peugeot", model="2008", title="Peugeot 2008 I PureTech 130", transmission="manual")
        second = classify_text(make="peugeot", model="2008", title="Peugeot 2008 II PureTech 130", transmission="BVM6")
        self.assertEqual((first.generation, second.generation), ("I", "II"))

    def test_3008_facelift_variants_remain_separate(self):
        before = classify_text(make="Peugeot", model="3008", title="3008 II pre-facelift 1.5 BlueHDi", transmission="EAT8")
        after = classify_text(make="Peugeot", model="3008", title="3008 II facelift 1.5 HDi 130", transmission="BVA8")
        self.assertEqual(before.facelift_status, Listing.FaceliftStatus.PRE_FACELIFT)
        self.assertEqual(after.facelift_status, Listing.FaceliftStatus.FACELIFT)

    def test_year_alone_does_not_assign_facelift(self):
        result = classify_text(make="Peugeot", model="3008", title="Peugeot 3008 II BlueHDi 130", transmission="automatic")
        self.assertEqual(result.facelift_status, Listing.FaceliftStatus.UNKNOWN)

    def test_engine_and_transmission_aliases_normalize(self):
        for title in ("1.5 BlueHDi", "BlueHDi 130", "1.5 HDi 130"):
            self.assertEqual(classify_text(make="Peugeot", model="3008", title=title, transmission="").engine_family, "1.5 BlueHDi")
        for title in ("1.2 PureTech", "PureTech 130"):
            self.assertEqual(classify_text(make="Peugeot", model="3008", title=title, transmission="").engine_family, "1.2 PureTech")
        for value in ("EAT8", "BVA8", "automatic", "automatica"):
            self.assertEqual(classify_text(make="Peugeot", model="3008", title="", transmission=value).transmission_category, "automatic")
        for value in ("BVM6", "manual"):
            self.assertEqual(classify_text(make="Peugeot", model="3008", title="", transmission=value).transmission_category, "manual")

    def test_unknown_facelift_does_not_match_strict_profile(self):
        listing = self.listing("Peugeot 3008 II BlueHDi 130", transmission="EAT8")
        classify_listing(listing)
        profile = MarketProfile.objects.create(
            name="Strict", source="example", make="Peugeot", model="3008", generation="II",
            facelift_status=Listing.FaceliftStatus.FACELIFT, engine_family="1.5 BlueHDi",
            transmission="automatic",
        )
        self.assertFalse(listings_for_profile(profile).exists())

    def test_manual_override_is_not_reclassified(self):
        listing = self.listing("Peugeot 3008 II facelift PureTech 130", transmission="EAT8")
        listing.generation = "Manual generation"
        listing.facelift_status = Listing.FaceliftStatus.PRE_FACELIFT
        listing.classification_method = Listing.ClassificationMethod.MANUAL
        listing.classification_manually_overridden = True
        listing.save()
        listing.title = "Peugeot 3008 II facelift 1.5 BlueHDi"
        classify_listing(listing)
        self.assertEqual(listing.generation, "Manual generation")
        self.assertEqual(listing.facelift_status, Listing.FaceliftStatus.PRE_FACELIFT)

    def test_import_does_not_overwrite_manual_classification(self):
        listing = self.listing("Original", transmission="manual")
        listing.generation = "I"
        listing.classification_method = Listing.ClassificationMethod.MANUAL
        listing.classification_manually_overridden = True
        listing.save()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rows.csv"
            with path.open("w", encoding="utf-8", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                writer.writerow({
                    "source": "example", "external_id": listing.external_id, "source_url": "",
                    "title": "Peugeot 3008 II facelift BlueHDi 130 EAT8", "make": "Peugeot",
                    "model": "3008", "year": "2022", "mileage": "90000", "fuel": "diesel",
                    "transmission": "EAT8", "asking_price": "25000", "observed_at": "2026-07-02T10:00:00Z",
                })
            import_listings(path, "example")
        listing.refresh_from_db()
        self.assertEqual(listing.generation, "I")
        self.assertTrue(listing.classification_manually_overridden)

    def test_classification_review_manual_update(self):
        listing = self.listing("Peugeot 3008 unknown")
        response = self.client.get(reverse("classification_review"))
        self.assertContains(response, listing.title)
        response = self.client.post(reverse("classification_review"), {
            "listing_id": listing.pk, "generation": "II",
            "facelift_status": Listing.FaceliftStatus.FACELIFT,
            "engine_family": "1.5 BlueHDi", "transmission_category": "automatic",
        })
        self.assertRedirects(response, reverse("classification_review"))
        listing.refresh_from_db()
        self.assertEqual(listing.classification_method, Listing.ClassificationMethod.MANUAL)
        self.assertTrue(listing.classification_manually_overridden)

    def test_priority_seed_commands_are_idempotent(self):
        for _ in range(2):
            call_command("seed_priority_market_profiles", stdout=StringIO())
            call_command("create_priority_market_demo_data", stdout=StringIO())
        self.assertEqual(MarketProfile.objects.filter(source="polovniautomobili").count(), 12)
        self.assertEqual(Listing.objects.filter(source="demo-priority-market").count(), 9)
