#!/usr/bin/env python3
# QRFS — QR-based Visual File System (v0.2.3)
# License: Creative Commons Attribution 4.0 (CC BY 4.0)
# Author: ChatGPT for Carl

import argparse
import concurrent.futures as futures
from datetime import datetime as dt, timezone
import hashlib
import json
import logging
import os
import stat
import struct
import sys
import time
from pathlib import Path
from typing import List, Optional

QR_ENGINE = None
_qrcodegen = None
_segno = None
_qrcode = None

try:
    from qrcodegen import QrCode, QrSegment
    _qrcodegen = (QrCode, QrSegment)
    QR_ENGINE = "qrcodegen"
except Exception:
    try:
        import segno as _segno
        QR_ENGINE = "segno"
    except Exception:
        try:
            import qrcode as _qrcode
            from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H
            QR_ENGINE = "qrcode"
        except Exception:
            raise SystemExit("No QR backend available. Install one of: qrcodegen, segno, qrcode")

from PIL import Image
from tqdm import tqdm

try:
    import reedsolo
    HAVE_RS = True
except Exception:
    HAVE_RS = False

MAGIC = b"QRF1"
TYPE_HEADER = 0x01
TYPE_DATA   = 0x02
TYPE_PARITY = 0x03
TYPE_FOOTER = 0x04
FORMAT_VERSION = 2

ECC_SET = {"L", "M", "H"}

def win_long(p: Path) -> str:
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + s
    return s

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def iter_files(src: Path) -> List[Path]:
    if src.is_file():
        return [src]
    files = []
    for root, dirs, fnames in os.walk(src):
        for fn in fnames:
            p = Path(root) / fn
            if p.is_file():
                files.append(p)
    files.sort()
    return files

def file_stat_dict(p: Path) -> dict:
    st = p.stat()
    return {
        "mode": stat.filemode(st.st_mode),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "ctime": st.st_ctime,
        "atime": st.st_atime,
        "uid": getattr(st, "st_uid", None),
        "gid": getattr(st, "st_gid", None),
    }

def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def pack_data_block(file_id: bytes, block_index: int, total_blocks: int, block_bytes: bytes, hex_mode: str) -> bytes:
    assert len(file_id) == 32
    data_len = len(block_bytes)
    sha = hashlib.sha256(block_bytes).digest()
    flags = 0
    if hex_mode == "inline" and (data_len * 2 + 1) <= 512:
        flags |= 0x01
    buf = bytearray()
    buf += MAGIC
    buf += bytes([TYPE_DATA, FORMAT_VERSION])
    buf += file_id
    buf += struct.pack(">II", block_index, total_blocks)
    buf += struct.pack(">I", data_len)
    buf += sha
    buf += bytes([flags])
    buf += block_bytes
    if flags & 0x01:
        buf += block_bytes.hex().encode("utf-8")
    return bytes(buf)

def pack_parity_block(file_id: bytes, stripe_index: int, parity_index: int, parity_bytes: bytes) -> bytes:
    buf = bytearray()
    buf += MAGIC
    buf += bytes([TYPE_PARITY, FORMAT_VERSION])
    buf += file_id
    buf += struct.pack(">I", stripe_index)
    buf += bytes([parity_index & 0xFF])
    buf += struct.pack(">I", len(parity_bytes))
    buf += parity_bytes
    return bytes(buf)

def _render_matrix_to_image(get_module, size: int, box_size: int, border: int) -> Image.Image:
    dim = (size + border * 2)
    img = Image.new("1", (dim * box_size, dim * box_size), 1)  # white background
    pixels = img.load()
    for y in range(size):
        for x in range(size):
            if get_module(x, y):
                px = (x + border) * box_size
                py = (y + border) * box_size
                for dy in range(box_size):
                    for dx in range(box_size):
                        pixels[px + dx, py + dy] = 0  # black
    return img

