import csv
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase
from django.urls import reverse
from django.utils.dateparse import parse_datetime

from vehicles.models import ImportRun, Listing, ListingSnapshot, Vehicle
from vehicles.services.import_listings import CSV_COLUMNS, import_listings
from vehicles.services.market_analysis import comparable_listings, summarize_market, with_latest_snapshot


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
    ):
        observed = parse_datetime(observed_at)
        listing = Listing.objects.create(
            source=source,
            external_id=external_id,
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

    def test_browser_sorts_by_latest_price(self):
        expensive = self.create_listing("expensive", price="15000")
        cheap = self.create_listing("cheap", price="9000")

        response = self.client.get(reverse("market:listing_list"), {"sort": "lowest_price"})

        self.assertEqual(list(response.context["page"].object_list), [cheap, expensive])

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
        self.assertContains(response, "€11000.00")

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
