"""Géoréférencement, tuiles et rasters sans GDAL (numpy + Pillow)."""
import math, os, sqlite3
import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = 300_000_000
R = 6378137.0
ORIGIN = math.pi * R
MIN_PTS = {"affine": 3, "poly2": 6, "tps": 4}
MAX_TILES = 500_000

def ll2m(lon, lat):
    return lon * ORIGIN / 180, math.log(math.tan((90 + lat) * math.pi / 360)) * R
def m2ll(x, y):
    return x / ORIGIN * 180, math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)

class Model:
    """Transformation 2D->2D : affine / polynôme 2 (moindres carrés) ou thin plate spline."""
    def __init__(self, src, dst, method):
        self.method = method
        src, dst = np.asarray(src, float), np.asarray(dst, float)
        self.mu = src.mean(0); self.sc = max(float(src.std(0).max()), 1e-9)
        s = (src - self.mu) / self.sc
        self.dmu = dst.mean(0); self.dsc = max(float(dst.std(0).max()), 1e-9)
        d = (dst - self.dmu) / self.dsc
        if method == "tps":
            n = len(s); A = np.zeros((n + 3, n + 3))
            A[:n, :n] = self._k(np.linalg.norm(s[:, None] - s[None], axis=2))
            A[:n, n] = 1; A[:n, n + 1:] = s; A[n, :n] = 1; A[n + 1:, :n] = s.T
            b = np.zeros((n + 3, 2)); b[:n] = d
            self.s = s; self.w = np.linalg.lstsq(A, b, rcond=None)[0]
        else:
            self.w = np.linalg.lstsq(self._design(s), d, rcond=None)[0]
    @staticmethod
    def _k(r):
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(r == 0, 0, r * r * np.log(r * r))
    def _design(self, s):
        x, y = s[:, 0], s[:, 1]; cols = [np.ones_like(x), x, y]
        if self.method == "poly2": cols += [x * x, x * y, y * y]
        return np.stack(cols, 1)
    def __call__(self, pts):
        s = (np.asarray(pts, float) - self.mu) / self.sc
        if self.method == "tps":
            n = len(self.s)
            K = self._k(np.linalg.norm(s[:, None] - self.s[None], axis=2))
            out = K @ self.w[:n] + self.w[n] + s @ self.w[n + 1:]
        else:
            out = self._design(s) @ self.w
        return out * self.dsc + self.dmu

def rmse(gcps, method):
    px = [(g["px"], g["py"]) for g in gcps]; mm = [ll2m(g["lon"], g["lat"]) for g in gcps]
    return np.linalg.norm(Model(mm, px, method)(mm) - np.array(px), axis=1).tolist()

def edge_points(w, h, n=60):
    t = np.linspace(0, 1, n)
    return np.concatenate([np.stack([t * w, 0 * t], 1), np.stack([0 * t + w, t * h], 1),
                           np.stack([(1 - t) * w, 0 * t + h], 1), np.stack([0 * t, (1 - t) * h], 1)])

def prep(w, h, gcps, method):
    px = [(g["px"], g["py"]) for g in gcps]; mm = [ll2m(g["lon"], g["lat"]) for g in gcps]
    fwd, inv = Model(px, mm, method), Model(mm, px, method)
    e = fwd(edge_points(w, h))
    b = (e[:, 0].min(), e[:, 1].min(), e[:, 0].max(), e[:, 1].max())
    mpp = max((b[2] - b[0]) / w, (b[3] - b[1]) / h)
    zn = min(24, max(0, math.ceil(math.log2(2 * ORIGIN / 256 / mpp))))
    return fwd, inv, b, mpp, zn

