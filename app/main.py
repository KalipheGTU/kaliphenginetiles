import json, os, secrets, shutil, sys, threading, zipfile
from pathlib import Path
from typing import List, Literal, Optional
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from PIL import Image
from . import georef, imageops, exports

DATA = Path.home() / "Documents" / "KalipheTiles"; DATA.mkdir(parents=True, exist_ok=True)
SETTINGS = Path.home() / ".kaliphetiles.json"
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
STATIC = BASE / "static"

app = FastAPI()
app.mount("/data", StaticFiles(directory=DATA), name="data")

class GCP(BaseModel):
    px: float; py: float; lon: float; lat: float
class ProcessReq(BaseModel):
    gcps: List[GCP]
    method: Literal["affine", "poly2", "tps"] = "affine"
    zmin: Optional[int] = None
    zmax: Optional[int] = None
class EditReq(BaseModel):
    op: Literal["crop", "rotate", "remove", "reset"]
    box: Optional[List[float]] = None
    angle: float = 0
    colors: List[List[int]] = []
    tol: int = 30
    gcps: List[GCP] = []
class ExportReq(BaseModel):
    gcps: List[GCP]
    method: Literal["affine", "poly2", "tps"] = "affine"
    kinds: List[str]
    crs: Literal["3857", "4326"] = "3857"
    max_px: int = 8000
class UploadReq(BaseModel):
    s3: dict
    what: List[str] = ["tiles", "mbtiles", "exports"]
class SaveReq(BaseModel):
    gcps: List[GCP] = []
    method: str = "affine"
    zmin: int = 0
    zmax: int = 18
    crs: str = "3857"

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
    return {"id": mid, "width": w, "height": h, "image": f"/data/{mid}/{p.name}"}

@app.post("/api/maps")
async def upload(file: UploadFile = File(...)):
    mid = new_id(); d = DATA / mid; d.mkdir()
    raw = d / "raw"; raw.write_bytes(await file.read())
    try:
        with Image.open(raw) as im: im.convert("RGB").save(d / "orig.jpg", quality=95)
    except Exception as e:
        shutil.rmtree(d); raise HTTPException(400, f"image invalide : {e}")
    raw.unlink(); shutil.copy(d / "orig.jpg", d / "source.jpg")
    save(d, status="uploaded", name=safe(Path(file.filename).stem))
    return img_info(d, mid)

@app.post("/api/maps/{mid}/edit")
def edit(mid: str, req: EditReq):
    d = mdir(mid); g = [x.model_dump() for x in req.gcps]
    try:
        if req.op == "reset":
            im, g = Image.open(d / "orig.jpg"), []
        elif req.op == "crop":
            im, g = imageops.crop(imageops.load(d), req.box or [0, 0, 0, 0], g)
        elif req.op == "rotate":
            im, g = imageops.rotate(imageops.load(d), req.angle, g)
        else:
            if not req.colors: raise ValueError("aucune couleur choisie")
            im = imageops.remove_colors(imageops.load(d), req.colors, req.tol)
        imageops.save_cur(d, im)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {**img_info(d, mid), "gcps": g}

def check(req_gcps, method):
    need = georef.MIN_PTS[method]
    if len(req_gcps) < need: raise HTTPException(400, f"{need} points minimum pour la méthode {method}")

@app.post("/api/maps/{mid}/estimate")
def estimate(mid: str, req: ProcessReq):
    d = mdir(mid); check(req.gcps, req.method); m = meta(d)
    try: return georef.estimate(m["width"], m["height"], [x.model_dump() for x in req.gcps], req.method, req.zmin, req.zmax)
    except Exception as e: raise HTTPException(400, f"calcul impossible : {e}")

def pipeline(d, req):
    try:
        g = [x.model_dump() for x in req.gcps]
        save(d, status="processing", progress=0, errors=georef.rmse(g, req.method))
        shutil.rmtree(d / "tiles", ignore_errors=True)
        b, zmin, zmax = georef.make_tiles(imageops.src_path(d), g, req.method, d / "tiles",
                                          lambda f: save(d, progress=round(f, 3)), req.zmin, req.zmax)
        georef.to_mbtiles(d / "tiles", d / "carte.mbtiles", b, zmin, zmax, meta(d)["name"])
        save(d, status="done", bounds=b, zmin=zmin, zmax=zmax)
    except Exception as e:
        save(d, status="error", error=str(e))

@app.post("/api/maps/{mid}/process")
def process(mid: str, req: ProcessReq):
    d = mdir(mid); check(req.gcps, req.method); save(d, status="queued")
    threading.Thread(target=pipeline, args=(d, req), daemon=True).start()
    return {"status": "queued"}

@app.get("/api/maps/{mid}")
def status(mid: str): return meta(mdir(mid))

@app.post("/api/maps/{mid}/export")
def export(mid: str, req: ExportReq):
    d = mdir(mid); check(req.gcps, req.method); m = meta(d)
    kinds = [k for k in req.kinds if k in exports.KINDS]
    if not kinds: raise HTTPException(400, "aucun format choisi")
    try:
        files = exports.export_all(d, m["name"], imageops.src_path(d), [x.model_dump() for x in req.gcps],
                                   req.method, kinds, req.crs, max(500, min(req.max_px, 20000)), m)
    except Exception as e: raise HTTPException(400, str(e))
    return {"files": files, "folder": str(d / "exports")}

def do_upload(d, mid, req):
    try:
        save(d, upload={"status": "running", "progress": 0})
        cfg = req.s3
        r = exports.s3_upload(d, cfg, req.what, mid, meta(d), lambda f: save(d, upload={"status": "running", "progress": round(f, 3)}))
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

@app.post("/api/maps/{mid}/save")
def save_project(mid: str, req: SaveReq):
    d = mdir(mid); m = meta(d); out = DATA / f"{m['name']}.ktproj"
    proj = {"version": 1, "name": m["name"], **req.model_dump()}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("project.json", json.dumps(proj))
        for f in ("orig.jpg", "source.jpg", "source.png"):
            if (d / f).exists(): z.write(d / f, f)
    return {"path": str(out)}

@app.post("/api/projects/open")
async def open_project(file: UploadFile = File(...)):
    mid = new_id(); d = DATA / mid; d.mkdir()
    try:
        with zipfile.ZipFile(file.file) as z:
            proj = json.loads(z.read("project.json"))
            for f in ("orig.jpg", "source.jpg", "source.png"):
                if f in z.namelist(): (d / f).write_bytes(z.read(f))
        save(d, status="uploaded", name=safe(proj.get("name", "projet")))
        return {**img_info(d, mid), "project": proj}
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True); raise HTTPException(400, f"projet invalide : {e}")

@app.post("/api/maps/{mid}/open")
def open_folder(mid: str):
    d = mdir(mid)
    if sys.platform == "win32": os.startfile(d)
    return {"ok": True}

@app.post("/api/open_root")
def open_root():
    if sys.platform == "win32": os.startfile(DATA)
    return {"ok": True}

@app.get("/api/settings")
def get_settings(): return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}

@app.post("/api/settings")
def set_settings(s: dict):
    keep = {"mapbox": str(s.get("mapbox", "")), "google": str(s.get("google", "")),
            "s3": {k: str(v) for k, v in (s.get("s3") or {}).items()}}
    SETTINGS.write_text(json.dumps(keep)); return {"ok": True}

@app.get("/", response_class=HTMLResponse)
def home(): return (STATIC / "index.html").read_text(encoding="utf-8")
