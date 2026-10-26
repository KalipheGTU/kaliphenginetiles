import datetime, hashlib, hmac, io, json, mimetypes, os, sqlite3, urllib.parse, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import numpy as np
from PIL import Image, TiffImagePlugin
from . import georef

WKT4326 = 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
WKT3857 = 'PROJCS["WGS 84 / Pseudo-Mercator",' + WKT4326 + ',PROJECTION["Mercator_1SP"],PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1]]'
WKT = {"4326": WKT4326, "3857": WKT3857}
KINDS = ["geotiff", "png_wld", "jpg_wld", "tif_wld", "kmz", "geojson", "shp", "gpkg"]

def write_geotiff(path, arr, x0, y0, res, crs):
    ifd = TiffImagePlugin.ImageFileDirectory_v2()
    ifd.tagtype[33550] = 12; ifd[33550] = (res, res, 0.0)
    ifd.tagtype[33922] = 12; ifd[33922] = (0.0, 0.0, 0.0, x0, y0, 0.0)
    proj = crs == "3857"
    ifd.tagtype[34735] = 3
    ifd[34735] = (1, 1, 0, 3, 1024, 0, 1, 1 if proj else 2, 1025, 0, 1, 1, 3072 if proj else 2048, 0, 1, int(crs))
    Image.fromarray(arr, "RGBA").save(path, "TIFF", tiffinfo=ifd, compression="tiff_deflate")

def write_wld(path, x0, y0, res):
    Path(path).write_text(f"{res}\n0\n0\n{-res}\n{x0 + res / 2}\n{y0 - res / 2}\n")

def frame_geojson(name, items, zmin=None, zmax=None):
    """items = [(nom_calque, anneau lon/lat)] : un contour par image + le rectangle englobant de l'ensemble."""
    xs = [q[0] for _, ring in items for q in ring]; ys = [q[1] for _, ring in items for q in ring]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    feats = [{"type": "Feature", "properties": {"name": n, "project": name, "kind": "footprint", "zmin": zmin, "zmax": zmax},
              "geometry": {"type": "Polygon", "coordinates": [ring]}} for n, ring in items]
    feats.append({"type": "Feature", "properties": {"name": name, "project": name, "kind": "bbox", "zmin": zmin, "zmax": zmax},
                  "geometry": {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}})
    return {"type": "FeatureCollection", "name": name + "_footprint", "bbox": [x0, y0, x1, y1], "features": feats}

def write_geojson(path, name, layers, zmin=None, zmax=None):
    items = [(l.name or name, georef.footprint(l.w, l.h, l.gcps, l.method)) for l in layers]
    Path(path).write_text(json.dumps(frame_geojson(name, items, zmin, zmax)))
    pts = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"layer": l.name, "n": i + 1, "px": g["px"], "py": g["py"]},
         "geometry": {"type": "Point", "coordinates": [g["lon"], g["lat"]]}}
        for l in layers for i, g in enumerate(l.gcps)]}
    Path(str(path).replace("_footprint.geojson", "_gcps.geojson")).write_text(json.dumps(pts))

def write_frame(out_dir, name, layers, zmin, zmax):
    out_dir = Path(out_dir); out_dir.mkdir(exist_ok=True)
    write_geojson(out_dir / f"{name}_footprint.geojson", name, layers, zmin, zmax)

def write_shp(base, name, layers):
    import shapefile
    w = shapefile.Writer(str(base), shapeType=shapefile.POLYGON); w.field("name", "C", 80)
    for l in layers:
        ring = georef.footprint(l.w, l.h, l.gcps, l.method)
        if sum(ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1] for i in range(len(ring) - 1)) > 0: ring = ring[::-1]
        w.poly([ring]); w.record(l.name or name)
    w.close(); Path(str(base) + ".prj").write_text(WKT4326)

