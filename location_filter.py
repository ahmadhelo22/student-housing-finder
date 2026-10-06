"""
location_filter.py
==================
Service that filters listings by how close they are to a specific location (Specific Location).
Takes the search results (OUTPUT 1) and the coordinates of the target location (latitude and longitude),
and returns only the listings inside a circle with a 5 km radius (by default) around that location,
adding each listing's coordinates and its distance from the location in km.

Where each listing's coordinates come from (in order):
1. The listing's own coordinates field, if present.
2. The on-disk coordinates cache (the same listing is never requested twice).
3. The listing's detail page via get_property_details (PropertyGuru, iProperty and SPEEDHOME).
   Mudah provides no coordinates at all, so its listings cannot be measured.
"""

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Literal, List, Dict, Any, Tuple
from urllib.parse import urlparse

import Scrapper_2 as scraper


# Default radius of the search circle around the specific location
DEFAULT_RADIUS_KM = 5.0
# Mean Earth radius in km
EARTH_RADIUS_KM = 6371.0088
# On-disk cache of listing coordinates, so detail pages are not re-requested on every run
COORDINATES_CACHE_FILE = "coordinates_cache.json"

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


def _parse_specific_location(specific_location: Dict[str, Any]) -> Tuple[float, float, float]:
    """Validate the specific location input and return (latitude, longitude, radius in km)."""
    coords = _parse_coordinates(specific_location)
    if coords is None:
        raise ValueError(
            "specific_location يجب أن يحتوي على latitude بين -90 و 90 و longitude بين -180 و 180"
        )
    radius_km = specific_location.get("radius_km")
    radius_km = DEFAULT_RADIUS_KM if radius_km is None else float(radius_km)
    if radius_km <= 0:
        raise ValueError("radius_km يجب أن يكون أكبر من صفر")
    return coords[0], coords[1], radius_km


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


def filter_by_distance(
    listings: List[Dict[str, Any]],
    specific_location: Dict[str, Any],
    keep_unknown: bool = False,
    sort_by: Literal["price", "distance"] = "price",
    cache_file: Optional[str] = COORDINATES_CACHE_FILE,
    output_file: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Main function: takes the search results (OUTPUT 1) and the specific location, and returns only the listings inside the radius.
    - specific_location: {"latitude": float, "longitude": float, "radius_km": float (optional, default 5)}
    - keep_unknown: if True, listings whose location cannot be determined are appended at the end with distance_km = None
    - sort_by: "price" (cheapest first, like the rest of the code) or "distance" (nearest first)
    Each listing in the result has the same fields as OUTPUT 1, plus coordinates and distance_km.
    """
    center_lat, center_lng, radius_km = _parse_specific_location(specific_location)
    cache = scraper._load_hash_cache(cache_file)

    # 1. Collect coordinates that are already known (from the listing itself or from the cache)
    known: Dict[str, Tuple[float, float]] = {}
    missing: List[str] = []
    for item in listings:
        url = item.get("property_url")
        coords = _parse_coordinates(item.get("coordinates")) or _parse_coordinates(cache.get(url))
        if coords:
            if url:
                known[url] = coords
        elif url and url not in missing and _platform_domain(url):
            missing.append(url)

    # 2. Request the missing detail pages concurrently, with a cap on simultaneous requests to avoid being blocked
    if missing:
        with ThreadPoolExecutor(max_workers=scraper.MAX_CONCURRENCY) as pool:
            for url, coords in zip(missing, pool.map(_fetch_coordinates, missing)):
                if coords:
                    known[url] = coords
                    cache[url] = list(coords)
        scraper._save_hash_cache(cache_file, cache)

    # 3. Compute the distance and keep only the listings inside the circle
    nearby, unknown = [], []
    for item in listings:
        coords = _parse_coordinates(item.get("coordinates")) or known.get(item.get("property_url"))
        if coords is None:
            if keep_unknown:
                unknown.append({**item, "coordinates": None, "distance_km": None})
            continue
        distance = haversine_km(center_lat, center_lng, coords[0], coords[1])
        if distance <= radius_km:
            nearby.append({
                **item,
                "coordinates": {"latitude": coords[0], "longitude": coords[1]},
                "distance_km": round(distance, 2),
            })

    if sort_by == "distance":
        nearby.sort(key=lambda p: p["distance_km"])
    else:
        nearby.sort(key=scraper._sort_key)
        unknown.sort(key=scraper._sort_key)
    results = nearby + unknown

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

    return results


if __name__ == "__main__":

    location = "Bukit Jalil"
    max_price = 1500
    housing_type = ["master_room", "medium_room"]
    # APU university in Bukit Jalil (approximate coordinates)
    specific_location = {"latitude": 3.0553, "longitude": 101.7006, "radius_km": 5}

    pg = scraper.Propertyguru()
    ip = scraper.IProperties()
    sh = scraper.Speedhome()

    total_start = time.perf_counter()

    # 1. Scrape all three sites together
    listings_pg, listings_ip, listings_sh = scraper.scrape_sites_parallel([
        (pg, pg.generate_url(location=location, max_price=max_price, housing_type=housing_type), None),
        (ip, ip.generate_url(location=location, max_price=max_price, housing_type=housing_type), None),
        (sh, sh.generate_url(location=location, max_price=max_price, housing_type=housing_type), None),
    ])

    # 2. Merge the results and remove duplicates
    merged = scraper.merge_and_deduplicate(
        sources=[("PropertyGuru", listings_pg), ("iProperty", listings_ip), ("Speedhome", listings_sh)],
        output_file=None,
    )

    # 3. Filter the listings by distance from the specific location
    t = time.perf_counter()
    nearby = filter_by_distance(merged, specific_location, sort_by="distance", output_file="nearby_properties.json")
    print(f"Distance filtering took: {time.perf_counter() - t:.2f}s")
    print(f"Total Execution Time: {time.perf_counter() - total_start:.2f}s")
    print(f"Merged: {len(merged)} | Within {specific_location['radius_km']} km: {len(nearby)}")
    for p in nearby:
        print(f"  {p['distance_km']:>5} km | {p['price']:<14} | {p['title']}")
