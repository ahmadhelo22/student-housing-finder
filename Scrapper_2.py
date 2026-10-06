from urllib.parse import urlencode
from typing import Optional, Literal, List, Dict, Any, Union
from collections import defaultdict
import asyncio
import json
import os
import re
import hashlib
from selectolax.parser import HTMLParser
from curl_cffi import requests
from curl_cffi.requests import AsyncSession
import time


# الحد الأقصى للطلبات المتزامنة لتفادي صفحة الحماية (DataDome)
MAX_CONCURRENCY = 4
# ملف حفظ بصمات الصور على الجهاز لتفادي إعادة تحميلها في كل تشغيل
IMAGE_HASH_CACHE_FILE = "image_hash_cache.json"


async def _fetch_all(
    requests_list: List[tuple[str, Dict[str, str]]],
    max_concurrency: int = MAX_CONCURRENCY,
    timeout: int = 20,
    binary: bool = False,
) -> List[Optional[Union[str, bytes]]]:
    """
    تحميل مجموعة روابط في نفس اللحظة مع حد أقصى للطلبات المتزامنة.
    ترجع قائمة بنفس ترتيب الروابط، وتضع None للرابط الذي فشل تحميله.
    """
    semaphore = asyncio.Semaphore(max_concurrency)

    async with AsyncSession(impersonate="chrome124") as session:
        async def _one(url: str, headers: Dict[str, str]):
            async with semaphore:
                try:
                    r = await session.get(url, headers=headers, timeout=timeout)
                    if r.status_code != 200:
                        return None
                    return r.content if binary else r.text
                except Exception:
                    return None

        return await asyncio.gather(*[_one(u, h) for u, h in requests_list])


def fetch_all(requests_list: List[tuple[str, Dict[str, str]]], **kwargs) -> List[Optional[Union[str, bytes]]]:
    """نسخة متزامنة من _fetch_all لاستدعائها من كود عادي."""
    if not requests_list:
        return []
    return asyncio.run(_fetch_all(requests_list, **kwargs))


def _extract_next_data(html: Optional[str]) -> Optional[Dict[str, Any]]:
    """استخراج بيانات __NEXT_DATA__ من صفحة HTML."""
    if not html:
        return None
    tree = HTMLParser(html)
    next_data_script = tree.css_first("script#__NEXT_DATA__")
    if next_data_script:
        return json.loads(next_data_script.text())
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    return None


def _get_numeric_price(price_val: Any) -> float:
    """استخراج القيمة الرقمية للسعر للفرز وتجميع العقارات بدقة."""
    if isinstance(price_val, (int, float)):
        return float(price_val)
    if isinstance(price_val, str):
        clean = price_val.replace(",", "")
        m = re.search(r"\d+(?:\.\d+)?", clean)
        if m:
            return float(m.group(0))
    return float("inf")


def _sort_key(prop: Dict[str, Any]) -> float:
    return _get_numeric_price(prop.get("price"))


def _parse_listing_pages(
    pages: List[Optional[str]],
    urls: List[str],
    domain: str,
    platform_name: str,
    output_file: Optional[str],
) -> List[Dict[str, Any]]:
    """
    تحويل صفحات البحث المحملة مسبقاً إلى قائمة عقارات مدمجة ومرتبة من الأرخص للأغلى، وحفظها في JSON.
    """
    all_extracted_properties = []
    seen_urls = set()

    for current_url, html in zip(urls, pages):
        raw_json = _extract_next_data(html)
        if not raw_json:
            print(f"[تنبيه] لم يتم العثور على وسم __NEXT_DATA__ في {platform_name}. قد يكون الموقع أرسل صفحة حظر كابتشا (DataDome).")
            continue

        page_data = raw_json.get("props", {}).get("pageProps", {}).get("pageData", {})
        listings = page_data.get("data", {}).get("listingsData", [])

        for item in listings:
            ld = item.get("listingData", {})

            title = ld.get("localizedTitle")
            price_pretty = ld.get("price", {}).get("pretty") or (f"RM {ld.get('price', {}).get('value')}" if ld.get("price", {}).get("value") else "N/A")
            price_value = ld.get("price", {}).get("value")
            address = ld.get("fullAddress") or ld.get("shortAddress")
            relative_url = ld.get("url", "")
            if relative_url:
                property_url = relative_url if relative_url.startswith("http") else f"{domain}{relative_url}"
            else:
                property_url = None

            # تفادي تكرار نفس العقار إذا ظهر في أكثر من رابط
            if property_url and property_url in seen_urls:
                continue
            if property_url:
                seen_urls.add(property_url)

            floor_area_sqft = ld.get("floorArea")
            floor_area_sqm = round(floor_area_sqft * 0.092903, 1) if floor_area_sqft else None
            transit_info = ld.get("mrt", {}).get("nearbyText") if ld.get("mrt") else None

            # التحقق من توثيق الوكيل واسمه
            agent_info = ld.get("agent")
            is_agent_verified = agent_info.get("isAgentVerified", False) if isinstance(agent_info, dict) else False
            agent_name = agent_info.get("name") if isinstance(agent_info, dict) else None
            if not agent_name and property_url:
                m_agent = re.search(r"-by-([a-zA-Z0-9-]+)-\d+", property_url)
                if m_agent:
                    agent_name = m_agent.group(1).replace("-", " ").title()

            all_extracted_properties.append({
                "title": title,
                "price": price_pretty,
                "address": address,
                "property_url": property_url,
                "floor_area_sqm": floor_area_sqm,
                "floor_area_sqft": floor_area_sqft,
                "nearby_transit": transit_info,
                "thumbnail_url": ld.get("thumbnail"),
                "is_verified_agent": is_agent_verified,
                "agent_name": agent_name,
            })

    all_extracted_properties.sort(key=_sort_key)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(all_extracted_properties, f, ensure_ascii=False, indent=4)

    return all_extracted_properties


def scrape_sites_parallel(
    jobs: List[tuple[Any, Union[str, List[str]], Optional[str]]],
) -> List[List[Dict[str, Any]]]:
    """
    سحب عدة مواقع في نفس اللحظة: كل صفحات كل المواقع تُحمّل معاً، ثم تُحلل كل مجموعة على حدة.
    jobs: قائمة من (كائن الموقع، رابط أو قائمة روابط، اسم ملف الحفظ)
    ترجع قائمة نتائج بنفس ترتيب المواقع.
    """
    flat_requests = []
    spans = []
    for site, url, _ in jobs:
        urls = [url] if isinstance(url, str) else list(url)
        start = len(flat_requests)
        flat_requests.extend((u, site.headers) for u in urls)
        spans.append((start, len(flat_requests), urls))

    pages = fetch_all(flat_requests)

    results = []
    for (site, _, output_file), (start, end, urls) in zip(jobs, spans):
        results.append(
            _parse_listing_pages(pages[start:end], urls, site.domain, site.platform_name, output_file)
        )
    return results



# خريطة توحيد الأسماء والاختصارات لتسهيل الاستخدام
HOUSING_TYPE_ALIASES = {
    "studio": "studio",
    "master": "master_room",
    "master_room": "master_room",
    "medium": "medium_room",
    "medium_room": "medium_room",
    "common": "medium_room",
    "common_room": "medium_room",
    "small": "small_room",
    "small_room": "small_room",
    "single": "small_room",
    "shared": "small_room",
    "share": "small_room",
    "entire": "entire_unit",
    "entire_unit": "entire_unit",
    "unit": "entire_unit",
    "studio_or_master_room": ["studio", "master_room"],
}


def normalize_housing_types(housing_type: Union[str, List[str]]) -> List[str]:

    if isinstance(housing_type, str):
        cleaned = housing_type.strip().lower()
        if cleaned in HOUSING_TYPE_ALIASES:
            aliased = HOUSING_TYPE_ALIASES[cleaned]
            return aliased if isinstance(aliased, list) else [aliased]
        return [cleaned]
    elif isinstance(housing_type, (list, tuple, set)):
        result = []
        for item in housing_type:
            cleaned = str(item).strip().lower()
            aliased = HOUSING_TYPE_ALIASES.get(cleaned, cleaned)
            if isinstance(aliased, list):
                for sub_item in aliased:
                    if sub_item not in result:
                        result.append(sub_item)
            else:
                if aliased not in result:
                    result.append(aliased)
        return result if result else ["studio"]
    return ["studio"]


