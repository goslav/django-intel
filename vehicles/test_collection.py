import json
from decimal import Decimal
from io import StringIO

import httpx
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase

from vehicles.models import (
    Listing, ListingSnapshot, SourceSearch, SourceSearchMembership, SourceSearchRun,
    SourceSearchRunListing,
)
from vehicles.services.market_analysis import listings_for_profile
from vehicles.services.source_search_refresh import refresh_source_search
from vehicles.sources.polovni_automobili.search_results import (
    CollectionError, canonicalize_public_ad_url, canonicalize_thumbnail_url, discover_advertisements,
    parse_search_page, public_ad_url_fallback, resolve_public_ad_url,
)
from vehicles.sources.polovni_automobili.detail_record import description_flags, extract_vin, normalize_detail, normalize_fuel
from vehicles.models import MarketProfile


SEARCH_URL = "https://www.polovniautomobili.com/auto-oglasi/pretraga?brand=Peugeot&model=3008"


def html(*ids, next_url=None):
    links = "".join(f'<a href="/auto-oglasi/{item}/peugeot-3008">Ad</a>' for item in ids)
    pagination = f'<nav aria-label="pagination"><a rel="next" href="{next_url}">Next</a></nav>' if next_url else ""
    next_data = json.dumps({
        "props": {"pageProps": {"searchResults": {"results": [
            {"id": int(item), "imageMain": f"https://cdn.polovniautomobili.com/thumbs/{item}.jpg?size=small"}
            for item in ids
        ]}}}
    })
    return f'<html><body>{links}{pagination}<script id="__NEXT_DATA__" type="application/json">{next_data}</script></body></html>'


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def detail_payload(listing_id="101", price=25000, mark="II facelift 1.5 BlueHDi EAT8"):
    return {
        "id": int(listing_id), "brand": "Peugeot", "model": "3008", "mark": mark,
        "year": 2022, "mileage": 90000, "fuel": "diesel", "gearBox": "EAT8",
        "engineVolume": "1499", "power": 96, "horsePower": 130, "chassis": "SUV",
        "chassisId": "demo-chassis", "price": price, "priceCurrency": "EUR",
        "publishDate": "2026-01-01", "renewDate": "2026-07-01", "conditionNew": False,
        "status": "active", "owner": {"email": "private@example.test", "phone": "123"},
        "description": "sensitive free text", "images": ["secret.jpg"],
    }