def tile_range(b, z):
    size = 2 * ORIGIN / 2 ** z; n = 2 ** z - 1
    c = lambda v: max(0, min(n, int(v)))
    return (c((b[0] + ORIGIN) // size), c((b[2] + ORIGIN) // size), c((ORIGIN - b[3]) // size), c((ORIGIN - b[1]) // size))

def tile_count(b, z):
    x0, x1, y0, y1 = tile_range(b, z); return (x1 - x0 + 1) * (y1 - y0 + 1)

def estimate(w, h, gcps, method, zmin=None, zmax=None):
    _, _, b, _, zn = prep(w, h, gcps, method)
    zmax = zn if zmax is None else max(0, min(24, int(zmax)))
    zmin = max(0, zmax - 6) if zmin is None else max(0, min(zmax, int(zmin)))
    counts = {z: tile_count(b, z) for z in range(zmin, zmax + 1)}
    return {"native": zn, "counts": counts, "total": sum(counts.values())}

def bilinear(arr, p):
    h, w = arr.shape[:2]; u, v = p[:, 0] - .5, p[:, 1] - .5
    ok = (u >= 0) & (u <= w - 1) & (v >= 0) & (v <= h - 1)
    u, v = np.clip(u, 0, w - 1.001), np.clip(v, 0, h - 1.001)
    iu, iv = u.astype(int), v.astype(int); fu, fv = (u - iu)[:, None], (v - iv)[:, None]
    c = (arr[iv, iu] * (1 - fu) * (1 - fv) + arr[iv, iu + 1] * fu * (1 - fv) +
         arr[iv + 1, iu] * (1 - fu) * fv + arr[iv + 1, iu + 1] * fu * fv)
    return c, ok

def to_rgba(c, ok):
    a = c[:, 3] if c.shape[1] == 4 else np.full(len(c), 255.0)
    return np.concatenate([c[:, :3], np.where(ok, a, 0)[:, None]], axis=1).astype(np.uint8)

def load_arr(path):
    im = Image.open(path)
    return im if im.mode in ("RGB", "RGBA") else im.convert("RGB")

def make_tiles(img_path, gcps, method, out_dir, progress=lambda f: None, zmin=None, zmax=None):
    im = load_arr(img_path); w, h = im.size
    _, inv, b, mpp, zn = prep(w, h, gcps, method)
    zmax = zn if zmax is None else max(0, min(24, int(zmax)))
    zmin = max(0, zmax - 6) if zmin is None else max(0, min(zmax, int(zmin)))
    total = sum(tile_count(b, z) for z in range(zmin, zmax + 1))
    if total > MAX_TILES:
        raise ValueError(f"{total:,} tuiles : trop (max {MAX_TILES:,}). Réduisez le zoom maximal.")
    done = 0; os.makedirs(out_dir, exist_ok=True)
    for z in range(zmax, zmin - 1, -1):
        size = 2 * ORIGIN / 2 ** z; res = size / 256
        scale = max(1.0, res / mpp)
        src = im.resize((max(2, int(w / scale)), max(2, int(h / scale))), Image.LANCZOS) if scale > 1.5 else im
        if src is im: scale = 1.0
        arr = np.asarray(src); x0, x1, y0, y1 = tile_range(b, z); step = (np.arange(256) + .5) * res
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                done += 1
                X, Y = np.meshgrid(-ORIGIN + tx * size + step, ORIGIN - ty * size - step)
                c, ok = bilinear(arr, inv(np.stack([X.ravel(), Y.ravel()], 1)) / scale)
                if not ok.any(): continue
                rgba = to_rgba(c, ok).reshape(256, 256, 4)
                d = os.path.join(out_dir, str(z), str(tx)); os.makedirs(d, exist_ok=True)
                Image.fromarray(rgba, "RGBA").save(os.path.join(d, f"{ty}.png"), compress_level=3)
            progress(done / total)
    west, south = m2ll(b[0], b[1]); east, north = m2ll(b[2], b[3])
    return [west, south, east, north], zmin, zmax

def render_raster(img_path, gcps, method, crs="3857", max_px=8000):
    """Raster nord en haut. crs '3857' (Mercator) ou '4326' (WGS84). Retourne (RGBA, x0, y0, résolution)."""
    im = load_arr(img_path); arr = np.asarray(im); h, w = arr.shape[:2]
    _, inv, b, mpp, _ = prep(w, h, gcps, method)
    if crs == "4326":
        west, south = m2ll(b[0], b[1]); east, north = m2ll(b[2], b[3])
        res = max(mpp / 111320, max(east - west, north - south) / max_px); x0, y0 = west, north
        W, H = math.ceil((east - west) / res), math.ceil((north - south) / res)
    else:
        res = max(mpp, max(b[2] - b[0], b[3] - b[1]) / max_px); x0, y0 = b[0], b[3]
        W, H = math.ceil((b[2] - b[0]) / res), math.ceil((b[3] - b[1]) / res)
    out = np.zeros((H, W, 4), np.uint8); cols = x0 + (np.arange(W) + .5) * res
    for r0 in range(0, H, 256):
        rows = y0 - (np.arange(r0, min(H, r0 + 256)) + .5) * res
        X, Y = np.meshgrid(cols, rows)
        if crs == "4326": X, Y = X * ORIGIN / 180, np.log(np.tan((90 + Y) * math.pi / 360)) * R
        c, ok = bilinear(arr, inv(np.stack([X.ravel(), Y.ravel()], 1)))
        out[r0:r0 + len(rows)] = to_rgba(c, ok).reshape(len(rows), W, 4)
    return out, x0, y0, res

def footprint(w, h, gcps, method, n=80):
    fwd, _, _, _, _ = prep(w, h, gcps, method)
    ring = [list(m2ll(x, y)) for x, y in fwd(edge_points(w, h, n))]
    return ring + [ring[0]]

def to_mbtiles(tiles_dir, path, bounds, zmin, zmax, name):
    if os.path.exists(path): os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metadata (name text, value text)")
    db.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
    db.execute("CREATE UNIQUE INDEX t ON tiles (zoom_level, tile_column, tile_row)")
    meta = {"name": name, "format": "png", "type": "overlay", "version": "1",
            "bounds": ",".join(f"{v:.6f}" for v in bounds), "minzoom": zmin, "maxzoom": zmax}
    db.executemany("INSERT INTO metadata VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    for z in os.listdir(tiles_dir):
        for x in os.listdir(os.path.join(tiles_dir, z)):
            for f in os.listdir(os.path.join(tiles_dir, z, x)):
                with open(os.path.join(tiles_dir, z, x, f), "rb") as fh:
                    db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (int(z), int(x), 2 ** int(z) - 1 - int(f[:-4]), fh.read()))
    db.commit(); db.close()
