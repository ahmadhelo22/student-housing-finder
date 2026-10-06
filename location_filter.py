"""
location_filter.py
==================
Service that filters listings by how close they are to the `location` given in SEARCH_INPUT.
Takes the search results (OUTPUT 1, usually from the scraper's JSON file) and the target point
(latitude and longitude), and returns only the listings inside a circle with a 5 km radius (by default)
around that point, adding each listing's coordinates and its distance from the point in km.

Where each listing's coordinates come from (in order):
1. The listing's own coordinates field (SPEEDHOME provides it in the search results): no request needed.
2. Its building: every unit in a building shares the building's location, so ONE detail page per building
   is enough (PropertyGuru and iProperty use the same building_id). E.g. 61 units in 11 buildings = 11 requests.
   Buildings are matched by building_id, not by name, because the same building appears under different names
   (e.g. "Residensi Bintang" and "Residensi Bintang, Bukit Jalil").
3. The listing itself, for units that do not belong to a building (e.g. a villa): one detail page per listing.
Locations from steps 2 and 3 are cached on disk, so a building already seen is never requested again.
Mudah provides no coordinates at all, so its listings cannot be measured.
"""

import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Literal, List, Dict, Any, Tuple
from urllib.parse import urlparse

import Scrapper_2 as scraper


# Default radius of the search circle around the target point
DEFAULT_RADIUS_KM = 5.0
# Mean Earth radius in km
EARTH_RADIUS_KM = 6371.0088
# On-disk cache of building / listing coordinates, so detail pages are not re-requested on every run
COORDINATES_CACHE_FILE = "coordinates_cache.json"
# How many listings of the same building to try if a detail page fails (e.g. the listing was just removed)
MAX_ATTEMPTS_PER_LOCATION = 2

# Platforms that provide coordinates on the detail page (Mudah is missing because it does not)
PLATFORMS_WITH_COORDINATES = {
    "propertyguru.com.my": scraper.Propertyguru,
    "iproperty.com.my": scraper.IProperties,
    "speedhome.com": scraper.Speedhome,
}

# Each thread gets its own platform objects, because curl_cffi sessions are not safe to share between threads
_thread_local = threading.local()


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance between two points on the Earth's surface, in km (Haversine formula)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _parse_coordinates(value: Any) -> Optional[Tuple[float, float]]:
    """
    Convert coordinates to (latitude, longitude) and validate them.
    Accepts a dict with latitude and longitude keys, or a two-number list [lat, lng].
    Returns None if the coordinates are missing or out of range.
    """
    if isinstance(value, dict):
        lat, lng = value.get("latitude"), value.get("longitude")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        lat, lng = value
    else:
        return None
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def parse_location(location: Dict[str, Any]) -> Tuple[float, float, float]:
    """Validate the `location` input and return (latitude, longitude, radius in km)."""
    if not isinstance(location, dict):
        # The area name used to live in "location"; it is now in "area"
        raise ValueError("location يجب أن يكون قاموساً فيه latitude و longitude، واسم المنطقة يوضع في area")
    coords = _parse_coordinates(location)
    if coords is None:
        raise ValueError("location يجب أن يحتوي على latitude بين -90 و 90 و longitude بين -180 و 180")
    radius_km = location.get("radius_km")
    radius_km = DEFAULT_RADIUS_KM if radius_km is None else float(radius_km)
    if radius_km <= 0:
        raise ValueError("radius_km يجب أن يكون أكبر من صفر")
    return coords[0], coords[1], radius_km


def _location_key(item: Dict[str, Any]) -> Optional[str]:
    """Cache key of a listing's location: its building if it belongs to one, otherwise the listing itself."""
    if item.get("building_id") is not None:
        return f"building:{item['building_id']}"
    return item.get("property_url")


