#!/usr/bin/env python3
# rebuild_from_slide_v3.py — v3.0.0
# Multi-slide rebuild. Slide-driven. Prefers SVG with embedded data URIs (from create_slide_qrfs_v2/v3).
# Can also accept PDF/GDS/OAS inputs in limited modes (see --format notes).
#
# What it does:
# - Scans one or more slide artifacts (SVG by default) and decodes each QR tile
# - Reconstructs original QRFS bundles (optionally) and rehydrates original files by concatenating data blocks
# - Verifies per-block SHA-256 (optional) and final file SHA-256 (when header provides it)
#
# Supported inputs:
# - SVG (recommended): decodes from <image href="data:..."> or filenames next to the SVG
# - PDF (optional): requires PyMuPDF (fitz) and either sibling SVG for tile geometry OR slide JSON + grid params
# - GDSII/OASIS (experimental): supports 2×2 panes produced by slice_slide_2x2_v2 (vector modules) via gdstk
#
import argparse, sys, json, re, binascii, base64, struct, hashlib, time
from pathlib import Path
from typing import Optional, Tuple, Dict, List, Any
import xml.etree.ElementTree as ET

# --- Optional decode backends ---
_HAVE_OPENCV = False
_HAVE_PYZBAR = False
_HAVE_PIL = False
_HAVE_FITZ = False
_HAVE_GDSTK = False

try:
    import cv2  # type: ignore
    _HAVE_OPENCV = True
except Exception:
    cv2 = None  # type: ignore

try:
    from pyzbar.pyzbar import decode as _zb_decode, ZBarSymbol  # type: ignore
    _HAVE_PYZBAR = True
except Exception:
    _zb_decode = None  # type: ignore
    ZBarSymbol = None  # type: ignore

try:
    from PIL import Image  # type: ignore
    _HAVE_PIL = True
    try:
        Image.MAX_IMAGE_PIXELS = None  # allow huge proofs
    except Exception:
        pass
except Exception:
    Image = None  # type: ignore

try:
    import fitz  # PyMuPDF  # type: ignore
    _HAVE_FITZ = True
except Exception:
    fitz = None  # type: ignore

try:
    import gdstk  # type: ignore
    _HAVE_GDSTK = True
except Exception:
    gdstk = None  # type: ignore


SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

MAGIC = b"QRF1"
TYPE_HEADER = 0x01
TYPE_DATA   = 0x02
TYPE_PARITY = 0x03
TYPE_FOOTER = 0x04

QR_V40_MODULES = 177


def eprint(*a):
    print(*a, file=sys.stderr)


def dprint(verbose: bool, *a):
    if verbose:
        eprint(*a)


def progress_bar(prefix: str, i: int, total: int, t0: float, width: int = 40):
    if total <= 0:
        return
    dt = max(1e-6, time.time() - t0)
    rate = i / dt
    pct = int(i * 100 / total)
    done = int(width * (i / total))
    bar = "#" * done + "-" * (width - done)
    end = "" if i < total else "\n"
    print(f"\r{prefix} [{bar}] {pct:3d}% ({i}/{total}) {rate:6.1f}/s", end=end, file=sys.stdout, flush=True)


def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default


# ---------------- QR decode helpers ----------------

def _opencv_decode_from_png_bytes(img_bytes: bytes) -> Optional[bytes]:
    if not _HAVE_OPENCV:
        return None
    try:
        import numpy as np  # type: ignore
        arr = np.frombuffer(img_bytes, dtype=np.uint8)
        mat = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)  # type: ignore
        if mat is None:
            return None
        det = cv2.QRCodeDetector()  # type: ignore
        data, pts, _ = det.detectAndDecode(mat)
        if data:
            return data.encode("latin-1", errors="ignore")
        # try some robustification paths
        try:
            _, th = cv2.threshold(mat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)  # type: ignore
            data2, _, _ = det.detectAndDecode(th)
            if data2:
                return data2.encode("latin-1", errors="ignore")
            h, w = mat.shape[:2]
            up2 = cv2.resize(mat, (w * 2, h * 2), interpolation=cv2.INTER_NEAREST)  # type: ignore
            data3, _, _ = det.detectAndDecode(up2)
            if data3:
                return data3.encode("latin-1", errors="ignore")
            up3 = cv2.resize(mat, (w * 3, h * 3), interpolation=cv2.INTER_NEAREST)  # type: ignore
            data4, _, _ = det.detectAndDecode(up3)
            if data4:
                return data4.encode("latin-1", errors="ignore")
        except Exception:
            pass
    except Exception:
        return None
    return None


def _pyzbar_decode_from_png_bytes(img_bytes: bytes) -> Optional[bytes]:
    if not _HAVE_PYZBAR or not _HAVE_PIL:
        return None
    try:
        from io import BytesIO
        im = Image.open(BytesIO(img_bytes))  # type: ignore
        res = _zb_decode(im, symbols=[ZBarSymbol.QRCODE])  # type: ignore
        if res:
            return bytes(res[0].data)
    except Exception:
        return None
    return None


