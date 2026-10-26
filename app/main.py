import json, os, re, secrets, shutil, sys, threading, zipfile
from pathlib import Path
from typing import List, Literal, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image
from . import georef, imageops, exports, basemaps

def documents_dir():
    if sys.platform == "win32":      # gère le dossier Documents redirigé (OneDrive)
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf)
            if buf.value: return Path(buf.value)
        except Exception: pass
    return Path.home() / "Documents"

DATA = documents_dir() / "KalipheTiles"; DATA.mkdir(parents=True, exist_ok=True)
CACHE = DATA / "_cache"
SETTINGS = Path.home() / ".kaliphetiles.json"
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
STATIC = BASE / "static"

app = FastAPI()
app.mount("/data", StaticFiles(directory=DATA), name="data")
if (STATIC / "vendor").exists():
    app.mount("/vendor", StaticFiles(directory=STATIC / "vendor"), name="vendor")

Method = Literal["affine", "poly2", "tps"]
class GCP(BaseModel):
    px: float; py: float; lon: float; lat: float
class LayerReq(BaseModel):
    id: str; name: str = ""; gcps: List[GCP] = []; method: Method = "affine"; vis: bool = True
class ProcessReq(BaseModel):
    layers: List[LayerReq]
    zmin: Optional[int] = None
    zmax: Optional[int] = None
    hd: bool = False
class PreviewReq(BaseModel):
    gcps: List[GCP]; method: Method = "affine"
class EditReq(BaseModel):
    op: Literal["crop", "rotate", "remove", "reset", "harmonize"]
    box: Optional[List[float]] = None
    angle: float = 0
    colors: List[List[int]] = []
    tol: int = 30
    gcps: List[GCP] = []
class ExportReq(BaseModel):
    layers: List[LayerReq]; kinds: List[str]
    crs: Literal["3857", "4326"] = "3857"
    max_px: int = 0
class UploadReq(BaseModel):
    s3: dict; what: List[str] = ["tiles", "mbtiles", "exports"]
class OpenTopoReq(BaseModel):
    api_key: str
    west: float; south: float; east: float; north: float
    demtype: Literal["SRTMGL1", "SRTMGL3", "COP30", "COP90", "NASADEM", "AW3D30"] = "SRTMGL1"
class SaveReq(BaseModel):
    layers: List[LayerReq]; zmin: int = 0; zmax: int = 18; crs: str = "3857"; hd: bool = False

def mdir(mid):
    if not mid.isalnum(): raise HTTPException(400, "id invalide")
    d = DATA / mid
    if not d.exists(): raise HTTPException(404, "introuvable")
    return d
_lock = threading.RLock()
def meta(d):
    with _lock: return json.loads((d / "meta.json").read_text())
def save(d, **kw):
    with _lock:
        m = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        m.update(kw); tmp = d / "meta.tmp"; tmp.write_text(json.dumps(m)); os.replace(tmp, d / "meta.json")
def safe(n): return "".join(c if c.isalnum() or c in "-_" else "_" for c in n)[:60] or "carte"
def new_id(): return secrets.token_urlsafe(8).replace("-", "a").replace("_", "b")
def img_info(d, mid):
    p = imageops.src_path(d); w, h = Image.open(p).size
    save(d, width=w, height=h)
    return {"id": mid, "width": w, "height": h, "image": f"/data/{mid}/{p.name}", "name": meta(d).get("name", "")}

def check_layer(l):
    need = georef.MIN_PTS[l.method]
    if len(l.gcps) < need: raise HTTPException(400, f"« {l.name or l.id} » : {need} points minimum pour la méthode {l.method}")

def build_layers(reqs):
    if not reqs: raise HTTPException(400, "aucune image à traiter")
    out = []
    for l in reqs:
        check_layer(l); d = mdir(l.id)
        try: out.append(georef.Layer(imageops.src_path(d), [g.model_dump() for g in l.gcps], l.method, l.name or meta(d).get("name", "")))
        except Exception as e: raise HTTPException(400, f"« {l.name or l.id} » : calcul impossible ({e})")
    return out

