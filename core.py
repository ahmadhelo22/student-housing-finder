import os
import re
import sys
import json
import logging
from typing import Callable, Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()


SCRAPER_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRAPER_DIR not in sys.path:
    sys.path.insert(0, SCRAPER_DIR)

import search_pipeline  

MAX_LISTINGS_IN_REPLY = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

MANDATORY_FIELDS = ["location", "housing_type", "max_price"]

HOUSING_TYPE_ALIASES = {
    "studio": "studio",
    "master": "master_room",
    "master_room": "master_room",
    "medium": "medium_room",
    "common": "medium_room",
    "common_room": "medium_room",
    "medium_room": "medium_room",
    "small": "small_room",
    "single": "small_room",
    "shared": "small_room",
    "share": "small_room",
    "small_room": "small_room",
    "entire": "entire_unit",
    "unit": "entire_unit",
    "entire_unit": "entire_unit",
    "studio_or_master": "studio_or_master_room",
    "studio_or_master_room": "studio_or_master_room",
}

VALID_HOUSING_TYPES = set(HOUSING_TYPE_ALIASES.values())

SEARCH_DEFAULTS = {
    "location": None,
    "max_price": None,
    "min_price": 0,
    "housing_type": None,
    "room_type": None,
    "bedrooms": None,
    "bathrooms": None,
    "property_structure": "high_rise",
    "floor_level": None,
    "is_furnished": True,
    "distance_to_mrt": None,
    "has_carpark": None,
    "is_verified_agent": True,
    "page": 1,
}

FIELD_PROMPTS = {
    "location": "which area or city are you looking in",
    "housing_type": "what type of housing (studio, master room, medium/common room, small/shared room, or entire unit)",
    "max_price": "what's your maximum monthly budget (RM)",
}

EXTRACTION_SYSTEM_PROMPT = """You are a data-extraction assistant for a room/property rental search bot in Malaysia.

Extract the following fields from the user's message. Respond with ONLY a raw JSON object — no markdown fences, no explanation, no extra keys.
The user may write in any language (English, Malay, Arabic, Chinese...) and may change a filter they gave before; always extract what THIS message says.

Fields:
- location: string or null. The city/area/neighbourhood the user wants (e.g. "Kuala Lumpur", "Cheras", "Petaling Jaya").
- housing_type: string, list of strings, or null. Each value must be exactly one of: "studio", "master_room", "medium_room", "small_room", "entire_unit".
  Use a list when the user is open to several types (e.g. "studio or master room" -> ["studio", "master_room"]).
  Mapping guide:
    studio -> studio
    master room / master -> master_room
    medium room / middle room / common room / common -> medium_room
    small room / single room / shared room / share -> small_room
    entire unit / whole unit / entire apartment / whole apartment / whole house -> entire_unit
- max_price: integer or null. Maximum monthly rent in RM. If the user gives a single price with no range, treat it as max_price.
- min_price: integer or null. Minimum monthly rent in RM — only if the user explicitly gives a range or a minimum.
- bedrooms: integer or null. Number of bedrooms (for an entire unit), e.g. "2 bedroom" / "2BR" -> 2.
- bathrooms: integer or null. Number of bathrooms, e.g. "2 bathrooms" -> 2.
- property_structure: "high_rise", "landed", or null.
    condo / apartment / serviced residence / flat / high rise -> "high_rise"
    landed / terrace / semi-d / bungalow / house with land -> "landed"
- floor_level: "LOW", "MID", "HIGH", "PENT", or null. Only when the user describes the floor in words:
    low floor / ground floor -> "LOW"; middle / mid floor -> "MID"; high floor / top floor -> "HIGH"; penthouse -> "PENT"
- floor_number: integer or null. Only when the user gives a specific floor number, e.g. "floor 10" / "10th floor" -> 10.
- is_furnished: true, false, or null.
    fully furnished / furnished -> true
    unfurnished / partially furnished / don't care about furniture / any furnishing -> false
- distance_to_mrt: integer or null. Max distance to MRT/LRT in km, e.g. "within 1km of MRT" -> 1. "near MRT" with no number -> 1.
- has_carpark: true or null. true only if the user wants a car park / parking.
- is_verified_agent: true, false, or null. false only if the user says any agent / owner / unverified is fine.

Rules:
- Only fill a field if the message clearly states it. Never guess or invent a value.
- If a field is not mentioned, its value must be null.
- Output valid JSON with exactly these keys: location, housing_type, max_price, min_price, bedrooms, bathrooms,
  property_structure, floor_level, floor_number, is_furnished, distance_to_mrt, has_carpark, is_verified_agent.
"""