def decode_qr_from_png_bytes(img_bytes: bytes, decoder: str = "auto") -> Optional[bytes]:
    if decoder == "opencv":
        return _opencv_decode_from_png_bytes(img_bytes)
    if decoder == "pyzbar":
        return _pyzbar_decode_from_png_bytes(img_bytes)
    # auto
    b = _opencv_decode_from_png_bytes(img_bytes)
    if b is not None:
        return b
    return _pyzbar_decode_from_png_bytes(img_bytes)


# ---------------- payload parsing ----------------

HEX_RE = re.compile(br"^[0-9a-fA-F\s]+$")
B64_RE = re.compile(br"^[A-Za-z0-9+/=\s]+$")


def try_unhex(payload: bytes) -> Optional[bytes]:
    if not HEX_RE.match(payload):
        return None
    s = b"".join(payload.split())
    if len(s) % 2 != 0:
        return None
    try:
        return binascii.unhexlify(s)
    except Exception:
        return None


def try_unbase64(payload: bytes) -> Optional[bytes]:
    if not B64_RE.match(payload):
        return None
    s = b"".join(payload.split())
    if len(s) < 16:
        return None
    try:
        return base64.b64decode(s, validate=True)
    except Exception:
        return None


def try_utf8_collapse(payload: bytes) -> Optional[bytes]:
    # common failure mode: bytes got coerced via utf-8; collapse back into latin-1
    try:
        return payload.decode("utf-8").encode("latin-1")
    except Exception:
        return None


def parse_qrfs_payload(payload: bytes, check_block_sha: bool = False) -> Tuple[str, Optional[dict]]:
    """
    Returns: (kind, obj)
      kind: json | data | parity | other | unknown
    """
    def _parse(buf: bytes) -> Tuple[str, Optional[dict]]:
        if buf[:1] in (b"{", b"["):
            try:
                return "json", json.loads(buf.decode("utf-8"))
            except Exception:
                return "unknown", None

        if len(buf) < 6:
            return "unknown", None

        if buf[:4] != MAGIC:
            i = buf.find(MAGIC)
            if i < 0 or len(buf) - i < 6:
                return "unknown", None
            buf = buf[i:]

        typ = buf[4]
        ver = buf[5]
        off = 6

        if typ == TYPE_DATA:
            need = off + 32 + 8 + 4 + 32 + 1
            if len(buf) < need:
                return "unknown", None

            file_id = buf[off:off + 32]; off += 32
            block_index, total_blocks = struct.unpack(">II", buf[off:off + 8]); off += 8
            data_len, = struct.unpack(">I", buf[off:off + 4]); off += 4
            block_sha = buf[off:off + 32]; off += 32
            flags = buf[off]; off += 1

            # data may be appended raw or encoded
            if len(buf) < off + data_len:
                rem = buf[off:]
                hx = try_unhex(rem)
                if hx is not None and len(hx) >= data_len:
                    data = hx[:data_len]
                else:
                    raw = try_unbase64(rem) or try_utf8_collapse(buf)
                    if raw is None:
                        return "unknown", None
                    return _parse(raw)
            else:
                data = buf[off:off + data_len]

            ok = (hashlib.sha256(data).digest() == block_sha)
            if check_block_sha and not ok:
                # keep it, but mark it bad; caller may reject
                return "data", {
                    "file_id_hex": file_id.hex(),
                    "block_index": int(block_index),
                    "total_blocks": int(total_blocks),
                    "data": data,
                    "flags": int(flags),
                    "block_sha_ok": False,
                }

            return "data", {
                "file_id_hex": file_id.hex(),
                "block_index": int(block_index),
                "total_blocks": int(total_blocks),
                "data": data,
                "flags": int(flags),
                "block_sha_ok": True,
            }

        if typ == TYPE_PARITY:
            need = off + 32 + 4 + 1 + 4
            if len(buf) < need:
                return "unknown", None
            file_id = buf[off:off + 32]; off += 32
            stripe_index, = struct.unpack(">I", buf[off:off + 4]); off += 4
            parity_index = buf[off]; off += 1
            plen, = struct.unpack(">I", buf[off:off + 4]); off += 4
            if len(buf) < off + plen:
                raw = try_utf8_collapse(buf)
                if raw is None:
                    return "unknown", None
                return _parse(raw)
            pdata = buf[off:off + plen]
            return "parity", {
                "file_id_hex": file_id.hex(),
                "stripe_index": int(stripe_index),
                "parity_index": int(parity_index),
                "data": pdata,
            }

        if typ in (TYPE_HEADER, TYPE_FOOTER):
            return "other", {"typ": int(typ), "ver": int(ver)}

        return "unknown", None

    kind, obj = _parse(payload)
    if kind != "unknown":
        return kind, obj

    for f in (try_unhex, try_unbase64, try_utf8_collapse):
        raw = f(payload)
        if raw is not None:
            kind2, obj2 = _parse(raw)
            if kind2 != "unknown":
                return kind2, obj2

    return "unknown", None