class SearchDiscoveryTests(TestCase):
    def test_public_advertisement_url_canonicalization(self):
        expected = "https://www.polovniautomobili.com/auto-oglasi/29966305/peugeot-508-1.5hdi-allure-at"
        inputs = (
            "/auto-oglasi/29966305/peugeot-508-1.5hdi-allure-at",
            expected + "?",
            expected + "?utm_source=test",
            "https://polovniautomobili.com/auto-oglasi/29966305/peugeot-508-1.5hdi-allure-at#details",
            "/auto-oglasi//29966305//peugeot-508-1.5hdi-allure-at",
        )
        for value in inputs:
            self.assertEqual(canonicalize_public_ad_url(value, "29966305"), ("29966305", expected))

    def test_public_url_rejects_non_advertisement_and_id_mismatch(self):
        invalid = (
            "https://core.polovniautomobili.com/api/v1/classifieds/car/29966305",
            SEARCH_URL,
            "https://www.polovniautomobili.com/prodavac/123",
            "https://example.com/auto-oglasi/29966305/car",
            "/auto-oglasi/not-numeric/car",
            "/auto-oglasi/29966305/file.pdf",
        )
        for value in invalid:
            with self.assertRaises(ValueError):
                canonicalize_public_ad_url(value)
        with self.assertRaises(ValueError):
            canonicalize_public_ad_url("/auto-oglasi/29966305/car", "123")

    def test_id_fallback_is_valid(self):
        fallback = public_ad_url_fallback("29966305")
        self.assertEqual(canonicalize_public_ad_url(fallback), ("29966305", fallback))

    def test_search_result_thumbnail_is_validated_and_tracking_is_removed(self):
        links, _ = parse_search_page(html("101"), SEARCH_URL)
        self.assertEqual(
            links[0].thumbnail_url,
            "https://cdn.polovniautomobili.com/thumbs/101.jpg",
        )
        with self.assertRaises(ValueError):
            canonicalize_thumbnail_url("https://example.com/thumb.jpg")

    def test_serbian_and_english_fuel_names_normalize_consistently(self):
        for value in ("Dizel", "diesel", "disel"):
            self.assertEqual(normalize_fuel(value), "diesel")
        for value in ("Benzin", "petrol", "gasoline"):
            self.assertEqual(normalize_fuel(value), "petrol")
        for value in ("Hibrid", "Hybrid", "Plug-in hibrid"):
            self.assertEqual(normalize_fuel(value), "hybrid")
        record = normalize_detail({**detail_payload(), "fuel": "Dizel"}, "101")
        self.assertEqual(record.fuel, "diesel")
        self.assertEqual(record.source_fuel, "Dizel")

    def test_opis_flags_service_sales_and_new_vehicles(self):
        self.assertEqual(
            description_flags("<p>Uslužna prodaja. Nekorišćeno vozilo.</p>"),
            ["Service/commission sale wording", "New/unused vehicle wording"],
        )

    def test_vin_is_extracted_only_from_dedicated_or_labelled_values(self):
        vin = "VF3MCBHXWKS123456"
        self.assertEqual(extract_vin({"vin": vin.lower()}), vin)
        self.assertEqual(extract_vin({"description": f"Broj šasije: {vin}"}), vin)
        self.assertEqual(extract_vin({"description": f"Reference {vin}"}), "")
        self.assertEqual(extract_vin({"description": "VIN: NOT-A-VALID-VIN"}), "")
        self.assertEqual(description_flags("Redovno održavan automobil."), [])

    def test_detail_without_published_price_is_identified_separately(self):
        with self.assertRaisesMessage(CollectionError, "Detail response has no asking price"):
            normalize_detail({**detail_payload(), "price": None}, "101")

    def test_search_url_validation_and_ad_url_rejection(self):
        SourceSearch(name="Valid", search_url=SEARCH_URL).full_clean()
        for invalid in (
            "http://www.polovniautomobili.com/auto-oglasi/pretraga",
            "https://example.com/automobili",
            "https://www.polovniautomobili.com/auto-oglasi/12345/car",
        ):
            with self.assertRaises(ValidationError):
                SourceSearch(name="Invalid", search_url=invalid).full_clean()

    def test_advertisement_links_are_extracted_and_deduplicated(self):
        links, next_url = parse_search_page(html("123", "123", "456"), SEARCH_URL)
        self.assertEqual([link.external_id for link in links], ["123", "456"])
        self.assertIsNone(next_url)
        self.assertEqual(links[0].public_url, "https://www.polovniautomobili.com/auto-oglasi/123/peugeot-3008")

    def test_slugged_duplicate_is_preferred(self):
        markup = '<a href="/auto-oglasi/123">ID only</a><a href="/auto-oglasi/123/real-slug?tracking=1">Slug</a>'
        links, _ = parse_search_page(markup, SEARCH_URL)
        self.assertEqual(links[0].public_url, "https://www.polovniautomobili.com/auto-oglasi/123/real-slug")

    def test_pagination_stays_in_same_search_and_maximum_is_enforced(self):
        next_url = SEARCH_URL + "&page=2"
        responses = {SEARCH_URL: html("1", next_url=next_url), next_url: html("2")}
        result = discover_advertisements(SEARCH_URL, maximum_pages=1, delay_seconds=0, client=client(lambda request: httpx.Response(200, text=responses[str(request.url)], request=request)))
        self.assertEqual(result.pages_completed, 1)
        self.assertTrue(result.was_truncated)
        with self.assertRaises(CollectionError):
            parse_search_page(html("1", next_url="https://www.polovniautomobili.com/auto-oglasi/pretraga?brand=Citroen"), SEARCH_URL)

    def test_current_ant_pagination_markup_finds_second_page(self):
        page_two = SEARCH_URL + "&page=2"
        representative = html("1") + f'<div id="paginationRow"><ul class="ant-pagination"><li class="ant-pagination-item ant-pagination-item-2"><a href="{page_two}">2</a></li><li class="ant-pagination-next"><a href="{page_two}"></a></li></ul></div>'
        _, next_url = parse_search_page(representative, SEARCH_URL)
        self.assertEqual(next_url, page_two)

    def test_blocking_challenges_and_changed_html_fail_safely(self):
        for status in (403, 429):
            with self.assertRaisesRegex(CollectionError, str(status)):
                discover_advertisements(SEARCH_URL, maximum_pages=1, delay_seconds=0, client=client(lambda request, status=status: httpx.Response(status, request=request)))
        with self.assertRaisesRegex(CollectionError, "CAPTCHA"):
            parse_search_page("<html>CAPTCHA verify you are human</html>", SEARCH_URL)
        with self.assertRaisesRegex(CollectionError, "structure may have changed"):
            parse_search_page("<html><body>No recognized advertisements</body></html>", SEARCH_URL)


