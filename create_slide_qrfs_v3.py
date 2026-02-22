#!/usr/bin/env python3
"""
create_slide_qrfs_v3.py — QRFS-aware slide builder (multi-slide, collision-safe, export SVG + optional PDF + optional GDSII/OASIS)

What it does
- Finds one or more *.qrfs directories (those containing header_main.qr + header_stat.qr) under --src (recursive).
- Orders QR tiles per QRFS layout: headers, then data blocks (block_######.qr) grouped into stripes of K=16,
  with parity tiles (parity_stripe_######_p#.qr) after each stripe, then footers.
- Packs one or more files onto one or more slides (as many as needed).
- For each slide:
  - slide_init marker QR
  - for each file-segment on the slide: file_open marker, headers, body chunk, optional footers, file_close marker
  - slide_close marker QR containing slide UUID + SHA256 over slide tile bytes
  - writes:
      slide_00001.svg  (vector layout with <image> hrefs)
      slide_00001.json (full mapping: row/col -> tile href + type/meta)
      proof_00001.png  (optional; only if Pillow is installed)
      slide_00001.pdf  (optional; --pdf)
      slide_00001.gds  (optional; --gds, requires gdstk or gdspy)
      slide_00001.oas  (optional; --oas, requires gdstk)

Design choices
- To avoid filename collisions (block_000000.qr appears in every file), tiles are copied into:
    <out>/slide_00001_tiles/<filetag>/...
  and the SVG href points to those per-slide tile subdirectories.
- Marker QRs are PNG images saved with a .qr extension (content is PNG).
- PDF export defaults to converting SVG -> PDF (cairosvg if available, else svglib+reportlab, else reportlab raster placement).
- GDSII/OASIS export converts each QR tile image into *module rectangles* at litho scale (nm pitch),
  using run-length rectangles per row to reduce shape count. This can be very large.

Windows note
- Uses long-path prefix for Pillow file opens where needed.

"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__VERSION__ = "3.1.0"

# Geometry defaults
DEFAULT_PIXEL_NM = 1500.0
DEFAULT_MODULE_PIXELS = 1
DEFAULT_SLIDE_MM = 93.0
DEFAULT_QUIET = 4
QR_V40_MODULES = 177  # Version 40 is 177x177 modules

# Stripe assumptions (must match qrfs_cli bundle naming)
DEFAULT_STRIPE_K = 16

# Optional libs
_HAVE_SEGNO = False
_HAVE_QRCODE = False
_HAVE_PIL = False
_HAVE_PYZBAR = False
_HAVE_OPENCV = False
_HAVE_CAIROSVG = False
_HAVE_SVGLIB = False
_HAVE_REPORTLAB = False
_HAVE_GDSTK = False
_HAVE_GDSPY = False

try:
    import segno  # type: ignore
    _HAVE_SEGNO = True
except Exception:
    pass

try:
    import qrcode  # type: ignore
    from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H  # type: ignore
    _HAVE_QRCODE = True
except Exception:
    pass

try:
    from PIL import Image, ImageDraw  # type: ignore
    _HAVE_PIL = True
except Exception:
    Image = None  # type: ignore

try:
    from pyzbar.pyzbar import decode as _zb_decode, ZBarSymbol  # type: ignore
    _HAVE_PYZBAR = True
except Exception:
    pass

try:
    import cv2  # type: ignore
    _HAVE_OPENCV = True
except Exception:
    pass

try:
    import cairosvg  # type: ignore
    _HAVE_CAIROSVG = True
except Exception:
    pass

try:
    from svglib.svglib import svg2rlg  # type: ignore
    from reportlab.graphics import renderPDF  # type: ignore
    _HAVE_SVGLIB = True
except Exception:
    pass

try:
    from reportlab.pdfgen import canvas  # type: ignore
    from reportlab.lib.units import mm as _rl_mm  # type: ignore
    from reportlab.lib.utils import ImageReader  # type: ignore
    _HAVE_REPORTLAB = True
except Exception:
    pass

try:
    import gdstk  # type: ignore
    _HAVE_GDSTK = True
except Exception:
    pass

try:
    import gdspy  # type: ignore
    _HAVE_GDSPY = True
except Exception:
    pass


# ---------- Utilities ----------

class Progress:
    def __init__(self, enabled: bool, stream_name: str = "stderr", mode: str = "auto", width: int = 40):
        self.enabled = enabled
        self.stream = sys.stderr if stream_name == "stderr" else sys.stdout
        self.mode = "bar" if mode == "auto" else mode
        self.width = width
        self.last_pct = -1

    def banner(self, text: str) -> None:
        if not self.enabled:
            return
        print(f"\n== {text} ==", file=self.stream, flush=True)

    def update(self, prefix: str, i: int, total: int) -> None:
        if not self.enabled or total <= 0:
            return
        pct = int((i * 100) // total)
        if pct == self.last_pct and (i != total):
            return
        self.last_pct = pct

        if self.mode == "bar":
            done = int(self.width * (pct / 100.0))
            bar = "#" * done + "-" * (self.width - done)
            msg = f"\r{prefix} [{bar}] {pct:3d}% ({i}/{total})"
            print(msg, end="", file=self.stream, flush=True)
            if i == total:
                print(file=self.stream, flush=True)
        else:
            print(f"{prefix} {pct:3d}% ({i}/{total})", file=self.stream, flush=True)

    def close(self) -> None:
        if self.enabled and self.mode == "bar":
            print(file=self.stream, flush=True)


def win_long(p: Path) -> str:
    x = str(p.resolve())
    if os.name == "nt" and not x.startswith("\\\\?\\"):
        return "\\\\?\\" + x
    return x


def mm_fmt(mm: float) -> str:
    return f"{mm:.6f}mm"


def compute_qr_physical_mm(pixel_nm: float, module_pixels: int, quiet_modules: int) -> float:
    # (modules + quiet zone on both sides) * pixels_per_module * pixel_size_mm
    return (QR_V40_MODULES + 2 * quiet_modules) * module_pixels * (pixel_nm / 1_000_000.0)


def compute_grid(slide_mm: float, qr_mm: float) -> Tuple[int, float]:
    side_cells = int(slide_mm // qr_mm)
    leftover = slide_mm - side_cells * qr_mm
    return side_cells, leftover


def extract_if_zip(src: Path) -> Path:
    if src.is_file() and src.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="qrfs_zip_"))
        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(tmp)
        return tmp
    return src


def find_qrfs_dirs(src: Path, max_depth: int = 0) -> List[Path]:
    """Find directories containing header_main.qr and header_stat.qr."""
    out: List[Path] = []
    src = src.resolve()
    for root, _dirs, files in os.walk(src):
        if max_depth > 0:
            rel = Path(root).relative_to(src).parts
            if len(rel) > max_depth:
                continue
        if "header_main.qr" in files and "header_stat.qr" in files:
            out.append(Path(root))
    out.sort()
    return out


def safe_rel_href(path_under_out: Path, out_dir: Path) -> str:
    # Always forward slashes for SVG hrefs.
    rel = path_under_out.relative_to(out_dir).as_posix()
    return rel.replace("&", "&amp;")


def copy_tile(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if hashlib.sha256(dst.read_bytes()).digest() == hashlib.sha256(src.read_bytes()).digest():
                return
        except Exception:
            pass
    shutil.copy2(src, dst)


# ---------- Decoding helpers (optional verification only) ----------

def decode_qr_bytes(img_path: Path) -> Optional[bytes]:
    if _HAVE_PYZBAR and _HAVE_PIL:
        try:
            from PIL import Image as _Image  # type: ignore
            img = _Image.open(win_long(img_path))
            res = _zb_decode(img, symbols=[ZBarSymbol.QRCODE])
            if res:
                return bytes(res[0].data)
        except Exception:
            pass

    if _HAVE_OPENCV:
        try:
            mat = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
            det = cv2.QRCodeDetector()
            data, _pts, _ = det.detectAndDecode(mat)
            if data:
                return data.encode("utf-8", errors="ignore")
        except Exception:
            pass
    return None


# ---------- Marker QR generation ----------

_ECC_MAP_SEGNO = {"L": "l", "M": "m", "Q": "q", "H": "h"}


def generate_marker_qr(payload: str, out_path: Path, ecc: str = "H", version: int = 40, quiet: int = DEFAULT_QUIET) -> Path:
    """
    Write a QR code PNG (but you may keep .qr extension). Returns out_path.
    Requires segno or qrcode. Raises RuntimeError if neither available.
    """
    ecc = ecc.upper()
    if _HAVE_SEGNO:
        qr = segno.make(payload, error=_ECC_MAP_SEGNO.get(ecc, "h"), version=version, micro=False, mode=None)
        qr.save(out_path, kind="png", scale=1, border=quiet, dark="black", light="white")
        return out_path

    if _HAVE_QRCODE:
        ec = {
            "L": ERROR_CORRECT_L,
            "M": ERROR_CORRECT_M,
            "Q": ERROR_CORRECT_Q,
            "H": ERROR_CORRECT_H,
        }.get(ecc, ERROR_CORRECT_H)
        qr = qrcode.QRCode(version=version, error_correction=ec, box_size=1, border=quiet)
        qr.add_data(payload)
        qr.make(fit=False)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(win_long(out_path))
        return out_path

    raise RuntimeError("No QR generator available. Install segno or qrcode.")


# ---------- QRFS ordering ----------

def gather_qrfs_order(dirp: Path, stripe_k: int = DEFAULT_STRIPE_K) -> Tuple[List[Tuple[str, Path]], Dict[str, Any]]:
    """
    Returns:
      ordered_tiles: list of (kind, path)
      meta: parsed bits when available (manifest fallback)
    """
    items: List[Tuple[str, Path]] = []

    hm = dirp / "header_main.qr"
    hs = dirp / "header_stat.qr"
    if hm.exists():
        items.append(("header_main", hm))
    if hs.exists():
        items.append(("header_stat", hs))

    # data blocks
    data_blocks = sorted(dirp.glob("block_*.qr"), key=lambda p: int(p.stem.split("_")[1]))
    stripes: Dict[int, List[Path]] = {}
    for p in data_blocks:
        bi = int(p.stem.split("_")[1])
        si = bi // stripe_k
        stripes.setdefault(si, []).append(p)

    for si in sorted(stripes.keys()):
        blocks = sorted(stripes[si], key=lambda p: int(p.stem.split("_")[1]))
        items.extend([("block", p) for p in blocks])

        pars = sorted(
            dirp.glob(f"parity_stripe_{si:06d}_p*.qr"),
            key=lambda p: int(p.stem.split("_")[-1][1:]) if p.stem.split("_")[-1].startswith("p") else 0,
        )
        items.extend([("parity", p) for p in pars])

    # footers (if present)
    fa = dirp / "footer_tagA_blockhashchain.qr"
    fb = dirp / "footer_tagB_dircontent.qr"
    if fa.exists():
        items.append(("footerA", fa))
    if fb.exists():
        items.append(("footerB", fb))

    # minimal metadata (best-effort)
    meta: Dict[str, Any] = {}
    mp = dirp / "manifest.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    return items, meta


# ---------- Slide packing ----------

@dataclass
class TileRef:
    kind: str               # "header_main", "block", "marker", ...
    href: str               # SVG href (relative path within out_dir)
    src: Optional[Path]     # original path, may be None for generated markers
    dst: Path               # where the tile lives under out_dir
    meta: Dict[str, Any]    # extra info


@dataclass
class Slide:
    index: int
    uuid: str
    tiles: List[TileRef]


class SlideBuilder:
    def __init__(
        self,
        out_dir: Path,
        slide_mm: float,
        qr_mm: float,
        side_cells: int,
        reserve_corners: bool,
        marker_ecc: str,
        marker_version: int,
        quiet_modules: int,
    ):
        self.out_dir = out_dir
        self.slide_mm = slide_mm
        self.qr_mm = qr_mm
        self.side_cells = side_cells
        self.reserve_corners = reserve_corners
        self.marker_ecc = marker_ecc
        self.marker_version = marker_version
        self.quiet_modules = quiet_modules

        total = side_cells * side_cells
        self.cap = total - (4 if reserve_corners else 0)

        self.slides: List[Slide] = []
        self._cur_slide: Optional[Slide] = None
        self._tile_dir: Optional[Path] = None

    def _start_slide(self) -> None:
        idx = len(self.slides) + 1
        su = str(uuid.uuid4())
        self._tile_dir = self.out_dir / f"slide_{idx:05d}_tiles"
        self._tile_dir.mkdir(parents=True, exist_ok=True)
        self._cur_slide = Slide(index=idx, uuid=su, tiles=[])

        # slide_init marker (always first)
        payload = json.dumps({"type": "slide_init", "slide_uuid": su, "slide_index": idx}, separators=(",", ":"), ensure_ascii=False)
        init_path = self._tile_dir / f"slide_{idx:05d}_init.qr"
        generate_marker_qr(payload, init_path, ecc=self.marker_ecc, version=self.marker_version, quiet=self.quiet_modules)
        self._cur_slide.tiles.append(TileRef(
            kind="marker",
            href=safe_rel_href(init_path, self.out_dir),
            src=None,
            dst=init_path,
            meta={"subtype": "slide_init"},
        ))

    def _remaining_for_payload(self) -> int:
        """
        Remaining usable slots on current slide, reserving 1 slot for slide_close marker.
        """
        assert self._cur_slide is not None
        used = len(self._cur_slide.tiles)
        # Reserve 1 for slide_close that we add at finalization
        return max(0, self.cap - used - 1)

    def _ensure_slide(self) -> None:
        if self._cur_slide is None:
            self._start_slide()

    def _finalize_slide(self) -> None:
        assert self._cur_slide is not None and self._tile_dir is not None
        # slide_close marker includes SHA over tile bytes already present (including init + file segments)
        sha = hashlib.sha256()
        for t in self._cur_slide.tiles:
            try:
                sha.update(Path(t.dst).read_bytes())
            except Exception:
                pass

        payload = json.dumps(
            {
                "type": "slide_close",
                "slide_uuid": self._cur_slide.uuid,
                "slide_index": self._cur_slide.index,
                "tiles": len(self._cur_slide.tiles),
                "tile_bytes_sha256": sha.hexdigest(),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        close_path = self._tile_dir / f"slide_{self._cur_slide.index:05d}_close.qr"
        generate_marker_qr(payload, close_path, ecc=self.marker_ecc, version=self.marker_version, quiet=self.quiet_modules)

        self._cur_slide.tiles.append(TileRef(
            kind="marker",
            href=safe_rel_href(close_path, self.out_dir),
            src=None,
            dst=close_path,
            meta={"subtype": "slide_close", "tile_bytes_sha256": sha.hexdigest()},
        ))

        self.slides.append(self._cur_slide)
        self._cur_slide = None
        self._tile_dir = None

    def _place_tile(self, tile: TileRef) -> None:
        self._ensure_slide()
        assert self._cur_slide is not None

        if self._remaining_for_payload() <= 0:
            self._finalize_slide()
            self._start_slide()

        self._cur_slide.tiles.append(tile)

    def _new_file_subdir(self, slide_tile_dir: Path, file_uuid: str, file_seq: int) -> Path:
        tag = f"f{file_seq:03d}_{file_uuid[:10]}"
        p = slide_tile_dir / tag
        p.mkdir(parents=True, exist_ok=True)
        return p

    def add_qrfs_dir(self, qrfs_dir: Path, file_seq: int, stripe_k: int = DEFAULT_STRIPE_K, embed_manifest: bool = False) -> None:
        """
        Adds one QRFS file directory, chunking across slides if needed.
        """
        self._ensure_slide()
        assert self._cur_slide is not None and self._tile_dir is not None

        ordered, meta = gather_qrfs_order(qrfs_dir, stripe_k=stripe_k)

        # Best-effort file identity
        file_path = ""
        overall_sha = ""
        if isinstance(meta, dict):
            file_path = meta.get("file") or ""
            overall_sha = meta.get("overall_file_sha256") or meta.get("overall_sha256") or ""
        if not file_path:
            file_path = str(qrfs_dir)
        if not overall_sha:
            overall_sha = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
        file_uuid = overall_sha

        headers: List[Tuple[str, Path]] = [(k, p) for (k, p) in ordered if k in ("header_main", "header_stat")]
        footers: List[Tuple[str, Path]] = [(k, p) for (k, p) in ordered if k in ("footerA", "footerB")]
        body: List[Tuple[str, Path]] = [(k, p) for (k, p) in ordered if k not in ("header_main", "header_stat", "footerA", "footerB")]

        manifest_chunks: List[Dict[str, Any]] = []
        if embed_manifest:
            mp = qrfs_dir / "manifest.json"
            if mp.exists():
                try:
                    raw = mp.read_bytes()
                    b64 = base64.b64encode(raw).decode("ascii")
                    # marker QR capacity is ECC-dependent; keep conservative chunks
                    CH = 1800
                    parts = [b64[i:i+CH] for i in range(0, len(b64), CH)]
                    for pi, part in enumerate(parts):
                        payload = json.dumps(
                            {"type": "manifest_b64", "file_uuid": file_uuid, "part": pi, "parts": len(parts), "b64": part},
                            separators=(",", ":"),
                            ensure_ascii=False,
                        )
                        manifest_chunks.append({"part": pi, "parts": len(parts), "payload": payload})
                except Exception:
                    manifest_chunks = []

        seg_idx = 0
        body_i = 0

        while True:
            seg_idx += 1

            # Minimal segment overhead: file_open + headers + file_close
            MIN_OVERHEAD = 2 + len(headers) + 1  # open + headers + close
            if self._remaining_for_payload() < MIN_OVERHEAD + 1:  # leave some breathing room
                self._finalize_slide()
                self._start_slide()

            assert self._cur_slide is not None and self._tile_dir is not None
            file_subdir = self._new_file_subdir(self._tile_dir, file_uuid, file_seq)

            payload_open = json.dumps(
                {
                    "type": "file_open",
                    "slide_uuid": self._cur_slide.uuid,
                    "slide_index": self._cur_slide.index,
                    "file_uuid": file_uuid,
                    "file": file_path,
                    "qrfs_dir": str(qrfs_dir),
                    "segment": seg_idx,
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            open_path = file_subdir / f"fileopen_s{seg_idx:04d}.qr"
            generate_marker_qr(payload_open, open_path, ecc=self.marker_ecc, version=self.marker_version, quiet=self.quiet_modules)
            self._place_tile(TileRef("marker", safe_rel_href(open_path, self.out_dir), None, open_path, {"subtype": "file_open", "segment": seg_idx}))

            for hk, hp in headers:
                dst = file_subdir / hp.name
                copy_tile(hp, dst)
                self._place_tile(TileRef(hk, safe_rel_href(dst, self.out_dir), hp, dst, {"segment": seg_idx}))

            rem = self._remaining_for_payload()
            remaining_body = len(body) - body_i
            final_extra = len(footers) + (len(manifest_chunks) if embed_manifest else 0)

            can_finish = (remaining_body + final_extra + 1) <= rem  # +1 for file_close
            if can_finish:
                take = remaining_body
            else:
                take = max(0, rem - 1)  # leave space for file_close

            for (bk, bp) in body[body_i: body_i + take]:
                dst = file_subdir / bp.name
                copy_tile(bp, dst)
                self._place_tile(TileRef(bk, safe_rel_href(dst, self.out_dir), bp, dst, {"segment": seg_idx}))
            body_i += take

            is_final = (body_i >= len(body))
            if is_final:
                for (fk, fp) in footers:
                    dst = file_subdir / fp.name
                    copy_tile(fp, dst)
                    self._place_tile(TileRef(fk, safe_rel_href(dst, self.out_dir), fp, dst, {"segment": seg_idx}))
                if embed_manifest and manifest_chunks:
                    for m in manifest_chunks:
                        mp = file_subdir / f"manifest_part_{m['part']:04d}.qr"
                        generate_marker_qr(m["payload"], mp, ecc=self.marker_ecc, version=self.marker_version, quiet=self.quiet_modules)
                        self._place_tile(TileRef("marker", safe_rel_href(mp, self.out_dir), None, mp, {"subtype": "manifest_b64", "part": m["part"], "parts": m["parts"], "segment": seg_idx}))

            payload_close = json.dumps(
                {
                    "type": "file_close",
                    "slide_uuid": self._cur_slide.uuid,
                    "slide_index": self._cur_slide.index,
                    "file_uuid": file_uuid,
                    "file": file_path,
                    "qrfs_dir": str(qrfs_dir),
                    "segment": seg_idx,
                    "final": bool(is_final),
                    "next_body_index": body_i if not is_final else None,
                    "total_body_items": len(body),
                },
                separators=(",", ":"),
                ensure_ascii=False,
            )
            close_path = file_subdir / f"fileclose_s{seg_idx:04d}.qr"
            generate_marker_qr(payload_close, close_path, ecc=self.marker_ecc, version=self.marker_version, quiet=self.quiet_modules)
            self._place_tile(TileRef("marker", safe_rel_href(close_path, self.out_dir), None, close_path, {"subtype": "file_close", "segment": seg_idx, "final": bool(is_final)}))

            if is_final:
                break

            # Start a new slide for the next segment of this file.
            self._finalize_slide()
            self._start_slide()

    def finish(self) -> List[Slide]:
        if self._cur_slide is not None:
            self._finalize_slide()
        return self.slides


# ---------- Rendering ----------

def make_grid(side_cells: int, tiles: List[TileRef], reserve_corners: bool) -> List[List[Optional[TileRef]]]:
    grid: List[List[Optional[TileRef]]] = [[None for _ in range(side_cells)] for _ in range(side_cells)]
    corners = {(0, 0), (0, side_cells - 1), (side_cells - 1, 0), (side_cells - 1, side_cells - 1)} if reserve_corners else set()

    it = iter(tiles)
    for r in range(side_cells):
        for c in range(side_cells):
            if (r, c) in corners:
                continue
            try:
                grid[r][c] = next(it)
            except StopIteration:
                return grid
    return grid


def draw_svg(out_svg_path: Path, slide_mm: float, qr_mm: float, side_cells: int, grid: List[List[Optional[TileRef]]], title: str) -> None:
    leftover = slide_mm - side_cells * qr_mm
    offset = leftover / 2.0 if leftover > 0 else 0.0

    svg: List[str] = []
    svg.append('<?xml version="1.0" encoding="UTF-8" standalone="no"?>')
    svg.append(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{mm_fmt(slide_mm)}" height="{mm_fmt(slide_mm)}" viewBox="0 0 {slide_mm:.6f} {slide_mm:.6f}">'
    )
    svg.append(f'<rect x="0" y="0" width="{slide_mm:.6f}" height="{slide_mm:.6f}" stroke="black" stroke-width="0.05" fill="none"/>')
    svg.append(f'<text x="1" y="3.0" font-size="2" font-family="monospace">{title.replace("&","&amp;")}</text>')

    for r in range(side_cells):
        for c in range(side_cells):
            cell = grid[r][c]
            if cell is None:
                continue
            x = offset + c * qr_mm
            y = offset + r * qr_mm
            href = cell.href
            svg.append(f'<image x="{x:.6f}" y="{y:.6f}" width="{qr_mm:.6f}" height="{qr_mm:.6f}" preserveAspectRatio="none" xlink:href="{href}" />')

    svg.append("</svg>")
    out_svg_path.write_text("\n".join(svg), encoding="utf-8")


def draw_proof_png(out_png_path: Path, proof_px: int, side_cells: int, grid: List[List[Optional[TileRef]]], progress: Progress) -> None:
    if not _HAVE_PIL:
        return
    W = H = proof_px
    cell = max(1, W // side_cells)
    W = H = cell * side_cells

    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    total = side_cells * side_cells
    n = 0
    for r in range(side_cells):
        for c in range(side_cells):
            n += 1
            progress.update("proof", n, total)

            slot = grid[r][c]
            x0 = c * cell
            y0 = r * cell

            if slot is None:
                draw.rectangle([x0, y0, x0 + cell, y0 + cell], outline=(220, 220, 220))
                continue

            try:
                tile = Image.open(win_long(slot.dst)).convert("L").resize((cell, cell))
                img.paste(tile.convert("RGB"), (x0, y0))
            except Exception:
                draw.rectangle([x0, y0, x0 + cell, y0 + cell], outline=(200, 0, 0))

    img.save(win_long(out_png_path))


def write_slide_json(out_json: Path, slide: Slide, slide_mm: float, pixel_nm: float, module_pixels: int, quiet_modules: int,
                     qr_mm: float, side_cells: int, reserve_corners: bool, grid: List[List[Optional[TileRef]]]) -> None:
    mapping: List[Dict[str, Any]] = []
    for r in range(side_cells):
        for c in range(side_cells):
            cell = grid[r][c]
            if cell is None:
                continue
            mapping.append({
                "r": r,
                "c": c,
                "href": cell.href,
                "kind": cell.kind,
                "meta": cell.meta,
            })
    mapping_sha = hashlib.sha256(json.dumps(mapping, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()

    doc = {
        "schema": "qrfs.slide.v3",
        "slide_index": slide.index,
        "slide_uuid": slide.uuid,
        "slide_mm": slide_mm,
        "pixel_nm": pixel_nm,
        "module_pixels": module_pixels,
        "quiet_modules": quiet_modules,
        "qr_physical_mm": qr_mm,
        "grid_side": side_cells,
        "reserve_corners": bool(reserve_corners),
        "total_cells": side_cells * side_cells,
        "usable_cells": (side_cells * side_cells) - (4 if reserve_corners else 0),
        "tiles_placed": len(mapping),
        "mapping_sha256": mapping_sha,
        "mapping": mapping,
        "notes": "Tiles are stored under slide_XXXXX_tiles/, referenced by relative hrefs in SVG.",
        "layout": "slide_init + [file_open + headers + body_chunk + (footers+manifest?) + file_close]* + slide_close",
    }
    out_json.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def verify_slide_tiles(slide: Slide, sample: int = 20, stream=sys.stderr) -> None:
    if not (_HAVE_PYZBAR or _HAVE_OPENCV):
        print("[WARN] --verify requested but no decoder available (pyzbar/opencv).", file=stream)
        return
    ok = 0
    tot = 0
    for t in slide.tiles[:sample]:
        tot += 1
        b = decode_qr_bytes(t.dst)
        if b:
            ok += 1
    print(f"[VERIFY] slide {slide.index:05d}: decoded {ok}/{tot} sample tiles from OUTPUT tiles", file=stream)


# ---------- PDF export ----------

def export_pdf(svg_path: Path, json_path: Path, out_pdf: Path, out_dir: Path, stream=sys.stderr) -> None:
    # Prefer SVG->PDF conversion; fallback to raster placement via reportlab.
    if _HAVE_CAIROSVG:
        try:
            cairosvg.svg2pdf(url=str(svg_path), write_to=str(out_pdf))
            return
        except Exception as e:
            print(f"[WARN] cairosvg SVG->PDF failed, falling back ({e})", file=stream)

    if _HAVE_SVGLIB:
        try:
            drawing = svg2rlg(str(svg_path))
            renderPDF.drawToFile(drawing, str(out_pdf))
            return
        except Exception as e:
            print(f"[WARN] svglib SVG->PDF failed, falling back ({e})", file=stream)

    if not _HAVE_REPORTLAB:
        raise RuntimeError("PDF export requested but no PDF backend is available (install cairosvg OR svglib+reportlab OR reportlab).")

    # Raster placement based on slide JSON mapping
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    slide_mm = float(doc["slide_mm"])
    qr_mm = float(doc["qr_physical_mm"])
    side = int(doc["grid_side"])
    leftover = slide_mm - side * qr_mm
    offset = leftover / 2.0 if leftover > 0 else 0.0

    # ReportLab uses points; 1mm = 2.834645669... points (reportlab has mm unit)
    c = canvas.Canvas(str(out_pdf), pagesize=(slide_mm * _rl_mm, slide_mm * _rl_mm))
    c.setTitle(str(svg_path.name))
    # Border
    c.rect(0, 0, slide_mm * _rl_mm, slide_mm * _rl_mm, stroke=1, fill=0)

    # Place tiles. SVG has origin top-left; PDF origin bottom-left.
    for item in doc["mapping"]:
        r = int(item["r"])
        col = int(item["c"])
        href = item["href"]
        tile_path = out_dir / Path(href)
        x_mm = offset + col * qr_mm
        y_mm_top = offset + r * qr_mm
        # Convert to PDF coordinates (bottom-left):
        y_mm = slide_mm - (y_mm_top + qr_mm)
        try:
            data = tile_path.read_bytes()
            ir = ImageReader(io.BytesIO(data))
            c.drawImage(ir, x_mm * _rl_mm, y_mm * _rl_mm, width=qr_mm * _rl_mm, height=qr_mm * _rl_mm, mask=None, preserveAspectRatio=False, anchor='c')
        except Exception:
            # draw a red box placeholder
            c.setStrokeColorRGB(1, 0, 0)
            c.rect(x_mm * _rl_mm, y_mm * _rl_mm, qr_mm * _rl_mm, qr_mm * _rl_mm, stroke=1, fill=0)
            c.setStrokeColorRGB(0, 0, 0)

    c.showPage()
    c.save()


# ---------- GDSII / OASIS export ----------

def _load_gray(img_path: Path):
    if _HAVE_PIL:
        im = Image.open(win_long(img_path)).convert("L")  # type: ignore
        return im
    if _HAVE_OPENCV:
        mat = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)  # type: ignore
        if mat is None:
            raise RuntimeError(f"cv2.imread failed for {img_path}")
        return mat
    raise RuntimeError("Need Pillow or OpenCV to read tile images for GDS/OAS export.")


def _sample_module_matrix(img_path: Path, modules: int, threshold: int = 128) -> List[List[int]]:
    """
    Returns a modules x modules matrix with 1 for black, 0 for white.
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

    # OpenCV ndarray path
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