EXTRACTED_FIELDS = [
    "location", "housing_type", "max_price", "min_price",
    "bedrooms", "bathrooms", "property_structure", "floor_level", "floor_number",
    "is_furnished", "distance_to_mrt", "has_carpark", "is_verified_agent",
]

FLOOR_LEVELS = {"LOW", "MID", "HIGH", "PENT"}


def floor_number_to_level(floor: int) -> str:
    
    if floor <= 5:
        return "LOW"
    if floor <= 15:
        return "MID"
    return "HIGH"


def _to_int(value) -> Optional[int]:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _to_bool(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def clean_extracted(extracted: dict) -> dict:
   
    cleaned = {}

    if isinstance(extracted.get("location"), str) and extracted["location"].strip():
        cleaned["location"] = extracted["location"].strip()

    housing_type = normalize_housing_type(extracted.get("housing_type"))
    if isinstance(housing_type, list):
        housing_type = [h for h in dict.fromkeys(housing_type) if h in VALID_HOUSING_TYPES]
        if len(housing_type) == 1:
            housing_type = housing_type[0]
    if housing_type and (isinstance(housing_type, list) or housing_type in VALID_HOUSING_TYPES):
        cleaned["housing_type"] = housing_type

    for key in ("max_price", "min_price", "bedrooms", "bathrooms", "distance_to_mrt", "floor_number"):
        number = _to_int(extracted.get(key))
        if number is not None:
            cleaned[key] = number

    if extracted.get("property_structure") in ("high_rise", "landed"):
        cleaned["property_structure"] = extracted["property_structure"]

    floor_level = str(extracted.get("floor_level") or "").upper()
    if floor_level in FLOOR_LEVELS:
        cleaned["floor_level"] = floor_level
    if "floor_number" in cleaned:
       
        cleaned["floor_level"] = floor_number_to_level(cleaned["floor_number"])
    elif "floor_level" in cleaned:
        cleaned["floor_number"] = None 

    for key in ("is_furnished", "is_verified_agent"):
        flag = _to_bool(extracted.get(key))
        if flag is not None:
            cleaned[key] = flag

   
    if _to_bool(extracted.get("has_carpark")) is True:
        cleaned["has_carpark"] = True

    return cleaned


def normalize_housing_type(value):
    if value is None:
        return None
    if isinstance(value, list):
        return [HOUSING_TYPE_ALIASES.get(str(v).lower().strip(), str(v).lower().strip()) for v in value]
    key = str(value).lower().strip()
    return HOUSING_TYPE_ALIASES.get(key, key)


def extract_fields_with_llm(user_text: str) -> dict:
    """Call the LLM to pull structured fields out of free text. Returns {} on failure."""
    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_text}],
        )
        raw_text = "".join(block.text for block in response.content if block.type == "text").strip()
        raw_text = re.sub(r"^```(json)?|```$", "", raw_text, flags=re.MULTILINE).strip()
        return json.loads(raw_text)
    except Exception as exc:
        logger.error("LLM extraction failed: %s", exc)
        return {}


def merge_fields(session: dict, extracted: dict) -> dict:
    """Add the fields from this message to what the user said before (newer values win)."""
    merged = dict(session)
    merged.update(clean_extracted(extracted))
    return merged


def missing_mandatory_fields(session: dict) -> list:
    return [f for f in MANDATORY_FIELDS if session.get(f) in (None, "", [])]