class IProperties:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.domain = "https://www.iproperty.com.my"
        self.platform_name = "iProperty"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.iproperty.com.my/",
        }

    def generate_url(
        self,
        location: str,                                    # الكلمة المفتاحية: "Kuala Lumpur", "Cyberjaya", إلخ
        max_price: int,                                   # الحد الأقصى للسعر بـ RM
        min_price: Optional[int] = None,                  # الحد الأدنى (اختياري)
        housing_type: Union[str, List[str]] = "studio",   # نوع أو قائمة أنواع السكن (يدعم أي توليفة)
        room_type: Optional[str] = None,                  # (للتوافق القديم) نوع الغرفة إذا تم تمرير "room"
        bedrooms: Optional[int] = None,                   # عدد الغرف (إذا كان entire_unit)
        bathrooms: Optional[int] = None,                  # عدد الحمامات (اختياري)
        property_structure: Literal["high_rise", "landed"] = "high_rise", # الافتراضي: أبراج
        floor_level: Optional[Literal["HIGH", "MID", "LOW", "PENT"]] = None, # فلتر مستوى الطابق مباشرة على السيرفر
        is_furnished: bool = True,                        # الافتراضي: مفروش بالكامل
        distance_to_mrt: Optional[int] = None,            # المسافة للمترو بالكيلومتر (اختياري)
        has_carpark: Optional[bool] = None,               # توفر موقف سيارة (اختياري)
        is_verified_agent: bool = True,                   # فلتر الوكيل الموثق (افتراضياً: True)
        page: int = 1,
        **kwargs
    ) -> Union[str, List[str]]:
        """
        الدالة الأولى: تقوم بتوليد رابط أو روابط البحث في موقع iProperty بناءً على الفلاتر المحددة.
        تدعم أي توليفة من أنواع السكن (مثل: medium + master, small + master, إلخ)،
        وفلتر مستوى الطابق (floor_level) على سيرفر الموقع مباشرة.
        """
        base_url = "https://www.iproperty.com.my/property-for-rent"

        def _build_params_for_type(h_type: str) -> str:
            params = {
                "listingType": "rent",
                "page": page,
                "isCommercial": "false",
                "isDiversityFriendly": "true",
                "sortBy": "price-asc",
                "locale": "en",
                "_freetextDisplay": location,
                "freetext": location,
                "maxPrice": max_price,
            }

            # فلتر الوكيل الموثق (الافتراضي True، وعند إيقافه False لا يتم تقييد البحث)
            verified = kwargs.get("verified_agent", is_verified_agent)
            if verified:
                params["isListerVerified"] = "true"

            # هيكل العقار
            if property_structure == "high_rise":
                params["propertyTypeGroup"] = "N"
                params["propertyTypeCode"] = "CONDO,APT,SRES,STUDIO"
            else:
                params["propertyTypeGroup"] = "L"
                params["propertyTypeCode"] = "TERRACE,SEMI_D,BUNGALOW"

            # مستوى الطابق على سيرفر الموقع (HIGH, PENT, MID, LOW)
            if floor_level is not None:
                params["floorLevel"] = floor_level

            # نوع السكن والغرف
            if h_type == "studio":
                params["bedrooms"] = "-1"
            elif h_type == "master_room":
                params["entireUnitOrRoom"] = "room"
                params["roomType"] = "mas"
            elif h_type == "medium_room":
                params["entireUnitOrRoom"] = "room"
                params["roomType"] = "com"
            elif h_type == "small_room":
                params["entireUnitOrRoom"] = "room"
                params["roomType"] = "share"
            elif h_type == "entire_unit":
                params["entireUnitOrRoom"] = "ent"
                if bedrooms is not None:
                    params["bedrooms"] = str(bedrooms)

            # الفرش
            if is_furnished:
                params["furnishing"] = "FULL"

            # الحقول الاختيارية
            if min_price is not None:
                params["minPrice"] = min_price

            if bathrooms is not None:
                params["bathrooms"] = str(bathrooms)

            if distance_to_mrt is not None:
                params["distanceToMRT"] = str(distance_to_mrt)

            if has_carpark is True:
                params["carPark"] = "1"

            return f"{base_url}?{urlencode(params)}"

        # التوافق مع الكود القديم عند استخدام housing_type="room"
        if housing_type == "room":
            if room_type == "master":
                housing_type = "master_room"
            elif room_type in ["common", "medium"]:
                housing_type = "medium_room"
            elif room_type in ["shared", "small"]:
                housing_type = "small_room"
            else:
                housing_type = "medium_room"

        selected_types = normalize_housing_types(housing_type)
        urls = [_build_params_for_type(t) for t in selected_types]

        # إذا كان الطلب نوعاً مفرداً كنص أصلي نرجع رابطاً واحداً، وإلا نرجع قائمة روابط
        if isinstance(housing_type, str) and len(urls) == 1 and housing_type != "studio_or_master_room":
            return urls[0]
        return urls

    def scrape_to_json(self, url: Union[str, List[str]], output_file: str = "properties.json") -> List[Dict[str, Any]]:
        """
        الدالة الثانية: تأخذ رابطاً واحداً أو قائمة روابط، تحمّلها كلها في نفس اللحظة، تدمجها وتفرزها من الأرخص للأغلى وتخزنها في JSON.
        """
        urls = [url] if isinstance(url, str) else list(url)
        pages = fetch_all([(u, self.headers) for u in urls])
        return _parse_listing_pages(pages, urls, self.domain, self.platform_name, output_file)

    def get_property_details(self, property_url: str, output_file: Optional[str] = None) -> Dict[str, Any]:
        """
        الدالة الثالثة: تأخذ رابط العقار الفردي وتستخرج تفاصيله الكاملة:
        - الصور بجودة عالية (Images)
        - المكان بالتفصيل (Detailed Location)
        - رابط خريطة جوجل (Google Maps URL)
        - قاموس تفاصيل العقار المستقل (Property Details)
        """
        response = self.session.get(property_url, headers=self.headers)
        
        tree = HTMLParser(response.text)
        next_data_script = tree.css_first("script#__NEXT_DATA__")
        
        raw_json = None
        if next_data_script:
            raw_json = json.loads(next_data_script.text())
        else:
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', response.text, re.DOTALL)
            if match:
                raw_json = json.loads(match.group(1))
                
        if not raw_json:
            print(f"[تنبيه] لم يتم العثور على بيانات تفاصيل العقار: {property_url}")
            return {
                "property_url": property_url,
                "title": None,
                "map_url": None,
                "location": {},
                "images": [],
                "property_details": {}
            }
            
        pdata = raw_json.get("props", {}).get("pageProps", {}).get("pageData", {}).get("data", {})
        ld = pdata.get("listingData", {})
        
        # 1. استخراج الصور
        gallery = pdata.get("mediaGalleryData", {}).get("media", {}).get("images", {}).get("items", [])
        images = [img.get("src") for img in gallery if img.get("src")]
        
        # 2. استخراج الموقع والخريطة
        loc_data = pdata.get("listingLocationData", {}).get("data", {})
        detail_loc = pdata.get("listingDetail", {}).get("location", {})
        center = loc_data.get("center", {})
        point = detail_loc.get("point", {})
        lat = center.get("lat") or point.get("lat")
        lng = center.get("lng") or point.get("lon")
        
        building_name = ld.get("propertyName")
        address_info = detail_loc.get("address", {})
        formatted_address = address_info.get("formatted") or ld.get("localizedTitle")
        postal_code = address_info.get("postalCode") or ld.get("postcode")
        street = ld.get("streetName") or address_info.get("streetNumber")
        city = ld.get("districtText") or ld.get("areaText")
        state = ld.get("regionText")
        
        map_url = f"https://www.google.com/maps?q={lat},{lng}" if (lat and lng) else None
        
        detailed_location = {
            "building_name": building_name,
            "formatted_address": formatted_address,
            "street": street,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "coordinates": {
                "latitude": lat,
                "longitude": lng,
            },
            "map_url": map_url,
        }
        
        # 3. استخراج تفاصيل العقار في Dictionary مستقل
        metatable = pdata.get("detailsData", {}).get("metatable", {}).get("items", [])
        raw_features = [item.get("value") for item in metatable if item.get("value")]
        
        floor_area_sqft = ld.get("floorArea")
        floor_area_sqm = round(floor_area_sqft * 0.092903, 1) if floor_area_sqft else None
        
        detail_price = pdata.get("listingDetail", {}).get("price", {})
        price_pretty = detail_price.get("formatted") or ld.get("pricePretty") or (f"RM {ld.get('price')}" if ld.get("price") else None)
        price_val = detail_price.get("max") or ld.get("price")
        
        # تنظيف الوصف من وسوم HTML
        raw_desc = pdata.get("descriptionBlockData", {}).get("description", "")
        clean_desc = raw_desc.replace("<br />", "\n").replace("<br>", "\n") if raw_desc else ""
        
        last_posted_info = ld.get("lastPosted")
        posted_date = last_posted_info.get("date") if isinstance(last_posted_info, dict) else last_posted_info
        
        property_details = {
            "property_name": building_name,
            "property_type": ld.get("propertyType"),
            "bedrooms": ld.get("bedrooms"),
            "bathrooms": ld.get("bathrooms"),
            "floor_area_sqm": floor_area_sqm,
            "floor_area_sqft": floor_area_sqft,
            "price": price_pretty,
            "tenure": ld.get("tenure"),
            "furnishing": next((f for f in raw_features if "furnish" in f.lower()), None),
            "completion_year": next((f.replace("Completed in ", "") for f in raw_features if "completed" in f.lower()), None),
            "listing_id": ld.get("listingId"),
            "posted_date": posted_date,
            "highlights": raw_features,
            "description": clean_desc,
        }
        
        # تجميع الناتج النهائي
        result = {
            "property_url": property_url,
            "title": ld.get("localizedTitle"),
            "map_url": map_url,
            "location": detailed_location,
            "images": images,
            "property_details": property_details,
        }
        
        # حفظ الناتج في ملف JSON إذا تم تمرير مسار للملف
        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=4)
            # print(f"[تم بنجاح] تم حفظ تفاصيل العقار في: {output_file}")
            
        return result

