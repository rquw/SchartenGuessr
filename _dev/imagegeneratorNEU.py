import os, sys, math, json, time, signal, threading, requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── CONFIG ────────────────────────────────────────────────────
API_KEY        = "AIzaSyB4R8mljt1tGtMqq2MihbNjN3dgwO9pGMQ"

BASE_DIR       = os.path.expanduser("~/Desktop/PROJEKT_GUESSR/SchartenGuessr")
OUTPUT_DIR     = os.path.join(BASE_DIR, "images")
LOG_FILE       = os.path.join(BASE_DIR, "log.json")
DATA_JS_FILE   = os.path.join(BASE_DIR, "data.js")
BOUNDARY_FILE  = os.path.join(BASE_DIR, "boundary.geojson")

BOUNDARY_QUERY = "Scharten, Oberösterreich, Austria"

GRID_SPACING_M  = 30       # metres between grid probe points
METADATA_RADIUS = 15       # snapping radius — must be <= GRID_SPACING_M / 2
                            # keeps each pano assigned to exactly one probe

HEADINGS        = [0, 90, 180, 270]
IMAGE_SIZE      = "640x640"
FOV             = 90
PITCH           = 0

META_WORKERS    = 32
IMG_WORKERS     = 4
MAX_RETRIES     = 3
MIN_IMAGE_BYTES = 8_000

FRESH_START     = False     # wipe everything and start over
# ─────────────────────────────────────────────────────────────

os.makedirs(OUTPUT_DIR, exist_ok=True)

lock           = threading.Lock()
quota_exceeded = threading.Event()

session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=META_WORKERS + IMG_WORKERS,
    pool_maxsize=META_WORKERS + IMG_WORKERS,
)
session.mount("http://",  _adapter)
session.mount("https://", _adapter)


def hr():  print("─" * 60, flush=True)
def ts():  return datetime.now().strftime("%H:%M:%S")
def eta_str(done, total, elapsed):
    if done == 0: return "?"
    return str(timedelta(seconds=int((total - done) / (done / elapsed))))


# ── boundary ──────────────────────────────────────────────────
BOUNDARY = []
LAT_MIN = LAT_MAX = LON_MIN = LON_MAX = 0

def _in_ring(lat, lon, ring):
    inside, n, j = False, len(ring), len(ring) - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

def in_area(lat, lon):
    for poly in BOUNDARY:
        if _in_ring(lat, lon, poly[0]) and not any(_in_ring(lat, lon, h) for h in poly[1:]):
            return True
    return False

def load_boundary():
    global LAT_MIN, LAT_MAX, LON_MIN, LON_MAX
    if os.path.exists(BOUNDARY_FILE):
        with open(BOUNDARY_FILE, "r", encoding="utf-8") as f:
            gj = json.load(f)
        print(f"[{ts()}]  Boundary loaded from cache", flush=True)
    else:
        print(f"[{ts()}]  Fetching boundary from OpenStreetMap…", flush=True)
        r = session.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": BOUNDARY_QUERY, "format": "jsonv2", "polygon_geojson": 1, "limit": 10},
            headers={"User-Agent": "WelsGuessr/1.0"},
            timeout=30,
        )
        results = r.json()
        def score(el):
            if el.get("geojson", {}).get("type") not in ("Polygon", "MultiPolygon"): return -1
            s = 1
            if el.get("category") == "boundary": s += 2
            if el.get("addresstype") in ("city", "municipality", "town"): s += 2
            return s
        best = max(results, key=score, default=None)
        if not best or score(best) < 0:
            raise RuntimeError(f"No boundary polygon found for '{BOUNDARY_QUERY}'")
        gj = {"type": "Feature", "properties": {}, "geometry": best["geojson"]}
        with open(BOUNDARY_FILE, "w", encoding="utf-8") as f:
            json.dump(gj, f)
        print(f"[{ts()}]  Boundary cached → {BOUNDARY_FILE}", flush=True)

    geom = gj["geometry"] if gj.get("type") == "Feature" else gj
    if geom["type"] == "Polygon":
        polys = [geom["coordinates"]]
    else:
        polys = list(geom["coordinates"])

    BOUNDARY.extend(polys)
    lons = [pt[0] for p in polys for ring in p for pt in ring]
    lats = [pt[1] for p in polys for ring in p for pt in ring]
    LAT_MIN, LAT_MAX = min(lats), max(lats)
    LON_MIN, LON_MAX = min(lons), max(lons)
    print(f"[{ts()}]  Area: {len(polys)} polygon(s), "
          f"lat {LAT_MIN:.5f}..{LAT_MAX:.5f}  lon {LON_MIN:.5f}..{LON_MAX:.5f}", flush=True)