def build_search_input(session: dict) -> dict:
    payload = dict(SEARCH_DEFAULTS)
    payload.update(session)
    if payload.get("min_price") is None:
        payload["min_price"] = 0
    return payload


def to_scraper_input(search_input: dict) -> dict:

    scraper_input = {k: v for k, v in search_input.items() if k not in ("location", "floor_number")}
    scraper_input["area"] = search_input.get("location")
    scraper_input["location"] = None
    if scraper_input.get("min_price") == 0:
        scraper_input["min_price"] = None
    return scraper_input


def run_scraper(search_input: dict) -> dict:
 
    scraper_input = to_scraper_input(search_input)
    logger.info("run_scraper() called with: %s", scraper_input)
    try:
     
        listings = search_pipeline.search(scraper_input, scraped_file=None)
    except Exception as exc:
        logger.exception("Scraper failed")
        return {"status": "error", "error": str(exc), "count": 0, "listings": []}
    return {"status": "ok", "count": len(listings), "listings": listings}


def format_results(search_input: dict, scraper_result: dict) -> str:
    header = (
        f"{search_input['housing_type']} in {search_input['location']}, "
        f"RM{search_input['min_price']} - RM{search_input['max_price']}"
    )
    if scraper_result["status"] != "ok":
        return f"Sorry, the search failed ({header}). Please try again in a bit."

    listings = scraper_result["listings"]
    if not listings:
        return f"No listings found for {header}. Try a higher budget or a nearby area."

    shown = listings[:MAX_LISTINGS_IN_REPLY]
    lines = [f"Found {len(listings)} listings for {header}. Cheapest first:\n"]
    for i, p in enumerate(shown, 1):
        entry = f"{i}. {p.get('title') or 'Untitled'}\n   {p.get('price') or 'Price n/a'}"
        if p.get("address"):
            entry += f" | {p['address']}"
        if p.get("nearby_transit"):
            entry += f"\n   {p['nearby_transit']}"
        if p.get("property_url"):
            entry += f"\n   {p['property_url']}"
        lines.append(entry)
    footer = "\nSend a new message to start another search."
    # Drop listings from the end until the reply fits in one Telegram message
    while len(lines) > 2 and len("\n".join(lines + [footer])) > 4000:
        lines.pop()
    return "\n".join(lines + [footer])


def listing_key(listing: dict) -> Optional[str]:

    listing_id, _, _ = search_pipeline.scraper.extract_identifiers(listing)
    return listing_id or listing.get("property_url")


MAX_PAGES_PER_BATCH = 3


def fetch_next_batch(
    search_input: dict,
    seen_keys: set,
    page: int,
    batch_size: int = MAX_LISTINGS_IN_REPLY,
) -> dict:
    """
    Re-run the scraper and return the next `batch_size` listings the user hasn't seen yet.
    Nothing is cached between taps: only `seen_keys` (listing IDs) and the current `page` are kept.

    Starts at `page`; when a page has no unseen listings left, moves on to page + 1.
    Returns {"status", "listings", "page", "exhausted"}. `page` is where the next call should start.
    """
    batch: list = []
    batch_keys: set = set()
    exhausted = False

    for _ in range(MAX_PAGES_PER_BATCH):
        result = run_scraper({**search_input, "page": page})
        if result["status"] != "ok":
            return {"status": "error", "listings": batch, "page": page, "exhausted": False}

        new_on_page = 0
        for listing in result["listings"]:
            key = listing_key(listing)
            if key in seen_keys or key in batch_keys:
                continue
            new_on_page += 1
            if len(batch) < batch_size:
                batch.append(listing)
                if key:
                    batch_keys.add(key)

        if len(batch) >= batch_size:
            
            break
        if new_on_page == 0 and (page > 1 or not result["listings"]):
           
            exhausted = True
            break
        page += 1  
    else:
     
        exhausted = not batch

    return {"status": "ok", "listings": batch, "page": page, "exhausted": exhausted}