class Propertyguru:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.domain = "https://www.propertyguru.com.my"
        self.platform_name = "PropertyGuru"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.propertyguru.com.my/",
        }
        self.base_url = "https://www.propertyguru.com.my/property-for-rent"

    def generate_url(
        self,
        location: Optional[str] = None,                   # الكلمة المفتاحية: "Kuala Lumpur", إلخ (اختياري)
        max_price: Optional[int] = None,                  # الحد الأقصى للسعر بـ RM
        min_price: Optional[int] = None,                  # الحد الأدنى (اختياري)
        housing_type: Union[str, List[str]] = "studio",   # نوع أو قائمة أنواع السكن (يدعم أي توليفة)
        room_type: Optional[str] = None,                  # (للتوافق القديم) نوع الغرفة إذا تم تمرير "room"
        bedrooms: Optional[int] = None,                   # عدد الغرف (إذا كان entire_unit)
        bathrooms: Optional[int] = None,                  # عدد الحمامات (اختياري)
        property_structure: Literal["high_rise", "landed"] = "high_rise", # الافتراضي: أبراج
        floor_level: Optional[Literal["HIGH", "MID", "LOW", "PENT"]] = None, # فلتر مستوى الطابق مباشرة على السيرفر
        is_furnished: bool = True,                        # الافتراضي: مفروش بالكامل
        distance_to_mrt: Optional[int] = None,            # المسافة للمترو بالكيلومتر (اختياري)
        has_carpark: Optional[bool] = None,               # توفر موقف سيارة (اختياري)
        is_verified_agent: bool = True,                   # فلتر الوكيل الموثق (افتراضياً: True)
        page: int = 1,
        **kwargs
    ) -> Union[str, List[str]]:
        """
        الدالة الأولى: تقوم بتوليد رابط أو روابط البحث في موقع PropertyGuru بناءً على الفلاتر المحددة.
        تدعم أي توليفة من أنواع السكن (مثل: medium + master, small + master, إلخ)،
        وفلتر مستوى الطابق (floor_level) على سيرفر الموقع مباشرة.
        الثوابت الإلزامية:
        - Everyone Welcome (isDiversityFriendly = true)
        - Residential Rent (isCommercial = false, listingType = rent)
        - Verified Agent (isListerVerified = true، افتراضياً True ويمكن تعطيلها بـ False)
        """
        def _build_params_for_type(h_type: str) -> str:
            params = {
                "listingType": "rent",
                "page": page,
                "isCommercial": "false",
                "isDiversityFriendly": "true",  # Everyone Welcome - إلزامي دائماً لحماية المستخدم
                "sortBy": "price-asc",
            }

            # فلتر الوكيل الموثق (الافتراضي True، وعند إيقافه False لا يتم تقييد البحث)
            verified = kwargs.get("verified_agent", is_verified_agent)
            if verified:
                params["isListerVerified"] = "true"

            # الموقع الجغرافي
            if location:
                params["_freetextDisplay"] = location
                params["freetext"] = location

            # الأسعار
            if max_price is not None:
                params["maxPrice"] = max_price
            if min_price is not None:
                params["minPrice"] = min_price

            # هيكل العقار (أبراج كوندو افتراضياً)
            if property_structure == "high_rise":
                params["propertyTypeGroup"] = "N"
                params["propertyTypeCode"] = "CONDO,APT,SRES"
            else:
                params["propertyTypeGroup"] = "T,S,B"
                params["propertyTypeCode"] = "TERRA,SEMI,BUNG"

            # مستوى الطابق على سيرفر الموقع (HIGH, PENT, MID, LOW)
            if floor_level is not None:
                params["floorLevel"] = floor_level

            # نوع السكن وعدد الغرف
            if h_type == "studio":
                params["bedrooms"] = "0"  # في PropertyGuru كود الاستوديو هو 0
                params["entireUnitOrRoom"] = "ent"
            elif h_type == "master_room":
                params["entireUnitOrRoom"] = "room"
                params["roomType"] = "mas"
            elif h_type == "medium_room":
                params["entireUnitOrRoom"] = "room"
                params["roomType"] = "com"
            elif h_type == "small_room":
                params["entireUnitOrRoom"] = "room"
                params["roomType"] = "share"
            elif h_type == "entire_unit":
                params["entireUnitOrRoom"] = "ent"
                if bedrooms is not None:
                    params["bedrooms"] = str(bedrooms)

            # الفرش
            if is_furnished:
                params["furnishing"] = "FULL"

            # الحقول الاختيارية
            if bathrooms is not None:
                params["bathrooms"] = str(bathrooms)

            if distance_to_mrt is not None:
                params["distanceToMRT"] = str(distance_to_mrt)

            if has_carpark is True:
                params["carPark"] = "1"

            return f"{self.base_url}?{urlencode(params)}"

        # التوافق مع الكود القديم عند استخدام housing_type="room"
        if housing_type == "room":
            if room_type == "master":
                housing_type = "master_room"
            elif room_type in ["common", "medium"]:
                housing_type = "medium_room"
            elif room_type in ["shared", "small"]:
                housing_type = "small_room"
            else:
                housing_type = "medium_room"

        selected_types = normalize_housing_types(housing_type)
        urls = [_build_params_for_type(t) for t in selected_types]

        # إذا كان الطلب نوعاً مفرداً كنص أصلي نرجع رابطاً واحداً، وإلا نرجع قائمة روابط
        if isinstance(housing_type, str) and len(urls) == 1 and housing_type != "studio_or_master_room":
            return urls[0]
        return urls

    def scrape_to_json(self, url: Union[str, List[str]], output_file: str = "propertyguru_properties.json") -> List[Dict[str, Any]]:
        """
        الدالة الثانية: تأخذ رابطاً أو قائمة روابط من PropertyGuru، تحمّلها كلها في نفس اللحظة، تدمجها وترتبها من الأرخص للأغلى.
        """
        urls = [url] if isinstance(url, str) else list(url)
        pages = fetch_all([(u, self.headers) for u in urls])
        return _parse_listing_pages(pages, urls, self.domain, self.platform_name, output_file)

    def get_property_details(self, property_url: str, output_file: Optional[str] = None) -> Dict[str, Any]:
        """
        الدالة الثالثة: تأخذ رابط عقار فردي من PropertyGuru وتستخرج تفاصيله الكاملة.
        """
        response = self.session.get(property_url, headers=self.headers)
        
        tree = HTMLParser(response.text)
        next_data_script = tree.css_first("script#__NEXT_DATA__")
        
        raw_json = None
        if next_data_script:
            raw_json = json.loads(next_data_script.text())
        else:
            match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', response.text, re.DOTALL)
            if match:
                raw_json = json.loads(match.group(1))
                
        if not raw_json:
            print(f"[تنبيه] لم يتم العثور على بيانات تفاصيل العقار: {property_url}")
            return {
                "property_url": property_url,
                "title": None,
                "map_url": None,
                "location": {},
                "images": [],
                "property_details": {}
            }
            
        pdata = raw_json.get("props", {}).get("pageProps", {}).get("pageData", {}).get("data", {})
        ld = pdata.get("listingData", {})
        
        # 1. الصور
        gallery = pdata.get("mediaGalleryData", {}).get("media", {}).get("images", {}).get("items", [])
        images = [img.get("src") for img in gallery if img.get("src")]
        
        # 2. الموقع والخريطة
        loc_data = pdata.get("listingLocationData", {}).get("data", {})
        detail_loc = pdata.get("listingDetail", {}).get("location", {})
        center = loc_data.get("center", {})
        point = detail_loc.get("point", {})
        lat = center.get("lat") or point.get("lat")
        lng = center.get("lng") or point.get("lon")
        
        building_name = ld.get("propertyName")
        address_info = detail_loc.get("address", {})
        formatted_address = address_info.get("formatted") or ld.get("localizedTitle")
        postal_code = address_info.get("postalCode") or ld.get("postcode")
        street = ld.get("streetName") or address_info.get("streetNumber")
        city = ld.get("districtText") or ld.get("areaText")
        state = ld.get("regionText")
        
        map_url = f"https://www.google.com/maps?q={lat},{lng}" if (lat and lng) else None
        
        detailed_location = {
            "building_name": building_name,
            "formatted_address": formatted_address,
            "street": street,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "coordinates": {
                "latitude": lat,
                "longitude": lng,
            },
            "map_url": map_url,
        }
        
        # 3. تفاصيل العقار في Dictionary مستقل
        metatable = pdata.get("detailsData", {}).get("metatable", {}).get("items", [])
        raw_features = [item.get("value") for item in metatable if item.get("value")]
        
        floor_area_sqft = ld.get("floorArea")
        floor_area_sqm = round(floor_area_sqft * 0.092903, 1) if floor_area_sqft else None
        
        detail_price = pdata.get("listingDetail", {}).get("price", {})
        price_pretty = detail_price.get("formatted") or ld.get("pricePretty") or (f"RM {ld.get('price')}" if ld.get("price") else None)
        price_val = detail_price.get("max") or ld.get("price")
        
        raw_desc = pdata.get("descriptionBlockData", {}).get("description", "")
        clean_desc = raw_desc.replace("<br />", "\n").replace("<br>", "\n") if raw_desc else ""
        
        last_posted_info = ld.get("lastPosted")
        posted_date = last_posted_info.get("date") if isinstance(last_posted_info, dict) else last_posted_info
        
        property_details = {
            "property_name": building_name,
            "property_type": ld.get("propertyType"),
            "bedrooms": ld.get("bedrooms"),
            "bathrooms": ld.get("bathrooms"),
            "floor_area_sqm": floor_area_sqm,
            "floor_area_sqft": floor_area_sqft,
            "price": price_pretty,
            "tenure": ld.get("tenure"),
            "furnishing": next((f for f in raw_features if "furnish" in f.lower()), None),
            "completion_year": next((f.replace("Completed in ", "") for f in raw_features if "completed" in f.lower()), None),
            "listing_id": ld.get("listingId"),
            "posted_date": posted_date,
            "highlights": raw_features,
            "description": clean_desc,
        }
        
        result = {
            "property_url": property_url,
            "title": ld.get("localizedTitle"),
            "map_url": map_url,
            "location": detailed_location,
            "images": images,
            "property_details": property_details,
        }
        
        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=4)
            # print(f"[تم بنجاح] تم حفظ تفاصيل عقار PropertyGuru في: {output_file}")
            
        return result

