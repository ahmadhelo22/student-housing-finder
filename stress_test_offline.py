"""
stress_test_offline.py
======================
Offline stress test for search_pipeline.search(), run against a local MOCK of the four sites.

No request ever leaves this machine. The real sites are never hammered: pushing them "until something
breaks" would be a denial-of-service against someone else's servers, and would get this IP blocked.
Instead, a mock server imitates PropertyGuru, iProperty, SPEEDHOME and Mudah (same page structure the
scrapers parse), and every curl_cffi request is redirected to it, so the project code runs unmodified.

Scenarios:
1. ramp     - N simultaneous searches, doubling N until the code breaks (shared files vs isolated files).
2. bigdata  - one search with huge result pages and thousands of buildings.
3. faults   - slow / hanging / blocked / broken pages, null fields, corrupted cache files.
4. fuzz     - malformed and extreme SEARCH_INPUT values.

Run:  .venv/bin/python stress_test_offline.py [ramp|bigdata|faults|fuzz|all]
"""

import hashlib
import json
import math
import multiprocessing as mp
import os
import random
import re
import resource
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Target point used by every scenario: APU main campus
CENTER_LAT, CENTER_LNG = 3.056069, 101.700466

DEFAULT_CONFIG = {
    "latency": [0.1, 0.4],     # seconds, uniform range per request
    "listings_per_page": 20,
    "buildings": 15,           # distinct buildings per search area
    "search_fault": None,      # None | malformed_json | null_price | null_listing | no_next_data | block_403 | error_500 | hang
    "detail_fault": None,      # same values, applied to detail pages
    "fault_rate": 1.0,         # fraction of matching requests that get the fault (deterministic per URL)
    "fault_hosts": None,       # list of hosts the faults apply to, None = all
    "fault_limit": None,       # inject at most this many faults (e.g. 1 = exactly one broken page), None = no limit
    "hang_seconds": 25,
}


# ---------------------------------------------------------------------------------------------
# Mock sites (runs in its own process, so it does not compete with the code under test)
# ---------------------------------------------------------------------------------------------

def _rng(*parts) -> random.Random:
    """Deterministic random generator: the same inputs always produce the same listings."""
    return random.Random(int(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest(), 16))


def building_coords(area: str, bid: int):
    """Location of a mock building: spread within ~11 km of the target point."""
    r = _rng("coords", area, bid)
    return CENTER_LAT + r.uniform(-0.1, 0.1), CENTER_LNG + r.uniform(-0.1, 0.1)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "x"


def _next_data_page(data) -> bytes:
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></body></html>'.encode()


def _price_range(query, key_max, key_min=None):
    try:
        hi = int(float(query.get(key_max, ["3000"])[0]))
    except ValueError:
        hi = 3000
    lo = 300
    if key_min and key_min in query:
        try:
            lo = max(lo, int(float(query[key_min][0])))
        except ValueError:
            pass
    return lo, hi


def _pg_search_page(query, cfg, fault):
    """PropertyGuru / iProperty search page. Both platforms list the SAME units (as in reality)."""
    area = query.get("freetext", [""])[0]
    lo, hi = _price_range(query, "maxPrice", "minPrice")
    key = (area, query.get("roomType", [""])[0], query.get("bedrooms", [""])[0], hi, lo, query.get("page", ["1"])[0])
    rng = _rng("pg", *key)
    listings = []
    for _ in range(cfg["listings_per_page"] if hi >= lo else 0):
        bid = rng.randrange(1, cfg["buildings"] + 1)
        lid = rng.randrange(10_000_000, 99_999_999)
        price = rng.randrange(lo, hi + 1)
        listings.append({"listingData": {
            "localizedTitle": f"{area} Tower {bid}",
            "price": {"pretty": f"RM {price:,} /mo", "value": price},
            "fullAddress": f"Jalan {bid}, {area}",
            "url": f"/property-listing/b{bid}-{_slug(area)}-for-rent-by-mock-agent-{lid}",
            "floorArea": rng.randrange(150, 1200),
            "mrt": {"nearbyText": "500 m from Mock LRT"},
            "agent": {"isAgentVerified": True, "name": "Mock Agent"},
            "thumbnail": f"https://my1-cdn.pgimgs.com/listing/{lid}/UPHO.{lid}.V550.jpg",
            "property": {"id": f"{_slug(area)}-{bid}"},
        }})
    if listings and fault == "null_price":
        listings[0]["listingData"]["price"] = None
    if listings and fault == "null_listing":
        listings[0]["listingData"] = None
    return {"props": {"pageProps": {"pageData": {"data": {"listingsData": listings}}}}}


