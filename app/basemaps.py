"""Fonds de carte servis par le programme lui-même (évite CORS / Referer / blocages de la fenêtre)."""
import json, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time

UA = "KalipheTiles/1.0 (georeferencing desktop app; https://github.com/KalipheGTU/georef)"
ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
IGN = ("https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER={}&STYLE=normal"
       "&TILEMATRIXSET=PM&TILEMATRIX={{z}}&TILEROW={{y}}&TILECOL={{x}}&FORMAT={}")
FREE = {
    "osm": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    "topo": "https://a.tile.opentopomap.org/{z}/{x}/{y}.png",
    "esri": ESRI.format("World_Imagery"), "esrit": ESRI.format("World_Topo_Map"), "esrir": ESRI.format("World_Street_Map"),
    "carto": "https://a.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    "ignp": IGN.format("GEOGRAPHICALGRIDSYSTEMS.PLANIGNV2", "image/png"),
    "igns": IGN.format("ORTHOIMAGERY.ORTHOPHOTOS", "image/jpeg"),
}
MAPBOX = {"mbs": "satellite-streets-v12", "mbr": "streets-v12", "mbo": "outdoors-v12"}
GOOGLE = {"gr": "roadmap", "gs": "satellite"}
_sessions = {}

class TileError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg); self.status, self.msg = status, msg

def fetch_url(url, headers=None, data=None, method="GET", timeout=15):
    h = {"User-Agent": UA, "Accept": "image/*,*/*"}; h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get_content_type(), r.read()
    except urllib.error.HTTPError as e:
        raise TileError(e.code, f"le serveur a répondu HTTP {e.code}")
    except Exception as e:
        raise TileError(0, f"connexion impossible ({type(e).__name__}: {e})")

def sniff(body):
    return "image/jpeg" if body[:3] == b"\xff\xd8\xff" else "image/webp" if body[8:12] == b"WEBP" else "image/png"

def google_session(key, kind):
    if (key, kind) not in _sessions:
        body = json.dumps({"mapType": kind, "language": "fr-FR", "region": "FR"}).encode()
        _, _, raw = fetch_url(f"https://tile.googleapis.com/v1/createSession?key={key}", {"Content-Type": "application/json"}, body, "POST")
        s = json.loads(raw).get("session")
        if not s: raise TileError(403, "Google : session refusée (clé, API ou facturation)")
        _sessions[(key, kind)] = s
    return _sessions[(key, kind)]

def url_for(prov, z, x, y, st):
    if prov in FREE: return FREE[prov].format(z=z, x=x, y=y)
    if prov in MAPBOX:
        if not st.get("mapbox"): raise TileError(401, "token Mapbox manquant")
        return f"https://api.mapbox.com/styles/v1/mapbox/{MAPBOX[prov]}/tiles/256/{z}/{x}/{y}@2x?access_token={st['mapbox']}"
    if prov in GOOGLE:
        if not st.get("google"): raise TileError(401, "clé Google manquante")
        s = google_session(st["google"], GOOGLE[prov])
        return f"https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session={s}&key={st['google']}"
    raise TileError(404, "fond inconnu")

def get_tile(prov, z, x, y, st, cache_dir):
    cache = Path(cache_dir) / prov / str(z) / str(x) / f"{y}.tile" if prov in FREE else None   # pas de cache Google/Mapbox (conditions d'utilisation)
    if cache and cache.exists():
        b = cache.read_bytes(); return sniff(b), b
    try:
        _, _, body = fetch_url(url_for(prov, z, x, y, st))
    except TileError as e:
        if prov in GOOGLE and e.status in (400, 403): _sessions.clear()
        raise
    if not body: raise TileError(404, "tuile vide")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True); cache.write_bytes(body)
    return sniff(body), body

def diag(st, cache_dir):
    provs = list(FREE) + [p for p in MAPBOX if st.get("mapbox")] + [p for p in GOOGLE if st.get("google")]
    def one(p):
        t = time.time()
        try:
            get_tile(p, 0, 0, 0, st, Path(cache_dir) / "_diag"); return {"prov": p, "ok": True, "ms": int((time.time() - t) * 1000)}
        except TileError as e: return {"prov": p, "ok": False, "error": e.msg}
        except Exception as e: return {"prov": p, "ok": False, "error": str(e)}
    with ThreadPoolExecutor(6) as ex: return list(ex.map(one, provs))

def get_font(stack, rng, cache_dir):
    f = Path(cache_dir) / "fonts" / stack.replace(",", "_") / f"{rng}.pbf"
    if f.exists(): return f.read_bytes()
    _, _, body = fetch_url(f"https://demotiles.maplibre.org/font/{urllib.request.quote(stack)}/{rng}.pbf")
    f.parent.mkdir(parents=True, exist_ok=True); f.write_bytes(body); return body 
