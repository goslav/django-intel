import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from vehicles.models import validate_polovni_search_url


USER_AGENT = "DjangoMarketIntel/0.1 (+local dealership market research)"
PUBLIC_BASE_URL = "https://www.polovniautomobili.com"
ADVERTISEMENT_PATH = re.compile(r"^/auto-oglasi/(\d+)(?:/([^/]+))?/?$")


class CollectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdvertisementLink:
    external_id: str
    public_url: str
    thumbnail_url: str = ""


@dataclass(frozen=True)
class DiscoveryResult:
    advertisements: tuple[AdvertisementLink, ...]
    pages_requested: int
    pages_completed: int
    is_complete: bool
    was_truncated: bool


def canonicalize_public_ad_url(href: str, expected_listing_id=None) -> tuple[str, str]:
    """Validate a search-page advertisement href and return its ID and public URL."""
    if not isinstance(href, str) or not href.strip():
        raise ValueError("A public advertisement URL is required.")
    parsed_input = urlparse(href.strip())
    if parsed_input.scheme and parsed_input.scheme != "https":
        raise ValueError("Public advertisement URLs must use HTTPS.")
    if parsed_input.netloc and parsed_input.hostname not in {"polovniautomobili.com", "www.polovniautomobili.com"}:
        raise ValueError("Public advertisement URL has an invalid host.")
    absolute = urljoin(f"{PUBLIC_BASE_URL}/", href.strip())
    parsed = urlparse(absolute)
    if parsed.scheme != "https" or parsed.hostname not in {"polovniautomobili.com", "www.polovniautomobili.com"}:
        raise ValueError("Public advertisement URL is not on the expected HTTPS host.")
    path = re.sub(r"/{2,}", "/", parsed.path)
    match = ADVERTISEMENT_PATH.fullmatch(path)
    if not match or (match.group(2) and match.group(2).casefold().endswith(".pdf")):
        raise ValueError("URL is not a public vehicle advertisement.")
    listing_id, slug = match.group(1), match.group(2)
    if expected_listing_id is not None and listing_id != str(expected_listing_id):
        raise ValueError("Public URL advertisement ID does not match the detail record.")
    canonical_path = f"/auto-oglasi/{listing_id}"
    if slug:
        canonical_path += f"/{slug}"
    return listing_id, urlunparse(("https", "www.polovniautomobili.com", canonical_path, "", "", ""))


def public_ad_url_fallback(listing_id) -> str:
    listing_id = str(listing_id)
    if not listing_id.isdigit():
        raise ValueError("A numeric listing ID is required for a public URL fallback.")
    return f"{PUBLIC_BASE_URL}/auto-oglasi/{listing_id}"


def resolve_public_ad_url(candidate_href, listing_id, existing_url="") -> tuple[str, str]:
    """Prefer a valid search href, then an existing canonical URL, then the ID fallback."""
    for href, source in ((candidate_href, "search_result"), (existing_url, "search_result")):
        try:
            _, public_url = canonicalize_public_ad_url(href, expected_listing_id=listing_id)
            return public_url, source
        except ValueError:
            pass
    return public_ad_url_fallback(listing_id), "id_fallback"


def _has_slug(public_url: str) -> bool:
    return bool(ADVERTISEMENT_PATH.fullmatch(urlparse(public_url).path).group(2))


def canonicalize_thumbnail_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme != "https" or parsed.hostname != "cdn.polovniautomobili.com":
        raise ValueError("Thumbnail URL is not on the expected HTTPS CDN host.")
    return urlunparse(("https", "cdn.polovniautomobili.com", parsed.path, "", "", ""))


def _search_result_thumbnails(soup) -> dict[str, str]:
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return {}
    try:
        page_props = json.loads(script.string)["props"]["pageProps"]
    except (KeyError, TypeError, ValueError):
        return {}
    thumbnails = {}
    for collection_name in ("searchResults", "topSearchAds"):
        for result in (page_props.get(collection_name) or {}).get("results", []):
            listing_id = str(result.get("id") or "")
            images = result.get("images") or []
            candidate_url = result.get("imageMain") or (images[0] if images else "")
            if not listing_id.isdigit() or not candidate_url:
                continue
            try:
                thumbnails[listing_id] = canonicalize_thumbnail_url(candidate_url)
            except ValueError:
                continue
    return thumbnails