@app.post("/api/opentopography")
def opentopography(req: OpenTopoReq):
    """Télécharge un MNT GeoTIFF depuis l'API Global DEM d'OpenTopography."""
    import urllib.parse, urllib.request
    if not req.api_key.strip():
        raise HTTPException(400, "Clé API OpenTopography requise")
    if not (-180 <= req.west < req.east <= 180 and -90 <= req.south < req.north <= 90):
        raise HTTPException(400, "Emprise invalide : Ouest < Est et Sud < Nord sont requis")
    params = urllib.parse.urlencode({
        "demtype": req.demtype, "south": req.south, "north": req.north,
        "west": req.west, "east": req.east, "outputFormat": "GTiff",
        "API_Key": req.api_key.strip()
    })
    url = "https://api.opentopography.org/API/globaldem?" + params
    request = urllib.request.Request(url, headers={"User-Agent": "KalipheTiles/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
            if response.status != 200 or len(data) < 8:
                raise ValueError("réponse vide ou invalide")
            if data[:4] not in (b"II*\x00", b"MM\x00*"):
                raise ValueError(data[:1200].decode("utf-8", errors="replace"))
            return Response(content=data, media_type="image/tiff",
                            headers={"Content-Disposition": 'attachment; filename="opentopography_dem.tif"'})
    except Exception as e:
        raise HTTPException(502, f"OpenTopography : {e}")

@app.post("/api/maps")
async def upload(file: UploadFile = File(...)):
    mid = new_id(); d = DATA / mid; d.mkdir()
    raw = d / "raw"; raw.write_bytes(await file.read())
    try:
        with Image.open(raw) as im:
            fmt, mode = im.format, im.mode
            if fmt == "JPEG" and mode == "RGB":           # original conservé tel quel (aucune recompression)
                raw.replace(d / "orig.jpg"); shutil.copy(d / "orig.jpg", d / "source.jpg")
            else:
                im2 = im.convert("RGBA" if "A" in mode or mode == "P" and "transparency" in im.info else "RGB")
                im2.save(d / "orig.png"); shutil.copy(d / "orig.png", d / "source.png")
        raw.unlink(missing_ok=True)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True); raise HTTPException(400, f"image invalide : {e}")
    save(d, status="uploaded", name=safe(Path(file.filename).stem))
    return img_info(d, mid)

@app.post("/api/maps/{mid}/edit")
def edit(mid: str, req: EditReq):
    d = mdir(mid); g = [x.model_dump() for x in req.gcps]
    try:
        if req.op == "reset":
            imageops.reset(d); g = []
        else:
            if req.op == "crop": im, g = imageops.crop(imageops.load(d), req.box or [0, 0, 0, 0], g)
            elif req.op == "rotate": im, g = imageops.rotate(imageops.load(d), req.angle, g)
            elif req.op == "harmonize":
                im = imageops.harmonize(imageops.load(d))
            else:
                if not req.colors: raise ValueError("aucune couleur choisie")
                im = imageops.remove_colors(imageops.load(d), req.colors, req.tol)
            imageops.save_cur(d, im)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**img_info(d, mid), "gcps": g}

@app.post("/api/maps/{mid}/preview")
def preview(mid: str, req: PreviewReq):
    """Aperçu rapide (image réduite) pour afficher l'image sur la carte dès que les points sont posés."""
    d = mdir(mid)
    if len(req.gcps) < georef.MIN_PTS[req.method]: raise HTTPException(400, "pas assez de points")
    p, f = imageops.prev_small(d)
    g = [{**x.model_dump(), "px": x.px * f, "py": x.py * f} for x in req.gcps]
    try:
        L = georef.Layer(p, g, req.method)
        arr, x0, y0, res = georef.render_raster([L], "3857", 1100)
    except Exception as e: raise HTTPException(400, f"aperçu impossible : {e}")
    Image.fromarray(arr, "RGBA").save(d / "preview.png", compress_level=1)
    H, W = arr.shape[:2]; m = georef.m2ll
    return {"url": f"/data/{mid}/preview.png",
            "coordinates": [list(m(x0, y0)), list(m(x0 + W * res, y0)), list(m(x0 + W * res, y0 - H * res)), list(m(x0, y0 - H * res))]}

@app.post("/api/maps/{mid}/estimate")
def estimate(mid: str, req: ProcessReq):
    mdir(mid); specs = []
    for l in req.layers:
        check_layer(l); m = meta(mdir(l.id)); specs.append((m["width"], m["height"], [g.model_dump() for g in l.gcps], l.method))
    try: return georef.estimate(specs, req.zmin, req.zmax, 512 if req.hd else 256)
    except Exception as e: raise HTTPException(400, f"calcul impossible : {e}")

def pipeline(d, req):
    try:
        save(d, status="processing", progress=0)
        layers = build_layers(req.layers); ts = 512 if req.hd else 256
        save(d, errors=[e for l in layers for e in georef.rmse(l.gcps, l.method)])
        shutil.rmtree(d / "tiles", ignore_errors=True)
        b, zmin, zmax = georef.make_tiles(layers, d / "tiles", lambda f: save(d, progress=round(f, 3)), req.zmin, req.zmax, ts)
        name = meta(d)["name"]
        georef.to_mbtiles(d / "tiles", d / "carte.mbtiles", b, zmin, zmax, name, ts)
        exports.write_frame(d / "exports", name, layers, zmin, zmax)               # cadre GeoJSON automatique
        save(d, status="done", bounds=b, zmin=zmin, zmax=zmax, ts=ts, frame=f"/data/{d.name}/exports/{name}_footprint.geojson")
    except HTTPException as e:
        save(d, status="error", error=str(e.detail))
    except Exception as e:
        save(d, status="error", error=str(e))

@app.post("/api/maps/{mid}/process")
def process(mid: str, req: ProcessReq):
    d = mdir(mid)
    for l in req.layers: check_layer(l)
    save(d, status="queued")
    threading.Thread(target=pipeline, args=(d, req), daemon=True).start()
    return {"status": "queued"}