def write_kmz(path, name, arr, x0, y0, res):
    H, W = arr.shape[:2]
    buf = io.BytesIO(); Image.fromarray(arr, "RGBA").save(buf, "PNG")
    kml = ('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><GroundOverlay>'
           f'<name>{name}</name><Icon><href>overlay.png</href></Icon><LatLonBox><north>{y0}</north><south>{y0 - H * res}</south>'
           f'<east>{x0 + W * res}</east><west>{x0}</west></LatLonBox></GroundOverlay></kml>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml); z.writestr("overlay.png", buf.getvalue())

def write_gpkg(path, tiles_dir, name, bounds_m, zmin, zmax, ts=256):
    if os.path.exists(path): os.remove(path)
    O = georef.ORIGIN; db = sqlite3.connect(path)
    db.execute("PRAGMA application_id=1196444487"); db.execute("PRAGMA user_version=10300")
    db.execute("CREATE TABLE gpkg_spatial_ref_sys(srs_name TEXT NOT NULL, srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT)")
    db.executemany("INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)", [
        ("Undefined Cartesian SRS", -1, "NONE", -1, "undefined", None), ("Undefined Geographic SRS", 0, "NONE", 0, "undefined", None),
        ("WGS 84", 4326, "EPSG", 4326, WKT4326, None), ("WGS 84 / Pseudo-Mercator", 3857, "EPSG", 3857, WKT3857, None)])
    db.execute("CREATE TABLE gpkg_contents(table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '', last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER)")
    db.execute("INSERT INTO gpkg_contents(table_name,data_type,identifier,min_x,min_y,max_x,max_y,srs_id) VALUES ('tiles','tiles',?,?,?,?,?,3857)", (name, *bounds_m))
    db.execute("CREATE TABLE gpkg_tile_matrix_set(table_name TEXT NOT NULL PRIMARY KEY, srs_id INTEGER NOT NULL, min_x DOUBLE NOT NULL, min_y DOUBLE NOT NULL, max_x DOUBLE NOT NULL, max_y DOUBLE NOT NULL)")
    db.execute("INSERT INTO gpkg_tile_matrix_set VALUES ('tiles',3857,?,?,?,?)", (-O, -O, O, O))
    db.execute("CREATE TABLE gpkg_tile_matrix(table_name TEXT NOT NULL, zoom_level INTEGER NOT NULL, matrix_width INTEGER NOT NULL, matrix_height INTEGER NOT NULL, tile_width INTEGER NOT NULL, tile_height INTEGER NOT NULL, pixel_x_size DOUBLE NOT NULL, pixel_y_size DOUBLE NOT NULL, PRIMARY KEY (table_name, zoom_level))")
    for z in range(zmin, zmax + 1):
        db.execute("INSERT INTO gpkg_tile_matrix VALUES ('tiles',?,?,?,?,?,?,?)", (z, 2 ** z, 2 ** z, ts, ts, 2 * O / ts / 2 ** z, 2 * O / ts / 2 ** z))
    db.execute("CREATE TABLE tiles(id INTEGER PRIMARY KEY AUTOINCREMENT, zoom_level INTEGER NOT NULL, tile_column INTEGER NOT NULL, tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL, UNIQUE(zoom_level,tile_column,tile_row))")
    for z in os.listdir(tiles_dir):
        for x in os.listdir(os.path.join(tiles_dir, z)):
            for f in os.listdir(os.path.join(tiles_dir, z, x)):
                with open(os.path.join(tiles_dir, z, x, f), "rb") as fh:
                    db.execute("INSERT INTO tiles(zoom_level,tile_column,tile_row,tile_data) VALUES (?,?,?,?)", (int(z), int(x), int(f[:-4]), fh.read()))
    db.commit(); db.close()

def export_all(d, name, layers, kinds, crs, max_px, meta):
    """Exporte le COLLAGE de tous les calques en un seul fichier par format."""
    out = d / "exports"; out.mkdir(exist_ok=True); files = []
    if "geojson" in kinds:
        write_geojson(out / f"{name}_footprint.geojson", name, layers, meta.get("zmin"), meta.get("zmax"))
        files += [f"{name}_footprint.geojson", f"{name}_gcps.geojson"]
    if "shp" in kinds:
        write_shp(out / f"{name}_footprint", name, layers); files.append(f"{name}_footprint.shp (+ .shx .dbf .prj)")
    cache = {}
    def ras(c):
        if c not in cache: cache[c] = georef.render_raster(layers, c, max_px)
        return cache[c]
    if "geotiff" in kinds:
        a, x0, y0, r = ras(crs); write_geotiff(out / f"{name}_{crs}.tif", a, x0, y0, r, crs); files.append(f"{name}_{crs}.tif")
    for fmt, ext, wext in (("png", "png", "pgw"), ("jpg", "jpg", "jgw"), ("tif", "tif", "tfw")):
        if f"{fmt}_wld" in kinds:
            a, x0, y0, r = ras(crs); base = out / f"{name}_wld"
            im = Image.fromarray(a, "RGBA")
            if fmt == "jpg":
                bg = Image.new("RGB", im.size, "white"); bg.paste(im, mask=im.split()[3]); bg.save(f"{base}.jpg", quality=95)
            else: im.save(f"{base}.{ext}")
            write_wld(f"{base}.{wext}", x0, y0, r); Path(f"{base}.prj").write_text(WKT[crs])
            files += [f"{name}_wld.{ext}", f"{name}_wld.{wext}", f"{name}_wld.prj"]
    if "kmz" in kinds:
        a, x0, y0, r = ras("4326"); write_kmz(out / f"{name}.kmz", name, a, x0, y0, r); files.append(f"{name}.kmz")
    if "gpkg" in kinds:
        if not (d / "tiles").exists(): raise ValueError("GeoPackage : générez d'abord les tuiles (bouton Géoréférencer)")
        b, _ = georef.union([(l.b, l.mpp) for l in layers])
        write_gpkg(out / f"{name}.gpkg", d / "tiles", name, b, meta["zmin"], meta["zmax"], meta.get("ts", 256)); files.append(f"{name}.gpkg")
    return files

# ---------- Envoi S3 (AWS S3, Cloudflare R2, Scaleway, MinIO…) : signature SigV4 ----------
def _hm(k, m): return hmac.new(k, m.encode(), hashlib.sha256).digest()

def s3_put(cfg, key, data, ctype):
    bucket, region = cfg["bucket"], cfg.get("region") or "us-east-1"
    ep = (cfg.get("endpoint") or "").rstrip("/")
    if ep:
        u = urllib.parse.urlparse(ep); host, scheme, path = u.netloc, u.scheme, f"{u.path}/{bucket}/{key}"
    else:
        host, scheme, path = f"{bucket}.s3.{region}.amazonaws.com", "https", f"/{key}"
    cpath = urllib.parse.quote(path, safe="/-_.~")
    amz = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"); ds = amz[:8]
    ph = hashlib.sha256(data).hexdigest()
    hdr = {"content-type": ctype, "host": host, "x-amz-content-sha256": ph, "x-amz-date": amz}
    sh = ";".join(sorted(hdr))
    canon = "PUT\n" + cpath + "\n\n" + "".join(f"{k}:{hdr[k]}\n" for k in sorted(hdr)) + "\n" + sh + "\n" + ph
    scope = f"{ds}/{region}/s3/aws4_request"
    sts = "AWS4-HMAC-SHA256\n" + amz + "\n" + scope + "\n" + hashlib.sha256(canon.encode()).hexdigest()
    k = _hm(b"AWS4" + cfg["secret_key"].encode(), ds)
    for part in (region, "s3", "aws4_request"): k = _hm(k, part)
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={cfg['access_key']}/{scope}, SignedHeaders={sh}, Signature={sig}"
    url = f"{scheme}://{host}{cpath}"
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "Content-Type": ctype, "x-amz-content-sha256": ph, "x-amz-date": amz, "Authorization": auth})
    urllib.request.urlopen(req, timeout=60).read()
    return url