def encode_qr_bytes(payload: bytes, version: Optional[int], ecc_level: str,
                    box_size: int, border: int, allow_fit_fallback: bool,
                    logger: Optional[logging.Logger] = None) -> Image.Image:
    assert ecc_level in ECC_SET

    if QR_ENGINE == "qrcodegen":
        QrCode, QrSegment = _qrcodegen
        ECL = {
            "L": QrCode.Ecc.LOW,
            "M": QrCode.Ecc.MEDIUM,
            "H": QrCode.Ecc.HIGH,
        }[ecc_level]
        segs = [QrSegment.make_bytes(payload)]
        try:
            if version is not None:
                qr = QrCode.encode_segments(segs, ECL, minversion=version, maxversion=version, mask=-1, boostecl=False)
            else:
                qr = QrCode.encode_segments(segs, ECL, minversion=1, maxversion=40, mask=-1, boostecl=False)
        except Exception as e:
            if not allow_fit_fallback:
                raise
            qr = QrCode.encode_segments(segs, ECL, minversion=1, maxversion=40, mask=-1, boostecl=False)
            if logger:
                logger.info("qrcodegen fit fallback: selected version %d", qr.version)
        # Some qrcodegen builds don't expose 'size'; derive from version.
        try:
            size = qr.size  # may not exist
        except Exception:
            size = 4 * int(getattr(qr, "version", 40)) + 17
        return _render_matrix_to_image(lambda x, y: qr.get_module(x, y), size, box_size, border)

    elif QR_ENGINE == "segno":
        import io
        error = {"L":"l","M":"m","H":"h"}[ecc_level]
        try:
            qrobj = _segno.make(payload, mode='byte', version=version, error=error, micro=False, boost_error=False)
        except Exception as e:
            if not allow_fit_fallback:
                raise
            qrobj = _segno.make(payload, mode='byte', version=None, error=error, micro=False, boost_error=False)
            if logger:
                try:
                    v = qrobj.version
                except Exception:
                    v = None
                logger.info("segno fit fallback: selected version %s", v)
        buf = io.BytesIO()
        qrobj.save(buf, kind='png', scale=box_size, border=border)
        buf.seek(0)
        return Image.open(buf)

    else:  # qrcode fallback
        from qrcode.constants import ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q, ERROR_CORRECT_H
        ECC_MAP = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "H": ERROR_CORRECT_H}
        import qrcode as _qrcode_local
        qr = _qrcode_local.QRCode(
            version=version,
            error_correction=ECC_MAP[ecc_level],
            box_size=box_size,
            border=border,
        )
        try:
            qr.add_data(payload)
            if version is None:
                qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            return img
        except Exception as e:
            if not allow_fit_fallback:
                raise
            qr = _qrcode_local.QRCode(
                version=None,
                error_correction=ECC_MAP[ecc_level],
                box_size=box_size,
                border=border,
            )
            qr.add_data(payload)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            if logger:
                logger.info("qrcode fallback: fit=True selected version %s", getattr(qr, "version", None))
            return img

def safe_save_png(img: Image.Image, out_path: Path, png_level: int, optimize: bool,
                  logger: Optional[logging.Logger], attempt: int = 1):
    try:
        img.save(win_long(out_path), format="PNG", optimize=optimize, compress_level=png_level)
        return
    except Exception as e:
        if logger:
            logger.warning("PNG save failed (attempt %d) for %s: %s", attempt, out_path.name, e)
        if attempt == 1:
            return safe_save_png(img, out_path, png_level, False, logger, attempt + 1)
        elif attempt == 2:
            return safe_save_png(img, out_path, 6, False, logger, attempt + 1)
        elif attempt == 3:
            try:
                img2 = img.convert("RGB")
            except Exception:
                img2 = img
            return safe_save_png(img2, out_path, 6, False, logger, attempt + 1)
        else:
            raise

def make_qr_bytes(payload: bytes, out_path: Path, version: int, ecc_level: str,
                  box_size: int, border: int, png_compress_level: int,
                  logger: Optional[logging.Logger] = None,
                  allow_fit_fallback: bool = True) -> None:
    img = encode_qr_bytes(payload, version, ecc_level, box_size, border, allow_fit_fallback, logger)
    safe_save_png(img, out_path, png_compress_level, True, logger)

def make_qr_json(obj: dict, out_path: Path, version: int, ecc_level: str,
                 box_size: int, border: int, png_compress_level: int,
                 logger: Optional[logging.Logger] = None,
                 allow_fit_fallback: bool = True) -> None:
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    make_qr_bytes(data, out_path, version, ecc_level, box_size, border, png_compress_level, logger, allow_fit_fallback)

def compute_stripe_parity(blocks: List[bytes]) -> list:
    rs = reedsolo.RSCodec(nsym=4)
    maxlen = max(len(b) for b in blocks) if blocks else 0
    parity = [bytearray(maxlen) for _ in range(4)]
    for pos in range(maxlen):
        col = bytes(b[pos] if pos < len(b) else 0 for b in blocks)
        codeword = rs.encode(col)
        for j in range(4):
            parity[j][pos] = codeword[len(col) + j]
    return [bytes(pb) for pb in parity]