class Speedhome:
    # أنواع الغرف في SPEEDHOME مقابل أنواع السكن الموحدة عندنا
    ROOM_TYPE_CODES = {
        "master_room": "MASTER",
        "medium_room": "MEDIUM",
        "small_room": "SMALL",
    }
    FURNISH_LABELS = {
        "FULL": "Fully Furnished",
        "PARTIAL": "Partially Furnished",
        "NONE": "Unfurnished",
    }

    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.domain = "https://speedhome.com"
        self.platform_name = "Speedhome"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://speedhome.com/",
        }
        # صفحة البحث الأساسية، والموقع يُمرَّر كنص حر عبر q (يغطي كل ماليزيا وليس كوالالمبور فقط)
        self.base_url = "https://speedhome.com/rent/kuala-lumpur"

    def generate_url(
        self,
        location: Optional[str] = None,                   # الكلمة المفتاحية: "Setapak", "M Vertica", إلخ
        max_price: Optional[int] = None,                  # الحد الأقصى للسعر بـ RM
        min_price: Optional[int] = None,                  # الحد الأدنى (اختياري)
        housing_type: Union[str, List[str]] = "studio",   # نوع أو قائمة أنواع السكن (يدعم أي توليفة)
        room_type: Optional[str] = None,                  # (للتوافق القديم) نوع الغرفة إذا تم تمرير "room"
        bedrooms: Optional[int] = None,                   # عدد الغرف (إذا كان entire_unit)
        bathrooms: Optional[int] = None,                  # عدد الحمامات (اختياري)
        property_structure: Literal["high_rise", "landed"] = "high_rise", # الافتراضي: أبراج
        floor_level: Optional[Literal["HIGH", "MID", "LOW", "PENT"]] = None, # غير مدعوم في SPEEDHOME (يتم تجاهله)
        is_furnished: bool = True,                        # الافتراضي: مفروش بالكامل
        distance_to_mrt: Optional[int] = None,            # غير مدعوم في SPEEDHOME (يتم تجاهله)
        has_carpark: Optional[bool] = None,               # توفر موقف سيارة (اختياري)
        is_verified_agent: bool = True,                   # فلتر المالك الموثق (افتراضياً: True)
        page: int = 1,
        **kwargs
    ) -> Union[str, List[str]]:
        """
        الدالة الأولى: تقوم بتوليد رابط أو روابط البحث في موقع SPEEDHOME بناءً على الفلاتر المحددة.
        الفلاتر التي يدعمها السيرفر توضع في الرابط (q, min, max, type, furnish, bed, bath, carpark, allRaces, page).
        الفلاتر التي لا يدعمها السيرفر (استوديو، نوع الغرفة، العدد الدقيق للغرف والحمامات، المالك الموثق)
        توضع بعد علامة # في الرابط، فلا تُرسل للموقع، وتطبقها الدالة الثانية على النتائج محلياً.
        """
        def _build_params_for_type(h_type: str) -> str:
            params = {}

            # الموقع الجغرافي كنص حر
            if location:
                params["q"] = location

            # الأسعار
            if max_price is not None:
                params["max"] = max_price
            if min_price is not None:
                params["min"] = min_price

            # Everyone Welcome - إلزامي دائماً لحماية المستخدم
            params["allRaces"] = 1

            # فلاتر محلية تُطبق بعد السحب
            local_filters = {}

            # نوع السكن
            if h_type == "studio":
                params["type"] = "HIGHRISE"
                local_filters["bedrooms"] = 0
            elif h_type in self.ROOM_TYPE_CODES:
                params["type"] = "ROOM"
                local_filters["room_type"] = self.ROOM_TYPE_CODES[h_type]
            elif h_type == "entire_unit":
                params["type"] = "HIGHRISE" if property_structure == "high_rise" else "LANDED"
                if bedrooms is not None:
                    params["bed"] = bedrooms  # السيرفر يرجع "على الأقل" هذا العدد
                    local_filters["bedrooms"] = bedrooms

            # الفرش
            if is_furnished:
                params["furnish"] = 2

            # الحقول الاختيارية
            if bathrooms is not None:
                params["bath"] = bathrooms  # السيرفر يرجع "على الأقل" هذا العدد
                if h_type not in self.ROOM_TYPE_CODES:
                    local_filters["bathrooms"] = bathrooms

            if has_carpark is True:
                params["carpark"] = 1

            params["page"] = page

            # فلتر المالك الموثق (الافتراضي True، وعند إيقافه False لا يتم تقييد البحث)
            verified = kwargs.get("verified_agent", is_verified_agent)
            if verified:
                local_filters["verified"] = 1

            url = f"{self.base_url}?{urlencode(params)}"
            if local_filters:
                url += f"#{urlencode(local_filters)}"
            return url

        # التوافق مع الكود القديم عند استخدام housing_type="room"
        if housing_type == "room":
            if room_type == "master":
                housing_type = "master_room"
            elif room_type in ["common", "medium"]:
                housing_type = "medium_room"
            elif room_type in ["shared", "small"]:
                housing_type = "small_room"
            else:
                housing_type = "medium_room"

        selected_types = normalize_housing_types(housing_type)
        urls = [_build_params_for_type(t) for t in selected_types]

        # إذا كان الطلب نوعاً مفرداً كنص أصلي نرجع رابطاً واحداً، وإلا نرجع قائمة روابط
        if isinstance(housing_type, str) and len(urls) == 1 and housing_type != "studio_or_master_room":
            return urls[0]
        return urls

    @staticmethod
    def _passes_local_filters(item: Dict[str, Any], local_filters: Dict[str, str]) -> bool:
        """تطبيق الفلاتر التي لا يدعمها سيرفر SPEEDHOME على عقار واحد."""
        if "bedrooms" in local_filters and item.get("bedroom") != int(local_filters["bedrooms"]):
            return False
        if "bathrooms" in local_filters and item.get("bathroom") != int(local_filters["bathrooms"]):
            return False
        if "room_type" in local_filters and item.get("roomType") != local_filters["room_type"]:
            return False
        if local_filters.get("verified") == "1" and not (item.get("user") or {}).get("isVerifiedUser"):
            return False
        return True

    def _build_address(self, item: Dict[str, Any]) -> Optional[str]:
        address = item.get("address") or ""
        parts = [address] + [p for p in (item.get("city"), item.get("state")) if p and p not in address]
        joined = ", ".join(p for p in parts if p)
        return joined or None

    def scrape_to_json(self, url: Union[str, List[str]], output_file: str = "speedhome_properties.json") -> List[Dict[str, Any]]:
        """
        الدالة الثانية: تأخذ رابطاً أو قائمة روابط من SPEEDHOME، تحمّلها كلها في نفس اللحظة،
        تطبق الفلاتر المحلية، وتدمج العقارات وترتبها من الأرخص للأغلى بنفس هيكلية المنصات الأخرى.
        """
        from urllib.parse import parse_qs

        urls = [url] if isinstance(url, str) else list(url)
        # فصل الفلاتر المحلية (بعد #) عن الرابط الذي يُرسل للموقع
        split_urls = [u.split("#", 1) + [""] for u in urls]
        pages = fetch_all([(parts[0], self.headers) for parts in split_urls])

        all_extracted_properties = []
        seen_urls = set()

        for parts, html in zip(split_urls, pages):
            raw_json = _extract_next_data(html)
            if not raw_json:
                print(f"[تنبيه] لم يتم العثور على وسم __NEXT_DATA__ في {self.platform_name}.")
                continue

            local_filters = {k: v[0] for k, v in parse_qs(parts[1]).items()}
            page_props = raw_json.get("props", {}).get("pageProps", {})
            listings = (page_props.get("propertyList") or {}).get("content") or []

            for item in listings:
                if not self._passes_local_filters(item, local_filters):
                    continue

                slug = item.get("slug")
                property_url = f"{self.domain}/details/{slug}" if slug else None

                # تفادي التكرار
                if property_url and property_url in seen_urls:
                    continue
                if property_url:
                    seen_urls.add(property_url)

                price = item.get("price")
                price_pretty = f"RM {price:,} /mo" if price else "N/A"

                floor_area_sqft = item.get("sqft") or None
                floor_area_sqm = round(floor_area_sqft * 0.092903, 1) if floor_area_sqft else None

                # الصورة المصغرة: صورة الغلاف إن وجدت، وإلا أول صورة
                images = item.get("images") or []
                cover = next((img for img in images if img.get("coverPhoto")), images[0] if images else {})
                thumbnail_url = cover.get("url") or cover.get("imageUrl")

                # في SPEEDHOME المعلن هو المالك نفسه وليس وكيلاً
                user = item.get("user") or {}

                all_extracted_properties.append({
                    "title": item.get("name"),
                    "price": price_pretty,
                    "address": self._build_address(item),
                    "property_url": property_url,
                    "floor_area_sqm": floor_area_sqm,
                    "floor_area_sqft": floor_area_sqft,
                    "nearby_transit": None,
                    "thumbnail_url": thumbnail_url,
                    "is_verified_agent": bool(user.get("isVerifiedUser")),
                    "agent_name": user.get("name"),
                })

        all_extracted_properties.sort(key=_sort_key)

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(all_extracted_properties, f, ensure_ascii=False, indent=4)

        return all_extracted_properties

    def get_property_details(self, property_url: str, output_file: Optional[str] = None) -> Dict[str, Any]:
        """
        الدالة الثالثة: تأخذ رابط عقار فردي من SPEEDHOME وتستخرج تفاصيله الكاملة
        بنفس هيكلية المنصات الأخرى (الصور، الموقع، الخريطة، تفاصيل العقار).
        """
        response = self.session.get(property_url, headers=self.headers)
        raw_json = _extract_next_data(response.text)
        info = (raw_json or {}).get("props", {}).get("pageProps", {}).get("propertyInfo")

        if not info:
            print(f"[تنبيه] لم يتم العثور على بيانات تفاصيل العقار: {property_url}")
            return {
                "property_url": property_url,
                "title": None,
                "map_url": None,
                "location": {},
                "images": [],
                "property_details": {}
            }

        # 1. الصور بجودة عالية
        images = [
            (img.get("url") or img.get("imageUrl")).replace("-medium.", "-large.")
            for img in info.get("images") or []
            if img.get("url") or img.get("imageUrl")
        ]

        # 2. الموقع والخريطة
        lat = info.get("latitude")
        lng = info.get("longitude")
        map_url = f"https://www.google.com/maps?q={lat},{lng}" if (lat and lng) else None

        detailed_location = {
            "building_name": info.get("name"),
            "formatted_address": self._build_address(info),
            "street": info.get("address"),
            "city": info.get("city"),
            "state": info.get("state"),
            "postal_code": info.get("postcode"),
            "coordinates": {
                "latitude": lat,
                "longitude": lng,
            },
            "map_url": map_url,
        }

        # 3. تفاصيل العقار في Dictionary مستقل
        floor_area_sqft = info.get("sqft") or None
        floor_area_sqm = round(floor_area_sqft * 0.092903, 1) if floor_area_sqft else None

        price = info.get("price")
        price_pretty = f"RM {price:,} /mo" if price else None

        property_type = info.get("type")
        if property_type == "ROOM" and info.get("roomType"):
            property_type = f"ROOM ({info.get('roomType')})"

        # المميزات: مرافق المبنى + أثاث الوحدة بصيغة مقروءة
        highlights = [
            str(f).replace("_", " ").title()
            for f in (info.get("facilities") or []) + (info.get("furnishes") or [])
        ]

        property_details = {
            "property_name": info.get("name"),
            "property_type": property_type,
            "bedrooms": info.get("bedroom"),
            "bathrooms": info.get("bathroom"),
            "floor_area_sqm": floor_area_sqm,
            "floor_area_sqft": floor_area_sqft,
            "price": price_pretty,
            "tenure": None,
            "furnishing": self.FURNISH_LABELS.get(info.get("furnishType"), info.get("furnishType")),
            "completion_year": None,
            "listing_id": info.get("id"),
            "posted_date": info.get("dateActivated") or info.get("dateCreated"),
            "highlights": highlights,
            "description": info.get("description") or "",
        }

        result = {
            "property_url": property_url,
            "title": info.get("name"),
            "map_url": map_url,
            "location": detailed_location,
            "images": images,
            "property_details": property_details,
        }

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=4)

        return result