def _pg_detail_page(path):
    m = re.search(r"/property-listing/b(\d+)-(.*?)-for-rent", path)
    bid, area_slug = (int(m.group(1)), m.group(2)) if m else (0, "x")
    # Coordinates depend on the area slug + building, so the harness can recompute them
    lat, lng = building_coords(area_slug, bid)
    return {"props": {"pageProps": {"pageData": {"data": {
        "listingData": {"localizedTitle": f"Tower {bid}", "propertyName": f"Tower {bid}"},
        "listingLocationData": {"data": {"center": {"lat": lat, "lng": lng}}},
    }}}}}


def _speedhome_search_page(query, cfg):
    area = query.get("q", [""])[0]
    lo, hi = _price_range(query, "max", "min")
    room = query.get("type", [""])[0] == "ROOM"
    rng = _rng("sh", area, query.get("type", [""])[0], hi, lo, query.get("page", ["1"])[0])
    content = []
    for i in range(cfg["listings_per_page"] if hi >= lo else 0):
        uid = rng.randrange(1_000_000, 9_999_999)
        lat, lng = building_coords(_slug(area) + "-sh", rng.randrange(1, cfg["buildings"] + 1))
        content.append({
            "slug": f"{_slug(area)}-unit-{uid}", "name": f"{area} Residence {i}", "price": rng.randrange(lo, hi + 1),
            "address": f"Jalan SH {i}", "city": "Kuala Lumpur", "state": "WP", "sqft": rng.randrange(150, 900),
            "images": [{"url": f"https://img.speedhome.com/{uid}-medium.jpg", "coverPhoto": True}],
            "user": {"isVerifiedUser": True, "name": "Mock Owner"},
            "bedroom": 1 if room else 0, "bathroom": 1,
            "roomType": ["MASTER", "MEDIUM", "SMALL"][i % 3] if room else None,
            "latitude": lat, "longitude": lng,
        })
    return {"props": {"pageProps": {"propertyList": {"content": content}}}}


def _mudah_search_page(query, cfg) -> bytes:
    area = query.get("q", [""])[0]
    rng = _rng("md", area, query.get("monthly_rent", [""])[0])
    cards = []
    for i in range(cfg["listings_per_page"]):
        ad_id = rng.randrange(100_000_000, 999_999_999)
        cards.append(
            f'<div><div><div><div><div><a href="/{_slug(area)}-room-{ad_id}.htm" title="{area} Room {i}">'
            f'<img src="https://img.mudah.my/{ad_id}.jpg"></a><span>RM {rng.randrange(300, 1500)}</span></div></div></div></div></div>'
        )
    return f"<html><body>{''.join(cards)}</body></html>".encode()