def export_gds_oas(json_path: Path, out_dir: Path, out_path: Path, kind: str, stream=sys.stderr,
                   layer: int = 1, datatype: int = 0, threshold: int = 128) -> None:
    """
    Convert slide JSON mapping -> GDSII/OASIS polygons (rectangles) at litho scale.
    Requires:
      - gdstk for .gds and .oas (preferred)
      - gdspy for .gds only (fallback)
    """
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    slide_mm = float(doc["slide_mm"])
    qr_mm = float(doc["qr_physical_mm"])
    side = int(doc["grid_side"])
    pixel_nm = float(doc["pixel_nm"])
    module_pixels = int(doc["module_pixels"])
    quiet_modules = int(doc["quiet_modules"])

    modules = QR_V40_MODULES + 2 * quiet_modules
    pitch_nm = pixel_nm * module_pixels
    pitch_um = pitch_nm / 1000.0  # GDS/OAS in microns here
    slide_um = slide_mm * 1000.0

    leftover = slide_mm - side * qr_mm
    offset_mm = leftover / 2.0 if leftover > 0 else 0.0
    offset_um = offset_mm * 1000.0

    slide_name = f"SLIDE_{int(doc['slide_index']):05d}"

    if kind == "oas" and not _HAVE_GDSTK:
        raise RuntimeError("OASIS export requires gdstk (pip install gdstk).")
    if kind == "gds" and not (_HAVE_GDSTK or _HAVE_GDSPY):
        raise RuntimeError("GDSII export requires gdstk or gdspy (pip install gdstk OR pip install gdspy).")

    if _HAVE_GDSTK:
        lib = gdstk.Library(unit=1e-6, precision=1e-9)  # microns, 1nm precision
        top = lib.new_cell(slide_name)

        # Optional border
        top.add(gdstk.rectangle((0, 0), (slide_um, slide_um), layer=layer, datatype=datatype))

        # Each tile -> rectangles
        # Coordinate system: origin at top-left in our math; GDS doesn't care. We'll keep y increasing down.
        for item in doc["mapping"]:
            r = int(item["r"])
            c = int(item["c"])
            href = item["href"]
            tile_path = out_dir / Path(href)

            # tile origin in um
            x0 = offset_um + c * (qr_mm * 1000.0)
            y0 = offset_um + r * (qr_mm * 1000.0)

            try:
                mat = _sample_module_matrix(tile_path, modules=modules, threshold=threshold)
            except Exception as e:
                print(f"[WARN] tile read failed for {href}: {e}", file=stream)
                continue

            # Run-length per row -> rectangles
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
                    end = cc  # exclusive
                    # rectangle in um
                    rx0 = x0 + start * pitch_um
                    ry0 = y0 + rr * pitch_um
                    rx1 = x0 + end * pitch_um
                    ry1 = y0 + (rr + 1) * pitch_um
                    top.add(gdstk.rectangle((rx0, ry0), (rx1, ry1), layer=layer, datatype=datatype))

        if kind == "gds":
            lib.write_gds(str(out_path))
        else:
            lib.write_oas(str(out_path))
        return

    # gdspy fallback (GDS only)
    lib = gdspy.GdsLibrary(unit=1e-6, precision=1e-9)  # type: ignore
    top = lib.new_cell(slide_name)
    top.add(gdspy.Rectangle((0, 0), (slide_um, slide_um), layer=layer, datatype=datatype))  # type: ignore

    for item in doc["mapping"]:
        r = int(item["r"])
        c = int(item["c"])
        href = item["href"]
        tile_path = out_dir / Path(href)
        x0 = offset_um + c * (qr_mm * 1000.0)
        y0 = offset_um + r * (qr_mm * 1000.0)

        try:
            mat = _sample_module_matrix(tile_path, modules=modules, threshold=threshold)
        except Exception as e:
            print(f"[WARN] tile read failed for {href}: {e}", file=stream)
            continue

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
                rx0 = x0 + start * pitch_um
                ry0 = y0 + rr * pitch_um
                rx1 = x0 + end * pitch_um
                ry1 = y0 + (rr + 1) * pitch_um
                top.add(gdspy.Rectangle((rx0, ry0), (rx1, ry1), layer=layer, datatype=datatype))  # type: ignore

    lib.write_gds(str(out_path))  # type: ignore