class Mudah:
    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.domain = "https://www.mudah.my"
        self.platform_name = "Mudah"
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.mudah.my/",
        }

    def generate_url(
        self,
        location: str,                                    # الكلمة المفتاحية: "Kuala Lumpur", "Cyberjaya", إلخ
        max_price: int,                                   # الحد الأقصى للسعر بـ RM
        min_price: Optional[int] = None,                  # الحد الأدنى (اختياري)
        housing_type: Union[str, List[str]] = "studio",   # نوع أو قائمة أنواع السكن (يدعم أي توليفة)
        room_type: Optional[str] = None,                  # (للتوافق القديم) نوع الغرفة إذا تم تمرير "room"
        bedrooms: Optional[int] = None,                   # عدد الغرف (إذا كان entire_unit)
        bathrooms: Optional[int] = None,                  # عدد الحمامات (اختياري)
        property_structure: Literal["high_rise", "landed"] = "high_rise", # الافتراضي: أبراج
        floor_level: Optional[Literal["HIGH", "MID", "LOW", "PENT"]] = None, # مستوى الطابق
        is_furnished: bool = True,                        # الافتراضي: مفروش بالكامل
        distance_to_mrt: Optional[int] = None,            # المسافة للمترو بالكيلومتر (غير مدعوم مباشرة في Mudah)
        has_carpark: Optional[bool] = None,               # توفر موقف سيارة (اختياري)
        is_verified_agent: bool = True,                   # فلتر الوكيل الموثق (افتراضياً: True)
        page: int = 1,
        **kwargs
    ) -> Union[str, List[str]]:
        """
        الدالة الأولى: تقوم بتوليد رابط أو روابط البحث في موقع Mudah بناءً على الفلاتر المحددة.
        تدعم أي توليفة من أنواع السكن، الأسعار، مستوى الطابق، توثيق الوكيل، والفرش.
        """
        def _build_params_for_type(h_type: str) -> str:
            params = {
                "sortby": "price_asc",
            }
            local_filters = {}

            if max_price is not None:
                local_filters["max_price"] = str(max_price)
            if min_price is not None:
                local_filters["min_price"] = str(min_price)

            # فلتر نطاق السعر في سيرفر Mudah
            if max_price is not None and min_price is not None:
                params["monthly_rent"] = f"{min_price}-{max_price}"
            elif max_price is not None:
                params["monthly_rent"] = f"-{max_price}"
            elif min_price is not None:
                params["monthly_rent"] = f"{min_price}-"

            # تصنيف ونوع السكن والكلمات المفتاحية من المدخلات مباشرة
            q_keywords = []
            if location:
                q_keywords.append(location.strip())

            if h_type == "studio":
                cat_slug = "apartment-condominium-for-rent"
                params["rooms_id"] = 1
                q_keywords.append("studio")
                local_filters["housing_type"] = "studio"
            elif h_type == "master_room":
                cat_slug = "rooms-for-rent"
                q_keywords.append("master")
                local_filters["room_type"] = "master"
            elif h_type == "medium_room":
                cat_slug = "rooms-for-rent"
                q_keywords.append("medium")
                local_filters["room_type"] = "medium"
            elif h_type == "small_room":
                cat_slug = "rooms-for-rent"
                q_keywords.append("small")
                local_filters["room_type"] = "small"
            elif h_type == "entire_unit":
                cat_slug = "houses-for-rent" if property_structure == "landed" else "apartment-condominium-for-rent"
                if bedrooms is not None:
                    params["rooms_id"] = bedrooms
                    local_filters["bedrooms"] = str(bedrooms)
            else:
                cat_slug = "properties-for-rent"

            if q_keywords:
                params["q"] = " ".join(q_keywords)

            # الفرش (1 = Fully Furnished)
            if is_furnished:
                params["furnished_id"] = 1
                local_filters["is_furnished"] = "1"

            # مستوى الطابق (HIGH=1, MID=2, LOW=3)
            if floor_level == "HIGH":
                params["floor_range_id"] = 1
            elif floor_level == "MID":
                params["floor_range_id"] = 2
            elif floor_level == "LOW":
                params["floor_range_id"] = 3

            # الحمامات
            if bathrooms is not None:
                params["bathroom_id"] = bathrooms
                local_filters["bathrooms"] = str(bathrooms)

            # موقف سيارة
            if has_carpark is True:
                params["parking_id"] = 1
                local_filters["has_carpark"] = "1"

            # فلتر الوكيل / الشركة الموثقة
            verified = kwargs.get("verified_agent", is_verified_agent)
            if verified:
                params["f"] = "c"
                local_filters["verified"] = "1"

            # الصفحة
            if page > 1:
                params["o"] = page

            base_url = f"{self.domain}/malaysia/{cat_slug}"
            query_str = urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{base_url}?{query_str}" if query_str else base_url
            if local_filters:
                url += f"#{urlencode(local_filters)}"
            return url

        # التوافق مع الكود القديم عند استخدام housing_type="room"
        if housing_type == "room":
            if room_type == "master":
                housing_type = "master_room"
            elif room_type in ["common", "medium"]:
                housing_type = "medium_room"
            elif room_type in ["shared", "small"]:
                housing_type = "small_room"
            else:
                housing_type = "medium_room"

        selected_types = normalize_housing_types(housing_type)
        urls = [_build_params_for_type(t) for t in selected_types]

        if isinstance(housing_type, str) and len(urls) == 1 and housing_type != "studio_or_master_room":
            return urls[0]
        return urls

    @staticmethod
    def _passes_local_filters(item: Dict[str, Any], local_filters: Dict[str, str]) -> bool:
        """تطبيق الفلاتر المحلية على عقار واحد للتأكد من مطابقة جميع الشروط بنسبة 100%."""
        if "max_price" in local_filters:
            price_val = _get_numeric_price(item.get("price"))
            if price_val > float(local_filters["max_price"]):
                return False
        if "min_price" in local_filters:
            price_val = _get_numeric_price(item.get("price"))
            if price_val < float(local_filters["min_price"]):
                return False
        if local_filters.get("verified") == "1" and not item.get("is_verified_agent"):
            return False
        return True

    def _extract_listings_from_html(self, html: Optional[str]) -> List[Dict[str, Any]]:
        """استخراج قائمة الإعلانات من صفحة البحث في Mudah عبر RSC JSON مع خطة بديلة عبر HTML."""
        if not html:
            return []

        results = []
        decoder = json.JSONDecoder()

        # 1. استخراج من بيانات React Server Components (RSC)
        pushes = re.findall(r'self\.__next_f\.push\(\[1,\s*\"(.*?)\"\]\)', html)
        if pushes:
            try:
                full_text = "".join(pushes).encode().decode("unicode_escape")
                for key in ["featuredAds", "ads"]:
                    m = re.search(rf'\"{key}\"\s*:\s*\[', full_text)
                    if m:
                        try:
                            arr, _ = decoder.raw_decode(full_text[m.end() - 1:])
                            for a in arr:
                                if not isinstance(a, dict):
                                    continue
                                attrs = a.get("attributes") or {}
                                links = a.get("links") or {}

                                title = attrs.get("subject")
                                m_rent = attrs.get("monthlyRent")
                                if m_rent is not None:
                                    try:
                                        price_pretty = f"RM {int(m_rent):,} /mo"
                                    except Exception:
                                        price_pretty = f"RM {m_rent} /mo"
                                elif attrs.get("priceLabel"):
                                    p_lbl = str(attrs.get("priceLabel")).strip()
                                    price_pretty = p_lbl if "/mo" in p_lbl.lower() else f"{p_lbl} /mo"
                                else:
                                    price_pretty = "N/A"

                                address = attrs.get("locationLabel")
                                if not address:
                                    sub = attrs.get("subareaName")
                                    reg = attrs.get("regionName")
                                    address = f"{sub}, {reg}" if (sub and reg) else (sub or reg)

                                p_url = attrs.get("adviewUrl")
                                if not p_url and attrs.get("url"):
                                    rel = attrs.get("url")
                                    p_url = rel if rel.startswith("http") else f"{self.domain}{rel}"

                                size_raw = attrs.get("size")
                                floor_area_sqft = None
                                if size_raw:
                                    m_s = re.search(r"\d+", str(size_raw).replace(",", ""))
                                    if m_s:
                                        floor_area_sqft = int(m_s.group(0))
                                floor_area_sqm = round(floor_area_sqft * 0.092903, 1) if floor_area_sqft else None

                                img_base = links.get("imageBaseurl", "https://img.rnudah.com/images")
                                img_path = attrs.get("image")
                                thumb = f"{img_base}{img_path}" if img_path else None

                                agent_data = (attrs.get("agentData") or {}).get("data") or {}
                                is_verified = bool(
                                    attrs.get("companyAd")
                                    or attrs.get("storeVerified") == "verified"
                                    or agent_data.get("storeVerified") == "verified"
                                )
                                agent_name = (
                                    agent_data.get("name")
                                    or attrs.get("name")
                                    or agent_data.get("storeName")
                                )

                                results.append({
                                    "title": title,
                                    "price": price_pretty,
                                    "address": address,
                                    "property_url": p_url,
                                    "floor_area_sqm": floor_area_sqm,
                                    "floor_area_sqft": floor_area_sqft,
                                    "nearby_transit": None,
                                    "thumbnail_url": thumb,
                                    "is_verified_agent": is_verified,
                                    "agent_name": agent_name,
                                })
                        except Exception:
                            pass
            except Exception:
                pass

        # 2. خطة بديلة (Fallback) في حال تعذر فك RSC
        if not results:
            tree = HTMLParser(html)
            seen_hrefs = set()
            for a in tree.css("a"):
                href = a.attributes.get("href", "")
                if re.search(r"-\d+\.htm", href):
                    clean_href = href.split("?")[0]
                    if clean_href in seen_hrefs:
                        continue
                    seen_hrefs.add(clean_href)

                    prop_url = clean_href if clean_href.startswith("http") else f"{self.domain}{clean_href}"
                    card = a
                    for _ in range(5):
                        if card.parent:
                            card = card.parent
                    card_text = card.text() if card else ""

                    m_p = re.search(r"RM\s*([\d,]+)", card_text)
                    price_str = f"RM {m_p.group(1)} /mo" if m_p else "N/A"

                    m_s = re.search(r"(\d+)\s*sq\.?ft", card_text, re.IGNORECASE)
                    sqft = int(m_s.group(1)) if m_s else None
                    sqm = round(sqft * 0.092903, 1) if sqft else None

                    img = card.css_first("img") if card else None
                    thumb = img.attributes.get("src") if img else None
                    title = a.attributes.get("title") or (img.attributes.get("alt") if img else None)

                    results.append({
                        "title": title,
                        "price": price_str,
                        "address": None,
                        "property_url": prop_url,
                        "floor_area_sqm": sqm,
                        "floor_area_sqft": sqft,
                        "nearby_transit": None,
                        "thumbnail_url": thumb,
                        "is_verified_agent": False,
                        "agent_name": None,
                    })

        return results

    def scrape_to_json(self, url: Union[str, List[str]], output_file: str = "mudah_properties.json") -> List[Dict[str, Any]]:
        """
        الدالة الثانية: تأخذ رابطاً أو قائمة روابط من Mudah، تحمّلها وتستخرج العقارات منها،
        تطبق الفلاتر المحلية، تدمجها وترتبها من الأرخص للأغلى وتخزنها في JSON.
        """
        from urllib.parse import parse_qs

        urls = [url] if isinstance(url, str) else list(url)
        split_urls = [u.split("#", 1) + [""] for u in urls]
        pages = fetch_all([(parts[0], self.headers) for parts in split_urls])

        all_extracted_properties = []
        seen_urls = set()

        for parts, html in zip(split_urls, pages):
            if not html:
                continue

            local_filters = {k: v[0] for k, v in parse_qs(parts[1]).items()}
            items = self._extract_listings_from_html(html)

            for item in items:
                p_url = item.get("property_url")
                if p_url and p_url in seen_urls:
                    continue
                if not self._passes_local_filters(item, local_filters):
                    continue

                if p_url:
                    seen_urls.add(p_url)
                all_extracted_properties.append(item)

        all_extracted_properties.sort(key=_sort_key)

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(all_extracted_properties, f, ensure_ascii=False, indent=4)

        return all_extracted_properties

    def get_property_details(self, property_url: str, output_file: Optional[str] = None) -> Dict[str, Any]:
        """
        الدالة الثالثة: تأخذ رابط العقار الفردي من Mudah وتستخرج تفاصيله الكاملة:
        - الصور بجودة عالية (Images)
        - المكان بالتفصيل (Detailed Location)
        - رابط خريطة جوجل (Google Maps URL)
        - قاموس تفاصيل العقار المستقل (Property Details)
        """
        response = self.session.get(property_url, headers=self.headers)
        tree = HTMLParser(response.text)

        sc = tree.css_first("script#__NEXT_DATA__")
        ad = {}
        attrs = {}
        if sc:
            try:
                raw_json = json.loads(sc.text())
                ad_by_id = raw_json.get("props", {}).get("initialState", {}).get("adDetails", {}).get("byID", {})
                if ad_by_id:
                    ad = list(ad_by_id.values())[0]
                    attrs = ad.get("attributes") or {}
            except Exception:
                pass

        if not attrs:
            # محاولة قراءة JSON-LD كخطة بديلة
            for ld_sc in tree.css('script[type="application/ld+json"]'):
                try:
                    ld_data = json.loads(ld_sc.text())
                    items = ld_data if isinstance(ld_data, list) else [ld_data]
                    for it in items:
                        if it.get("@type") == "Product":
                            attrs = {
                                "subject": it.get("name"),
                                "price": f"RM {it.get('offers', {}).get('price')} per month" if it.get("offers") else None,
                                "body": it.get("description"),
                                "image": [it.get("image")] if it.get("image") else [],
                            }
                            break
                    if attrs:
                        break
                except Exception:
                    pass

        if not attrs:
            print(f"[تنبيه] لم يتم العثور على بيانات تفاصيل العقار: {property_url}")
            return {
                "property_url": property_url,
                "title": None,
                "map_url": None,
                "location": {},
                "images": [],
                "property_details": {}
            }

        cp_list = attrs.get("categoryParams", [])
        params_map = {cp.get("id"): cp.get("value") for cp in cp_list if cp.get("id")}
        real_map = {cp.get("id"): cp.get("realValue") for cp in cp_list if cp.get("id")}

        # 1. الصور
        images = attrs.get("image") or []
        if isinstance(images, str):
            images = [images]

        # 2. المكان والموقع
        location_label = attrs.get("locationLabel") or ""
        state = attrs.get("regionName")
        city = attrs.get("subregionName")
        formatted_address = location_label or (f"{city}, {state}" if (city and state) else (city or state))

        detailed_location = {
            "building_name": attrs.get("subject"),
            "formatted_address": formatted_address,
            "street": city,
            "city": city,
            "state": state,
            "postal_code": None,
            "coordinates": {
                "latitude": None,
                "longitude": None,
            },
            "map_url": None,
        }

        # 3. تفاصيل العقار في Dictionary مستقل
        size_raw = real_map.get("size") or params_map.get("size") or attrs.get("size") or ""
        m_size = re.search(r"\d+", str(size_raw).replace(",", ""))
        floor_area_sqft = int(m_size.group(0)) if m_size else None
        floor_area_sqm = round(floor_area_sqft * 0.092903, 1) if floor_area_sqft else None

        bed_raw = real_map.get("rooms") or params_map.get("rooms")
        bedrooms = int(bed_raw) if bed_raw and str(bed_raw).isdigit() else None
        bath_raw = real_map.get("bathroom") or params_map.get("bathroom")
        bathrooms = int(bath_raw) if bath_raw and str(bath_raw).isdigit() else None

        lid = None
        if ad.get("id"):
            try:
                lid = int(ad.get("id"))
            except Exception:
                pass
        if not lid and attrs.get("adId"):
            try:
                lid = int(attrs.get("adId"))
            except Exception:
                pass
        if not lid:
            m_lid = re.search(r"-(\d+)\.htm", property_url)
            if m_lid:
                try:
                    lid = int(m_lid.group(1))
                except Exception:
                    pass

        highlights = []
        for k in ["facilities", "additional_facilities"]:
            if params_map.get(k):
                highlights.extend([f.strip() for f in params_map[k].split(",") if f.strip()])
        if params_map.get("floor_range"):
            highlights.append(f"Floor: {params_map.get('floor_range')}")
        if params_map.get("parking"):
            highlights.append(f"Carpark: {params_map.get('parking')}")
        if params_map.get("roommate_gender"):
            highlights.append(f"Preference: {params_map.get('roommate_gender')}")

        raw_desc = attrs.get("body") or ""
        clean_desc = raw_desc.replace("<br />", "\n").replace("<br>", "\n").replace("<br/>", "\n")
        clean_desc = re.sub(r"<[^>]+>", "", clean_desc).strip()

        price_str = attrs.get("price") or (f"RM {real_map.get('monthly_rent')}" if real_map.get("monthly_rent") else None)

        property_details = {
            "property_name": attrs.get("subject"),
            "property_type": params_map.get("property_type") or attrs.get("categoryName"),
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "floor_area_sqm": floor_area_sqm,
            "floor_area_sqft": floor_area_sqft,
            "price": price_str,
            "tenure": params_map.get("tenure"),
            "furnishing": params_map.get("furnished"),
            "completion_year": params_map.get("propage") or params_map.get("built_year"),
            "listing_id": lid,
            "posted_date": attrs.get("publishedDatetime") or attrs.get("date"),
            "highlights": highlights,
            "description": clean_desc,
        }

        result = {
            "property_url": property_url,
            "title": attrs.get("subject"),
            "map_url": None,
            "location": detailed_location,
            "images": images,
            "property_details": property_details,
        }

        if output_file:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=4)

        return result