# ---------------- SVG parsing / tile bytes ----------------

def _num(s: Optional[str]) -> float:
    if not s:
        return 0.0
    s = re.sub(r"[^0-9.\-]+", "", s.strip())
    try:
        return float(s)
    except Exception:
        return 0.0


def parse_svg(svg_path: Path) -> Tuple[Tuple[float, float], List[Tuple[str, float, float, float, float]]]:
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    tree = ET.parse(str(svg_path))
    root = tree.getroot()

    vb = root.get("viewBox")
    view_w = view_h = None
    if vb:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) == 4:
            try:
                view_w = float(parts[2])
                view_h = float(parts[3])
            except Exception:
                pass
    if view_w is None or view_h is None:
        view_w = _num(root.get("width") or "93")
        view_h = _num(root.get("height") or "93")

    ns = {"svg": SVG_NS, "xlink": XLINK_NS}
    items = []
    for el in root.findall(".//svg:image", ns):
        href = el.get(f"{{{XLINK_NS}}}href") or el.get("href") or ""
        x = _num(el.get("x") or "0")
        y = _num(el.get("y") or "0")
        w = _num(el.get("width") or "0")
        h = _num(el.get("height") or "0")
        items.append((href, x, y, w, h))
    return (view_w, view_h), items


def load_href_png_bytes(href: str, base_dir: Path) -> Optional[bytes]:
    if not href:
        return None
    if href.startswith("data:"):
        try:
            comma = href.find(",")
            if comma >= 0:
                return base64.b64decode(href[comma + 1:])
        except Exception:
            return None
    # file path next to SVG (compat mode)
    p = (base_dir / href)
    try:
        if p.exists() and p.is_file():
            return p.read_bytes()
    except Exception:
        return None
    return None


def crop_from_proof_mm(proof_path: Path,
                       x_mm: float, y_mm: float, w_mm: float, h_mm: float,
                       grid_w_mm: float, grid_h_mm: float,
                       offset_x_mm: float, offset_y_mm: float) -> Optional[bytes]:
    if Image is None:
        return None
    try:
        im = Image.open(str(proof_path)).convert("L")  # type: ignore
        px_w, px_h = im.size
        sx = px_w / grid_w_mm if grid_w_mm > 0 else 1.0
        sy = px_h / grid_h_mm if grid_h_mm > 0 else 1.0

        left = int(round((x_mm - offset_x_mm) * sx))
        top  = int(round((y_mm - offset_y_mm) * sy))
        right = int(round((x_mm - offset_x_mm + w_mm) * sx))
        bottom= int(round((y_mm - offset_y_mm + h_mm) * sy))

        left = max(0, left)
        top = max(0, top)
        right = min(px_w, right)
        bottom = min(px_h, bottom)
        if right <= left or bottom <= top:
            return None

        crop = im.crop((left, top, right, bottom))
        from io import BytesIO
        bio = BytesIO()
        crop.save(bio, format="PNG")
        return bio.getvalue()
    except Exception:
        return None


# ---------------- PDF input (optional) ----------------

def tiles_from_pdf(pdf_path: Path,
                   svg_for_geometry: Optional[Path],
                   json_path: Optional[Path],
                   slides_dir: Path,
                   verbose: bool,
                   dpi: int = 600) -> Tuple[Tuple[float, float], List[Tuple[str, float, float, float, float, bytes]]]:
    """
    Returns: ((slide_w_mm, slide_h_mm), [(name, x,y,w,h, png_bytes), ...])
    If svg_for_geometry is provided, use its <image> coordinates.
    Else require json_path to provide grid_side, qr_mm, offset_mm to synthesize tile boxes.
    """
    if not _HAVE_FITZ:
        raise RuntimeError("PDF decoding requires PyMuPDF (pip install pymupdf).")

    if svg_for_geometry and svg_for_geometry.exists():
        (view_w, view_h), items = parse_svg(svg_for_geometry)
        boxes = [(f"tile_{i:06d}", x, y, w, h) for i, (_, x, y, w, h) in enumerate(items)]
    else:
        if not json_path or not json_path.exists():
            raise RuntimeError("PDF decoding without SVG geometry requires slide JSON (slide_XXXXX.json).")
        J = json.loads(json_path.read_text(encoding="utf-8"))
        grid_side = int(J.get("grid_side"))
        qr_mm = float(J.get("qr_mm"))
        offset_mm = float(J.get("offset_mm", 0.0))
        view_w = float(J.get("slide_mm", 93.0))
        view_h = float(J.get("slide_mm", 93.0))
        boxes = []
        i = 0
        for r in range(grid_side):
            for c in range(grid_side):
                x = offset_mm + c * qr_mm
                y = offset_mm + r * qr_mm
                boxes.append((f"tile_{i:06d}", x, y, qr_mm, qr_mm))
                i += 1

    mm_to_pt = 72.0 / 25.4
    doc = fitz.open(str(pdf_path))  # type: ignore
    page = doc[0]
    # compute scale via dpi
    zoom = dpi / 72.0
    items_out = []
    for name, x, y, w, h in boxes:
        # PDF coord origin bottom-left
        x0 = x * mm_to_pt
        y0 = (view_h - (y + h)) * mm_to_pt
        x1 = (x + w) * mm_to_pt
        y1 = (view_h - y) * mm_to_pt
        clip = fitz.Rect(x0, y0, x1, y1)  # type: ignore
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip, alpha=False)  # type: ignore
        png_bytes = pix.tobytes("png")
        items_out.append((name, x, y, w, h, png_bytes))
    doc.close()
    return (view_w, view_h), items_out


