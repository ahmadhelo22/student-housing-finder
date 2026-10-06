"""
search_pipeline.py
==================
Single entry point of the project: takes SEARCH_INPUT (INPUT 1) and returns the final listings.

Workflow:
1. Scrape all platforms at the same time, using the `area` name (e.g. "Kuala Lumpur").
2. Merge the platforms, remove duplicates, and save everything to SCRAPED_FILE (OUTPUT 1).
3. If `location` is set: read SCRAPED_FILE, keep only the listings within radius_km of the point
   (building by building, see location_filter), and save them to OUTPUT_FILE (OUTPUT 1B).
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal, List, Dict, Any, Callable

import Scrapper_2 as scraper
import location_filter


# All search results (scraper output)
SCRAPED_FILE = "merged_properties.json"
# Listings near `location` (filter output, given to the user)
OUTPUT_FILE = "nearby_properties.json"


def _safe(platform_name: str, task: Callable[[], List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Run one platform's scrape; if it fails, the search continues with the other platforms."""
    try:
        return task()
    except Exception as e:
        print(f"[تنبيه] فشل السحب من {platform_name}: {e}")
        return []


def _scrape_all(params: Dict[str, Any], include_mudah: bool) -> List[List[Dict[str, Any]]]:
    """Scrape every platform at the same time and return one list of listings per platform."""
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
        return list(pool.map(lambda t: _safe(*t), tasks))


def search(
    search_input: Dict[str, Any],
    sort_by: Literal["price", "distance"] = "price",
    keep_unknown: bool = False,
    scraped_file: str = SCRAPED_FILE,
    output_file: str = OUTPUT_FILE,
) -> List[Dict[str, Any]]:
    """
    Run a full search from SEARCH_INPUT.
    - Without `location`: returns OUTPUT 1 (all platforms merged, cheapest first), saved in scraped_file.
    - With `location`: returns OUTPUT 1B (only listings within radius_km, plus coordinates and distance_km),
      saved in output_file. sort_by and keep_unknown only apply in this case (see location_filter.filter_by_distance).
    """
    if not search_input.get("area") or search_input.get("max_price") is None:
        raise ValueError("area و max_price مطلوبان في SEARCH_INPUT")

    location = search_input.get("location")
    if location is not None:
        # Fail fast, before sending any request
        location_filter.parse_location(location)

    # The sites search by area name; None means "not mentioned", so it is dropped and generate_url applies its default
    params = {k: v for k, v in search_input.items() if k not in ("area", "location") and v is not None}
    params["location"] = search_input["area"]

    # Mudah has no coordinates, so it is skipped when all of its listings would be dropped anyway
    per_platform = _scrape_all(params, include_mudah=location is None or keep_unknown)

    # Steps 1-2: merge and save all the search results
    names = ["PropertyGuru + iProperty", "Speedhome", "Mudah"]
    results = scraper.merge_and_deduplicate(sources=list(zip(names, per_platform)), output_file=scraped_file)

    # Step 3: filter the saved file by distance from the target point
    if location is not None:
        results = location_filter.filter_file(
            scraped_file, location, output_file=output_file, sort_by=sort_by, keep_unknown=keep_unknown,
        )

    return results


if __name__ == "__main__":

    SEARCH_INPUT = {
        "area": "Bukit Jalil",
        "max_price": 1500,
        "housing_type": ["master_room", "medium_room", "small_room", "studio"],
        # APU university in Bukit Jalil (approximate coordinates)
        "location": {"latitude": 3.0553, "longitude": 101.7006, "radius_km": 5},
    }

    start = time.perf_counter()
    results = search(SEARCH_INPUT, sort_by="distance")
    print(f"Total Execution Time: {time.perf_counter() - start:.2f}s")
    print(f"Within {SEARCH_INPUT['location']['radius_km']} km: {len(results)} (saved in {OUTPUT_FILE})")
    for p in results:
        print(f"  {p['distance_km']:>5} km | {p['price']:<14} | {p['title']}")