# ---------- CLI ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="QRFS slide builder v3: packs QRFS bundles onto as many slides as needed, writes SVG+JSON (+optional proof PNG) and optional PDF/GDSII/OASIS."
    )
    ap.add_argument("--src", required=True, help="Input directory (or .zip) containing one or more *.qrfs dirs (recursive search).")
    ap.add_argument("--out", default="slides_out", help="Output directory")
    ap.add_argument("--pixel-nm", type=float, default=DEFAULT_PIXEL_NM, help="Lithography pixel size in nanometers (default 1500)")
    ap.add_argument("--module-pixels", type=int, default=DEFAULT_MODULE_PIXELS, help="Pixels per QR module (default 1)")
    ap.add_argument("--quiet-modules", type=int, default=DEFAULT_QUIET, help="Quiet zone in modules (default 4)")
    ap.add_argument("--slide-mm", type=float, default=DEFAULT_SLIDE_MM, help="Slide width/height in mm (default 93)")
    ap.add_argument("--reserve-corners", action="store_true", help="Leave 4 corners empty (not used for tiles).")
    ap.add_argument("--proof-px", type=int, default=8192, help="Proof PNG side in pixels (only if Pillow installed)")
    ap.add_argument("--stripe-k", type=int, default=DEFAULT_STRIPE_K, help="Stripe K (blocks per stripe) for ordering parity (default 16)")
    ap.add_argument("--max-depth", type=int, default=0, help="Directory recursion limit when searching for *.qrfs dirs (0=unlimited).")
    ap.add_argument("--embed-manifest", action="store_true", help="Embed manifest.json as marker QR chunk(s) at end of file (base64).")
    ap.add_argument("--marker-ecc", choices=["L", "M", "Q", "H"], default="H", help="ECC for marker QRs (default H).")
    ap.add_argument("--marker-version", type=int, default=40, help="QR version for marker QRs (default 40).")

    # Export options
    ap.add_argument("--pdf", action="store_true", help="Also export slide_XXXXX.pdf (SVG->PDF when possible).")
    ap.add_argument("--gds", action="store_true", help="Also export slide_XXXXX.gds (requires gdstk or gdspy).")
    ap.add_argument("--oas", action="store_true", help="Also export slide_XXXXX.oas (requires gdstk).")
    ap.add_argument("--gds-layer", type=int, default=1, help="GDS/OAS layer for black modules (default 1).")
    ap.add_argument("--gds-datatype", type=int, default=0, help="GDS/OAS datatype for black modules (default 0).")
    ap.add_argument("--gds-threshold", type=int, default=128, help="Threshold (0-255) to classify black modules from tile images (default 128).")

    ap.add_argument("--no-progress", action="store_true", help="Disable progress output.")
    ap.add_argument("--progress-stream", choices=["stdout", "stderr"], default="stderr")
    ap.add_argument("--progress-mode", choices=["auto", "bar", "simple"], default="auto")
    ap.add_argument("--verify", action="store_true", help="Decode a small sample of tiles from OUTPUT to sanity-check.")
    ap.add_argument("--version", action="store_true", help="Print version and exit.")
    args = ap.parse_args()

    if args.version:
        print(__VERSION__)
        return 0

    stream = sys.stderr if args.progress_stream == "stderr" else sys.stdout
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src_root = extract_if_zip(Path(args.src))
    qrfs_dirs = find_qrfs_dirs(src_root, max_depth=args.max_depth)

    if not qrfs_dirs:
        print("[ERROR] No QRFS directories found (expected header_main.qr + header_stat.qr).", file=stream)
        return 2

    # Compute capacity
    qr_mm = compute_qr_physical_mm(args.pixel_nm, args.module_pixels, args.quiet_modules)
    side_cells, leftover = compute_grid(args.slide_mm, qr_mm)
    cap = side_cells * side_cells - (4 if args.reserve_corners else 0)

    print(f"[INFO] Files (qrfs dirs) to place: {len(qrfs_dirs)}", file=stream)
    print(f"[INFO] QR v40 physical size = {qr_mm:.6f} mm (pixel_nm={args.pixel_nm}, module_pixels={args.module_pixels}, quiet={args.quiet_modules})", file=stream)
    print(f"[INFO] Grid = {side_cells} x {side_cells} = {side_cells*side_cells} (usable {cap}); leftover margin = {leftover:.6f} mm", file=stream)

    if not (_HAVE_SEGNO or _HAVE_QRCODE):
        print("[ERROR] No QR generator available for markers. Install segno or qrcode.", file=stream)
        return 3

    # Build slides
    p = Progress(not args.no_progress, args.progress_stream, args.progress_mode)
    p.banner("Packing tiles into slides")

    sb = SlideBuilder(
        out_dir=out_dir,
        slide_mm=args.slide_mm,
        qr_mm=qr_mm,
        side_cells=side_cells,
        reserve_corners=args.reserve_corners,
        marker_ecc=args.marker_ecc,
        marker_version=args.marker_version,
        quiet_modules=args.quiet_modules,
    )

    for i, d in enumerate(qrfs_dirs, start=1):
        p.update("files", i - 1, len(qrfs_dirs))
        sb.add_qrfs_dir(d, file_seq=i, stripe_k=args.stripe_k, embed_manifest=args.embed_manifest)
        p.update("files", i, len(qrfs_dirs))
    p.close()

    slides = sb.finish()
    print(f"[INFO] Slides generated = {len(slides)}", file=stream)

    # Render slides
    p2 = Progress(not args.no_progress, args.progress_stream, args.progress_mode)
    p2.banner("Writing slide SVG/JSON (+optional proof/PDF/GDS/OAS)")

    index_doc: Dict[str, Any] = {
        "schema": "qrfs.slide_index.v3",
        "version": __VERSION__,
        "out_dir": str(out_dir.resolve()),
        "slide_mm": args.slide_mm,
        "pixel_nm": args.pixel_nm,
        "module_pixels": args.module_pixels,
        "quiet_modules": args.quiet_modules,
        "qr_physical_mm": qr_mm,
        "grid_side": side_cells,
        "reserve_corners": bool(args.reserve_corners),
        "slides": [],
    }

    for si, slide in enumerate(slides, start=1):
        p2.update("slides", si - 1, len(slides))

        base = f"slide_{slide.index:05d}"
        out_svg = out_dir / f"{base}.svg"
        out_json = out_dir / f"{base}.json"
        out_png = out_dir / f"proof_{slide.index:05d}.png"
        out_pdf = out_dir / f"{base}.pdf"
        out_gds = out_dir / f"{base}.gds"
        out_oas = out_dir / f"{base}.oas"

        grid = make_grid(side_cells, slide.tiles, reserve_corners=args.reserve_corners)

        draw_svg(out_svg, args.slide_mm, qr_mm, side_cells, grid, title=f"{base} [{slide.uuid}]")
        write_slide_json(out_json, slide, args.slide_mm, args.pixel_nm, args.module_pixels, args.quiet_modules, qr_mm, side_cells, args.reserve_corners, grid)

        if _HAVE_PIL:
            pp = Progress(not args.no_progress, args.progress_stream, args.progress_mode)
            draw_proof_png(out_png, args.proof_px, side_cells, grid, pp)

        if args.pdf:
            try:
                export_pdf(out_svg, out_json, out_pdf, out_dir, stream=stream)
            except Exception as e:
                print(f"[WARN] PDF export failed for {base}: {e}", file=stream)

        if args.gds:
            try:
                export_gds_oas(out_json, out_dir, out_gds, "gds", stream=stream, layer=args.gds_layer, datatype=args.gds_datatype, threshold=args.gds_threshold)
            except Exception as e:
                print(f"[WARN] GDS export failed for {base}: {e}", file=stream)

        if args.oas:
            try:
                export_gds_oas(out_json, out_dir, out_oas, "oas", stream=stream, layer=args.gds_layer, datatype=args.gds_datatype, threshold=args.gds_threshold)
            except Exception as e:
                print(f"[WARN] OAS export failed for {base}: {e}", file=stream)

        index_doc["slides"].append({
            "slide_index": slide.index,
            "slide_uuid": slide.uuid,
            "svg": f"{base}.svg",
            "json": f"{base}.json",
            "tiles_dir": f"{base}_tiles",
            "proof_png": f"proof_{slide.index:05d}.png" if _HAVE_PIL else None,
            "pdf": f"{base}.pdf" if args.pdf else None,
            "gds": f"{base}.gds" if args.gds else None,
            "oas": f"{base}.oas" if args.oas else None,
            "tiles": len(slide.tiles),
        })

        if args.verify:
            verify_slide_tiles(slide, sample=20, stream=stream)

        p2.update("slides", si, len(slides))

    p2.close()

    (out_dir / "slides_index.json").write_text(json.dumps(index_doc, indent=2), encoding="utf-8")

    print("[DONE]", file=stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