@app.get("/api/maps/{mid}")
def status(mid: str): return meta(mdir(mid))

@app.post("/api/maps/{mid}/export")
def export(mid: str, req: ExportReq):
    d = mdir(mid); m = meta(d)
    kinds = [k for k in req.kinds if k in exports.KINDS]
    if not kinds: raise HTTPException(400, "aucun format choisi")
    layers = build_layers(req.layers)
    try: files = exports.export_all(d, m["name"], layers, kinds, req.crs, req.max_px, m)
    except Exception as e: raise HTTPException(400, str(e))
    return {"files": files, "folder": str(d / "exports")}

def do_upload(d, mid, req):
    try:
        save(d, upload={"status": "running", "progress": 0})
        r = exports.s3_upload(d, req.s3, req.what, mid, meta(d), lambda f: save(d, upload={"status": "running", "progress": round(f, 3)}))
        save(d, upload={"status": "done", **r})
    except Exception as e:
        save(d, upload={"status": "error", "error": str(e)})

@app.post("/api/maps/{mid}/upload")
def upload_cloud(mid: str, req: UploadReq):
    d = mdir(mid)
    for k in ("bucket", "access_key", "secret_key"):
        if not req.s3.get(k): raise HTTPException(400, f"champ manquant : {k}")
    threading.Thread(target=do_upload, args=(d, mid, req), daemon=True).start()
    return {"status": "running"}

IMGS = ("orig.jpg", "orig.png", "source.jpg", "source.png")
@app.post("/api/save_project")
def save_project(req: SaveReq):
    first = mdir(req.layers[0].id); out = DATA / f"{meta(first)['name']}.ktproj"
    proj = {"version": 2, **req.model_dump(exclude={"layers"}), "layers": [l.model_dump() for l in req.layers]}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("project.json", json.dumps(proj))
        for i, l in enumerate(req.layers):
            d = mdir(l.id)
            for f in IMGS:
                if (d / f).exists(): z.write(d / f, f"L{i}/{f}")
    return {"path": str(out)}

@app.post("/api/projects/open")
async def open_project(file: UploadFile = File(...)):
    made = []
    try:
        with zipfile.ZipFile(file.file) as z:
            proj = json.loads(z.read("project.json")); out = []
            for i, l in enumerate(proj.get("layers", [])):
                mid = new_id(); d = DATA / mid; d.mkdir(); made.append(d)
                for f in IMGS:
                    if f"L{i}/{f}" in z.namelist(): (d / f).write_bytes(z.read(f"L{i}/{f}"))
                save(d, status="uploaded", name=safe(l.get("name") or f"image{i + 1}"))
                out.append({**img_info(d, mid), "name": l.get("name") or meta(d)["name"], "gcps": l.get("gcps", []),
                            "method": l.get("method", "affine"), "vis": l.get("vis", True)})
        proj.pop("layers", None)
        return {"layers": out, "project": proj}
    except Exception as e:
        for d in made: shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"projet invalide : {e}")

@app.post("/api/maps/{mid}/open")
def open_folder(mid: str):
    d = mdir(mid)
    if sys.platform == "win32": os.startfile(d)
    return {"ok": True}

@app.post("/api/open_root")
def open_root():
    if sys.platform == "win32": os.startfile(DATA)
    return {"ok": True}

def load_settings():
    return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}

@app.get("/api/settings")
def get_settings(): return load_settings()

@app.post("/api/settings")
def set_settings(s: dict):
    keep = {"mapbox": str(s.get("mapbox", "")), "google": str(s.get("google", "")),
            "s3": {k: str(v) for k, v in (s.get("s3") or {}).items()}}
    SETTINGS.write_text(json.dumps(keep)); return {"ok": True}

@app.get("/tiles/{prov}/{z}/{x}/{y}")
def tiles(prov: str, z: int, x: int, y: int):
    if not (0 <= z <= 24 and 0 <= x < 2 ** z and 0 <= y < 2 ** z): raise HTTPException(404)
    try:
        ct, body = basemaps.get_tile(prov, z, x, y, load_settings(), CACHE)
    except basemaps.TileError as e:
        return JSONResponse({"detail": e.msg}, status_code=e.status if e.status in (401, 403, 404) else 502)
    return Response(body, media_type=ct, headers={"Cache-Control": "max-age=86400"})

@app.get("/fonts/{stack}/{rng}.pbf")
def fonts(stack: str, rng: str):
    if not re.fullmatch(r"[A-Za-z0-9 ,_-]+", stack) or not re.fullmatch(r"\d+-\d+", rng): raise HTTPException(404)
    try: return Response(basemaps.get_font(stack, rng, CACHE), media_type="application/x-protobuf")
    except basemaps.TileError: raise HTTPException(404)

@app.get("/api/diag")
def diag():
    return {"data_dir": str(DATA), "frozen": bool(getattr(sys, "frozen", False)), "tests": basemaps.diag(load_settings(), CACHE)}

@app.get("/", response_class=HTMLResponse)
def home(): return (STATIC / "index.html").read_text(encoding="utf-8")