# ---------------- GDS/OAS (experimental; 2x2 panes) ----------------

def _raster_from_module_matrix(bits: List[List[int]], scale: int = 6) -> bytes:
    # Create PNG bytes from 0/1 matrix using OpenCV if available, else Pillow.
    h = len(bits)
    w = len(bits[0]) if h else 0
    if w == 0:
        return b""
    import numpy as np  # type: ignore
    mat = np.zeros((h * scale, w * scale), dtype=np.uint8)
    for r in range(h):
        row = bits[r]
        for c in range(w):
            if row[c]:
                mat[r * scale:(r + 1) * scale, c * scale:(c + 1) * scale] = 0
            else:
                mat[r * scale:(r + 1) * scale, c * scale:(c + 1) * scale] = 255
    if _HAVE_OPENCV:
        ok, buf = cv2.imencode(".png", mat)  # type: ignore
        if ok:
            return bytes(buf)
    if _HAVE_PIL:
        from io import BytesIO
        im = Image.fromarray(mat, mode="L")  # type: ignore
        bio = BytesIO()
        im.save(bio, format="PNG")
        return bio.getvalue()
    raise RuntimeError("Need OpenCV or Pillow to rasterize module matrix.")


def _bits_from_gds_rectangles(polys, tile_x0_um: float, tile_y0_um: float,
                             tile_w_um: float, tile_h_um: float,
                             modules: int, quiet_modules: int) -> List[List[int]]:
    # QR modules matrix including quiet zone. Mark black where rectangles cover a module.
    bits = [[0] * modules for _ in range(modules)]
    pitch_um = tile_w_um / modules
    x1 = tile_x0_um + tile_w_um
    y1 = tile_y0_um + tile_h_um

    for p in polys:
        pts = p.points
        xs = [pt[0] for pt in pts]
        ys = [pt[1] for pt in pts]
        bx0, bx1 = min(xs), max(xs)
        by0, by1 = min(ys), max(ys)

        # Skip shapes outside tile
        if bx1 <= tile_x0_um or bx0 >= x1 or by1 <= tile_y0_um or by0 >= y1:
            continue

        # Skip likely border/background rectangles
        if (bx1 - bx0) > tile_w_um * 0.90 and (by1 - by0) > tile_h_um * 0.90:
            continue

        # clamp to tile
        bx0 = max(tile_x0_um, bx0)
        by0 = max(tile_y0_um, by0)
        bx1 = min(x1, bx1)
        by1 = min(y1, by1)

        c0 = int(round((bx0 - tile_x0_um) / pitch_um))
        c1 = int(round((bx1 - tile_x0_um) / pitch_um))
        r0 = int(round((by0 - tile_y0_um) / pitch_um))
        r1 = int(round((by1 - tile_y0_um) / pitch_um))

        c0 = max(0, min(modules, c0))
        c1 = max(0, min(modules, c1))
        r0 = max(0, min(modules, r0))
        r1 = max(0, min(modules, r1))
        if c1 <= c0 or r1 <= r0:
            continue

        for rr in range(r0, r1):
            row = bits[rr]
            for cc in range(c0, c1):
                row[cc] = 1
    return bits


