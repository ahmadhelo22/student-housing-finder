"""
location_filter.py
==================
خدمة فلترة العقارات حسب القرب من موقع محدد (Specific Location).
تأخذ نتائج البحث (OUTPUT 1) وإحداثيات الموقع المطلوب (خط العرض وخط الطول)،
وترجع فقط العقارات الموجودة داخل دائرة نصف قطرها 5 كيلومتر (افتراضياً) حول الموقع،
مع إضافة إحداثيات كل عقار ومسافته عن الموقع بالكيلومتر.

مصدر إحداثيات كل عقار (بالترتيب):
1. حقل coordinates داخل العقار نفسه إن وُجد.
2. ذاكرة الإحداثيات المحفوظة على الجهاز (لا يُعاد طلب نفس العقار مرتين).
3. صفحة تفاصيل العقار عبر get_property_details (PropertyGuru و iProperty و SPEEDHOME).
   Mudah لا يوفر إحداثيات إطلاقاً، فعقاراته لا يمكن قياس مسافتها.
"""

import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Literal, List, Dict, Any, Tuple
from urllib.parse import urlparse

import Scrapper_2 as scraper


# نصف القطر الافتراضي لدائرة البحث حول الموقع المحدد
DEFAULT_RADIUS_KM = 5.0
# نصف قطر الأرض المتوسط بالكيلومتر
EARTH_RADIUS_KM = 6371.0088
# ملف حفظ إحداثيات العقارات على الجهاز لتفادي إعادة طلب صفحات التفاصيل في كل تشغيل
COORDINATES_CACHE_FILE = "coordinates_cache.json"

# المنصات التي توفر إحداثيات في صفحة التفاصيل (Mudah غير موجود لأنه لا يوفرها)
PLATFORMS_WITH_COORDINATES = {
    "propertyguru.com.my": scraper.Propertyguru,
    "iproperty.com.my": scraper.IProperties,
    "speedhome.com": scraper.Speedhome,
}

# كل خيط (Thread) يملك نسخته الخاصة من كائنات المنصات لأن جلسات curl_cffi غير آمنة للمشاركة بين الخيوط
_thread_local = threading.local()


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """حساب المسافة الحقيقية على سطح الأرض بين نقطتين بالكيلومتر (معادلة Haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _parse_coordinates(value: Any) -> Optional[Tuple[float, float]]:
    """
    تحويل الإحداثيات إلى (خط العرض، خط الطول) مع التحقق من صحتها.
    تقبل قاموساً بالمفاتيح latitude و longitude، أو قائمة من رقمين [lat, lng].
    ترجع None إذا كانت الإحداثيات ناقصة أو خارج النطاق.
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
    """التحقق من مدخل الموقع المحدد وإرجاع (خط العرض، خط الطول، نصف القطر بالكيلومتر)."""
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
    """إرجاع نطاق المنصة إذا كانت توفر إحداثيات، وإلا None."""
    host = urlparse(url).netloc.lower()
    for domain in PLATFORMS_WITH_COORDINATES:
        if host == domain or host.endswith("." + domain):
            return domain
    return None


def _fetch_coordinates(url: str) -> Optional[Tuple[float, float]]:
    """استخراج إحداثيات عقار واحد من صفحة تفاصيله."""
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
    الدالة الرئيسية: تأخذ نتائج البحث (OUTPUT 1) والموقع المحدد، وترجع العقارات داخل دائرة نصف القطر فقط.
    - specific_location: {"latitude": float, "longitude": float, "radius_km": float (اختياري، الافتراضي 5)}
    - keep_unknown: إذا True تُضاف العقارات التي لا يمكن معرفة موقعها في آخر القائمة مع distance_km = None
    - sort_by: "price" (من الأرخص للأغلى كباقي الكود) أو "distance" (من الأقرب للأبعد)
    كل عقار في الناتج يحتوي على نفس حقول OUTPUT 1 مضافاً إليها coordinates و distance_km.
    """
    center_lat, center_lng, radius_km = _parse_specific_location(specific_location)
    cache = scraper._load_hash_cache(cache_file)

    # 1. جمع الإحداثيات المتوفرة مسبقاً (من العقار نفسه أو من الذاكرة)
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

    # 2. طلب صفحات التفاصيل الناقصة في نفس اللحظة مع حد أقصى للطلبات المتزامنة لتفادي الحظر
    if missing:
        with ThreadPoolExecutor(max_workers=scraper.MAX_CONCURRENCY) as pool:
            for url, coords in zip(missing, pool.map(_fetch_coordinates, missing)):
                if coords:
                    known[url] = coords
                    cache[url] = list(coords)
        scraper._save_hash_cache(cache_file, cache)

    # 3. حساب المسافة والإبقاء على العقارات داخل الدائرة فقط
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
    # جامعة APU في بوكيت جليل (إحداثيات تقريبية)
    specific_location = {"latitude": 3.0553, "longitude": 101.7006, "radius_km": 5}

    pg = scraper.Propertyguru()
    ip = scraper.IProperties()
    sh = scraper.Speedhome()

    total_start = time.perf_counter()

    # 1. سحب المواقع الثلاثة معاً
    listings_pg, listings_ip, listings_sh = scraper.scrape_sites_parallel([
        (pg, pg.generate_url(location=location, max_price=max_price, housing_type=housing_type), None),
        (ip, ip.generate_url(location=location, max_price=max_price, housing_type=housing_type), None),
        (sh, sh.generate_url(location=location, max_price=max_price, housing_type=housing_type), None),
    ])

    # 2. دمج النتائج وإلغاء التكرار
    merged = scraper.merge_and_deduplicate(
        sources=[("PropertyGuru", listings_pg), ("iProperty", listings_ip), ("Speedhome", listings_sh)],
        output_file=None,
    )

    # 3. فلترة العقارات حسب القرب من الموقع المحدد
    t = time.perf_counter()
    nearby = filter_by_distance(merged, specific_location, sort_by="distance", output_file="nearby_properties.json")
    print(f"Distance filtering took: {time.perf_counter() - t:.2f}s")
    print(f"Total Execution Time: {time.perf_counter() - total_start:.2f}s")
    print(f"Merged: {len(merged)} | Within {specific_location['radius_km']} km: {len(nearby)}")
    for p in nearby:
        print(f"  {p['distance_km']:>5} km | {p['price']:<14} | {p['title']}")
