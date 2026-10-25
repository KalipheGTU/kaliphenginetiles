import math
import numpy as np
from PIL import Image

def src_path(d):
    p = d / "source.png"
    return p if p.exists() else d / "source.jpg"

def load(d):
    return Image.open(src_path(d))

def save_cur(d, im):
    for f in ("source.png", "source.jpg"): (d / f).unlink(missing_ok=True)
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