def tiles_from_gds_2x2(path: Path, quiet_modules: int, verbose: bool) -> Tuple[Tuple[float, float], List[Tuple[str, float, float, float, float, bytes]]]:
    """
    Treat input as a 2×2 pane produced by slice_slide_2x2_v2.py.
    Returns: ((pane_w_mm, pane_h_mm), [(name, x,y,w,h,png_bytes), ...])
    """
    if not _HAVE_GDSTK:
        raise RuntimeError("GDS/OAS decode requires gdstk (pip install gdstk).")

    lib = gdstk.read_gds(str(path)) if path.suffix.lower() == ".gds" else gdstk.read_oas(str(path))  # type: ignore
    cells = list(lib.cells)
    if not cells:
        raise RuntimeError("No cells found in GDS/OAS.")
    cell = cells[-1]
    bbox = cell.bounding_box()
    if bbox is None:
        raise RuntimeError("No geometry found in GDS/OAS cell.")
    (x0, y0), (x1, y1) = bbox
    pane_w_um = float(x1 - x0)
    pane_h_um = float(y1 - y0)
    pane_w_mm = pane_w_um / 1000.0
    pane_h_mm = pane_h_um / 1000.0
    qr_w_um = pane_w_um / 2.0
    qr_h_um = pane_h_um / 2.0

    modules = QR_V40_MODULES + 2 * quiet_modules
    polys = list(cell.polygons)

    items = []
    k = 0
    for r in range(2):
        for c in range(2):
            tx0 = x0 + c * qr_w_um
            ty0 = y0 + r * qr_h_um
            bits = _bits_from_gds_rectangles(polys, tx0, ty0, qr_w_um, qr_h_um, modules=modules, quiet_modules=quiet_modules)
            pngb = _raster_from_module_matrix(bits, scale=6)
            items.append((f"tile_{k:03d}", c * (qr_w_um / 1000.0), r * (qr_h_um / 1000.0), qr_w_um / 1000.0, qr_h_um / 1000.0, pngb))
            k += 1
    return (pane_w_mm, pane_h_mm), items


# ---------------- Rebuild core ----------------

def infer_slide_list(slides_dir: Path, fmt: str, glob_pat: Optional[str] = None) -> List[Path]:
    if fmt == "svg":
        pat = glob_pat or "slide_*.svg"
        return sorted(slides_dir.glob(pat))
    if fmt == "pdf":
        pat = glob_pat or "slide_*.pdf"
        return sorted(slides_dir.glob(pat))
    if fmt == "gds":
        pat = glob_pat or "*.gds"
        return sorted(slides_dir.glob(pat))
    if fmt == "oas":
        pat = glob_pat or "*.oas"
        return sorted(slides_dir.glob(pat))

    # auto
    svgs = sorted(slides_dir.glob(glob_pat or "slide_*.svg"))
    if svgs:
        return svgs
    pdfs = sorted(slides_dir.glob("slide_*.pdf"))
    if pdfs:
        return pdfs
    gds = sorted(slides_dir.glob("*.gds"))
    if gds:
        return gds
    oas = sorted(slides_dir.glob("*.oas"))
    return oas


def slide_id_from_path(p: Path) -> str:
    m = re.search(r"(\d+)", p.stem)
    return m.group(1) if m else p.stem


def add_header_info(files: Dict[str, dict], obj: dict):
    # Expect header_main/header_stat JSON from qrfs_cli.py output
    t = obj.get("type") or obj.get("kind") or ""
    if t not in ("header_main", "header_stat", "footer_main", "footer_stat"):
        return
    fid = obj.get("overall_sha256") or obj.get("file_id") or obj.get("file_id_hex")
    if not fid:
        return
    f = files.get(fid) or {"blocks": {}, "basename": None, "overall": None, "total": 0, "bad_blocks": 0,
                           "parity": {}, "seen_headers": set(), "seen_footers": set()}
    if obj.get("basename"):
        f["basename"] = obj.get("basename")
    if obj.get("overall_sha256"):
        f["overall"] = obj.get("overall_sha256")
    if obj.get("total_blocks") is not None:
        f["total"] = max(int(obj.get("total_blocks")), int(f.get("total") or 0))
    if t.startswith("header"):
        f["seen_headers"].add(t)
    if t.startswith("footer"):
        f["seen_footers"].add(t)
    files[fid] = f


