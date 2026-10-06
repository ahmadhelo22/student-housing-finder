# Scraper I/O Spec — `Scrapper_2.py`

## INPUT 1 — Search parameters

Defaults are applied when the user does not mention the field. `None` = no filter.

```python
SEARCH_INPUT = {
    "area": None,                      # str  (required) area name the sites search in, e.g. "Kuala Lumpur", "Melaka"
    "max_price": None,                 # int  RM/month (required)
    "min_price": None,                 # int  RM/month | None
    "housing_type": "studio",          # str | list[str]: "studio", "master_room", "medium_room", "small_room", "entire_unit", "studio_or_master_room"
    "room_type": None,                 # str | None: "master", "common", "medium", "shared", "small" (only when housing_type == "room")
    "bedrooms": None,                  # int | None (only with "entire_unit")
    "bathrooms": None,                 # int | None
    "property_structure": "high_rise", # "high_rise" | "landed"
    "floor_level": None,               # "HIGH" | "MID" | "LOW" | "PENT" | None
    "is_furnished": True,              # True | False
    "distance_to_mrt": None,           # int km | None
    "has_carpark": None,               # True | None
    "is_verified_agent": True,         # True | False
    "page": 1,                         # int
    "location": None,                  # dict | None: target point {"latitude": float, "longitude": float, "radius_km": float = 5}
}
```

Run a search with `search_pipeline.search(SEARCH_INPUT)` — the single entry point:

1. Scrape all platforms at once, searching by `area`.
2. Merge, remove duplicates, and save to `merged_properties.json` (OUTPUT 1).
3. If `location` is set: read that file, keep only listings within `radius_km` of the point,
   and save them to `nearby_properties.json` (OUTPUT 1B) — the file given to the user.

`location` is not sent to the sites; it is applied by `location_filter`.
`area` is passed to each platform's `generate_url(location=...)`.

`housing_type` accepted aliases:

| Value | Aliases |
|---|---|
| `"studio"` | `studio` |
| `"master_room"` | `master` |
| `"medium_room"` | `medium`, `common`, `common_room` |
| `"small_room"` | `small`, `single`, `shared`, `share` |
| `"entire_unit"` | `entire`, `unit` |
| `"studio_or_master_room"` | = `["studio", "master_room"]` |

## OUTPUT 1 — Search results

`list[dict]`, sorted by price ascending, duplicates removed.

```python
[
    {
        "title": str | None,
        "price": str,                  # e.g. "RM 630 /mo"
        "address": str | None,
        "property_url": str | None,
        "floor_area_sqm": float | None,
        "floor_area_sqft": int | None,
        "nearby_transit": str | None,
        "thumbnail_url": str | None,
        "is_verified_agent": bool,
        "agent_name": str | None,
        "building_id": int | None,     # PropertyGuru / iProperty building ID (same ID on both platforms)
        "coordinates": dict | None,    # {"latitude": float, "longitude": float} — SPEEDHOME only, else None
    },
]
```

## OUTPUT 1B — Search results near `location`

`list[dict]` — same fields as OUTPUT 1, only listings within `radius_km` of the point,
sorted by price ascending (or by distance with `sort_by="distance"`).

```python
[
    {
        # ...all OUTPUT 1 fields...
        "coordinates": {"latitude": float, "longitude": float},
        "distance_km": float,          # straight-line (haversine) distance, 2 decimals
    },
]
```

Where each listing's coordinates come from (in order):

| # | Case | Source | Requests |
|---|---|---|---|
| 1 | listing has `coordinates` (SPEEDHOME) | the search results | 0 |
| 2 | listing has `building_id` (PropertyGuru, iProperty) | detail page of one unit of that building | 1 per building |
| 3 | no building (e.g. a villa) | the listing's own detail page | 1 per listing |
| — | Mudah | not available — dropped (or kept with `distance_km: None` when `keep_unknown=True`); Mudah is not scraped at all when filtering, unless `keep_unknown=True` | 0 |

Cases 2 and 3 are cached in `coordinates_cache.json` (keys `building:<id>` or the listing URL),
so a building already seen is never requested again.

## INPUT 2 — Property detail

One `property_url` taken from OUTPUT 1.

```python
DETAIL_INPUT = {
    "property_url": str,               # "https://www.propertyguru.com.my/..." | "https://www.iproperty.com.my/..."
}
```

| URL domain | Function |
|---|---|
| `propertyguru.com.my` | `Propertyguru().get_property_details(property_url)` |
| `iproperty.com.my` | `IProperties().get_property_details(property_url)` |

## OUTPUT 2 — Property details

`dict`

```python
{
    "property_url": str,
    "title": str | None,
    "map_url": str | None,             # "https://www.google.com/maps?q=<lat>,<lng>"
    "location": {
        "building_name": str | None,
        "formatted_address": str | None,
        "street": str | None,
        "city": str | None,
        "state": str | None,
        "postal_code": str | None,
        "coordinates": {
            "latitude": float | None,
            "longitude": float | None,
        },
        "map_url": str | None,
    },
    "images": list[str],
    "property_details": {
        "property_name": str | None,
        "property_type": str | None,
        "bedrooms": int | None,
        "bathrooms": int | None,
        "floor_area_sqm": float | None,
        "floor_area_sqft": int | None,
        "price": str | None,
        "tenure": str | None,
        "furnishing": str | None,
        "completion_year": str | None,
        "listing_id": int | None,
        "posted_date": str | None,
        "highlights": list[str],
        "description": str,
    },
}
```
