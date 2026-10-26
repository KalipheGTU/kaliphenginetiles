import math
import numpy as np
from PIL import Image, ImageOps

def src_path(d):
    p = d / "source.png"
    return p if p.exists() else d / "source.jpg"

def load(d):
    return Image.open(src_path(d))

def save_cur(d, im):
    for f in ("source.png", "source.jpg", "prev_small.png", "preview.png"): (d / f).unlink(missing_ok=True)
    if im.mode == "RGBA": im.save(d / "source.png")
    else: im.convert("RGB").save(d / "source.jpg", quality=95)

def crop(im, box, gcps):
    w, h = im.size
    x0, y0 = max(0, int(round(box[0]))), max(0, int(round(box[1])))
    x1, y1 = min(w, int(round(box[2]))), min(h, int(round(box[3])))
    if x1 - x0 < 8 or y1 - y0 < 8: raise ValueError("zone de rognage trop petite")
    return im.crop((x0, y0, x1, y1)), [{**g, "px": g["px"] - x0, "py": g["py"] - y0} for g in gcps]

def rotate(im, angle, gcps):
    """angle en degrés, positif = anti-horaire. Fond transparent."""
    W, H = im.size
    im2 = im.convert("RGBA").rotate(angle, expand=True, resample=Image.BICUBIC)
    W2, H2 = im2.size; a = math.radians(angle); c, s = math.cos(a), math.sin(a)
    out = []
    for g in gcps:
        dx, dy = g["px"] - W / 2, g["py"] - H / 2
        out.append({**g, "px": W2 / 2 + dx * c + dy * s, "py": H2 / 2 - dx * s + dy * c})
    return im2, out

def remove_colors(im, colors, tol):
    a = np.asarray(im.convert("RGBA")).copy(); rgb = a[..., :3].astype(int)
    m = np.zeros(a.shape[:2], bool)
    for c in colors: m |= np.abs(rgb - np.array(c[:3])).max(axis=2) <= tol
    a[m, 3] = 0
    return Image.fromarray(a, "RGBA")

def reset(d):
    for f in ("source.png", "source.jpg", "prev_small.png", "preview.png"): (d / f).unlink(missing_ok=True)
    o = d / "orig.jpg" if (d / "orig.jpg").exists() else d / "orig.png"
    import shutil; shutil.copy(o, d / ("source.jpg" if o.suffix == ".jpg" else "source.png"))

def prev_small(d, side=2048):
    """Copie réduite de l'image courante, pour un aperçu rapide. Retourne (chemin, facteur d'échelle)."""
    src = src_path(d); p = d / "prev_small.png"
    w, h = Image.open(src).size
    if not p.exists():
        im = Image.open(src); f = min(1.0, side / max(w, h))
        if f < 1: im.draft("RGB", (round(w * f), round(h * f)))
        im = im.convert("RGBA" if im.mode == "RGBA" else "RGB")
        if f < 1: im = im.resize((round(w * f), round(h * f)), Image.LANCZOS)
        im.save(p)
    return p, Image.open(p).size[0] / w


def harmonize(im):
    """Correction douce et automatique du contraste/couleur, en préservant l'alpha."""
    rgba = im.convert("RGBA")
    a = np.asarray(rgba).copy()
    rgb = a[..., :3]
    # Ignore les pixels transparents pour calculer les percentiles.
    valid = a[..., 3] > 0
    if not valid.any():
        return rgba
    out = rgb.copy()
    for c in range(3):
        vals = rgb[..., c][valid]
        lo, hi = np.percentile(vals, (1.0, 99.0))
        if hi > lo:
            scaled = (rgb[..., c].astype(np.float32) - lo) * (255.0 / (hi - lo))
            out[..., c] = np.clip(scaled, 0, 255).astype(np.uint8)
    a[..., :3] = out
    return Image.fromarray(a, "RGBA")