class _MockState:
    config = dict(DEFAULT_CONFIG)
    stats = Counter()
    in_flight = 0
    peak_in_flight = 0
    lock = threading.Lock()


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 120

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/__config":
            with _MockState.lock:
                _MockState.config = {**DEFAULT_CONFIG, **json.loads(body or b"{}")}
            return self._send(200, b"ok", "text/plain")
        self._send(404, b"")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/__stats":
            with _MockState.lock:
                data = {**_MockState.stats, "peak_in_flight": _MockState.peak_in_flight}
            return self._send(200, json.dumps(data).encode(), "application/json")
        if url.path == "/__reset":
            with _MockState.lock:
                _MockState.stats.clear()
                _MockState.peak_in_flight = 0
            return self._send(200, b"ok", "text/plain")

        with _MockState.lock:
            _MockState.in_flight += 1
            _MockState.peak_in_flight = max(_MockState.peak_in_flight, _MockState.in_flight)
            cfg = dict(_MockState.config)
        try:
            self._serve_site(url, cfg)
        finally:
            with _MockState.lock:
                _MockState.in_flight -= 1

    def _serve_site(self, url, cfg):
        host = self.headers.get("X-Original-Host", "").lower().removeprefix("www.")
        query = parse_qs(url.query)
        is_search = (
            (host in ("propertyguru.com.my", "iproperty.com.my") and url.path == "/property-for-rent")
            or (host == "speedhome.com" and url.path.startswith("/rent/"))
            or (host == "mudah.my" and url.path.startswith("/malaysia/"))
        )
        is_detail = (
            (host in ("propertyguru.com.my", "iproperty.com.my") and url.path.startswith("/property-listing/"))
            or (host == "speedhome.com" and url.path.startswith("/details/"))
            or (host == "mudah.my" and url.path.endswith(".htm"))
        )
        kind = "search" if is_search else "detail" if is_detail else "image"
        with _MockState.lock:
            _MockState.stats[f"{kind}:{host}"] += 1
            _MockState.stats[kind] += 1

        time.sleep(random.uniform(*cfg["latency"]))

        # Fault injection (deterministic per URL, so a scenario is reproducible)
        fault = cfg.get(f"{kind}_fault") if kind != "image" else None
        if fault and cfg.get("fault_hosts") and host not in cfg["fault_hosts"]:
            fault = None
        if fault and int(hashlib.md5(self.path.encode()).hexdigest(), 16) % 1000 >= cfg["fault_rate"] * 1000:
            fault = None
        if fault:
            with _MockState.lock:
                injected = sum(v for k, v in _MockState.stats.items() if k.startswith("fault:"))
                if cfg.get("fault_limit") is not None and injected >= cfg["fault_limit"]:
                    fault = None
                else:
                    _MockState.stats[f"fault:{fault}"] += 1
        if fault == "hang":
            time.sleep(cfg["hang_seconds"])
        elif fault == "block_403":
            return self._send(403, b"<html>DataDome: please solve the captcha</html>")
        elif fault == "error_500":
            return self._send(500, b"<html>Internal Server Error</html>")
        elif fault == "no_next_data":
            return self._send(200, b"<html><body>Please enable JavaScript</body></html>")
        elif fault == "malformed_json":
            return self._send(200, b'<html><script id="__NEXT_DATA__" type="application/json">{"props": {"pageProps": </script></html>')

        if kind == "image":
            return self._send(200, hashlib.md5(self.path.encode()).digest() * 64, "image/jpeg")
        if host in ("propertyguru.com.my", "iproperty.com.my"):
            page = _pg_search_page(query, cfg, fault) if is_search else _pg_detail_page(url.path)
            return self._send(200, _next_data_page(page))
        if host == "speedhome.com" and is_search:
            return self._send(200, _next_data_page(_speedhome_search_page(query, cfg)))
        if host == "mudah.my" and is_search:
            return self._send(200, _mudah_search_page(query, cfg))
        self._send(404, b"<html>not found</html>")


class _MockServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 4096

    def handle_error(self, request, client_address):
        # A client giving up on a hanging page resets the connection; that is expected here
        pass


def run_mock_server(port_queue):
    server = _MockServer(("127.0.0.1", 0), MockHandler)
    port_queue.put(server.server_address[1])
    server.serve_forever()


# ---------------------------------------------------------------------------------------------
# Harness (runs the real project code, with its HTTP traffic redirected to the mock)
# ---------------------------------------------------------------------------------------------

