"""
search_pipeline.py
==================
Single entry point of the project: takes SEARCH_INPUT (INPUT 1) and returns the final listings.

Steps:
1. Scrape all platforms at the same time (Scrapper_2).
2. If specific_location is set: keep only the listings within its radius (location_filter).
   Filtering runs BEFORE merging, so merge_and_deduplicate never downloads photos of far-away listings.
3. Merge the platforms and remove duplicates (merge_and_deduplicate).
"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Literal, List, Dict, Any, Callable

import Scrapper_2 as scraper
import location_filter


def _safe(platform_name: str, task: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Run one platform's scrape; if it fails, the search continues with the other platforms."""
    try:
        return task()
    except Exception as e:
        print(f"[تنبيه] فشل السحب من {platform_name}: {e}")
        return []


def _scrape_all(params: Dict[str, Any], include_mudah: bool) -> List[Dict[str, Any]]:
    """Scrape every platform at the same time and return all their listings in one list."""
    pg, ip, sh = scraper.Propertyguru(), scraper.IProperties(), scraper.Speedhome()

    def scrape_pg_ip() -> List[Dict[str, Any]]:
        # PropertyGuru and iProperty share one parser, so their pages are downloaded together in one batch
        pg_listings, ip_listings = scraper.scrape_sites_parallel([
            (pg, pg.generate_url(**params), None),
            (ip, ip.generate_url(**params), None),
        ])
        return pg_listings + ip_listings

    # SPEEDHOME and Mudah have their own parsers (and local filters), so they use their own scrape_to_json
    tasks = [
        ("PropertyGuru + iProperty", scrape_pg_ip),
        ("Speedhome", lambda: sh.scrape_to_json(sh.generate_url(**params), output_file=None)),
    ]
    if include_mudah:
        md = scraper.Mudah()
        tasks.append(("Mudah", lambda: md.scrape_to_json(md.generate_url(**params), output_file=None)))

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        results = list(pool.map(lambda t: _safe(*t), tasks))
    return [item for listings in results for item in listings]


def search(
    search_input: Dict[str, Any],
    sort_by: Literal["price", "distance"] = "price",
    keep_unknown: bool = False,
    output_file: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Run a full search from SEARCH_INPUT.
    - Without specific_location: returns OUTPUT 1 (all platforms merged, cheapest first).
    - With specific_location: returns OUTPUT 1B (only listings within radius_km, plus coordinates and distance_km).
      sort_by and keep_unknown only apply in this case (see location_filter.filter_by_distance).
    """
    if not search_input.get("location") or search_input.get("max_price") is None:
        raise ValueError("location و max_price مطلوبان في SEARCH_INPUT")

    specific_location = search_input.get("specific_location")
    if specific_location is not None:
        # Fail fast, before sending any request
        location_filter.parse_specific_location(specific_location)

    # None means "not mentioned": drop it so each generate_url applies its own default
    params = {k: v for k, v in search_input.items() if k != "specific_location" and v is not None}

    # Mudah has no coordinates, so it is skipped when all of its listings would be dropped anyway
    listings = _scrape_all(params, include_mudah=specific_location is None or keep_unknown)

    if specific_location is not None:
        listings = location_filter.filter_by_distance(
            listings, specific_location, keep_unknown=keep_unknown, sort_by=None,
        )

    results = scraper.merge_and_deduplicate(sources=[("All platforms", listings)], output_file=None)

    if specific_location is not None:
        # merge_and_deduplicate sorts by price; apply the requested order (unknown-distance listings last)
        location_filter.sort_results(results, sort_by)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=4)

    return results


if __name__ == "__main__":

    SEARCH_INPUT = {
        "location": "Bukit Jalil",
        "max_price": 1500,
        "housing_type": ["master_room", "medium_room", "small_room", "studio"],
        # APU university in Bukit Jalil (approximate coordinates)
        "specific_location": {"latitude": 3.0553, "longitude": 101.7006, "radius_km": 5},
    }

    start = time.perf_counter()
    results = search(SEARCH_INPUT, sort_by="distance", output_file="nearby_properties.json")
    print(f"Total Execution Time: {time.perf_counter() - start:.2f}s")
    print(f"Within {SEARCH_INPUT['specific_location']['radius_km']} km: {len(results)}")
    for p in results:
        print(f"  {p['distance_km']:>5} km | {p['price']:<14} | {p['title']}")