# ── http ───────────────────────────────────────────────────────
def get(url, params, timeout=15):
    delay = 0.5
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(min(delay * (2 ** attempt), 30)); continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(delay); delay = min(delay * 2, 15); continue
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            time.sleep(delay); delay = min(delay * 2, 15)
    return None


# ── grid scan ─────────────────────────────────────────────────
def build_grid():
    lat_step = GRID_SPACING_M / 111320.0
    lon_step = GRID_SPACING_M / (111320.0 * math.cos(math.radians((LAT_MIN + LAT_MAX) / 2)))
    pts, lat = [], LAT_MIN
    while lat <= LAT_MAX:
        lon = LON_MIN
        while lon <= LON_MAX:
            if in_area(lat, lon):
                pts.append((round(lat, 7), round(lon, 7)))
            lon += lon_step
        lat += lat_step
    return pts

def probe(lat, lon):
    r = get(
        "https://maps.googleapis.com/maps/api/streetview/metadata",
        {"location": f"{lat},{lon}", "radius": METADATA_RADIUS, "source": "outdoor", "key": API_KEY},
    )
    if r is None: return None
    try: data = r.json()
    except Exception: return None
    if data.get("status") != "OK": return None
    loc = data.get("location", {})
    return {"pano_id": data["pano_id"], "lat": loc.get("lat", lat), "lon": loc.get("lng", lon)}

def scan(known_ids):
    points = build_grid()
    total, done, found = len(points), 0, []
    seen = set(known_ids)
    start = time.time()
    hr()
    print(f"[{ts()}]  Scanning {total:,} grid points inside Wels boundary  (workers: {META_WORKERS})", flush=True)
    hr()
    with ThreadPoolExecutor(max_workers=META_WORKERS) as ex:
        futures = {ex.submit(probe, la, lo): (la, lo) for la, lo in points}
        for fut in as_completed(futures):
            done += 1
            e = None
            try: e = fut.result()
            except Exception: pass
            if e and e["pano_id"] not in seen and in_area(e["lat"], e["lon"]):
                seen.add(e["pano_id"])
                found.append(e)
            if done % 500 == 0 or done == total:
                rate = done / max(time.time() - start, 0.01)
                print(f"\r  {done/total*100:5.1f}%  [{done:,}/{total:,}]  "
                      f"found={len(found):,}  {rate:.0f} pts/s  "
                      f"ETA {eta_str(done, total, time.time() - start)}   ",
                      end="", flush=True)
    print(flush=True)
    print(f"[{ts()}]  Found {len(found):,} new panoramas", flush=True)
    return found


# ── download ──────────────────────────────────────────────────
def download_image(pano_id, heading):
    if quota_exceeded.is_set(): return "quota"
    fpath = os.path.join(OUTPUT_DIR, f"{pano_id}_h{heading:03d}.jpg")
    if os.path.exists(fpath) and os.path.getsize(fpath) >= MIN_IMAGE_BYTES: return "skip"
    r = get("https://maps.googleapis.com/maps/api/streetview",
            {"size": IMAGE_SIZE, "pano": pano_id, "heading": heading,
             "fov": FOV, "pitch": PITCH, "key": API_KEY}, timeout=20)
    if r is None: return "fail"
    if r.status_code == 403: quota_exceeded.set(); return "quota"
    if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
        if len(r.content) < MIN_IMAGE_BYTES: return "grey"
        with open(fpath, "wb") as f: f.write(r.content)
        return "ok"
    return "fail"

def download_pano(entry):
    res = {"ok": 0, "skip": 0, "grey": 0, "fail": 0, "quota": 0}
    for h in HEADINGS:
        r = download_image(entry["pano_id"], h)
        res[r] += 1
        if r == "quota": break
    return res