class Mock:
    """One or more mock server processes; several processes keep the mock from being the bottleneck."""

    def __init__(self, processes: int = 1):
        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        self.procs = [ctx.Process(target=run_mock_server, args=(q,), daemon=True) for _ in range(processes)]
        for proc in self.procs:
            proc.start()
        self.ports = [q.get(timeout=30) for _ in self.procs]

    def configure(self, **cfg):
        for port in self.ports:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/__config", data=json.dumps(cfg).encode(), method="POST")
            urllib.request.urlopen(req, timeout=60).read()
            urllib.request.urlopen(f"http://127.0.0.1:{port}/__reset", timeout=60).read()

    def stats(self):
        total = Counter()
        for port in self.ports:
            try:
                total.update(json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/__stats", timeout=60).read()))
            except Exception as e:
                total["stats_unavailable"] += 1
                print(f"         (mock stats unavailable on port {port}: {type(e).__name__})")
        return total

    def stop(self):
        for proc in self.procs:
            proc.terminate()


def install_redirect(ports):
    """Send every curl_cffi request (sync and async) to a mock server instead of the real site."""
    from curl_cffi.requests import Session, AsyncSession

    def rewrite(url, headers):
        u = urlparse(url)
        headers = dict(headers or {})
        headers["X-Original-Host"] = u.netloc
        port = random.choice(ports)
        return f"http://127.0.0.1:{port}{u.path or '/'}" + (f"?{u.query}" if u.query else ""), headers

    def patch(cls):
        original = cls.request

        def request(self, method, url, *args, headers=None, **kwargs):
            url, headers = rewrite(url, headers)
            return original(self, method, url, *args, headers=headers, **kwargs)

        cls.request = request

    patch(Session)
    patch(AsyncSession)


class ResourceMonitor:
    """Samples thread count and open file descriptors while a scenario runs."""

    def __init__(self):
        self.peak_threads = 0
        self.peak_fds = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.peak_threads = max(self.peak_threads, threading.active_count())
            try:
                self.peak_fds = max(self.peak_fds, len(os.listdir("/dev/fd")))
            except OSError:
                pass
            time.sleep(0.05)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def rss_mb() -> float:
    # ru_maxrss is in bytes on macOS, kilobytes on Linux
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def make_input(i: int, **overrides):
    base = {
        "area": f"Area{i}",
        "max_price": 1500,
        "housing_type": ["master_room", "medium_room", "small_room", "studio"],
        "location": {"latitude": CENTER_LAT, "longitude": CENTER_LNG, "radius_km": 5},
    }
    base.update(overrides)
    return base


def fresh_dir(tag: str) -> str:
    d = tempfile.mkdtemp(prefix=f"stress_{tag}_")
    os.chdir(d)
    return d


def check_results(search_input, results, expected_count=None):
    """Return a list of correctness problems in one search's results."""
    problems = []
    area = str(search_input.get("area"))
    loc = search_input.get("location") or {}
    radius = loc.get("radius_km", 5)
    foreign = [p for p in results if not str(p.get("title", "")).startswith(area + " ")]
    if foreign:
        problems.append(f"{len(foreign)} listings from another user's search")
    if loc:
        bad = [p for p in results if p.get("distance_km") is None or p["distance_km"] > radius]
        if bad:
            problems.append(f"{len(bad)} listings outside the radius")
    if expected_count is not None and len(results) != expected_count:
        problems.append(f"{len(results)} results instead of {expected_count}")
    return problems


def run_one(search_input, unique_files: bool, tag: str, **kwargs):
    import search_pipeline
    t = time.perf_counter()
    if unique_files:
        kwargs.setdefault("scraped_file", f"merged_{tag}.json")
        kwargs.setdefault("output_file", f"nearby_{tag}.json")
    try:
        results = search_pipeline.search(search_input, **kwargs)
        return {"ok": True, "results": results, "time": time.perf_counter() - t}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:160], "time": time.perf_counter() - t,
                "trace": traceback.format_exc()}


def percentile(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(math.ceil(p / 100 * len(values))) - 1)]


# ------------------------------------------- scenarios -------------------------------------------

