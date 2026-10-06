# Scraper I/O Spec — `Scrapper_2.py`

## INPUT 1 — Search parameters

Defaults are applied when the user does not mention the field. `None` = no filter.

```python
SEARCH_INPUT = {
    "location": None,                  # str  (required)
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
}
```

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
    },
]
```

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