def format_summary(search_input: dict, scraper_result: dict) -> str:
    """One-line header sent before the per-listing cards."""
    header = (
        f"{search_input['housing_type']} in {search_input['location']}, "
        f"RM{search_input['min_price']} - RM{search_input['max_price']}"
    )
    if scraper_result["status"] != "ok":
        return f"Sorry, the search failed ({header}). Please try again in a bit."
    count = scraper_result["count"]
    if not count:
        return f"No listings found for {header}. Try a higher budget or a nearby area."
    shown = min(count, MAX_LISTINGS_IN_REPLY)
    return f"Found listings for {header}. Here are the cheapest {shown}:"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_listing_caption(listing: dict, index: Optional[int] = None) -> str:
    """Short card text for one listing (fits Telegram's 1024-char photo caption limit)."""
    prefix = f"{index}. " if index is not None else ""
    lines = [f"{prefix}{listing.get('title') or 'Untitled'}", f"💰 {listing.get('price') or 'Price n/a'}"]
    if listing.get("address"):
        lines.append(f"📍 {listing['address']}")
    if listing.get("floor_area_sqft"):
        lines.append(f"📐 {listing['floor_area_sqft']} sqft")
    if listing.get("nearby_transit"):
        lines.append(f"🚆 {listing['nearby_transit']}")
    if listing.get("agent_name"):
        verified = " ✅" if listing.get("is_verified_agent") else ""
        lines.append(f"👤 {listing['agent_name']}{verified}")
    return _truncate("\n".join(lines), 1024)


def _detail_scraper_for(url: str):
    """Pick the platform class whose get_property_details() handles this URL (new instance = thread-safe)."""
    scraper = search_pipeline.scraper
    host = urlparse(url).netloc.lower()
    if "propertyguru.com.my" in host:
        return scraper.Propertyguru()
    if "iproperty.com.my" in host:
        return scraper.IProperties()
    if "speedhome.com" in host:
        return scraper.Speedhome()
    if "mudah.my" in host:
        return scraper.Mudah()
    return None


def get_listing_details(property_url: str) -> Optional[dict]:
    """Scrape a listing's detail page (OUTPUT 2 in SCRAPER_INPUT_SPEC.md). Returns None on failure."""
    platform = _detail_scraper_for(property_url)
    if platform is None:
        logger.warning("No detail scraper for %s", property_url)
        return None
    try:
        details = platform.get_property_details(property_url)
    except Exception:
        logger.exception("Detail scrape failed for %s", property_url)
        return None
    # The scraper returns an empty shell when the page couldn't be parsed
    if not details or not (details.get("title") or details.get("property_details")):
        return None
    return details


TENURE_LABELS = {"F": "Freehold", "L": "Leasehold"}


def format_details(details: dict, limit: int = 4000) -> str:
    """Full detail text for one listing (fits Telegram's 4096-char message limit)."""
    pd = details.get("property_details") or {}
    loc = details.get("location") or {}

    def _count(value):
  
        return value if isinstance(value, int) and value > 0 else None

    tenure = pd.get("tenure")
    tenure = TENURE_LABELS.get(str(tenure).upper(), tenure) if tenure else None

    lines = [details.get("title") or pd.get("property_name") or "Listing details", ""]
    facts = [
        ("💰 Price", pd.get("price")),
        ("🏠 Type", pd.get("property_type")),
        ("🛏 Bedrooms", _count(pd.get("bedrooms"))),
        ("🛁 Bathrooms", _count(pd.get("bathrooms"))),
        ("📐 Size", f"{pd['floor_area_sqft']} sqft" if pd.get("floor_area_sqft") else None),
        ("🛋 Furnishing", pd.get("furnishing")),
        ("📜 Tenure", tenure),
        ("🏗 Completed", pd.get("completion_year")),
        ("📅 Posted", pd.get("posted_date")),
        ("📍 Address", loc.get("formatted_address")),
    ]
    lines += [f"{label}: {value}" for label, value in facts if value not in (None, "", [])]

    highlights = [h for h in (pd.get("highlights") or []) if h]
    if highlights:
        lines += ["", "✨ Highlights: " + ", ".join(highlights[:15])]

    links = []
    map_url = details.get("map_url") or loc.get("map_url")
    if map_url:
        links.append(f"🗺 Map: {map_url}")
    if details.get("property_url"):
        links.append(f"🔗 Listing: {details['property_url']}")
    footer = "\n".join([""] + links) if links else ""

    description = re.sub(r"\n{3,}", "\n\n", (pd.get("description") or "").strip())
    body = "\n".join(lines)
    if description:
        room = limit - len(body) - len(footer) - len("\n\n📝 ")
        if room > 100:
            body += "\n\n📝 " + _truncate(description, room)
    return _truncate(body + footer, limit)