VIEWER = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>__N__</title>
<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css" rel=stylesheet><script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>html,body,#m{margin:0;height:100%}#o{position:absolute;top:10px;left:10px;background:#fff;padding:6px 10px;border-radius:6px;font:13px sans-serif}</style>
<div id=m></div><div id=o>Opacité <input id=op type=range min=0 max=1 step=.05 value=1></div>
<script>const B=__B__,m=new maplibregl.Map({container:"m",bounds:B,fitBoundsOptions:{padding:40},style:{version:8,sources:{
o:{type:"raster",tileSize:256,tiles:["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],attribution:"© OpenStreetMap"},
c:{type:"raster",tileSize:256,bounds:B,minzoom:__Z0__,maxzoom:__Z1__,tiles:[new URL("tiles/{z}/{x}/{y}.png",location.href).href]}},
layers:[{id:"o",type:"raster",source:"o"},{id:"c",type:"raster",source:"c"}]}});
op.oninput=()=>m.setPaintProperty("c","raster-opacity",+op.value)</script>"""

def s3_upload(d, cfg, what, prefix_id, meta, progress):
    items = []
    if "tiles" in what and (d / "tiles").exists():
        items += [(p, f"tiles/{p.relative_to(d / 'tiles').as_posix()}") for p in (d / "tiles").rglob("*.png")]
    if "mbtiles" in what and (d / "carte.mbtiles").exists(): items.append((d / "carte.mbtiles", "carte.mbtiles"))
    if "exports" in what and (d / "exports").exists():
        items += [(p, f"exports/{p.name}") for p in (d / "exports").iterdir() if p.is_file()]
    root = "/".join(x for x in [cfg.get("prefix", "").strip("/"), prefix_id] if x)
    if "tiles" in what and meta.get("bounds"):
        html = (VIEWER.replace("__N__", meta.get("name", "carte")).replace("__B__", json.dumps(meta["bounds"]))
                .replace("__Z0__", str(meta.get("zmin", 0))).replace("__Z1__", str(meta.get("zmax", 22))))
        (d / "viewer.html").write_text(html, encoding="utf-8"); items.append((d / "viewer.html", "index.html"))
    if not items: raise ValueError("rien à envoyer : générez d'abord les tuiles")
    done = [0]; first = [None]
    def one(it):
        p, rel = it; ct = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix == ".geojson": ct = "application/geo+json"
        u = s3_put(cfg, f"{root}/{rel}", p.read_bytes(), ct)
        done[0] += 1; progress(done[0] / len(items))
        return u
    with ThreadPoolExecutor(8) as ex: urls = list(ex.map(one, items))
    ep = (cfg.get("endpoint") or "").rstrip("/")
    base = f"{ep}/{cfg['bucket']}/{root}" if ep else f"https://{cfg['bucket']}.s3.{cfg.get('region') or 'us-east-1'}.amazonaws.com/{root}"
    return {"base": base, "viewer": base + "/index.html", "xyz": base + "/tiles/{z}/{x}/{y}.png", "count": len(items)}