def save_bundle_tile(bundle_dir: Path, kind: str, obj: Optional[dict], img_bytes: bytes):
    try:
        bundle_dir.mkdir(parents=True, exist_ok=True)
        if kind == "data" and obj:
            idx = int(obj["block_index"])
            (bundle_dir / f"block_{idx:06d}.qr").write_bytes(img_bytes)
        elif kind == "parity" and obj:
            sidx = int(obj["stripe_index"])
            pidx = int(obj["parity_index"])
            (bundle_dir / f"parity_stripe_{sidx:06d}_p{pidx}.qr").write_bytes(img_bytes)
        elif kind == "json" and obj:
            t = obj.get("type") or obj.get("kind") or "json"
            (bundle_dir / f"{t}.qr").write_bytes(img_bytes)
        else:
            # unknown/other
            n = int(time.time() * 1000) % 10_000_000
            (bundle_dir / f"unknown_{n}.qr").write_bytes(img_bytes)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Rebuild QRFS files from one or more slide artifacts.")
    ap.add_argument("--slides-dir", required=True, help="Directory containing slide_*.svg (recommended) and/or other formats")
    ap.add_argument("--out", required=True, help="Output directory for recovered files")
    ap.add_argument("--format", choices=["auto", "svg", "pdf", "gds", "oas"], default="auto",
                    help="Input format. auto prefers SVG if present.")
    ap.add_argument("--glob", default=None, help="Optional glob pattern for selecting slides (depends on --format)")
    ap.add_argument("--max-slides", type=int, default=0, help="Optional limit (for testing)")
    ap.add_argument("--decoder", choices=["auto", "opencv", "pyzbar"], default="auto")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--check-block-sha", action="store_true", help="Validate SHA-256 embedded in each data block payload")
    ap.add_argument("--reject-bad-blocks", action="store_true", help="If a block fails embedded SHA, discard it")
    ap.add_argument("--emit-qr-bundles", action="store_true",
                    help="Also reconstruct a .qrfs-style bundle directory per file (writes .qr images as decoded from slide)")

    # proof fallback (SVG only)
    ap.add_argument("--use-proof", action="store_true", help="If SVG href isn't data URI, crop tiles from proof PNG")
    ap.add_argument("--proof", help="Optional proof PNG path for single-slide runs; otherwise proof_<id>.png is auto-detected")

    # PDF params
    ap.add_argument("--pdf-dpi", type=int, default=600, help="PDF render DPI (tile clip rendering).")

    # GDS/OAS params (2x2 panes)
    ap.add_argument("--quiet-modules", type=int, default=4, help="Quiet zone modules used in writer (default 4).")
    ap.add_argument("--force-full-gds", action="store_true",
                    help="Allow attempting decode on non-2x2 GDS/OAS (can be huge / slow).")

    args = ap.parse_args()

    slides_dir = Path(args.slides_dir)
    if not slides_dir.exists():
        eprint("[ERROR] slides-dir not found:", slides_dir)
        return 2

    slide_paths = infer_slide_list(slides_dir, args.format, args.glob)
    if not slide_paths:
        eprint("[ERROR] No slide inputs found in", slides_dir, "for format", args.format)
        return 2

    if args.max_slides and args.max_slides > 0:
        slide_paths = slide_paths[:args.max_slides]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    files: Dict[str, dict] = {}
    stats = {"slides": 0, "tiles": 0, "decoded": 0, "unknown": 0, "data": 0, "parity": 0, "json": 0, "other": 0, "bad_blocks": 0}

    # Process each slide/pane
    for sp in slide_paths:
        sid = slide_id_from_path(sp)
        stats["slides"] += 1
        dprint(args.verbose, f"\n== Slide {stats['slides']}/{len(slide_paths)}: {sp.name} ==")

        if sp.suffix.lower() == ".svg":
            (view_w, view_h), items = parse_svg(sp)
            total = len(items)
            stats["tiles"] += total

            # geometry helpers (for proof crop)
            offset_mm = 0.0
            grid_side = None
            qr_mm = None
            jsonp = slides_dir / f"{sp.stem}.json"
            if jsonp.exists():
                try:
                    J = json.loads(jsonp.read_text(encoding="utf-8"))
                    offset_mm = float(J.get("offset_mm", 0.0))
                    grid_side = safe_int(J.get("grid_side"), 0) or None
                    qr_mm = float(J.get("qr_mm")) if J.get("qr_mm") is not None else None
                except Exception:
                    pass

            if offset_mm == 0.0 and items:
                offset_mm = min([x for _, x, _, _, _ in items])
            if qr_mm is None and items:
                ws = sorted([w for *_, w, _ in items if w > 0])
                qr_mm = ws[len(ws) // 2] if ws else 0.25
            if grid_side is None and qr_mm:
                xs = [x for _, x, _, _, _ in items]
                cols = sorted(set(int(round((x - offset_mm) / qr_mm)) for x in xs))
                grid_side = (cols[-1] + 1) if cols else 0
            grid_w_mm = (grid_side or 0) * (qr_mm or 0.0)
            grid_h_mm = grid_w_mm

            proof_path = None
            if args.use_proof:
                if args.proof:
                    proof_path = Path(args.proof)
                else:
                    guess = slides_dir / f"proof_{sid}.png"
                    if guess.exists():
                        proof_path = guess

            t0 = time.time()
            for i, (href, x, y, w, h) in enumerate(items, 1):
                img_bytes = load_href_png_bytes(href, sp.parent)
                if img_bytes is None and args.use_proof and proof_path is not None and proof_path.exists():
                    img_bytes = crop_from_proof_mm(proof_path, x, y, w, h, grid_w_mm, grid_h_mm, offset_mm, offset_mm)

                if img_bytes is None:
                    stats["unknown"] += 1
                    if args.verbose:
                        dprint(True, "[WARN] unresolved tile href:", href[:64] + ("..." if len(href) > 64 else ""))
                    if i % 200 == 0 or i == total:
                        progress_bar("decode", i, total, t0)
                    continue

                payload = decode_qr_from_png_bytes(img_bytes, decoder=args.decoder)
                if payload is None:
                    stats["unknown"] += 1
                    if args.verbose:
                        dprint(True, "[DECFAIL] tile", i, "href:", href[:64] + ("..." if len(href) > 64 else ""))
                    if i % 200 == 0 or i == total:
                        progress_bar("decode", i, total, t0)
                    continue

                kind, obj = parse_qrfs_payload(payload, check_block_sha=args.check_block_sha)
                stats["decoded"] += 1
                stats[kind] = stats.get(kind, 0) + 1

                # bookkeeping
                if kind == "json" and obj:
                    add_header_info(files, obj)
                    # manifest/json may not map to a file id; ignore
                elif kind == "data" and obj:
                    fid = obj["file_id_hex"]
                    f = files.get(fid) or {"blocks": {}, "basename": None, "overall": None, "total": 0, "bad_blocks": 0,
                                           "parity": {}, "seen_headers": set(), "seen_footers": set()}
                    f["total"] = max(int(obj["total_blocks"]), int(f.get("total") or 0))
                    bidx = int(obj["block_index"])
                    if args.check_block_sha and not obj.get("block_sha_ok", True):
                        stats["bad_blocks"] += 1
                        f["bad_blocks"] = int(f.get("bad_blocks") or 0) + 1
                        if args.reject_bad_blocks:
                            files[fid] = f
                            if i % 200 == 0 or i == total:
                                progress_bar("decode", i, total, t0)
                            continue
                    if bidx not in f["blocks"]:
                        f["blocks"][bidx] = obj["data"]
                    files[fid] = f
                elif kind == "parity" and obj:
                    fid = obj["file_id_hex"]
                    f = files.get(fid) or {"blocks": {}, "basename": None, "overall": None, "total": 0, "bad_blocks": 0,
                                           "parity": {}, "seen_headers": set(), "seen_footers": set()}
                    key = (int(obj["stripe_index"]), int(obj["parity_index"]))
                    if key not in f["parity"]:
                        f["parity"][key] = obj["data"]
                    files[fid] = f

                # optional bundle emit
                if args.emit_qr_bundles:
                    # Determine which bundle dir to write into (best-effort)
                    bundle_key = None
                    if kind == "data" and obj:
                        bundle_key = obj["file_id_hex"]
                    elif kind == "parity" and obj:
                        bundle_key = obj["file_id_hex"]
                    elif kind == "json" and obj:
                        bundle_key = obj.get("overall_sha256") or obj.get("file_id") or obj.get("file_id_hex")
                    if bundle_key:
                        bdir = out_dir / f"{bundle_key}.qrfs"
                        save_bundle_tile(bdir, kind, obj, img_bytes)

                if i % 200 == 0 or i == total:
                    progress_bar("decode", i, total, t0)

        elif sp.suffix.lower() == ".pdf":
            svg_geom = slides_dir / f"{sp.stem}.svg"
            jsonp = slides_dir / f"{sp.stem}.json"
            (view_w, view_h), items = tiles_from_pdf(sp, svg_geom if svg_geom.exists() else None,
                                                     jsonp if jsonp.exists() else None,
                                                     slides_dir, args.verbose, dpi=args.pdf_dpi)
            total = len(items)
            stats["tiles"] += total
            t0 = time.time()

            for i, (name, x, y, w, h, img_bytes) in enumerate(items, 1):
                payload = decode_qr_from_png_bytes(img_bytes, decoder=args.decoder)
                if payload is None:
                    stats["unknown"] += 1
                    if args.verbose:
                        dprint(True, "[DECFAIL-PDF]", name)
                    if i % 50 == 0 or i == total:
                        progress_bar("decode", i, total, t0)
                    continue

                kind, obj = parse_qrfs_payload(payload, check_block_sha=args.check_block_sha)
                stats["decoded"] += 1
                stats[kind] = stats.get(kind, 0) + 1

                if kind == "json" and obj:
                    add_header_info(files, obj)
                elif kind == "data" and obj:
                    fid = obj["file_id_hex"]
                    f = files.get(fid) or {"blocks": {}, "basename": None, "overall": None, "total": 0, "bad_blocks": 0,
                                           "parity": {}, "seen_headers": set(), "seen_footers": set()}
                    f["total"] = max(int(obj["total_blocks"]), int(f.get("total") or 0))
                    bidx = int(obj["block_index"])
                    if args.check_block_sha and not obj.get("block_sha_ok", True):
                        stats["bad_blocks"] += 1
                        f["bad_blocks"] = int(f.get("bad_blocks") or 0) + 1
                        if args.reject_bad_blocks:
                            files[fid] = f
                            if i % 50 == 0 or i == total:
                                progress_bar("decode", i, total, t0)
                            continue
                    if bidx not in f["blocks"]:
                        f["blocks"][bidx] = obj["data"]
                    files[fid] = f
                elif kind == "parity" and obj:
                    fid = obj["file_id_hex"]
                    f = files.get(fid) or {"blocks": {}, "basename": None, "overall": None, "total": 0, "bad_blocks": 0,
                                           "parity": {}, "seen_headers": set(), "seen_footers": set()}
                    key = (int(obj["stripe_index"]), int(obj["parity_index"]))
                    if key not in f["parity"]:
                        f["parity"][key] = obj["data"]
                    files[fid] = f

                if i % 50 == 0 or i == total:
                    progress_bar("decode", i, total, t0)

        elif sp.suffix.lower() in (".gds", ".oas"):
            # Experimental: assume it's a 2x2 pane unless --force-full-gds is set.
            if not args.force_full_gds:
                (view_w, view_h), items = tiles_from_gds_2x2(sp, quiet_modules=args.quiet_modules, verbose=args.verbose)
            else:
                raise RuntimeError("Full-slide GDS/OAS decode is not implemented (too large). Use SVG input instead.")

            total = len(items)
            stats["tiles"] += total
            t0 = time.time()
            for i, (name, x, y, w, h, img_bytes) in enumerate(items, 1):
                payload = decode_qr_from_png_bytes(img_bytes, decoder=args.decoder)
                if payload is None:
                    stats["unknown"] += 1
                    if args.verbose:
                        dprint(True, "[DECFAIL-GDS]", name)
                    progress_bar("decode", i, total, t0)
                    continue
                kind, obj = parse_qrfs_payload(payload, check_block_sha=args.check_block_sha)
                stats["decoded"] += 1
                stats[kind] = stats.get(kind, 0) + 1

                if kind == "json" and obj:
                    add_header_info(files, obj)
                elif kind == "data" and obj:
                    fid = obj["file_id_hex"]
                    f = files.get(fid) or {"blocks": {}, "basename": None, "overall": None, "total": 0, "bad_blocks": 0,
                                           "parity": {}, "seen_headers": set(), "seen_footers": set()}
                    f["total"] = max(int(obj["total_blocks"]), int(f.get("total") or 0))
                    bidx = int(obj["block_index"])
                    if args.check_block_sha and not obj.get("block_sha_ok", True):
                        stats["bad_blocks"] += 1
                        f["bad_blocks"] = int(f.get("bad_blocks") or 0) + 1
                        if args.reject_bad_blocks:
                            files[fid] = f
                            progress_bar("decode", i, total, t0)
                            continue
                    if bidx not in f["blocks"]:
                        f["blocks"][bidx] = obj["data"]
                    files[fid] = f
                elif kind == "parity" and obj:
                    fid = obj["file_id_hex"]
                    f = files.get(fid) or {"blocks": {}, "basename": None, "overall": None, "total": 0, "bad_blocks": 0,
                                           "parity": {}, "seen_headers": set(), "seen_footers": set()}
                    key = (int(obj["stripe_index"]), int(obj["parity_index"]))
                    if key not in f["parity"]:
                        f["parity"][key] = obj["data"]
                    files[fid] = f
                progress_bar("decode", i, total, t0)

        else:
            dprint(args.verbose, "[WARN] skipping unsupported:", sp)
            continue

    # Rehydrate files
    wrote = 0
    for fid, f in files.items():
        total_blocks = int(f.get("total") or 0)
        have = len(f.get("blocks") or {})
        if total_blocks <= 0:
            dprint(True, f"[WARN] {fid[:16]}… no total_blocks; have={have}")
            continue
        missing = [i for i in range(total_blocks) if i not in f["blocks"]]
        if missing:
            eprint(f"[WARN] incomplete file {fid}: missing {len(missing)} blocks (tb={total_blocks}, have={have})")
            # parity reconstruction not implemented here
            continue

        data = b"".join(f["blocks"][i] for i in range(total_blocks))
        sha = hashlib.sha256(data).hexdigest()
        name = f.get("basename") or (fid + ".bin")
        out_path = out_dir / name
        out_path.write_bytes(data)
        chk = "OK" if (not f.get("overall") or f["overall"] == sha) else "MISMATCH"
        print(f"[WRITE] {out_path}  size={len(data)}  sha256={sha}  check={chk}")
        wrote += 1

    print(f"\n[STATS] slides={stats['slides']} tiles={stats['tiles']} decoded={stats['decoded']} "
          f"json={stats.get('json',0)} data={stats.get('data',0)} parity={stats.get('parity',0)} "
          f"bad_blocks={stats.get('bad_blocks',0)} unknown={stats['unknown']} files_ok={wrote}")
    return 0 if wrote > 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        eprint("[ERROR]", str(e))
        sys.exit(2)