FLOOR_LEVEL_LABELS = {"LOW": "low floor (1-5)", "MID": "mid floor (6-15)", "HIGH": "high floor (16+)", "PENT": "penthouse"}


def describe_filters(search_input: dict) -> str:
    """Human-readable list of every filter that will be sent to the scraper."""
    housing = search_input["housing_type"]
    housing = " or ".join(housing) if isinstance(housing, list) else housing
    lines = [
        f"- Location: {search_input['location']}",
        f"- Housing type: {housing}",
        f"- Price: RM{search_input['min_price']} - RM{search_input['max_price']}",
        f"- Building: {'landed' if search_input.get('property_structure') == 'landed' else 'high-rise (condo/apartment)'}",
        f"- Furnishing: {'fully furnished only' if search_input.get('is_furnished') else 'any'}",
        f"- Agent: {'verified only' if search_input.get('is_verified_agent') else 'any'}",
    ]
    if search_input.get("floor_level"):
        floor = FLOOR_LEVEL_LABELS.get(search_input["floor_level"], search_input["floor_level"])
        if search_input.get("floor_number"):
            floor = f"{floor}, closest match to floor {search_input['floor_number']}"
        lines.append(f"- Floor: {floor}")
    if search_input.get("bedrooms") is not None:
        lines.append(f"- Bedrooms: {search_input['bedrooms']}")
    if search_input.get("bathrooms") is not None:
        lines.append(f"- Bathrooms: {search_input['bathrooms']}")
    if search_input.get("distance_to_mrt") is not None:
        lines.append(f"- Max distance to MRT: {search_input['distance_to_mrt']} km")
    if search_input.get("has_carpark"):
        lines.append("- Car park: required")
    return "\n".join(lines)


def ask_for_missing(missing: list, session: dict) -> str:
    known_bits = []
    if session.get("location"):
        known_bits.append(f"location: {session['location']}")
    if session.get("housing_type"):
        known_bits.append(f"housing type: {session['housing_type']}")
    if session.get("max_price") is not None:
        known_bits.append(f"max price: RM{session['max_price']}")
    known_line = f"Got it so far ({', '.join(known_bits)}).\n\n" if known_bits else ""
    questions = "\n".join(f"- {FIELD_PROMPTS[f]}?" for f in missing)
    return f"{known_line}I still need:\n{questions}"


def process_message(
    session: dict,
    user_text: str,
    on_search_start: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    on_search_start: optional callback, called with a short status message right before
    the (slow) scrape begins, so the caller can tell the user to wait.
    """

    extracted = extract_fields_with_llm(user_text)
    session = merge_fields(session, extracted)
    missing = missing_mandatory_fields(session)

    if missing:
        return {
            "session": session,
            "complete": False,
            "reply": ask_for_missing(missing, session),
            "search_input": None,
            "scraper_result": None,
        }

    search_input = build_search_input(session)

    if on_search_start:
        on_search_start(
            "Got everything I need, searching now:\n"
            + describe_filters(search_input)
            + "\n\n(This can take up to a minute...)"
        )

    scraper_result = run_scraper(search_input)
    reply = format_results(search_input, scraper_result)

    return {
        "session": {},  
        "complete": True,
        "reply": reply, 
        "summary": format_summary(search_input, scraper_result),  
        "search_input": search_input,
        "scraper_result": scraper_result,
    }