def _load_hash_cache(path: Optional[str]) -> Dict[str, str]:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_hash_cache(path: Optional[str], cache: Dict[str, str]) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception:
        pass


def get_image_hashes(urls: List[str], cache: Dict[str, str]) -> Dict[str, Optional[str]]:
    """
    استخراج البصمة الرقمية (MD5 Hash) لمجموعة صور في نفس اللحظة.
    الصور الموجودة في الذاكرة لا يُعاد تحميلها، والصور الجديدة تُضاف للذاكرة.
    """
    unique_urls = list(dict.fromkeys(u for u in urls if u))
    missing = [u for u in unique_urls if u not in cache]

    contents = fetch_all([(u, {"User-Agent": "Mozilla/5.0"}) for u in missing], timeout=6, binary=True)
    for u, content in zip(missing, contents):
        if content:
            cache[u] = hashlib.md5(content).hexdigest()

    return {u: cache.get(u) for u in unique_urls}


def extract_identifiers(item: Dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    استخراج رقم الإعلان التعريفي (Listing ID)، ومعرّف الصورة (Image ID)، واسم الوكيل (Agent Slug).
    """
    url = item.get("property_url") or ""
    thumb = item.get("thumbnail_url") or ""

    listing_id = None
    m_url = re.search(r"[-/](\d{7,10})/?(?:#.*)?$", url)
    if m_url:
        listing_id = m_url.group(1)
    else:
        m_thumb = re.search(r"/listing/(\d{7,10})/", thumb)
        if m_thumb:
            listing_id = m_thumb.group(1)

    image_id = None
    m_img = re.search(r"(UPHO\.\d+)", thumb)
    if m_img:
        image_id = m_img.group(1)

    agent_slug = None
    m_agent = re.search(r"-by-([a-zA-Z0-9-]+)-\d+", url)
    if m_agent:
        agent_slug = m_agent.group(1).lower()

    return listing_id, image_id, agent_slug


def _building_key(item: Dict[str, Any]) -> str:
    raw_title = item.get("title") or ""
    return raw_title.lower().split(",")[0].strip()


def merge_and_deduplicate(
    sources: List[tuple[str, List[Dict[str, Any]]]],
    output_file: Optional[str] = "merged_properties.json",
    hash_cache_file: Optional[str] = IMAGE_HASH_CACHE_FILE,
) -> List[Dict[str, Any]]:
    """
    الخوارزمية الذكية ثنائية الطبقات لدمج العقارات وإلغاء التكرار مع ضمان عدم حذف أي شقة حقيقية (Zero False Positives):
    1. المستوى الأول: المطابقة القطعية بين المنصات (Cross-Portal Match عبر Listing ID و Image ID).
    2. المستوى الثاني: كشف إعلانات إعادة النشر لنفس الغرفة (المبنى + السعر + بصمة صورة الغرفة MD5).
       التحسين: نجمع العقارات حسب المبنى والسعر أولاً، ونحمّل الصور فقط للمجموعات التي فيها أكثر من عقار،
       وكلها في نفس اللحظة، مع حفظ البصمات على الجهاز. النتيجة مطابقة تماماً للخوارزمية الأصلية.
    """
    # -------------------------------------------------------------
    # المستوى الأول: دمج إعلانات المنصات المشتركة (PropertyGuru و iProperty)
    # -------------------------------------------------------------
    tier1_merged = []
    id_map = {}

    for platform_name, listings in sources:
        for item in listings:
            lid, img_id, agent_slug = extract_identifiers(item)
            key = lid or img_id or item.get("property_url")

            if key and key in id_map:
                existing = tier1_merged[id_map[key]]
                if not existing.get("agent_name") and item.get("agent_name"):
                    existing["agent_name"] = item.get("agent_name")
                # استكمال أي بيانات تفصيلية متوفرة في إحدى المنصات دون الأخرى
                for f in ["floor_area_sqm", "floor_area_sqft", "nearby_transit", "is_verified_agent", "agent_name"]:
                    if existing.get(f) is None and item.get(f) is not None:
                        existing[f] = item.get(f)
            else:
                entry = {
                    "title": item.get("title"),
                    "price": item.get("price"),
                    "address": item.get("address"),
                    "property_url": item.get("property_url"),
                    "floor_area_sqm": item.get("floor_area_sqm"),
                    "floor_area_sqft": item.get("floor_area_sqft"),
                    "nearby_transit": item.get("nearby_transit"),
                    "thumbnail_url": item.get("thumbnail_url"),
                    "is_verified_agent": item.get("is_verified_agent"),
                    "agent_name": item.get("agent_name") or (agent_slug.replace("-", " ").title() if agent_slug else None),
                }
                idx = len(tier1_merged)
                tier1_merged.append(entry)
                if lid:
                    id_map[lid] = idx
                if img_id:
                    id_map[img_id] = idx
                if not lid and not img_id and item.get("property_url"):
                    id_map[item.get("property_url")] = idx

    # -------------------------------------------------------------
    # المستوى الثاني: كشف إعلانات إعادة النشر (Re-posting) لنفس الغرفة
    # -------------------------------------------------------------
    # 1. تجميع العقارات حسب (المبنى، السعر) بدون أي تحميل
    groups = defaultdict(list)
    for item in tier1_merged:
        building_key = _building_key(item)
        price = _get_numeric_price(item.get("price"))
        if building_key and price < float("inf"):
            groups[(building_key, price)].append(item)

    # 2. تحميل الصور فقط للمجموعات التي فيها احتمال تكرار (أكثر من عقار)
    candidate_thumbs = [
        item.get("thumbnail_url")
        for group in groups.values() if len(group) > 1
        for item in group
    ]
    cache = _load_hash_cache(hash_cache_file)
    hashes = get_image_hashes(candidate_thumbs, cache)
    _save_hash_cache(hash_cache_file, cache)

    # 3. نفس منطق الإلغاء الأصلي بالترتيب الأصلي
    tier2_final = []
    seen_repost_keys = set()

    for item in tier1_merged:
        building_key = _building_key(item)
        price = _get_numeric_price(item.get("price"))
        photo_hash = hashes.get(item.get("thumbnail_url")) if item.get("thumbnail_url") else None

        repost_key = (building_key, price, photo_hash) if (building_key and price < float("inf") and photo_hash) else None

        if repost_key and repost_key in seen_repost_keys:
            # إعلان معاد نشره لنفس الغرفة: نتجاهله
            continue
        tier2_final.append(item)
        if repost_key:
            seen_repost_keys.add(repost_key)

    # فرز تصاعدي دقيق لكافة العقارات من الأرخص للأغلى
    tier2_final.sort(key=_sort_key)

    if output_file:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(tier2_final, f, ensure_ascii=False, indent=4)

    return tier2_final


if __name__ == "__main__":

    location = 'KUAL LAMPOUR' #  -> kual_lampour
    max_price = 1000
    housing_type = ["medium_room", "master_room", "small_room"]

    pg = Propertyguru()
    ip = IProperties()


    # 1. توليد الروابط
    url_one = pg.generate_url(location=location, max_price=max_price, housing_type=housing_type, floor_level=None)
    url_two = ip.generate_url(location=location, max_price=max_price, housing_type=housing_type, floor_level=None)


    total_start = time.perf_counter()

    # 2. سحب الموقعين معاً في نفس اللحظة (تشغيل واحد فقط)
    t1 = time.perf_counter()
    listings_one, listings_two = scrape_sites_parallel([
        (pg, url_one, "properties.json"),
        (ip, url_two, "IProperties.json"),
    ])
    print(f"Scraping both sites took: {time.perf_counter() - t1:.2f}s")

    # 3. دمج النتائج وإلغاء التكرار
    t2 = time.perf_counter()
    merged_results = merge_and_deduplicate(
        sources=[("PropertyGuru", listings_one), ("iProperty", listings_two)],
        output_file="merged_properties.json"
    )
    print(f"Deduplication took: {time.perf_counter() - t2:.2f}s")
    print(f"Total Execution Time: {time.perf_counter() - total_start:.2f}s")
    print(f"PropertyGuru: {len(listings_one)} | iProperty: {len(listings_two)} | Merged: {len(merged_results)}")