def scenario_ramp(mock: Mock, levels=(1, 2, 5, 10, 25, 50, 100, 200, 400, 800, 1600, 3200), distinct=40,
                  level_timeout=300, modes=("shared_files", "isolated_files")):
    print("\n=== 1. CONCURRENCY RAMP ===")
    mock.configure()
    # Expected result count per area, measured one search at a time
    fresh_dir("baseline")
    baseline = {}
    for i in range(distinct):
        r = run_one(make_input(i), unique_files=True, tag=f"b{i}")
        baseline[i] = len(r["results"]) if r["ok"] else None
    print(f"baseline: {distinct} distinct searches, results per search: "
          f"min {min(baseline.values())} / max {max(baseline.values())}")

    report = {}
    for mode in modes:
        print(f"\n-- mode: {mode} "
              f"({'every search writes the same merged/nearby JSON files' if mode == 'shared_files' else 'each search gets its own JSON files'})")
        print(f"{'users':>6} | {'wall s':>7} | {'p50 s':>6} | {'p95 s':>6} | {'crashed':>7} | {'wrong':>5} | "
              f"{'threads':>7} | {'fds':>5} | {'RSS MB':>7} | {'site reqs':>9} | {'detail':>6} | verdict")
        report[mode] = []
        for level in levels:
            fresh_dir(f"{mode}_{level}")
            mock.configure()
            inputs = [(i % distinct, make_input(i % distinct)) for i in range(level)]
            start = time.perf_counter()
            with ResourceMonitor() as mon:
                pool = ThreadPoolExecutor(max_workers=level)
                futures = [pool.submit(run_one, inp, mode == "isolated_files", f"u{n}") for n, (_, inp) in enumerate(inputs)]
                outcomes, timed_out = [], 0
                for f in futures:
                    try:
                        outcomes.append(f.result(timeout=max(1, level_timeout - (time.perf_counter() - start))))
                    except FutureTimeout:
                        timed_out += 1
                        outcomes.append({"ok": False, "error": "TIMEOUT", "time": level_timeout})
                pool.shutdown(wait=False, cancel_futures=True)
            wall = time.perf_counter() - start
            crashed = sum(1 for o in outcomes if not o["ok"])
            wrong = 0
            errors = Counter(o["error"].split(":")[0] for o in outcomes if not o["ok"])
            samples = []
            for (i, inp), o in zip(inputs, outcomes):
                if o["ok"]:
                    probs = check_results(inp, o["results"], baseline[i])
                    if probs:
                        wrong += 1
                        samples.append(probs[0])
            st = mock.stats()
            times = [o["time"] for o in outcomes]
            verdict = "PASS" if not crashed and not wrong else ("WRONG RESULTS" if not crashed else "FAIL")
            print(f"{level:>6} | {wall:>7.1f} | {percentile(times, 50):>6.1f} | {percentile(times, 95):>6.1f} | "
                  f"{crashed:>7} | {wrong:>5} | {mon.peak_threads:>7} | {mon.peak_fds:>5} | {rss_mb():>7.0f} | "
                  f"{st.get('search', 0) + st.get('detail', 0) + st.get('image', 0):>9} | {st.get('detail', 0):>6} | {verdict}")
            if errors:
                print(f"         errors: {dict(errors.most_common(3))}")
            if samples:
                print(f"         e.g.: {Counter(samples).most_common(2)}")
            report[mode].append({"users": level, "wall": wall, "crashed": crashed, "wrong": wrong,
                                 "errors": dict(errors), "threads": mon.peak_threads, "verdict": verdict})
            # Stop the ramp once most searches fail, or the level could not finish in time
            if timed_out or crashed > level / 2 or (mode == "shared_files" and level >= 25 and verdict != "PASS"):
                print("         -> stopping this ramp")
                break
    return report