def process_file(p: Path, src_root: Path, out_root: Path, args, pbar) -> None:
    rel_dir = p.parent.relative_to(src_root)
    tgt_dir = out_root / rel_dir / (p.name + ".qrfs")
    ensure_dir(tgt_dir)

    from logging import Logger, FileHandler, Formatter, getLogger
    flog_path = tgt_dir / "run.log"
    file_logger: Logger = getLogger(f"qrfs.{tgt_dir}")
    file_logger.setLevel(getLogger().level)
    file_logger.propagate = False
    fh = FileHandler(win_long(flog_path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(Formatter("%(asctime)s %(levelname)s %(message)s"))
    file_logger.addHandler(fh)

    try:
        file_logger.info("Processing file: %s (QR engine: %s)", str(p), QR_ENGINE)
        fi = file_stat_dict(p)
        file_sha = sha256_file(p)
        file_id = bytes.fromhex(file_sha)

        total_blocks = (fi["size"] + args.block_size - 1) // args.block_size

        header_main = {
            "type": "header_main",
            "file": str(p),
            "rel_dir": str(rel_dir),
            "basename": p.name,
            "stats": fi,
            "overall_sha256": file_sha,
            "block_size": args.block_size,
            "total_blocks": total_blocks,
            "qr_version": args.version,
            "qr_ecc": args.ecc,
            "hex_mode": args.hex_mode,
            "created_utc": dt.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "generator": "qrfs v0.2.2",
            "qr_engine": QR_ENGINE,
        }
        make_qr_json(header_main, tgt_dir / "header_main.qr", args.version, args.ecc,
                     args.box_size, args.border, args.png_compress_level, file_logger, args.fit_fallback)
        pbar.update(1)

        header_stat = {"type": "header_stat", "st": fi}
        make_qr_json(header_stat, tgt_dir / "header_stat.qr", args.version, args.ecc,
                     args.box_size, args.border, args.png_compress_level, file_logger, args.fit_fallback)
        pbar.update(1)

        block_hashes = []
        block_files = []
        stripe = []
        stripe_index = 0
        block_index = 0

        with p.open("rb") as f:
            while True:
                chunk = f.read(args.block_size)
                if not chunk:
                    break

                payload = pack_data_block(file_id, block_index, total_blocks, chunk, args.hex_mode)
                out_path = tgt_dir / f"block_{block_index:06d}.qr"
                try:
                    make_qr_bytes(payload, out_path, args.version, args.ecc,
                                  args.box_size, args.border, args.png_compress_level,
                                  file_logger, args.fit_fallback)
                except Exception as e:
                    if args.hex_mode == "inline":
                        file_logger.warning("Retrying block %d without inline hex due to encode failure: %s", block_index, e)
                        payload = pack_data_block(file_id, block_index, total_blocks, chunk, "derived")
                        make_qr_bytes(payload, out_path, args.version, args.ecc,
                                      args.box_size, args.border, args.png_compress_level,
                                      file_logger, args.fit_fallback)
                    else:
                        file_logger.error("Failed to write data QR for block %d: %s", block_index, e)
                        raise

                bh = hashlib.sha256(chunk).digest()
                block_hashes.append(bh)
                block_files.append(str(out_path.name))
                pbar.update(1)

                stripe.append(chunk)
                if len(stripe) == 16:
                    if args.parity and HAVE_RS:
                        try:
                            parity_blocks = compute_stripe_parity(stripe)
                            for j, pb in enumerate(parity_blocks):
                                ppayload = pack_parity_block(file_id, stripe_index, j, pb)
                                pout = tgt_dir / f"parity_stripe_{stripe_index:06d}_p{j}.qr"
                                make_qr_bytes(ppayload, pout, args.version, args.ecc,
                                              args.box_size, args.border, args.png_compress_level,
                                              file_logger, args.fit_fallback)
                                block_files.append(str(pout.name))
                                pbar.update(1)
                        except Exception as e:
                            file_logger.error("Parity generation failed on stripe %d: %s", stripe_index, e)
                    stripe_index += 1
                    stripe.clear()

                block_index += 1

            if stripe:
                if args.parity and HAVE_RS:
                    try:
                        parity_blocks = compute_stripe_parity(stripe)
                        for j, pb in enumerate(parity_blocks):
                            ppayload = pack_parity_block(file_id, stripe_index, j, pb)
                            pout = tgt_dir / f"parity_stripe_{stripe_index:06d}_p{j}.qr"
                            make_qr_bytes(ppayload, pout, args.version, args.ecc,
                                          args.box_size, args.border, args.png_compress_level,
                                          file_logger, args.fit_fallback)
                            block_files.append(str(pout.name))
                            pbar.update(1)
                    except Exception as e:
                        file_logger.error("Parity generation failed on tail stripe %d: %s", stripe_index, e)
                stripe_index += 1
                stripe.clear()

        tagA = hashlib.sha256(b"".join(block_hashes)).hexdigest()
        footer_A = {
            "type": "footer_tagA_blockhashchain",
            "sha256_over_block_hashes_concat": tagA,
            "detail": "sha256(concat(sha256(block_i) for i in order))",
            "total_blocks": total_blocks,
        }
        make_qr_json(footer_A, tgt_dir / "footer_tagA_blockhashchain.qr", args.version, args.ecc,
                     args.box_size, args.border, args.png_compress_level, file_logger, args.fit_fallback)
        pbar.update(1)

        manifest_entries = []
        for name in block_files + ["header_main.qr", "header_stat.qr"]:
            fp = tgt_dir / name
            h = hashlib.sha256(Path(fp).read_bytes()).hexdigest()
            manifest_entries.append({"name": name, "sha256": h})
        manifest = {
            "type": "manifest",
            "file": str(p),
            "overall_file_sha256": file_sha,
            "block_size": args.block_size,
            "entries": manifest_entries,
            "parity_enabled": bool(args.parity and HAVE_RS),
        }
        (tgt_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (tgt_dir / "manifest.sha256").write_text(
            hashlib.sha256(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(),
            encoding="utf-8"
        )
        dircontent_hash = hashlib.sha256(json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        footer_B = {
            "type": "footer_tagB_dircontent",
            "sha256_over_manifest_excluding_footers": dircontent_hash,
            "manifest_file": "manifest.json",
        }
        make_qr_json(footer_B, tgt_dir / "footer_tagB_dircontent.qr", args.version, args.ecc,
                     args.box_size, args.border, args.png_compress_level, file_logger, args.fit_fallback)
        pbar.update(1)

        file_logger.info("Completed file: %s", str(p))

    finally:
        file_logger.removeHandler(fh)
        fh.close()

def main():
    ap = argparse.ArgumentParser(description="QR-based Visual File System (QRFS)")
    ap.add_argument("--src", required=True, help="File or directory to ingest")
    ap.add_argument("--out", required=True, help="Output directory (mirrors source tree)")
    ap.add_argument("--block-size", type=int, default=1024, help="Block size in bytes (default 1024)")
    ap.add_argument("--threads", type=int, default=8, help="Concurrent files to process (default 8)")
    ap.add_argument("--ecc", "--ecc-mode", "--ecc-level", dest="ecc", choices=["L", "M", "H"], default="H",
                    help="QR ECC level: L, M, or H (default H)")
    ap.add_argument("--version", type=int, default=40, help="QR version (default 40)")
    ap.add_argument("--hex-mode", choices=["derived", "inline"], default="derived",
                    help="Include hex string inline when feasible (default derived)")
    ap.add_argument("--log-level", default="DEBUG", help="Log level (DEBUG, INFO, WARNING, ERROR)")
    ap.add_argument("--box-size", type=int, default=3, help="QR pixel size per module (default 3)")
    ap.add_argument("--border", type=int, default=4, help="QR quiet zone border in modules (default 4)")
    ap.add_argument("--png-compress-level", type=int, default=4, help="PNG zlib level 0-9 (default 4)")
    ap.add_argument("--no-parity", action="store_true", help="Disable RS(20,16) parity generation")
    ap.add_argument("--no-fit-fallback", action="store_true",
                    help="Disable fallback to fit=True QR version if fixed version fails (default: fallback enabled)")
    args = ap.parse_args()

    args.parity = not args.no_parity
    args.fit_fallback = not args.no_fit_fallback

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.DEBUG),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.parity and not HAVE_RS:
        logging.warning("reedsolo not installed; continuing WITHOUT parity. Run: pip install reedsolo==2.0.13")

    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    ensure_dir(out)

    files = iter_files(src)
    if not files:
        logging.error("No files found under: %s", str(src))
        sys.exit(2)

    total_units = 0
    for p in files:
        size = p.stat().st_size
        blocks = (size + args.block_size - 1) // args.block_size
        stripes = (blocks + 15) // 16
        parity_units = (4 * stripes) if (args.parity and HAVE_RS) else 0
        total_units += 2 + blocks + parity_units + 2

    start = time.time()
    with tqdm(total=total_units, desc="QRFS", unit="qr") as pbar:
        with futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
            futs = [ex.submit(process_file, p, src, out, args, pbar) for p in files]
            for fut in futures.as_completed(futs):
                exc = fut.exception()
                if exc:
                    logging.error("Error: %s", exc)
                    raise exc

    logging.info("All done in %.2fs", time.time() - start)

if __name__ == "__main__":
    main()
