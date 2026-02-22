#!/usr/bin/env python3
"""
slice_slide_2x2.py

Take a full slide SVG produced by create_slide_qrfs_v2/v3.py and split it into many
mini-artifacts, each containing a 2×2 block of QR codes (4 tiles).

Outputs (per pane, row-major):
- <out>/<prefix>_2x2_<seq>.svg
- Optional: <out>/<prefix>_2x2_<seq>.pdf      (if --pdf)
- Optional: <out>/<prefix>_2x2_<seq>.gds      (if --gds)
- Optional: <out>/<prefix>_2x2_<seq>.oas      (if --oas)
- Optional manifest JSON mapping each seq -> original grid position + slide coordinates (+ output filenames).

Notes
- GDSII/OASIS export converts each tile image into *module rectangles* (vector) at litho scale.
  This requires Pillow (preferred) or OpenCV for reading tile images, and gdstk (preferred) or gdspy (GDS-only).
- Exporting PDF/GDS/OAS for a full 93mm slide can create *a lot* of output files. Use --max to test first.

Example:
  py -u slice_slide_2x2.py --svg Z:\\slide_out\\slide_00001.svg --out Z:\\slide_out\\chunks --manifest --pdf --gds

"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Dict, Tuple, List, Optional

# ---- Optional deps ----
_HAVE_PIL = False
_HAVE_OPENCV = False
_HAVE_CAIROSVG = False
_HAVE_SVGLIB = False
_HAVE_REPORTLAB = False
_HAVE_GDSTK = False
_HAVE_GDSPY = False

try:
    from PIL import Image  # type: ignore
    _HAVE_PIL = True
except Exception:
    Image = None  # type: ignore

try:
    import cv2  # type: ignore
    _HAVE_OPENCV = True
except Exception:
    cv2 = None  # type: ignore

try:
    import cairosvg  # type: ignore
    _HAVE_CAIROSVG = True
except Exception:
    cairosvg = None  # type: ignore

try:
    from svglib.svglib import svg2rlg  # type: ignore
    from reportlab.graphics import renderPDF  # type: ignore
    _HAVE_SVGLIB = True
except Exception:
    svg2rlg = None  # type: ignore
    renderPDF = None  # type: ignore

try:
    from reportlab.pdfgen import canvas  # type: ignore
    from reportlab.lib.units import mm as _rl_mm  # type: ignore
    from reportlab.lib.utils import ImageReader  # type: ignore
    import io
    _HAVE_REPORTLAB = True
except Exception:
    canvas = None  # type: ignore
    _rl_mm = None  # type: ignore
    ImageReader = None  # type: ignore
    io = None  # type: ignore

try:
    import gdstk  # type: ignore
    _HAVE_GDSTK = True
except Exception:
    gdstk = None  # type: ignore

try:
    import gdspy  # type: ignore
    _HAVE_GDSPY = True
except Exception:
    gdspy = None  # type: ignore


SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

QR_V40_MODULES = 177


def die(msg: str, code: int = 2):
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(code)


def progress_bar(prefix: str, i: int, total: int, t0: float, width: int = 40):
    if total <= 0:
        return
    dt = max(1e-6, time.time() - t0)
    rate = i / dt
    pct = int(i * 100 / total)
    done = int(width * (i / total))
    bar = "#" * done + "-" * (width - done)
    end = "" if i < total else "\n"
    print(f"\r{prefix} [{bar}] {pct:3d}% ({i}/{total}) {rate:7.1f}/s", end=end, file=sys.stdout, flush=True)


def get_href(el: ET.Element) -> str:
    return el.get(f"{{{XLINK_NS}}}href") or el.get("href") or ""


def parse_float(s: Optional[str]) -> float:
    if not s:
        return 0.0
    s = s.strip()
    s = re.sub(r"[^0-9.\-]+", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0


def is_windows_abs(p: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", p)) or p.startswith("\\\\")


def resolve_href(href: str, base_dir: Path) -> Path:
    # href in SVG is usually relative to the slide SVG directory.
    if href.startswith("data:"):
        raise RuntimeError("data: URIs are not supported for GDS/OAS export.")
    if is_windows_abs(href):
        return Path(href)
    # Handle file:// URLs
    if href.startswith("file://"):
        h = href[7:]
        return Path(h)
    return (base_dir / href).resolve()


def parse_slide(svg_path: Path):
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    tree = ET.parse(str(svg_path))
    root = tree.getroot()

    vb = root.get("viewBox")
    slide_w = slide_h = None
    if vb:
        parts = re.split(r"[\s,]+", vb.strip())
        if len(parts) == 4:
            slide_w = parse_float(parts[2])
            slide_h = parse_float(parts[3])
    if slide_w is None or slide_h is None:
        slide_w = parse_float(root.get("width") or "0")
        slide_h = parse_float(root.get("height") or "0")

    images = []
    for el in root.findall(f".//{{{SVG_NS}}}image"):
        href = get_href(el)
        x = parse_float(el.get("x") or "0")
        y = parse_float(el.get("y") or "0")
        w = parse_float(el.get("width") or "0")
        h = parse_float(el.get("height") or "0")
        images.append((href, x, y, w, h))

    if not images:
        die("No <image> elements found in SVG. Is this a slide SVG?")

    return slide_w, slide_h, images


def build_grid(images):
    ws = sorted([w for _, _, _, w, _ in images if w > 0])
    if not ws:
        die("Could not determine tile width (qr_mm).")
    qr_mm = ws[len(ws) // 2]

    xs = [x for _, x, _, _, _ in images]
    ys = [y for _, _, y, _, _ in images]
    x0 = min(xs) if xs else 0.0
    y0 = min(ys) if ys else 0.0

    def col_of(x: float) -> int:
        return int(round((x - x0) / qr_mm))

    def row_of(y: float) -> int:
        return int(round((y - y0) / qr_mm))

    grid: Dict[Tuple[int, int], str] = {}
    max_r = 0
    max_c = 0
    for href, x, y, w, h in images:
        r = row_of(y)
        c = col_of(x)
        grid[(r, c)] = href
        if r > max_r:
            max_r = r
        if c > max_c:
            max_c = c

    rows = max_r + 1
    cols = max_c + 1
    return qr_mm, x0, y0, rows, cols, grid


def write_chunk_svg(path: Path, qr_mm: float, hrefs: List[Optional[str]]):
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)

    size = 2 * qr_mm
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "version": "1.1",
            "width": f"{size:.6f}mm",
            "height": f"{size:.6f}mm",
            "viewBox": f"0 0 {size:.6f} {size:.6f}",
        },
    )

    positions = [
        (0.0, 0.0, hrefs[0]),
        (qr_mm, 0.0, hrefs[1]),
        (0.0, qr_mm, hrefs[2]),
        (qr_mm, qr_mm, hrefs[3]),
    ]
    for x, y, href in positions:
        if not href:
            continue
        ET.SubElement(
            root,
            f"{{{SVG_NS}}}image",
            {
                f"{{{XLINK_NS}}}href": href,
                "x": f"{x:.6f}",
                "y": f"{y:.6f}",
                "width": f"{qr_mm:.6f}",
                "height": f"{qr_mm:.6f}",
                "preserveAspectRatio": "none",
            },
        )

    path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")


# ---------------- PDF export ----------------

def export_pdf_from_svg(svg_path: Path, pdf_path: Path) -> bool:
    if _HAVE_CAIROSVG:
        try:
            cairosvg.svg2pdf(url=str(svg_path), write_to=str(pdf_path))  # type: ignore
            return True
        except Exception:
            return False
    if _HAVE_SVGLIB:
        try:
            drawing = svg2rlg(str(svg_path))  # type: ignore
            renderPDF.drawToFile(drawing, str(pdf_path))  # type: ignore
            return True
        except Exception:
            return False
    return False


def export_pdf_from_tiles(pdf_path: Path, size_mm: float, qr_mm: float, hrefs: List[Optional[str]], base_dir: Path) -> bool:
    if not _HAVE_REPORTLAB:
        return False
    # Place the 4 tile images on a 2*qr_mm page.
    c = canvas.Canvas(str(pdf_path), pagesize=(size_mm * _rl_mm, size_mm * _rl_mm))  # type: ignore
    # Coordinates: PDF origin bottom-left; pane origin top-left.
    coords = [
        (0.0, 0.0, hrefs[0]),
        (qr_mm, 0.0, hrefs[1]),
        (0.0, qr_mm, hrefs[2]),
        (qr_mm, qr_mm, hrefs[3]),
    ]
    for x_mm, y_mm_top, href in coords:
        if not href:
            continue
        tile_path = resolve_href(href, base_dir)
        y_mm = size_mm - (y_mm_top + qr_mm)
        try:
            data = tile_path.read_bytes()
            ir = ImageReader(io.BytesIO(data))  # type: ignore
            c.drawImage(ir, x_mm * _rl_mm, y_mm * _rl_mm, width=qr_mm * _rl_mm, height=qr_mm * _rl_mm,
                        preserveAspectRatio=False, mask=None)  # type: ignore
        except Exception:
            # red box placeholder
            c.setStrokeColorRGB(1, 0, 0)  # type: ignore
            c.rect(x_mm * _rl_mm, y_mm * _rl_mm, qr_mm * _rl_mm, qr_mm * _rl_mm, stroke=1, fill=0)  # type: ignore
            c.setStrokeColorRGB(0, 0, 0)  # type: ignore
    c.showPage()
    c.save()
    return True


# ---------------- GDS/OAS export ----------------

def _load_gray(img_path: Path):
    if _HAVE_PIL:
        im = Image.open(str(img_path)).convert("L")  # type: ignore
        return im
    if _HAVE_OPENCV:
        mat = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)  # type: ignore
        if mat is None:
            raise RuntimeError(f"cv2.imread failed for {img_path}")
        return mat
    raise RuntimeError("Need Pillow or OpenCV to read tile images (for GDS/OAS).")


def _sample_module_matrix(img_path: Path, modules: int, threshold: int) -> List[List[int]]:
    """
    Return modules x modules matrix: 1=black, 0=white (sample at module centers).
    """
    im = _load_gray(img_path)
    if _HAVE_PIL and hasattr(im, "size"):
        w, h = im.size  # type: ignore
        getpix = im.getpixel  # type: ignore
        sx = w / modules
        sy = h / modules
        out = [[0] * modules for _ in range(modules)]
        for r in range(modules):
            y = int((r + 0.5) * sy)
            if y >= h:
                y = h - 1
            for c in range(modules):
                x = int((c + 0.5) * sx)
                if x >= w:
                    x = w - 1
                out[r][c] = 1 if getpix((x, y)) < threshold else 0
        return out

    mat = im  # type: ignore
    h, w = mat.shape[:2]
    sx = w / modules
    sy = h / modules
    out = [[0] * modules for _ in range(modules)]
    for r in range(modules):
        y = int((r + 0.5) * sy)
        if y >= h:
            y = h - 1
        for c in range(modules):
            x = int((c + 0.5) * sx)
            if x >= w:
                x = w - 1
            out[r][c] = 1 if int(mat[y, x]) < threshold else 0
    return out


def export_gds_oas_for_pane(out_path: Path, kind: str, pane_name: str, size_mm: float, qr_mm: float,
                           hrefs: List[Optional[str]], base_dir: Path,
                           quiet_modules: int, layer: int, datatype: int, threshold: int) -> None:
    """
    Produce a single pane library with one cell. Units in microns.
    """
    modules = QR_V40_MODULES + 2 * quiet_modules
    pitch_mm = qr_mm / modules
    pitch_um = pitch_mm * 1000.0
    size_um = size_mm * 1000.0

    if kind == "oas" and not _HAVE_GDSTK:
        raise RuntimeError("OAS export requires gdstk (pip install gdstk).")
    if kind == "gds" and not (_HAVE_GDSTK or _HAVE_GDSPY):
        raise RuntimeError("GDS export requires gdstk or gdspy (pip install gdstk OR gdspy).")

    # helper to add rectangles in a row using run-length
    def add_tile_rects(add_rect, x0_um: float, y0_um: float, tile_path: Path):
        mat = _sample_module_matrix(tile_path, modules=modules, threshold=threshold)
        for rr in range(modules):
            row = mat[rr]
            cc = 0
            while cc < modules:
                while cc < modules and row[cc] == 0:
                    cc += 1
                if cc >= modules:
                    break
                start = cc
                while cc < modules and row[cc] == 1:
                    cc += 1
                end = cc
                rx0 = x0_um + start * pitch_um
                ry0 = y0_um + rr * pitch_um
                rx1 = x0_um + end * pitch_um
                ry1 = y0_um + (rr + 1) * pitch_um
                add_rect(rx0, ry0, rx1, ry1)

    # tile placement in pane coordinates (y down)
    coords = [
        (0.0, 0.0, hrefs[0]),
        (qr_mm, 0.0, hrefs[1]),
        (0.0, qr_mm, hrefs[2]),
        (qr_mm, qr_mm, hrefs[3]),
    ]

    if _HAVE_GDSTK:
        lib = gdstk.Library(unit=1e-6, precision=1e-9)  # microns, nm precision
        top = lib.new_cell(pane_name)
        # optional border
        top.add(gdstk.rectangle((0, 0), (size_um, size_um), layer=layer, datatype=datatype))
        def add_rect(rx0, ry0, rx1, ry1):
            top.add(gdstk.rectangle((rx0, ry0), (rx1, ry1), layer=layer, datatype=datatype))

        for x_mm, y_mm, href in coords:
            if not href:
                continue
            tile_path = resolve_href(href, base_dir)
            add_tile_rects(add_rect, x_mm * 1000.0, y_mm * 1000.0, tile_path)

        if kind == "gds":
            lib.write_gds(str(out_path))
        else:
            lib.write_oas(str(out_path))
        return

    # gdspy fallback (gds only)
    lib = gdspy.GdsLibrary(unit=1e-6, precision=1e-9)  # type: ignore
    top = lib.new_cell(pane_name)
    top.add(gdspy.Rectangle((0, 0), (size_um, size_um), layer=layer, datatype=datatype))  # type: ignore
    def add_rect(rx0, ry0, rx1, ry1):
        top.add(gdspy.Rectangle((rx0, ry0), (rx1, ry1), layer=layer, datatype=datatype))  # type: ignore

    for x_mm, y_mm, href in coords:
        if not href:
            continue
        tile_path = resolve_href(href, base_dir)
        add_tile_rects(add_rect, x_mm * 1000.0, y_mm * 1000.0, tile_path)

    lib.write_gds(str(out_path))  # type: ignore


def main():
    ap = argparse.ArgumentParser(description="Split a slide SVG into 2×2 mini panes (SVG + optional PDF/GDS/OAS).")
    ap.add_argument("--svg", required=True, help="Input full slide SVG")
    ap.add_argument("--out", required=True, help="Output directory for 2×2 panes")
    ap.add_argument("--prefix", default=None, help="Output filename prefix (default: input stem)")
    ap.add_argument("--start", type=int, default=1, help="Starting sequence number (default: 1)")
    ap.add_argument("--pad", type=int, default=8, help="Zero-pad width for sequence numbers (default: 8)")
    ap.add_argument("--manifest", action="store_true", help="Write a manifest JSON mapping seq -> original grid coords")
    ap.add_argument("--max", type=int, default=0, help="Optional: limit number of panes (for testing)")

    # Extra formats
    ap.add_argument("--pdf", action="store_true", help="Also export each pane as a PDF.")
    ap.add_argument("--gds", action="store_true", help="Also export each pane as GDSII (vector modules).")
    ap.add_argument("--oas", action="store_true", help="Also export each pane as OASIS (vector modules).")

    # GDS/OAS parameters
    ap.add_argument("--quiet-modules", type=int, default=4, help="Quiet zone modules (default 4; must match how tiles were generated).")
    ap.add_argument("--gds-layer", type=int, default=1, help="Layer number for black modules (default 1).")
    ap.add_argument("--gds-datatype", type=int, default=0, help="Datatype for black modules (default 0).")
    ap.add_argument("--gds-threshold", type=int, default=128, help="Pixel threshold (0-255) to classify black modules (default 128).")
    args = ap.parse_args()

    svg_path = Path(args.svg)
    if not svg_path.exists():
        die(f"SVG not found: {svg_path}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.prefix or svg_path.stem
    base_dir = svg_path.parent

    slide_w, slide_h, images = parse_slide(svg_path)
    qr_mm, x0, y0, rows, cols, grid = build_grid(images)

    blocks_x = (cols + 1) // 2 if (cols % 2) else (cols // 2)
    blocks_y = (rows + 1) // 2 if (rows % 2) else (rows // 2)
    total_blocks = blocks_x * blocks_y

    print(f"[INFO] slide viewBox = {slide_w:.6f} x {slide_h:.6f}")
    print(f"[INFO] grid = {rows} x {cols}  qr_mm={qr_mm:.6f}")
    print(f"[INFO] 2×2 panes = {blocks_y} x {blocks_x} = {total_blocks}")
    if args.gds or args.oas:
        if not (_HAVE_PIL or _HAVE_OPENCV):
            die("GDS/OAS requested but neither Pillow nor OpenCV is available to read tile images.")
        if args.oas and not _HAVE_GDSTK:
            die("OAS requested but gdstk is not installed. pip install gdstk")
        if args.gds and not (_HAVE_GDSTK or _HAVE_GDSPY):
            die("GDS requested but neither gdstk nor gdspy is installed. pip install gdstk  (or gdspy)")

    if args.pdf and not (_HAVE_CAIROSVG or _HAVE_SVGLIB or _HAVE_REPORTLAB):
        die("PDF requested but no PDF backend available. Install cairosvg OR svglib+reportlab OR reportlab.")

    manifest = {
        "source_svg": str(svg_path),
        "qr_mm": qr_mm,
        "grid_rows": rows,
        "grid_cols": cols,
        "panes_x": blocks_x,
        "panes_y": blocks_y,
        "pane_qr": 2,
        "output_prefix": prefix,
        "start_seq": args.start,
        "pad": args.pad,
        "formats": {
            "svg": True,
            "pdf": bool(args.pdf),
            "gds": bool(args.gds),
            "oas": bool(args.oas),
        },
        "panes": [],
    }

    seq = args.start
    wrote = 0
    t0 = time.time()
    cap = args.max or total_blocks

    for by in range(blocks_y):
        for bx in range(blocks_x):
            if wrote >= cap:
                break
            r0 = by * 2
            c0 = bx * 2
            hrefs = [
                grid.get((r0, c0)),
                grid.get((r0, c0 + 1)),
                grid.get((r0 + 1, c0)),
                grid.get((r0 + 1, c0 + 1)),
            ]

            stem = f"{prefix}_2x2_{seq:0{args.pad}d}"
            svg_name = f"{stem}.svg"
            out_svg = out_dir / svg_name
            write_chunk_svg(out_svg, qr_mm, hrefs)

            pdf_name = gds_name = oas_name = None

            # PDF export
            if args.pdf:
                out_pdf = out_dir / f"{stem}.pdf"
                ok = export_pdf_from_svg(out_svg, out_pdf)
                if not ok:
                    ok = export_pdf_from_tiles(out_pdf, size_mm=2 * qr_mm, qr_mm=qr_mm, hrefs=hrefs, base_dir=base_dir)
                if not ok:
                    die("PDF export failed (no working backend).")
                pdf_name = out_pdf.name

            # GDS export
            if args.gds:
                out_gds = out_dir / f"{stem}.gds"
                export_gds_oas_for_pane(
                    out_gds, "gds", pane_name=stem.upper(), size_mm=2 * qr_mm, qr_mm=qr_mm, hrefs=hrefs, base_dir=base_dir,
                    quiet_modules=args.quiet_modules, layer=args.gds_layer, datatype=args.gds_datatype, threshold=args.gds_threshold
                )
                gds_name = out_gds.name

            # OAS export
            if args.oas:
                out_oas = out_dir / f"{stem}.oas"
                export_gds_oas_for_pane(
                    out_oas, "oas", pane_name=stem.upper(), size_mm=2 * qr_mm, qr_mm=qr_mm, hrefs=hrefs, base_dir=base_dir,
                    quiet_modules=args.quiet_modules, layer=args.gds_layer, datatype=args.gds_datatype, threshold=args.gds_threshold
                )
                oas_name = out_oas.name

            if args.manifest:
                manifest["panes"].append(
                    {
                        "seq": seq,
                        "stem": stem,
                        "svg": svg_name,
                        "pdf": pdf_name,
                        "gds": gds_name,
                        "oas": oas_name,
                        "grid_r0": r0,
                        "grid_c0": c0,
                        "slide_x_mm": x0 + c0 * qr_mm,
                        "slide_y_mm": y0 + r0 * qr_mm,
                        "tile_present": [bool(h) for h in hrefs],
                        "hrefs": hrefs,
                    }
                )

            seq += 1
            wrote += 1
            if wrote % 250 == 0 or wrote == cap:
                progress_bar("write", wrote, cap, t0)

        if wrote >= cap:
            break

    if args.manifest:
        man_path = out_dir / f"{prefix}_2x2_manifest.json"
        man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"[INFO] wrote manifest: {man_path}")

    print(f"[DONE] wrote {wrote} pane(s) to {out_dir}")


if __name__ == "__main__":
    main()