def scenario_bigdata(mock: Mock, sizes=(20, 200, 1000, 5000, 20000), time_limit=300):
    print("\n=== 2. BIG DATA (one search, huge pages) ===")
    print(f"{'per page':>8} | {'buildings':>9} | {'scraped':>7} | {'kept':>6} | {'time s':>7} | {'RSS MB':>7} | "
          f"{'detail reqs':>11} | {'image reqs':>10} | {'JSON MB':>7} | verdict")
    for size in sizes:
        fresh_dir(f"big_{size}")
        buildings = max(10, size // 4)
        mock.configure(listings_per_page=size, buildings=buildings, latency=[0.001, 0.005])
        r = run_one(make_input(0), unique_files=False, tag="big")
        st = mock.stats()
        scraped = json.load(open("merged_properties.json")) if os.path.exists("merged_properties.json") else []
        size_mb = os.path.getsize("merged_properties.json") / 1e6 if scraped else 0
        verdict = "PASS" if r["ok"] and not check_results(make_input(0), r["results"]) else f"FAIL {r.get('error', '')}"
        print(f"{size:>8} | {buildings:>9} | {len(scraped):>7} | {len(r.get('results', [])):>6} | {r['time']:>7.1f} | "
              f"{rss_mb():>7.0f} | {st.get('detail', 0):>11} | {st.get('image', 0):>10} | {size_mb:>7.1f} | {verdict}")
        if r["time"] > time_limit or not r["ok"]:
            print("         -> stopping")
            break


FAULTS = [
    # name, mock config, what a robust search should do
    ("slow pages (5 s each)", {"latency": [5, 5]}, "same results, slower"),
    ("search pages hang 25 s (> 20 s timeout)", {"search_fault": "hang", "hang_seconds": 25}, "empty or partial, fails fast"),
    ("detail pages hang 40 s", {"detail_fault": "hang", "hang_seconds": 40, "buildings": 8}, "drop unknown buildings, bounded time"),
    ("all search pages blocked (403 DataDome)", {"search_fault": "block_403"}, "report the block, not 'no listings'"),
    ("half the search pages return 500", {"search_fault": "error_500", "fault_rate": 0.5}, "partial results"),
    ("ONE PropertyGuru page has broken JSON", {"search_fault": "malformed_json", "fault_limit": 1,
                                               "fault_hosts": ["propertyguru.com.my"]}, "lose only that page"),
    ("ONE listing has price = null", {"search_fault": "null_price", "fault_limit": 1,
                                      "fault_hosts": ["propertyguru.com.my"]}, "lose only that listing"),
    ("ONE listing has listingData = null", {"search_fault": "null_listing", "fault_limit": 1,
                                            "fault_hosts": ["propertyguru.com.my"]}, "lose only that listing"),
    ("all detail pages have broken JSON", {"detail_fault": "malformed_json"}, "keep SPEEDHOME results"),
    ("SPEEDHOME returns no __NEXT_DATA__", {"search_fault": "no_next_data", "fault_hosts": ["speedhome.com"]},
     "keep the other platforms"),
]


def scenario_faults(mock: Mock, case_timeout=420):
    print("\n=== 3. FAULT INJECTION (one search each) ===")
    fresh_dir("fault_baseline")
    mock.configure()
    base = run_one(make_input(0), unique_files=False, tag="f")
    base_n = len(base["results"])
    base_by_platform = Counter(urlparse(p["property_url"]).netloc for p in base["results"])
    print(f"baseline without faults: {base_n} results {dict(base_by_platform)} in {base['time']:.1f}s")
    print(f"{'fault':<42} | {'time s':>7} | {'results':>7} | outcome")
    cases = list(FAULTS) + [("corrupted cache files", None, "ignore the cache and continue")]
    only = sys.argv[2] if len(sys.argv) > 2 else ""
    cases = [c for c in cases if only.lower() in c[0].lower()]
    for name, cfg, expected in cases:
        fresh_dir("fault")
        mock.configure(**(cfg or {}))
        if cfg is None:
            for f in ("coordinates_cache.json", "image_hash_cache.json"):
                with open(f, "w") as fh:
                    fh.write('{"building:1": [3.0, ')
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(run_one, make_input(0), False, "f")
        try:
            r = future.result(timeout=case_timeout)
        except FutureTimeout:
            r = {"ok": False, "error": f"TIMEOUT after {case_timeout}s", "time": case_timeout}
        pool.shutdown(wait=False)
        st = mock.stats()
        if r["ok"]:
            n = len(r["results"])
            by_platform = Counter(urlparse(p["property_url"]).netloc for p in r["results"])
            outcome = f"{n}/{base_n} results {dict(by_platform)}"
        else:
            outcome = f"EXCEPTION {r['error']}"
        print(f"{name:<42} | {r['time']:>7.1f} | {len(r.get('results', [])):>7} | {outcome}")
        print(f"{'':<42} |         |         | expected: {expected}; injected {sum(v for k, v in st.items() if k.startswith('fault:'))} faults")


FUZZ = [
    # name, SEARCH_INPUT changes, "reject" (should raise a clear error) or "accept"
    ("area empty string", {"area": ""}, "reject"),
    ("area only spaces", {"area": "   "}, "reject"),
    ("area 20,000 characters", {"area": "K" * 20000}, "reject"),
    ("area Arabic text", {"area": "كوالالمبور"}, "accept"),
    ("area with CRLF header injection", {"area": "KL\r\nX-Evil: 1"}, "accept"),
    ("area with URL parameter injection", {"area": "KL&maxPrice=1&page=999"}, "accept"),
    ("area is a number", {"area": 12345}, "reject"),
    ("max_price missing", {"max_price": None}, "reject"),
    ("max_price negative", {"max_price": -100}, "reject"),
    ("max_price is a string", {"max_price": "1500"}, "accept"),
    ("max_price NaN", {"max_price": float("nan")}, "reject"),
    ("max_price 10^12", {"max_price": 10 ** 12}, "accept"),
    ("min_price > max_price", {"min_price": 5000, "max_price": 1000}, "reject"),
    ("housing_type unknown 'penthouse'", {"housing_type": "penthouse"}, "reject"),
    ("housing_type empty list", {"housing_type": []}, "reject"),
    ("housing_type is a number", {"housing_type": 7}, "reject"),
    ("housing_type 50 duplicates", {"housing_type": ["studio"] * 50}, "accept"),
    ("location latitude = True", {"location": {"latitude": True, "longitude": 101.7}}, "reject"),
    ("location latitude as string '3.05'", {"location": {"latitude": "3.05", "longitude": "101.70"}}, "accept"),
    ("location radius NaN", {"location": {"latitude": 3.05, "longitude": 101.7, "radius_km": float("nan")}}, "reject"),
    ("location radius infinity", {"location": {"latitude": 3.05, "longitude": 101.7, "radius_km": float("inf")}}, "reject"),
    ("location radius 'abc'", {"location": {"latitude": 3.05, "longitude": 101.7, "radius_km": "abc"}}, "reject"),
    ("location radius 1 mm", {"location": {"latitude": 3.05, "longitude": 101.7, "radius_km": 1e-6}}, "accept"),
    ("location empty dict", {"location": {}}, "reject"),
    ("location as a list", {"location": [3.05, 101.7]}, "reject"),
    ("location lat/lng swapped", {"location": {"latitude": 101.7, "longitude": 3.05}}, "reject"),
    ("location old text style", {"location": "Bukit Jalil"}, "reject"),
    ("page = 0", {"page": 0}, "reject"),
    ("page = -5", {"page": -5}, "reject"),
    ("bedrooms 'two'", {"bedrooms": "two", "housing_type": "entire_unit"}, "reject"),
    ("property_structure 'castle'", {"property_structure": "castle"}, "reject"),
    ("floor_level 'BASEMENT'", {"floor_level": "BASEMENT"}, "reject"),
    ("is_verified_agent 'no' (string)", {"is_verified_agent": "no"}, "reject"),
    ("unknown key 'colour'", {"colour": "blue"}, "accept"),
    ("SEARCH_INPUT is a list", "LIST", "reject"),
    ("SEARCH_INPUT is None", "NONE", "reject"),
]


def scenario_fuzz(mock: Mock, case_timeout=60):
    print("\n=== 4. INPUT FUZZING ===")
    mock.configure(latency=[0.001, 0.01])
    print(f"{'input':<36} | {'expected':<8} | {'outcome':<46} | verdict")
    tally = Counter()
    for name, change, expected in FUZZ:
        fresh_dir("fuzz")
        if change == "LIST":
            inp = [make_input(0)]
        elif change == "NONE":
            inp = None
        else:
            inp = make_input(0)
            inp.update(change)
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(run_one, inp, False, "z")
        try:
            r = future.result(timeout=case_timeout)
        except FutureTimeout:
            r = {"ok": False, "error": "TIMEOUT", "time": case_timeout}
        pool.shutdown(wait=False)
        if r["ok"]:
            outcome = f"accepted, {len(r['results'])} results"
            verdict = "PASS" if expected == "accept" else "WEAK (bad input accepted silently)"
        elif r["error"].startswith("ValueError"):
            outcome = "rejected: " + r["error"].split(": ", 1)[1][:36]
            verdict = "PASS" if expected == "reject" else "WEAK (valid input rejected)"
        else:
            outcome = r["error"][:46]
            verdict = "BUG (crash)" if r["error"] != "TIMEOUT" else "BUG (hang)"
        tally[verdict.split(" ")[0]] += 1
        print(f"{name:<36} | {expected:<8} | {outcome:<46} | {verdict}")
    print(f"summary: {dict(tally)}")


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    sys.path.insert(0, PROJECT_DIR)
    # Several mock processes for the ramp, so the mock is not what breaks first
    mock = Mock(processes=8 if which in ("ramp", "all") else 1)
    install_redirect(mock.ports)
    print(f"mock sites on 127.0.0.1 ports {mock.ports} — no request leaves this machine")
    print(f"open files limit (ulimit -n): {resource.getrlimit(resource.RLIMIT_NOFILE)[0]}")
    try:
        if which in ("ramp", "all"):
            kwargs = {}
            if len(sys.argv) > 2 and sys.argv[2] in ("shared", "isolated"):
                kwargs["modes"] = (f"{sys.argv[2]}_files",)
            if len(sys.argv) > 3:
                kwargs["levels"] = tuple(int(x) for x in sys.argv[3].split(","))
            scenario_ramp(mock, **kwargs)
        if which in ("bigdata", "all"):
            scenario_bigdata(mock)
        if which in ("faults", "all"):
            scenario_faults(mock)
        if which in ("fuzz", "all"):
            scenario_fuzz(mock)
    except BaseException:
        print("\n!!! the stress test itself crashed:")
        traceback.print_exc(file=sys.stdout)
    finally:
        mock.stop()
        sys.stdout.flush()
        sys.stderr.flush()
        # Searches stuck in hanging requests would otherwise keep the process alive
        os._exit(0)


if __name__ == "__main__":
    main()
