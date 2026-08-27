from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import Dealer, DealerInventorySnapshot, DealerListingMembership, Listing
from .services.dealer_intelligence import DealerAPIError, ingest_dealer_inventory
from .services.dealer_intelligence import _next_dealer_page_url


class DealerIntelligenceTests(TestCase):
    def setUp(self):
        self.dealer = Dealer.objects.create(name="Example Motors", source="dealer-api", external_id="dealer-1", api_url="https://example.test/inventory", price_bracket_size=5000)

    def record(self, external_id, price, **changes):
        values = {"external_id": external_id, "title": f"Vehicle {external_id}", "make": "Peugeot", "model": "3008", "year": 2022, "mileage": 80000, "fuel": "diesel", "transmission": "automatic", "asking_price": price, "published_at": "2026-07-01"}
        values.update(changes)
        return values

    def test_snapshot_calculates_dealer_metrics(self):
        snapshot = ingest_dealer_inventory(self.dealer, [self.record("a", "10000"), self.record("b", "12000"), self.record("c", "19000")], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        self.assertEqual(snapshot.inventory_count, 3)
        self.assertEqual(snapshot.median_price, Decimal("12000"))
        self.assertEqual(snapshot.dominant_price_bracket_low, Decimal("10000"))
        self.assertEqual(snapshot.dominant_price_bracket_high, Decimal("15000"))
        self.assertEqual(snapshot.dominant_price_bracket_count, 2)
        self.assertEqual(snapshot.average_active_days, Decimal("10"))

    def test_complete_response_tracks_disappearance_and_reappearance(self):
        first, second, third = (parse_datetime(value) for value in ("2026-07-11T10:00:00Z", "2026-07-12T10:00:00Z", "2026-07-13T10:00:00Z"))
        ingest_dealer_inventory(self.dealer, [self.record("a", "10000"), self.record("b", "12000")], observed_at=first)
        snapshot = ingest_dealer_inventory(self.dealer, [self.record("a", "10500")], observed_at=second)
        missing = DealerListingMembership.objects.get(listing__external_id="b")
        self.assertEqual(snapshot.disappeared_count, 1)
        self.assertFalse(missing.is_currently_present)
        self.assertEqual(missing.disappeared_at, second)
        ingest_dealer_inventory(self.dealer, [self.record("a", "10500"), self.record("b", "12500")], observed_at=third)
        missing.refresh_from_db()
        self.assertTrue(missing.is_currently_present)
        self.assertIsNone(missing.disappeared_at)
        self.assertEqual(Listing.objects.filter(source="dealer-api").count(), 2)

    def test_first_seen_is_used_without_publication_date(self):
        snapshot = ingest_dealer_inventory(self.dealer, [self.record("a", "10000", published_at=None)], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        self.assertEqual(snapshot.average_active_days, Decimal("0"))

    def test_duplicate_ids_reject_entire_snapshot(self):
        with self.assertRaises(DealerAPIError):
            ingest_dealer_inventory(self.dealer, [self.record("a", "10000"), self.record("a", "11000")])
        self.assertFalse(self.dealer.inventory_snapshots.exists())

    def test_unknown_fuel_can_be_preserved_without_guessing(self):
        snapshot = ingest_dealer_inventory(
            self.dealer, [self.record("unknown-fuel", "10000", fuel="unknown")]
        )
        self.assertEqual(snapshot.inventory_items.get().listing.fuel, "unknown")

    def test_vin_is_saved_in_listing_and_immutable_snapshots(self):
        snapshot = ingest_dealer_inventory(
            self.dealer,
            [self.record("vin-ad", "10000", vin=" vf3mc bhxw ks123456 ")],
            observed_at=parse_datetime("2026-07-11T10:00:00Z"),
        )
        item = snapshot.inventory_items.select_related("listing").get()
        self.assertEqual(item.listing.vin, "VF3MCBHXWKS123456")
        self.assertEqual(item.vin, "VF3MCBHXWKS123456")
        self.assertEqual(item.listing.snapshots.get().vin, "VF3MCBHXWKS123456")

    def test_new_external_id_with_known_vin_is_flagged_as_repeat(self):
        first_time = parse_datetime("2026-07-11T10:00:00Z")
        second_time = parse_datetime("2026-07-12T10:00:00Z")
        ingest_dealer_inventory(
            self.dealer, [self.record("original", "10000", vin="VF3MCBHXWKS123456")],
            observed_at=first_time,
        )
        ingest_dealer_inventory(
            self.dealer, [self.record("relisted", "10500", vin="vf3mcbhxwks123456")],
            observed_at=second_time,
        )
        original = Listing.objects.get(external_id="original")
        relisted = Listing.objects.get(external_id="relisted")
        self.assertEqual(relisted.repeated_listing_of, original)
        self.assertEqual(relisted.repeat_detection_method, Listing.RepeatDetectionMethod.VIN)
        self.assertEqual(relisted.repeat_detected_at, second_time)

    def test_dashboard_uses_latest_snapshot(self):
        ingest_dealer_inventory(self.dealer, [self.record("a", "10000")], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        self.assertContains(response, "Example Motors")

    def test_polovni_dealer_pages_link_to_the_source_storefront(self):
        self.dealer.source = "polovniautomobili"
        self.dealer.api_url = "https://www.polovniautomobili.com/example-motors"
        self.dealer.save()

        detail = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        dealer_list = self.client.get(reverse("dealers:list"))
        for response in (detail, dealer_list):
            self.assertContains(response, "View on Polovni Automobili")
            self.assertContains(response, self.dealer.api_url)
            self.assertContains(response, 'target="_blank"')

    def test_dashboard_sorts_by_mileage_and_supports_larger_pages(self):
        ingest_dealer_inventory(self.dealer, [
            self.record("high", "10000", mileage=120000),
            self.record("low", "11000", mileage=30000),
            self.record("middle", "12000", mileage=70000),
        ], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        response = self.client.get(
            reverse("dealers:detail", args=(self.dealer.pk,)),
            {"sort": "mileage_desc", "page_size": "200"},
        )
        self.assertEqual(response.context["page_size"], 200)
        self.assertEqual(
            [item.mileage for item in response.context["inventory_page"]],
            [120000, 70000, 30000],
        )
        self.assertContains(response, "€10.000")

    def test_vehicle_can_be_excluded_and_restored(self):
        snapshot = ingest_dealer_inventory(self.dealer, [
            self.record("keep", "10000"), self.record("remove", "30000"),
        ], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        removed = snapshot.inventory_items.get(listing__external_id="remove").listing
        url = reverse("dealers:detail", args=(self.dealer.pk,))
        response = self.client.post(url, {"listing_ids": [removed.pk], "bulk_action": "exclude"})
        self.assertRedirects(response, url)
        membership = DealerListingMembership.objects.get(dealer=self.dealer, listing=removed)
        self.assertFalse(membership.is_relevant)
        response = self.client.get(url)
        self.assertEqual(response.context["intelligence_summary"]["count"], 1)
        self.assertEqual(response.context["intelligence_summary"]["median_price"], Decimal("10000"))
        self.assertNotContains(response, "Vehicle remove")
        excluded = self.client.get(url, {"visibility": "excluded"})
        self.assertEqual([item.listing.external_id for item in excluded.context["inventory_page"]], ["remove"])
        self.assertContains(excluded, "Restore")
        restore_response = self.client.post(url, {
            "restore_listing_id": removed.pk, "return_visibility": "excluded",
        })
        self.assertRedirects(restore_response, f"{url}?visibility=excluded")
        membership.refresh_from_db()
        self.assertTrue(membership.is_relevant)

    def test_description_flag_is_visible_and_filterable_without_description_storage(self):
        snapshot = ingest_dealer_inventory(self.dealer, [
            self.record("flagged", "10000", description_flags=["Service/commission sale wording"]),
            self.record("ordinary", "11000"),
        ])
        membership = self.dealer.listing_memberships.get(listing__external_id="flagged")
        self.assertEqual(membership.description_flags, ["Service/commission sale wording"])
        self.assertNotIn("description", snapshot.inventory_items.get(listing__external_id="flagged").raw_data)

        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)), {"visibility": "flagged"})

        self.assertContains(response, "Opis flagged: Service/commission sale wording")
        self.assertContains(response, "Vehicle flagged")
        self.assertNotContains(response, "Vehicle ordinary")

    def test_dashboard_shows_and_sorts_active_days(self):
        ingest_dealer_inventory(self.dealer, [
            self.record("newer", "10000", published_at="2026-07-10"),
            self.record("older", "20000", published_at="2026-07-01"),
        ], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        response = self.client.get(
            reverse("dealers:detail", args=(self.dealer.pk,)),
            {"sort": "active_days_desc"},
        )
        self.assertEqual(
            [(item.listing.external_id, item.active_days) for item in response.context["inventory_page"]],
            [("older", 10), ("newer", 1)],
        )
        self.assertContains(response, "Ad age")

    def test_multiple_vehicles_can_be_excluded_together(self):
        snapshot = ingest_dealer_inventory(self.dealer, [
            self.record("one", "10000"), self.record("two", "20000"),
            self.record("three", "30000"),
        ], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        selected_ids = list(snapshot.inventory_items.filter(
            listing__external_id__in=("one", "two")
        ).values_list("listing_id", flat=True))
        url = reverse("dealers:detail", args=(self.dealer.pk,))
        response = self.client.post(url, {
            "listing_ids": selected_ids, "bulk_action": "exclude",
        }, follow=True)
        self.assertContains(response, "2 vehicle(s) were excluded")
        self.assertEqual(
            DealerListingMembership.objects.filter(dealer=self.dealer, is_relevant=False).count(),
            2,
        )
        self.assertEqual(response.context["intelligence_summary"]["count"], 1)
        excluded = self.client.get(url, {"visibility": "excluded"})
        self.assertEqual([item.listing.external_id for item in excluded.context["inventory_page"]], ["one", "two"])
        self.client.post(url, {"listing_ids": selected_ids, "bulk_action": "restore"})
        self.assertFalse(DealerListingMembership.objects.filter(dealer=self.dealer, is_relevant=False).exists())

    def test_dashboard_formats_price_and_mileage_with_dots(self):
        ingest_dealer_inventory(self.dealer, [self.record("formatted", "22990", mileage=83463)], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        self.assertContains(response, "€22.990")
        self.assertContains(response, "83.463 km")

    def test_first_complete_snapshot_has_no_disappearance_comparison(self):
        ingest_dealer_inventory(self.dealer, [self.record("one", "10000")], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        self.assertIsNone(response.context["previous_snapshot"])
        self.assertIsNone(response.context["snapshot_changes"])
        self.assertContains(response, "No previous complete snapshot exists")
        self.assertContains(response, "First observed")
        self.assertNotContains(response, "Active days")

    def test_snapshot_changes_badges_and_composition(self):
        ingest_dealer_inventory(self.dealer, [
            self.record("reduced", "10000"), self.record("increased", "20000"),
            self.record("gone", "30000", year=2020, mileage=123456),
        ], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        ingest_dealer_inventory(self.dealer, [
            self.record("reduced", "9000"), self.record("increased", "21000"),
            self.record("new", "40000"),
        ], observed_at=parse_datetime("2026-07-12T10:00:00Z"))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        changes = response.context["snapshot_changes"]
        self.assertEqual(changes, {
            "new": 1, "disappeared": 1, "reduced": 1, "increased": 1,
            "inventory_change": 0, "median_change": Decimal("1000"),
        })
        self.assertContains(response, "Price reduced")
        self.assertContains(response, "Price increased")
        self.assertContains(response, ">New<", html=False)
        self.assertContains(response, "First registration")
        self.assertContains(response, "â‚¬30.000")
        self.assertContains(response, "123.456 km")
        self.assertContains(response, ">2020<", html=False)
        self.assertEqual(response.context["comparison_disappeared_items"][0].listing.external_id, "gone")
        self.assertEqual(response.context["recently_disappeared"][0]["captured_item"].asking_price, Decimal("30000"))
        self.assertEqual(response.context["top_models"], [("Peugeot 3008", 3)])
        self.assertTrue(response.context["price_brackets"])

    def test_recent_additions_show_captured_details_and_replenishment_rate(self):
        ingest_dealer_inventory(
            self.dealer, [self.record("baseline", "18000")],
            observed_at=parse_datetime("2026-07-10T10:00:00Z"),
        )
        ingest_dealer_inventory(self.dealer, [
            self.record("baseline", "18000"),
            self.record(
                "new-ad", "22990", year=2021, mileage=83463,
                source_url="https://www.polovniautomobili.com/auto-oglasi/new-ad",
            ),
        ], observed_at=parse_datetime("2026-07-11T10:00:00Z"))

        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))

        row = response.context["recently_added"][0]
        self.assertEqual(row["membership"].listing.external_id, "new-ad")
        self.assertEqual(row["captured_item"].asking_price, Decimal("22990"))
        self.assertEqual(response.context["replenishment"], {
            "additions_7d": 1, "additions_30d": 1,
            "average_per_week": Decimal("3.5"), "rate_window_days": 2,
        })
        self.assertContains(response, "Recently added ads")
        self.assertContains(response, "€22.990")
        self.assertContains(response, "83.463 km")
        self.assertContains(response, ">2021<", html=False)
        self.assertContains(response, "Open on Polovni")

    def test_activity_dashboard_ranks_additions_and_disappearances(self):
        second = Dealer.objects.create(
            name="Quieter Dealer", source="dealer-api", external_id="dealer-2",
            api_url="https://example.test/inventory-2",
        )
        baseline = parse_datetime("2026-07-01T10:00:00Z")
        latest = parse_datetime("2026-07-08T10:00:00Z")
        ingest_dealer_inventory(self.dealer, [
            self.record("kept", "10000"), self.record("gone", "12000"),
        ], observed_at=baseline)
        ingest_dealer_inventory(self.dealer, [
            self.record("kept", "10000"), self.record("added", "14000"),
        ], observed_at=latest)
        ingest_dealer_inventory(second, [self.record("second-base", "20000")], observed_at=baseline)
        ingest_dealer_inventory(second, [
            self.record("second-base", "20000"), self.record("second-new", "22000"),
        ], observed_at=latest)

        response = self.client.get(reverse("dealers:activity"), {"period": 7})

        self.assertEqual(response.context["period_days"], 7)
        self.assertEqual(response.context["rows"][0]["dealer"], self.dealer)
        self.assertEqual(response.context["rows"][0]["additions"], 1)
        self.assertEqual(response.context["rows"][0]["disappearances"], 1)
        self.assertEqual(response.context["rows"][0]["total_activity"], 2)
        self.assertEqual(response.context["total_additions"], 2)
        self.assertEqual(response.context["total_disappearances"], 1)
        self.assertContains(response, "Most active dealers")
        self.assertContains(response, "Disappeared / possibly sold")

    def test_snapshot_history_can_open_an_older_snapshot(self):
        first = ingest_dealer_inventory(self.dealer, [self.record("one", "10000")], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        ingest_dealer_inventory(self.dealer, [self.record("one", "11000"), self.record("two", "20000")], observed_at=parse_datetime("2026-07-12T10:00:00Z"))
        url = reverse("dealers:detail", args=(self.dealer.pk,))
        response = self.client.get(url, {"snapshot": first.pk})
        self.assertEqual(len(response.context["snapshot_history"]), 2)
        detail = response.context["selected_snapshot_detail"]
        self.assertEqual(detail["snapshot"], first)
        self.assertEqual(detail["relevant_count"], 1)
        self.assertEqual([item.listing.external_id for item in detail["items"]], ["one"])
        self.assertContains(response, "Snapshot history")

    def test_stock_age_buckets_and_oldest_models(self):
        ingest_dealer_inventory(self.dealer, [
            self.record("a", "10000", model="3008", published_at="2026-07-15"),
            self.record("b", "11000", model="3008", published_at="2026-06-15"),
            self.record("c", "12000", model="2008", published_at="2026-05-15"),
            self.record("d", "13000", model="508", published_at="2026-04-01"),
        ], observed_at=parse_datetime("2026-08-01T10:00:00Z"))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        self.assertEqual(response.context["stock_age_buckets"], {
            "0–30 days": 1, "31–60 days": 1, "61–90 days": 1, "90+ days": 1,
        })
        self.assertEqual(response.context["oldest_models"][0][0], "Peugeot 508")

    def test_listing_detail_shows_dealer_observation_and_disappearance(self):
        first = ingest_dealer_inventory(self.dealer, [self.record("gone", "10000")], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        listing = first.inventory_items.get().listing
        ingest_dealer_inventory(self.dealer, [], observed_at=parse_datetime("2026-07-12T10:00:00Z"))
        response = self.client.get(reverse("market:listing_detail", args=(listing.pk,)))
        self.assertContains(response, "First appeared in monitoring")
        self.assertContains(response, "Disappeared from dealer inventory")
        self.assertContains(response, "Observed price history")

    def test_model_level_intelligence_uses_snapshot_changes(self):
        ingest_dealer_inventory(self.dealer, [
            self.record("3008-existing", "20000", model="3008", published_at="2026-07-01"),
            self.record("2008-gone", "15000", model="2008", published_at="2026-07-05"),
        ], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        ingest_dealer_inventory(self.dealer, [
            self.record("3008-existing", "21000", model="3008", published_at="2026-07-01"),
            self.record("3008-new", "23000", model="3008", published_at="2026-07-12"),
            self.record("508-new", "30000", model="508", published_at="2026-07-10"),
        ], observed_at=parse_datetime("2026-07-12T10:00:00Z"))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        rows = {(row["make"], row["model"]): row for row in response.context["model_summary"]}
        self.assertEqual(rows[("Peugeot", "3008")]["current_count"], 2)
        self.assertEqual(rows[("Peugeot", "3008")]["median_price"], Decimal("22000"))
        self.assertEqual(rows[("Peugeot", "3008")]["new_count"], 1)
        self.assertEqual(rows[("Peugeot", "3008")]["disappeared_count"], 0)
        self.assertEqual(rows[("Peugeot", "2008")]["current_count"], 0)
        self.assertEqual(rows[("Peugeot", "2008")]["disappeared_count"], 1)
        self.assertEqual(rows[("Peugeot", "2008")]["apparent_turnover"], Decimal("100"))
        self.assertEqual(rows[("Peugeot", "3008")]["apparent_turnover"], Decimal("0"))
        self.assertContains(response, "Model-level intelligence")
        self.assertContains(response, "Apparent turnover")
        self.assertContains(response, "disappearance does not confirm a sale")

    def test_slow_stock_groups_vehicles_and_reports_price_reductions(self):
        ingest_dealer_inventory(self.dealer, [
            self.record("slow", "20000", model="3008", published_at="2026-03-01"),
            self.record("fresh", "18000", model="2008", published_at="2026-07-20"),
        ], observed_at=parse_datetime("2026-07-25T10:00:00Z"))
        ingest_dealer_inventory(self.dealer, [
            self.record("slow", "19000", model="3008", published_at="2026-03-01"),
            self.record("fresh", "18000", model="2008", published_at="2026-07-20"),
        ], observed_at=parse_datetime("2026-07-27T10:00:00Z"))
        ingest_dealer_inventory(self.dealer, [
            self.record("slow", "18000", model="3008", published_at="2026-03-01"),
            self.record("fresh", "18000", model="2008", published_at="2026-07-20"),
        ], observed_at=parse_datetime("2026-08-01T10:00:00Z"))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        groups = response.context["slow_stock_groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["model"], "Peugeot 3008")
        vehicle = groups[0]["vehicles"][0]
        self.assertEqual(vehicle["reduction_count"], 2)
        self.assertEqual(vehicle["total_reduction"], Decimal("2000"))
        self.assertGreater(vehicle["ad_age"], 90)
        self.assertContains(response, "2 reductions")

    def test_30_60_90_day_summary_uses_snapshot_baselines(self):
        schedules = (
            ("2026-04-23T10:00:00Z", [self.record("one", "10000", published_at="2026-04-01")]),
            ("2026-05-23T10:00:00Z", [self.record("one", "10000", published_at="2026-04-01"), self.record("two", "20000", published_at="2026-05-01")]),
            ("2026-06-22T10:00:00Z", [self.record("one", "12000", published_at="2026-04-01"), self.record("two", "20000", published_at="2026-05-01")]),
            ("2026-08-01T10:00:00Z", [self.record("one", "14000", published_at="2026-04-01"), self.record("two", "22000", published_at="2026-05-01"), self.record("three", "30000", published_at="2026-07-01")]),
        )
        for observed_at, records in schedules:
            ingest_dealer_inventory(self.dealer, records, observed_at=parse_datetime(observed_at))
        response = self.client.get(reverse("dealers:detail", args=(self.dealer.pk,)))
        periods = {row["days"]: row for row in response.context["period_summaries"]}
        self.assertEqual(periods[30]["baseline"]["snapshot"].observed_at, parse_datetime("2026-06-22T10:00:00Z"))
        self.assertEqual(periods[30]["inventory_change"], 1)
        self.assertEqual(periods[30]["median_change"], Decimal("6000"))
        self.assertEqual(periods[60]["baseline"]["snapshot"].observed_at, parse_datetime("2026-05-23T10:00:00Z"))
        self.assertEqual(periods[90]["baseline"]["snapshot"].observed_at, parse_datetime("2026-04-23T10:00:00Z"))
        self.assertContains(response, "30/60/90-day summary")

    def test_snapshot_items_persist_complete_captured_state_and_raw_data(self):
        observed = parse_datetime("2026-07-11T10:00:00Z")
        snapshot = ingest_dealer_inventory(self.dealer, [self.record("captured", "22990", mileage=83463)], observed_at=observed)
        item = snapshot.inventory_items.get()
        self.assertEqual(item.asking_price, Decimal("22990"))
        self.assertEqual(item.mileage, 83463)
        self.assertEqual(item.status, Listing.Status.ACTIVE)
        self.assertEqual(item.observed_at, observed)
        self.assertEqual(item.raw_data["external_id"], "captured")

    def test_completed_snapshot_and_captured_rows_are_immutable(self):
        snapshot = ingest_dealer_inventory(self.dealer, [self.record("fixed", "10000")], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        snapshot.median_price = Decimal("1")
        with self.assertRaisesMessage(ValidationError, "Completed dealer snapshots are immutable"):
            snapshot.save()
        item = snapshot.inventory_items.get()
        item.asking_price = Decimal("1")
        with self.assertRaisesMessage(ValidationError, "Captured dealer snapshot listings are immutable"):
            item.save()

    def test_dedicated_snapshot_history_and_detail_pages(self):
        snapshot = ingest_dealer_inventory(self.dealer, [self.record("history", "10000")], observed_at=parse_datetime("2026-07-11T10:00:00Z"))
        history = self.client.get(reverse("dealers:snapshot_history", args=(self.dealer.pk,)))
        self.assertContains(history, "Completed captures are preserved as immutable")
        self.assertContains(history, reverse("dealers:snapshot_detail", args=(self.dealer.pk, snapshot.pk)))
        detail = self.client.get(reverse("dealers:snapshot_detail", args=(self.dealer.pk, snapshot.pk)))
        self.assertContains(detail, "Captured listings")
        self.assertContains(detail, "Vehicle history")
        self.assertContains(detail, "80.000 km")

    def test_dealer_pagination_is_discovered_from_storefront_links(self):
        storefront = "https://www.polovniautomobili.com/example-dealer"
        soup = BeautifulSoup(
            '<a href="/example-dealer?page=1">1</a>'
            '<a href="/example-dealer?page=3">3</a>'
            '<a rel="next" href="/example-dealer?page=2">Next</a>'
            '<a href="/some-other-dealer?page=2">Other</a>',
            "html.parser",
        )

        self.assertEqual(
            _next_dealer_page_url(soup, storefront, storefront),
            "https://www.polovniautomobili.com/example-dealer?page=2",
        )
        last_page = BeautifulSoup('<a href="/example-dealer?page=1">1</a>', "html.parser")
        self.assertIsNone(
            _next_dealer_page_url(last_page, f"{storefront}?page=2", storefront)
        )


class RefreshActiveDealersCommandTests(TestCase):
    def setUp(self):
        self.active = Dealer.objects.create(
            name="Active Motors", source="dealer-api", external_id="active",
            api_url="https://example.test/active",
        )
        self.inactive = Dealer.objects.create(
            name="Inactive Motors", source="dealer-api", external_id="inactive",
            api_url="https://example.test/inactive", is_active=False,
        )

    @patch("vehicles.management.commands.refresh_active_dealers.refresh_dealer")
    def test_refreshes_only_active_dealers_and_skips_a_completed_day(self, refresh):
        refresh.side_effect = lambda dealer, observed_at: DealerInventorySnapshot.objects.create(
            dealer=dealer, observed_at=observed_at,
            status=DealerInventorySnapshot.Status.COMPLETED,
        )

        call_command("refresh_active_dealers", stdout=StringIO())
        call_command("refresh_active_dealers", stdout=StringIO())

        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(refresh.call_args.args[0], self.active)
        self.assertEqual(self.active.inventory_snapshots.count(), 1)
        self.assertFalse(self.inactive.inventory_snapshots.exists())

    @patch("vehicles.management.commands.refresh_dealer.refresh_dealer")
    def test_direct_refresh_skips_a_completed_day(self, refresh):
        DealerInventorySnapshot.objects.create(
            dealer=self.active, observed_at=timezone.now(),
            status=DealerInventorySnapshot.Status.COMPLETED,
        )
        output = StringIO()

        call_command("refresh_dealer", self.active.pk, stdout=output)

        refresh.assert_not_called()
        self.assertIn("already refreshed", output.getvalue())

    @patch("vehicles.management.commands.refresh_active_dealers.refresh_dealer")
    def test_failure_does_not_stop_other_dealers_or_replace_success(self, refresh):
        second = Dealer.objects.create(
            name="Second Motors", source="dealer-api", external_id="second",
            api_url="https://example.test/second",
        )
        previous = DealerInventorySnapshot.objects.create(
            dealer=self.active, observed_at=parse_datetime("2026-08-17T10:00:00Z"),
            status=DealerInventorySnapshot.Status.COMPLETED, inventory_count=7,
        )

        def perform(dealer, observed_at):
            if dealer == self.active:
                raise DealerAPIError("temporary outage")
            return DealerInventorySnapshot.objects.create(
                dealer=dealer, observed_at=observed_at,
                status=DealerInventorySnapshot.Status.COMPLETED, inventory_count=2,
            )

        refresh.side_effect = perform
        with self.assertRaises(CommandError):
            call_command("refresh_active_dealers", stdout=StringIO(), stderr=StringIO())

        previous.refresh_from_db()
        self.assertEqual(previous.inventory_count, 7)
        self.assertEqual(self.active.inventory_snapshots.count(), 1)
        self.assertEqual(second.inventory_snapshots.filter(status="completed").count(), 1)


class SeedTrackedDealersCommandTests(TestCase):
    def test_seed_is_idempotent_updates_by_url_and_preserves_kia(self):
        kia = Dealer.objects.create(
            name="Old Kia Name", source="polovniautomobili", external_id="old-kia-id",
            api_url="https://www.polovniautomobili.com/Service-Maxx", is_active=False,
        )

        call_command("seed_tracked_dealers", stdout=StringIO())
        call_command("seed_tracked_dealers", stdout=StringIO())

        self.assertEqual(Dealer.objects.filter(source="polovniautomobili").count(), 22)
        kia.refresh_from_db()
        self.assertEqual(kia.external_id, "Service-Maxx")
        self.assertEqual(kia.name, "KIA CENTAR BEOGRAD")
        self.assertTrue(kia.is_active)
        self.assertEqual(kia.inventory_type, Dealer.InventoryType.MIXED)
        ak = Dealer.objects.get(external_id="akkompresor")
        self.assertTrue(ak.is_active)
        self.assertEqual(ak.inventory_type, Dealer.InventoryType.NEW)
        autoland = Dealer.objects.get(external_id="autoland")
        self.assertTrue(autoland.is_active)
        self.assertEqual(autoland.inventory_type, Dealer.InventoryType.USED)
        nena = Dealer.objects.get(external_id="auto-nena-still-peugeot")
        self.assertTrue(nena.is_active)
        self.assertEqual(nena.inventory_type, Dealer.InventoryType.MIXED)
        arena = Dealer.objects.get(external_id="arena-auto")
        self.assertTrue(arena.is_active)
        self.assertEqual(arena.inventory_type, Dealer.InventoryType.USED)
        auto_system = Dealer.objects.get(external_id="autosystem")
        self.assertEqual(auto_system.name, "Auto System")
        self.assertTrue(auto_system.is_active)
        self.assertEqual(auto_system.inventory_type, Dealer.InventoryType.USED)
        autokomerc = Dealer.objects.get(external_id="autokomerc")
        self.assertTrue(autokomerc.is_active)
        self.assertEqual(autokomerc.inventory_type, Dealer.InventoryType.USED)
        tree_m_auto = Dealer.objects.get(external_id="tree-m-auto")
        self.assertTrue(tree_m_auto.is_active)
        self.assertEqual(tree_m_auto.inventory_type, Dealer.InventoryType.USED)
        fiba_auto = Dealer.objects.get(external_id="fiba-auto")
        self.assertTrue(fiba_auto.is_active)
        self.assertEqual(fiba_auto.inventory_type, Dealer.InventoryType.USED)
        nbg_autos = Dealer.objects.get(external_id="nbg-autos")
        self.assertTrue(nbg_autos.is_active)
        self.assertEqual(nbg_autos.inventory_type, Dealer.InventoryType.USED)
        balkan_car_sales = Dealer.objects.get(external_id="balkan-car-sales")
        self.assertTrue(balkan_car_sales.is_active)
        self.assertEqual(balkan_car_sales.inventory_type, Dealer.InventoryType.USED)
        british_motors = Dealer.objects.get(external_id="british-motors-polovna-vozila")
        self.assertTrue(british_motors.is_active)
        self.assertEqual(british_motors.inventory_type, Dealer.InventoryType.USED)
        emil_frey = Dealer.objects.get(external_id="emil-frey-auto-centar")
        self.assertTrue(emil_frey.is_active)
        self.assertEqual(emil_frey.inventory_type, Dealer.InventoryType.USED)
        french_concept = Dealer.objects.get(external_id="french-concept")
        self.assertTrue(french_concept.is_active)
        self.assertEqual(french_concept.inventory_type, Dealer.InventoryType.USED)
        delta = Dealer.objects.get(external_id="delta-polovni-automobili")
        self.assertTrue(delta.is_active)
        self.assertEqual(delta.inventory_type, Dealer.InventoryType.USED)
        holliday = Dealer.objects.get(external_id="holliday")
        self.assertTrue(holliday.is_active)
        self.assertTrue(holliday.is_qa)
        self.assertEqual(holliday.inventory_type, Dealer.InventoryType.USED)
        jp_company = Dealer.objects.get(external_id="jpcompany")
        self.assertEqual(jp_company.name, "JP Company")
        self.assertEqual(jp_company.inventory_type, Dealer.InventoryType.USED)
        force_luxury = Dealer.objects.get(external_id="force-luxury-cars")
        self.assertEqual(force_luxury.name, "Force Luxury Cars")
        self.assertEqual(force_luxury.inventory_type, Dealer.InventoryType.USED)


class CrossDealerModelComparisonTests(TestCase):
    def record(self, external_id, price, **changes):
        values = {
            "external_id": external_id, "title": f"Peugeot 3008 {external_id}",
            "source_url": f"https://www.polovniautomobili.com/auto-oglasi/{external_id}/peugeot-3008",
            "make": "Peugeot", "model": "3008", "year": 2022,
            "mileage": 80000, "fuel": "diesel", "transmission": "automatic",
            "asking_price": price, "published_at": "2026-06-01",
        }
        values.update(changes)
        return values

    def setUp(self):
        self.first = Dealer.objects.create(
            name="First Dealer", source="comparison", external_id="first",
            api_url="https://example.test/first",
        )
        self.second = Dealer.objects.create(
            name="Second Dealer", source="comparison", external_id="second",
            api_url="https://example.test/second",
        )
        baseline = parse_datetime("2026-07-01T10:00:00Z")
        latest = parse_datetime("2026-08-05T10:00:00Z")
        ingest_dealer_inventory(self.first, [
            self.record("common", "12000"),
            self.record("gone", "15000"),
            self.record("excluded", "90000"),
        ], observed_at=baseline)
        self.first_latest = ingest_dealer_inventory(self.first, [
            self.record("common", "10000"),
            self.record("new", "20000", published_at="2026-08-01"),
            self.record("excluded", "100000"),
        ], observed_at=latest)
        DealerListingMembership.objects.filter(
            dealer=self.first, listing__external_id="excluded",
        ).update(is_relevant=False)
        ingest_dealer_inventory(self.second, [
            self.record(
                "second-current", "30000", year=2021, mileage=60000,
                fuel="petrol", transmission="manual",
            ),
        ], observed_at=latest)

    def comparison(self, **params):
        defaults = {"make": "Peugeot", "model": "3008"}
        defaults.update(params)
        return self.client.get(reverse("dealers:model_comparison"), defaults)

    def test_cross_dealer_matching(self):
        response = self.comparison()
        self.assertEqual([row["dealer"] for row in response.context["rows"]], [self.first, self.second])
        self.assertEqual(response.context["rows"][0]["stock_count"], 2)
        self.assertContains(response, "Cross-dealer model comparison")

    def test_latest_complete_snapshot_selection(self):
        DealerInventorySnapshot.objects.create(
            dealer=self.first, observed_at=parse_datetime("2026-08-06T10:00:00Z"),
            status=DealerInventorySnapshot.Status.FAILED,
        )
        row = self.comparison().context["rows"][0]
        self.assertEqual(row["latest"], self.first_latest)

    def test_30_day_new_and_disappeared_counts(self):
        row = self.comparison().context["rows"][0]
        self.assertEqual(row["new_count"], 1)
        self.assertEqual(row["disappeared_count"], 1)
        self.assertEqual(row["opening_stock_count"], 2)
        self.assertEqual(row["apparent_turnover"], Decimal("50"))
        self.assertEqual(row["median_ad_age"], Decimal("34.5"))

    def test_price_reduction_counts(self):
        row = self.comparison().context["rows"][0]
        self.assertEqual(row["reduction_count"], 1)

    def test_make_model_dealer_year_fuel_and_transmission_filters(self):
        cases = (
            ({"dealer": self.second.pk}, [self.second]),
            ({"year_min": 2022}, [self.first]),
            ({"year_max": 2021}, [self.second]),
            ({"fuel": "diesel"}, [self.first]),
            ({"transmission": Listing.TransmissionCategory.AUTOMATIC}, [self.first]),
            ({"make": "Renault"}, []),
            ({"model": "2008"}, []),
        )
        for filters, expected in cases:
            with self.subTest(filters=filters):
                self.assertEqual(
                    [row["dealer"] for row in self.comparison(**filters).context["rows"]],
                    expected,
                )

    def test_model_frequency_ranking_and_filters_use_relevant_latest_stock(self):
        response = self.client.get(reverse("dealers:model_comparison"))
        frequency = next(
            row for row in response.context["model_frequencies"]
            if (row["make"], row["model"]) == ("Peugeot", "3008")
        )
        self.assertEqual(frequency["stock_count"], 3)
        self.assertEqual(frequency["dealer_count"], 2)
        self.assertEqual(frequency["median_ad_days"], Decimal("65"))
        self.assertEqual(frequency["opening_stock_count"], 2)
        self.assertEqual(frequency["disappeared_count"], 1)
        self.assertEqual(frequency["apparent_turnover"], Decimal("50"))
        self.assertContains(response, "Most frequently observed models")

        filtered = self.comparison(
            dealer=self.first.pk, year_min=2022, fuel="diesel",
            transmission=Listing.TransmissionCategory.AUTOMATIC,
        )
        self.assertEqual(filtered.context["model_frequencies"], [{
            "make": "Peugeot", "model": "3008", "stock_count": 2,
            "dealer_count": 1, "median_ad_days": Decimal("34.5"),
            "opening_stock_count": 2, "disappeared_count": 1,
            "apparent_turnover": Decimal("50"),
        }])

    def test_multiple_dealers_can_be_selected_or_left_empty_for_all(self):
        third = Dealer.objects.create(
            name="Third Dealer", source="comparison", external_id="third",
            api_url="https://example.test/third",
        )
        ingest_dealer_inventory(third, [
            self.record("third-current", "25000"),
        ], observed_at=parse_datetime("2026-08-05T10:00:00Z"))

        all_response = self.comparison()
        self.assertEqual(
            {row["dealer"] for row in all_response.context["rows"]},
            {self.first, self.second, third},
        )
        selected = self.client.get(reverse("dealers:model_comparison"), {
            "make": "Peugeot", "model": "3008",
            "dealer": [self.first.pk, third.pk],
        })
        self.assertEqual(
            [row["dealer"] for row in selected.context["rows"]],
            [self.first, third],
        )
        self.assertEqual(selected.context["filters"].dealer_ids, (self.first.pk, third.pk))
        self.assertContains(selected, f'dealer={self.first.pk}')
        self.assertContains(selected, f'dealer={third.pk}')

    def test_dealer_drilldown_shows_current_and_no_longer_observed_vehicles(self):
        response = self.client.get(
            reverse("dealers:model_comparison_detail", args=(self.first.pk,)),
            {"make": "Peugeot", "model": "3008"},
        )
        self.assertContains(response, "Peugeot 3008 common")
        self.assertContains(response, "Peugeot 3008 gone")
        self.assertContains(response, "No longer observed")
        self.assertContains(response, "1 reduction")
        self.assertContains(response, "https://www.polovniautomobili.com/auto-oglasi/common/peugeot-3008")

    def test_excluded_listings_do_not_affect_statistics_or_drilldown(self):
        row = self.comparison().context["rows"][0]
        self.assertEqual(row["stock_count"], 2)
        self.assertEqual(row["median_price"], Decimal("15000"))
        self.assertEqual(row["new_count"], 1)
        self.assertEqual(row["reduction_count"], 1)
        detail = self.client.get(
            reverse("dealers:model_comparison_detail", args=(self.first.pk,)),
            {"make": "Peugeot", "model": "3008"},
        )
        self.assertNotContains(detail, "Peugeot 3008 excluded")