def run_downloads(entries):
    total = len(entries)
    done = ok = skip = grey = fail = 0
    start = time.time()
    hr()
    print(f"[{ts()}]  Downloading images for {total:,} panoramas  (workers: {IMG_WORKERS})", flush=True)
    hr()
    with ThreadPoolExecutor(max_workers=IMG_WORKERS) as ex:
        futures = [ex.submit(download_pano, e) for e in entries]
        for fut in as_completed(futures):
            done += 1
            try: res = fut.result()
            except Exception: res = {"ok": 0, "skip": 0, "grey": 0, "fail": len(HEADINGS), "quota": 0}
            ok += res["ok"]; skip += res["skip"]; grey += res["grey"]; fail += res["fail"]
            if done % 10 == 0 or done == total:
                rate = done / max(time.time() - start, 0.01)
                tag = "  ⚠ QUOTA" if quota_exceeded.is_set() else ""
                print(f"\r  {done/total*100:5.1f}%  [{done:,}/{total:,}]  "
                      f"new={ok:,}  have={skip:,}  grey={grey:,}  fail={fail:,}  "
                      f"{rate:.1f}/s  ETA {eta_str(done, total, time.time() - start)}{tag}   ",
                      end="", flush=True)
    print(flush=True)
    return ok, skip, grey, fail


# ── persistence ───────────────────────────────────────────────
def load_log():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f: return json.load(f)
    return []

def save_log(entries):
    with open(LOG_FILE, "w", encoding="utf-8") as f: json.dump(entries, f, indent=2)

def write_data_js(entries):
    # nur Panos mit ALLEN 4 vollständigen Bildern aufnehmen, sonst 404s im Spiel
    def complete(pid):
        for h in HEADINGS:
            p = os.path.join(OUTPUT_DIR, f"{pid}_h{h:03d}.jpg")
            if not (os.path.exists(p) and os.path.getsize(p) >= MIN_IMAGE_BYTES):
                return False
        return True
    locs = [{"id": e["pano_id"], "lat": e["lat"], "lng": e["lon"]}
            for e in entries if complete(e["pano_id"])]
    with open(DATA_JS_FILE, "w", encoding="utf-8") as f:
        f.write("const LOCATIONS = " + json.dumps(locs, separators=(",", ":")) + ";\n")
    print(f"[{ts()}]  data.js: {len(locs):,} Standorte mit vollständigen Bildern", flush=True)

def wipe():
    n = 0
    for fname in os.listdir(OUTPUT_DIR):
        if fname.endswith(".jpg"): os.remove(os.path.join(OUTPUT_DIR, fname)); n += 1
    for f in (LOG_FILE, DATA_JS_FILE):
        if os.path.exists(f): os.remove(f)
    return n


# ── ctrl-c ────────────────────────────────────────────────────
def _exit(sig, frame):
    print(f"\n[{ts()}]  Stopped — progress saved. Re-run to resume.", flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT,  _exit)
signal.signal(signal.SIGTERM, _exit)


# ── MAIN ──────────────────────────────────────────────────────
if __name__ == "__main__":
    hr()
    print("  SchartenGuessr Image Generator", flush=True)
    load_boundary()
    hr()

    if FRESH_START:
        n = wipe()
        print(f"[{ts()}]  Fresh start — wiped {n:,} images + log", flush=True)

    log = load_log()
    by_id = {e["pano_id"]: e for e in log}

    new = scan(by_id.keys())
    for e in new: by_id[e["pano_id"]] = e
    if new:
        save_log(list(by_id.values()))

    all_panos = list(by_id.values())
    print(f"[{ts()}]  Total panoramas in log: {len(all_panos):,}", flush=True)

    if not all_panos:
        print(f"[{ts()}]  Nothing to download.", flush=True)
        sys.exit(0)

    ok, skip, grey, fail = run_downloads(all_panos)
    write_data_js(all_panos)

    hr()
    print(f"[{ts()}]  {'⚠ QUOTA EXCEEDED' if quota_exceeded.is_set() else 'FERTIG'}", flush=True)
    print(f"          Panoramas  : {len(all_panos):,}", flush=True)
    print(f"          New images : {ok:,}", flush=True)
    print(f"          Skipped    : {skip:,}", flush=True)
    print(f"          Grey       : {grey:,}", flush=True)
    print(f"          Failed     : {fail:,}", flush=True)
    print(f"          data.js    : {DATA_JS_FILE}", flush=True)
    hr()