def _platform_domain(url: str) -> Optional[str]:
    """Return the platform domain if it provides coordinates, otherwise None."""
    host = urlparse(url).netloc.lower()
    for domain in PLATFORMS_WITH_COORDINATES:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def _fetch_coordinates(url: str) -> Optional[Tuple[float, float]]:
    """Extract the coordinates of a single listing from its detail page."""
    domain = _platform_domain(url)
    if domain is None:
        return None
    sites = getattr(_thread_local, "sites", None)
    if sites is None:
        sites = _thread_local.sites = {}
    if domain not in sites:
        sites[domain] = PLATFORMS_WITH_COORDINATES[domain]()
    site = sites[domain]
    try:
        details = site.get_property_details(url)
    except Exception:
        return None
    return _parse_coordinates((details.get("location") or {}).get("coordinates"))


def _fetch_first(urls: List[str]) -> Optional[Tuple[float, float]]:
    """Try the detail pages of one location (building or listing) in order, and return the first coordinates found."""
    for url in urls[:MAX_ATTEMPTS_PER_LOCATION]:
        coords = _fetch_coordinates(url)
        if coords:
            return coords
    return None


def _sort_results(listings: List[Dict[str, Any]], sort_by: Literal["price", "distance"]) -> None:
    """Sort filtered listings in place, cheapest or nearest first, with unknown-distance listings last."""
    def key(item: Dict[str, Any]):
        unknown = item.get("distance_km") is None
        if sort_by == "distance":
            return unknown, item.get("distance_km") or 0.0
        return unknown, scraper._sort_key(item)

    listings.sort(key=key)


def filter_by_distance(
    listings: List[Dict[str, Any]],
    location: Dict[str, Any],
    keep_unknown: bool = False,
    sort_by: Literal["price", "distance"] = "price",
    cache_file: Optional[str] = COORDINATES_CACHE_FILE,
    output_file: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Main function: takes the search results (OUTPUT 1) and the target point, and returns only the listings inside the radius.
    - location: {"latitude": float, "longitude": float, "radius_km": float (optional, default 5)}
    - keep_unknown: if True, listings whose location cannot be determined are kept, at the end, with distance_km = None
    - sort_by: "price" (cheapest first, like the rest of the code) or "distance" (nearest first)
    Each listing in the result has the same fields as OUTPUT 1, plus coordinates and distance_km.
    """
    center_lat, center_lng, radius_km = parse_location(location)
    # Maps a location key ("building:<id>" or a listing URL) to [lat, lng]
    cache = scraper._load_hash_cache(cache_file)

    # 1. Group the listings that still need a location by building (or by listing when there is no building)
    pending: Dict[str, List[str]] = {}
    for item in listings:
        key = _location_key(item)
        if not key or _parse_coordinates(item.get("coordinates")) or _parse_coordinates(cache.get(key)):
            continue
        url = item.get("property_url")
        if url and _platform_domain(url):
            pending.setdefault(key, []).append(url)

    # 2. One detail request per unknown location, all at once, with a cap on simultaneous requests to avoid being blocked
    if pending:
        keys = list(pending)
        with ThreadPoolExecutor(max_workers=scraper.MAX_CONCURRENCY) as pool:
            for key, coords in zip(keys, pool.map(_fetch_first, (pending[k] for k in keys))):
                if coords:
                    cache[key] = list(coords)
        scraper._save_hash_cache(cache_file, cache)

    # 3. Compute the distance and keep only the listings inside the circle
    results = []
    for item in listings:
        coords = _parse_coordinates(item.get("coordinates")) or _parse_coordinates(cache.get(_location_key(item)))
        if coords is None:
            if keep_unknown:
                results.append({**item, "coordinates": None, "distance_km": None})
            continue
        distance = haversine_km(center_lat, center_lng, coords[0], coords[1])
        if distance <= radius_km:
            results.append({
                **item,
                "coordinates": {"latitude": coords[0], "longitude": coords[1]},
                "distance_km": round(distance, 2),
            })

    _sort_results(results, sort_by)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

    return results


def filter_file(
    input_file: str,
    location: Dict[str, Any],
    output_file: str = "nearby_properties.json",
    **kwargs,
) -> List[Dict[str, Any]]:
    """Read the scraper's JSON file, filter it by distance from `location`, and save the result as a new JSON file."""
    with open(input_file, "r", encoding="utf-8") as f:
        listings = json.load(f)
    return filter_by_distance(listings, location, output_file=output_file, **kwargs)
