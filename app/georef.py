"""Géoréférencement, tuiles et rasters sans GDAL (numpy + Pillow). Collage multi-images."""
import math, os, sqlite3, threading
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = 400_000_000
R = 6378137.0
ORIGIN = math.pi * R
MIN_PTS = {"affine": 3, "poly2": 6, "tps": 4}
MAX_TILES = 500_000
MAX_RASTER_PX = 250_000_000

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
    return fwd, inv, b, mpp

class Layer:
    """Une image calée (chargée à la demande)."""
    def __init__(self, path, gcps, method, name=""):
        im = Image.open(path)
        self.im = im if im.mode in ("RGB", "RGBA") else im.convert("RGB")
        self.w, self.h = self.im.size; self.name = name; self.gcps = gcps; self.method = method
        self.fwd, self.inv, self.b, self.mpp = prep(self.w, self.h, gcps, method)

def union(bs_mpps):
    bs = [b for b, _ in bs_mpps]
    return (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)), min(m for _, m in bs_mpps)

def native_z(mpp, ts):
    return min(24, max(0, math.ceil(math.log2(2 * ORIGIN / ts / mpp))))

def tile_range(b, z):
    size = 2 * ORIGIN / 2 ** z; n = 2 ** z - 1
    c = lambda v: max(0, min(n, int(v)))
    return (c((b[0] + ORIGIN) // size), c((b[2] + ORIGIN) // size), c((ORIGIN - b[3]) // size), c((ORIGIN - b[1]) // size))

def tile_count(b, z):
    x0, x1, y0, y1 = tile_range(b, z); return (x1 - x0 + 1) * (y1 - y0 + 1)

def zrange(zn, zmin, zmax):
    zmax = zn if zmax is None else max(0, min(24, int(zmax)))
    zmin = max(0, zmax - 6) if zmin is None else max(0, min(zmax, int(zmin)))
    return zmin, zmax

def estimate(specs, zmin=None, zmax=None, ts=256):
    """specs = [(w, h, gcps, method)]"""
    info = [prep(w, h, g, m)[2:] for w, h, g, m in specs]
    b, mpp = union(info); zn = native_z(mpp, ts); zmin, zmax = zrange(zn, zmin, zmax)
    counts = {z: tile_count(b, z) for z in range(zmin, zmax + 1)}
    return {"native": zn, "zmin": zmin, "zmax": zmax, "counts": counts, "total": sum(counts.values())}

# ---------- échantillonnage ----------
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

def composite(parts):
    """parts : liste de tableaux (n,4) uint8, du dessous vers le dessus."""
    if len(parts) == 1: return parts[0]
    acc, acc_a = 0.0, 0.0
    for p in parts:
        a = p[:, 3:4] / 255.0; acc = p[:, :3] * a + acc * (1 - a); acc_a = a + acc_a * (1 - a)
    return np.concatenate([acc / np.maximum(acc_a, 1e-6), acc_a * 255], axis=1).astype(np.uint8)

def _up(A, n, step, axis):
    t = np.arange(n) / step; i0 = np.minimum(t.astype(int), A.shape[axis] - 2); f = t - i0
    if axis == 1: return A[:, i0] * (1 - f) + A[:, i0 + 1] * f
    return A[i0, :] * (1 - f)[:, None] + A[i0 + 1, :] * f[:, None]

def uv_grid(inv, xs, ys, W, H, step):
    """Coordonnées pixel source pour une grille W×H, calculées sur une grille grossière (pas `step`) puis interpolées."""
    X, Y = np.meshgrid(xs, ys)
    P = inv(np.stack([X.ravel(), Y.ravel()], 1)).reshape(len(ys), len(xs), 2)
    return _up(_up(P[..., 0], W, step, 1), H, step, 0), _up(_up(P[..., 1], W, step, 1), H, step, 0)

def reduced(layer, scale):
    k = int(scale) if scale >= 2 else 1
    return (np.asarray(layer.im.reduce(k)), float(k)) if k > 1 else (np.asarray(layer.im), 1.0)

def make_tiles(layers, out_dir, progress=lambda f: None, zmin=None, zmax=None, ts=256):
    b, mpp = union([(l.b, l.mpp) for l in layers])
    zmin, zmax = zrange(native_z(mpp, ts), zmin, zmax)
    total = sum(tile_count(b, z) for z in range(zmin, zmax + 1))
    if total > MAX_TILES:
        raise ValueError(f"{total:,} tuiles : trop (max {MAX_TILES:,}). Réduisez le zoom maximal.")
    os.makedirs(out_dir, exist_ok=True)
    done = [0]; lock = threading.Lock(); step = ts // 32; K = ts // step + 2
    pool = ThreadPoolExecutor(min(8, os.cpu_count() or 2))
    for z in range(zmax, zmin - 1, -1):
        size = 2 * ORIGIN / 2 ** z; res = size / ts
        srcs = [reduced(l, res / l.mpp) for l in layers]
        x0, x1, y0, y1 = tile_range(b, z)
        def job(t):
            tx, ty = t; ox, oy = -ORIGIN + tx * size, ORIGIN - ty * size
            parts = []
            for l, (arr, k) in zip(layers, srcs):
                if l.b[2] < ox or l.b[0] > ox + size or l.b[3] < oy - size or l.b[1] > oy: continue
                xs = ox + (np.arange(K) * step + .5) * res; ys = oy - (np.arange(K) * step + .5) * res
                U, V = uv_grid(l.inv, xs, ys, ts, ts, step)
                c, ok = bilinear(arr, np.stack([U.ravel() / k, V.ravel() / k], 1))
                if ok.any(): parts.append(to_rgba(c, ok))
            if parts:
                d = os.path.join(out_dir, str(z), str(tx)); os.makedirs(d, exist_ok=True)
                Image.fromarray(composite(parts).reshape(ts, ts, 4), "RGBA").save(os.path.join(d, f"{ty}.png"), compress_level=1)
            with lock:
                done[0] += 1
                if done[0] % 25 == 0: progress(done[0] / total)
        jobs = [(tx, ty) for tx in range(x0, x1 + 1) for ty in range(y0, y1 + 1)]
        for i in range(0, len(jobs), 4000): list(pool.map(job, jobs[i:i + 4000]))
    pool.shutdown()
    west, south = m2ll(b[0], b[1]); east, north = m2ll(b[2], b[3])
    return [west, south, east, north], zmin, zmax

def render_raster(layers, crs="3857", max_px=8000):
    """Raster nord en haut (collage des calques). crs '3857' ou '4326'. max_px<=0 : résolution native.
    Retourne (RGBA, x0, y0, résolution)."""
    b, mpp = union([(l.b, l.mpp) for l in layers])
    if crs == "4326":
        west, south = m2ll(b[0], b[1]); east, north = m2ll(b[2], b[3]); span = max(east - west, north - south)
        res = mpp / 111320 if max_px <= 0 else max(mpp / 111320, span / max_px)
        x0, y0, W, H = west, north, math.ceil((east - west) / res), math.ceil((north - south) / res)
    else:
        span = max(b[2] - b[0], b[3] - b[1])
        res = mpp if max_px <= 0 else max(mpp, span / max_px)
        x0, y0, W, H = b[0], b[3], math.ceil((b[2] - b[0]) / res), math.ceil((b[3] - b[1]) / res)
    if W * H > MAX_RASTER_PX:
        raise ValueError(f"raster de {W}×{H} px ({W * H / 1e6:.0f} Mpx) trop grand : choisissez une taille maximale plus petite")
    res_m = res * 111320 if crs == "4326" else res
    srcs = [reduced(l, res_m / l.mpp) for l in layers]
    out = np.zeros((H, W, 4), np.uint8); step = 32; rows_chunk = max(16, min(256, 3_000_000 // W))
    xn = np.arange((W - 1) // step + 2) * step
    for r0 in range(0, H, rows_chunk):
        r1 = min(H, r0 + rows_chunk); yn = r0 + np.arange((r1 - r0 - 1) // step + 2) * step
        if crs == "4326":
            xs = (x0 + (xn + .5) * res) * ORIGIN / 180
            ys = np.log(np.tan((90 + (y0 - (yn + .5) * res)) * math.pi / 360)) * R
        else:
            xs, ys = x0 + (xn + .5) * res, y0 - (yn + .5) * res
        parts = []
        for l, (arr, k) in zip(layers, srcs):
            if l.b[2] < xs.min() or l.b[0] > xs.max() or l.b[3] < ys.min() or l.b[1] > ys.max(): continue
            U, V = uv_grid(l.inv, xs, ys, W, r1 - r0, step)
            c, ok = bilinear(arr, np.stack([U.ravel() / k, V.ravel() / k], 1))
            parts.append(to_rgba(c, ok))
        if parts: out[r0:r1] = composite(parts).reshape(r1 - r0, W, 4)
    return out, x0, y0, res

def footprint(w, h, gcps, method, n=80):
    fwd = prep(w, h, gcps, method)[0]
    ring = [list(m2ll(x, y)) for x, y in fwd(edge_points(w, h, n))]
    return ring + [ring[0]]

def to_mbtiles(tiles_dir, path, bounds, zmin, zmax, name, ts=256):
    if os.path.exists(path): os.remove(path)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metadata (name text, value text)")
    db.execute("CREATE TABLE tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob)")
    db.execute("CREATE UNIQUE INDEX t ON tiles (zoom_level, tile_column, tile_row)")
    meta = {"name": name, "format": "png", "type": "overlay", "version": "1", "tilesize": ts,
            "bounds": ",".join(f"{v:.6f}" for v in bounds), "minzoom": zmin, "maxzoom": zmax}
    db.executemany("INSERT INTO metadata VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    for z in os.listdir(tiles_dir):
        for x in os.listdir(os.path.join(tiles_dir, z)):
            for f in os.listdir(os.path.join(tiles_dir, z, x)):
                with open(os.path.join(tiles_dir, z, x, f), "rb") as fh:
                    db.execute("INSERT INTO tiles VALUES (?,?,?,?)", (int(z), int(x), 2 ** int(z) - 1 - int(f[:-4]), fh.read()))
    db.commit(); db.close()