class SourceSearchRefreshTests(TestCase):
    def setUp(self):
        self.search = SourceSearch.objects.create(name="3008 broad", search_url=SEARCH_URL, maximum_pages=2, request_delay_seconds=0)

    def test_conservative_configuration_defaults(self):
        configured = SourceSearch(name="Defaults", search_url=SEARCH_URL)
        self.assertEqual(configured.maximum_pages, 2)
        self.assertEqual(configured.request_delay_seconds, 2)

    def discovery_client(self, ids=("101",), *, next_url=None):
        return client(lambda request: httpx.Response(200, text=html(*ids, next_url=next_url), request=request))

    def detail_client(self, prices=None, marks=None):
        prices, marks = prices or {}, marks or {}
        def handler(request):
            listing_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=detail_payload(listing_id, prices.get(listing_id, 25000), marks.get(listing_id, "II facelift 1.5 BlueHDi EAT8")), request=request)
        return client(handler)

    def refresh(self, ids=("101",), **kwargs):
        return refresh_source_search(self.search, delay_seconds=0, discovery_client=self.discovery_client(ids), detail_client=self.detail_client(**kwargs))

    def test_detail_creates_listing_snapshot_membership_and_classification(self):
        result = self.refresh()
        listing = Listing.objects.get(external_id="101")
        self.assertEqual((result.run.listings_created, result.run.snapshots_created), (1, 1))
        self.assertEqual(listing.source_url, "https://www.polovniautomobili.com/auto-oglasi/101/peugeot-3008")
        self.assertEqual(listing.thumbnail_url, "https://cdn.polovniautomobili.com/thumbs/101.jpg")
        self.assertEqual(listing.published_at.isoformat(), "2026-01-01")
        self.assertEqual(listing.snapshots.get().raw_data["publish_date"], "2026-01-01")
        self.assertEqual(listing.facelift_status, Listing.FaceliftStatus.FACELIFT)
        self.assertEqual(listing.engine_family, "1.5 BlueHDi")
        self.assertNotIn("private@example.test", str(listing.snapshots.get().raw_data))
        self.assertTrue(SourceSearchMembership.objects.get().is_currently_present)

    def test_unchanged_observation_is_idempotent_and_later_price_is_preserved(self):
        self.refresh()
        self.refresh()
        self.assertEqual(ListingSnapshot.objects.count(), 1)
        self.refresh(prices={"101": 24000})
        self.assertEqual(list(ListingSnapshot.objects.order_by("observed_at").values_list("asking_price", flat=True)), [Decimal("25000"), Decimal("24000")])

    def test_manual_classification_is_preserved(self):
        self.refresh()
        listing = Listing.objects.get(external_id="101")
        listing.facelift_status = Listing.FaceliftStatus.PRE_FACELIFT
        listing.classification_method = Listing.ClassificationMethod.MANUAL
        listing.classification_manually_overridden = True
        listing.save()
        self.refresh(marks={"101": "II facelift 1.5 BlueHDi EAT8"})
        listing.refresh_from_db()
        self.assertEqual(listing.facelift_status, Listing.FaceliftStatus.PRE_FACELIFT)

    def test_one_collection_supplies_separate_strict_profiles(self):
        self.refresh(ids=("101", "102"), marks={"101": "II pre-facelift 1.5 BlueHDi EAT8", "102": "II facelift 1.5 BlueHDi EAT8"})
        profiles = []
        for status in (Listing.FaceliftStatus.PRE_FACELIFT, Listing.FaceliftStatus.FACELIFT):
            profiles.append(MarketProfile.objects.create(name=status, source="polovniautomobili", make="Peugeot", model="3008", generation="II", facelift_status=status, engine_family="1.5 BlueHDi", transmission="automatic"))
        self.assertEqual([listings_for_profile(profile).count() for profile in profiles], [1, 1])
        self.assertEqual(Listing.objects.count(), 2)

    def test_complete_run_marks_only_its_missing_membership(self):
        self.refresh()
        listing = Listing.objects.get(external_id="101")
        other = SourceSearch.objects.create(name="Other", search_url=SEARCH_URL + "&yearFrom=2022")
        SourceSearchMembership.objects.create(source_search=other, listing=listing, first_seen_at=listing.first_seen_at, last_seen_at=listing.last_seen_at)
        self.refresh(ids=("102",))
        self.assertFalse(SourceSearchMembership.objects.get(source_search=self.search, listing=listing).is_currently_present)
        self.assertTrue(SourceSearchMembership.objects.get(source_search=other, listing=listing).is_currently_present)
        listing.refresh_from_db()
        self.assertEqual(listing.status, Listing.Status.ACTIVE)

    def test_partial_run_does_not_mark_memberships_missing(self):
        self.refresh()
        next_url = SEARCH_URL + "&page=2"
        refresh_source_search(self.search, maximum_pages=1, delay_seconds=0, discovery_client=self.discovery_client(("102",), next_url=next_url), detail_client=self.detail_client())
        self.assertTrue(SourceSearchMembership.objects.get(listing__external_id="101").is_currently_present)
        self.assertEqual(self.search.runs.first().status, SourceSearchRun.Status.PARTIAL)

    def test_failed_detail_is_recorded_and_does_not_mark_missing(self):
        self.refresh()
        listing = Listing.objects.get(external_id="101")
        listing.source_url = "https://www.polovniautomobili.com/auto-oglasi/101"
        listing.save(update_fields=("source_url",))
        failing_client = client(lambda request: httpx.Response(403, request=request))
        result = refresh_source_search(
            self.search, delay_seconds=0,
            discovery_client=self.discovery_client(("101",)), detail_client=failing_client,
        )
        observation = SourceSearchRunListing.objects.get(source_search_run=result.run)
        self.assertEqual(observation.external_id, "101")
        self.assertEqual(observation.listing, listing)
        self.assertEqual(observation.detail_fetch_status, SourceSearchRunListing.DetailStatus.FAILED)
        self.assertEqual(result.run.status, SourceSearchRun.Status.FAILED)
        self.assertTrue(SourceSearchMembership.objects.get(listing__external_id="101").is_currently_present)
        listing.refresh_from_db()
        self.assertEqual(listing.source_url, "https://www.polovniautomobili.com/auto-oglasi/101/peugeot-3008")

    def test_dry_run_has_no_collection_side_effects(self):
        result = refresh_source_search(self.search, dry_run=True, maximum_pages=1, delay_seconds=0, discovery_client=self.discovery_client())
        self.assertEqual(len(result.discovery.advertisements), 1)
        self.assertEqual((SourceSearchRun.objects.count(), Listing.objects.count()), (0, 0))
        self.assertEqual((ListingSnapshot.objects.count(), SourceSearchMembership.objects.count()), (0, 0))

    def test_valid_existing_url_survives_bad_candidate(self):
        self.refresh()
        listing = Listing.objects.get(external_id="101")
        valid_url = listing.source_url
        resolved, source = resolve_public_ad_url(
            "/auto-oglasi/999/wrong", listing.external_id, valid_url
        )
        self.assertEqual((resolved, source), (valid_url, "search_result"))
        listing.refresh_from_db()
        self.assertEqual(listing.source_url, valid_url)


class RepairListingUrlTests(TestCase):
    def test_dry_run_and_repair_scope(self):
        observed = __import__("django.utils.timezone", fromlist=["now"]).now()
        malformed = Listing.objects.create(
            source="polovniautomobili", external_id="123", source_url="https://core.polovniautomobili.com/api/v1/classifieds/car/123",
            make="Peugeot", model="3008", year=2022, mileage=1, fuel="diesel",
            first_seen_at=observed, last_seen_at=observed,
        )
        other = Listing.objects.create(
            source="other", external_id="456", source_url="https://example.test/wrong",
            make="Peugeot", model="3008", year=2022, mileage=1, fuel="diesel",
            first_seen_at=observed, last_seen_at=observed,
        )
        call_command("repair_pa_listing_urls", "--dry-run", stdout=StringIO())
        malformed.refresh_from_db()
        self.assertIn("core.polovniautomobili.com", malformed.source_url)
        call_command("repair_pa_listing_urls", stdout=StringIO())
        malformed.refresh_from_db(); other.refresh_from_db()
        self.assertEqual(malformed.source_url, "https://www.polovniautomobili.com/auto-oglasi/123")
        self.assertEqual(other.source_url, "https://example.test/wrong")