def _same_search(current_url: str, candidate: str) -> bool:
    current, other = urlparse(current_url), urlparse(candidate)
    ignored = {"page", "pageNumber", "currentPage"}
    current_query = sorted((key, value) for key, value in parse_qsl(current.query, keep_blank_values=True) if key not in ignored)
    other_query = sorted((key, value) for key, value in parse_qsl(other.query, keep_blank_values=True) if key not in ignored)
    return current.hostname == other.hostname and current.path == other.path and current_query == other_query


def parse_search_page(html: str, page_url: str) -> tuple[tuple[AdvertisementLink, ...], str | None]:
    lowered = html.casefold()
    if any(marker in lowered for marker in ("captcha", "cf-chl-", "verify you are human", "access denied")):
        raise CollectionError("The search page returned a CAPTCHA or access challenge.")
    if "login" in lowered and "password" in lowered:
        raise CollectionError("The search page unexpectedly requires login.")
    soup = BeautifulSoup(html, "html.parser")
    thumbnails = _search_result_thumbnails(soup)
    found = {}
    for anchor in soup.find_all("a", href=True):
        try:
            listing_id, public_url = canonicalize_public_ad_url(anchor["href"])
        except ValueError:
            continue
        candidate = AdvertisementLink(listing_id, public_url, thumbnails.get(listing_id, ""))
        existing = found.get(listing_id)
        if existing is None or (_has_slug(public_url) and not _has_slug(existing.public_url)):
            found[listing_id] = candidate
    if not found:
        raise CollectionError("No advertisement links were found; the search HTML structure may have changed.")

    next_url = None
    current_page = int(dict(parse_qsl(urlparse(page_url).query)).get("page", "1"))
    candidates = soup.select('a[rel~="next"][href], .pagination a[href], nav[aria-label*="pagination" i] a[href], #paginationRow a[href], .ant-pagination a[href]')
    for anchor in candidates:
        label = " ".join((anchor.get_text(" ", strip=True), anchor.get("aria-label", ""))).casefold()
        parent_classes = anchor.parent.get("class", []) if anchor.parent else []
        is_next_page_number = label.isdigit() and int(label) == current_page + 1
        is_ant_next = "ant-pagination-next" in parent_classes
        if not ("next" in label or "slede" in label or label in {">", ">>"} or "next" in anchor.get("rel", []) or is_next_page_number or is_ant_next):
            continue
        candidate = urljoin(page_url, anchor["href"])
        try:
            validate_polovni_search_url(candidate)
        except Exception as exc:
            raise CollectionError("Pagination left the configured passenger-car search.") from exc
        if not _same_search(page_url, candidate):
            raise CollectionError("Pagination left the configured search filters.")
        next_url = candidate
        break
    return tuple(found.values()), next_url


def discover_advertisements(search_url: str, *, maximum_pages: int, delay_seconds: float, client=None) -> DiscoveryResult:
    validate_polovni_search_url(search_url)
    owns_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=httpx.Timeout(20, connect=10),
        follow_redirects=True, transport=httpx.HTTPTransport(retries=2),
    )
    advertisements = {}
    requested = completed = 0
    current_url = search_url
    truncated = False
    try:
        while current_url and completed < maximum_pages:
            requested += 1
            try:
                response = client.get(current_url)
            except httpx.HTTPError as exc:
                raise CollectionError(f"Search request failed: {exc}") from exc
            if response.status_code in (403, 429):
                raise CollectionError(f"Search stopped safely after HTTP {response.status_code}.")
            if response.status_code >= 500:
                raise CollectionError(f"Search failed after repeated server response HTTP {response.status_code}.")
            if response.status_code != 200:
                raise CollectionError(f"Unexpected search response HTTP {response.status_code}.")
            links, next_url = parse_search_page(response.text, current_url)
            completed += 1
            for link in links:
                existing = advertisements.get(link.external_id)
                if existing is None or (_has_slug(link.public_url) and not _has_slug(existing.public_url)):
                    advertisements[link.external_id] = link
            current_url = next_url
            if current_url and completed >= maximum_pages:
                truncated = True
                break
            if current_url and delay_seconds:
                time.sleep(delay_seconds)
    finally:
        if owns_client:
            client.close()
    return DiscoveryResult(tuple(advertisements.values()), requested, completed, not current_url and not truncated, truncated)
