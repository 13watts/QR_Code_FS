# QRFS Nanometer Slide Experiment

A practical experiment in turning files into **QR mosaics** designed for **nanometer‑scale lithography**.

This repo is *not* “QR codes for your coffee shop menu.” It’s a pipeline to test whether we can reliably **write** and later **read** dense QR payloads when the “pixels” are measured in **nanometers**, using lithography workflows and scanners/cameras downstream.

---

## What this is testing

We generate **QR Code Version 40** tiles (configurable ECC), pack them onto a **93 mm × 93 mm** “slide”, export to vector formats suitable for lithography, optionally slice into smaller **2×2 panes** for a writer, then rebuild the original file(s) from the slide artifacts.

The goal: validate end‑to‑end **data survivability** through a physical process:

1) digital payload → 2) QR tiles → 3) slide layout → 4) vector export → 5) nano lithography → 6) imaging → 7) decode → 8) file rehydration + hash verification

If this works, it’s a path toward **nano‑written, machine‑readable, long‑life storage experiments** (and a nice way to make your storage admin friends uncomfortable).

---

## Key physical parameters

Default settings used by the slide builders:

- **Slide size:** 93 mm × 93 mm  
- **Pixel size:** 1500 nm (1.5 µm) per pixel (configurable)  
- **Module size:** 1 pixel per module (configurable)  
- **Quiet zone:** 4 modules (configurable)  
- **QR Version:** 40 (177×177 modules before quiet zone)

### Derived tile size (example)
With Version 40 and quiet zone 4:

- Modules per side = 177 + 8 = **185**
- Module pitch = 1500 nm × 1 = **1.5 µm**
- QR side length = 185 × 1.5 µm = **277.5 µm = 0.2775 mm**

### Derived slide grid capacity (example)
93 mm / 0.2775 mm ≈ **335 tiles per side**, so:

- Grid = **335 × 335 = 112,225 tiles per slide**
- Remaining margin is distributed as a small offset.

Your capacity will change if you change pixel pitch, module pixels, quiet zone, or reserve regions for markers/corners.

---

## The four programs

### 1) `qrfs_cli_ecc_lmh.py`
Encodes a file (or recursively a directory) into a `*.qrfs/` bundle:

- `header_main.qr`, `header_stat.qr` (filename, stats, file hash)
- `block_######.qr` data blocks (default 1024 bytes each)
- optional `parity_stripe_######_p#.qr` (erasure/parity per stripe)
- footer/markers (optional)

> ECC is switchable (L/M/H). If you want maximum payload per tile, ECC‑L is your friend. If you want your tiles to survive a rough life, ECC‑H is your insurance bill.

### 2) `create_slide_qrfs_v3.py`
Packs one or more `*.qrfs` bundles onto one or more slides:

- outputs `slide_#####.svg` (preferred, vector)
- outputs `slide_#####.json` (layout metadata)
- optional proof PNG (huge; for humans)

### 3) `slice_slide_2x2_v2.py`
Slices a slide into **2×2** QR panes (writer‑friendly), with sequence numbers, plus optional exports:

- SVG (always)
- PDF (optional)
- GDSII / OASIS (optional; intended for lithography tooling)

### 4) `rebuild_from_slide_v3.py`
Rebuilds original file(s) from slide artifacts:

- processes **multiple slides** (`--slides-dir`)
- prefers **SVG with embedded `data:` images** (what you actually lithograph)
- optional per‑block SHA checking + final file hash verification (when available)
- PDF input supported with PyMuPDF
- GDS/OAS supported primarily for **2×2 panes** (full‑slide GDS/OAS decode is intentionally avoided)

---

## Quickstart 

### Install Python deps
Use the OS‑specific requirements files:

- `requirements_windows.txt`
- `requirements_linux.txt`
- `requirements_macos.txt`

Example:
```bat
py -m pip install -r requirements_windows.txt
```

**Decoder note:** `pyzbar` needs a ZBar runtime on Windows. If you decode with OpenCV only, ZBar may be optional.

### Encode → Slide → Slice → Rebuild
```bat
REM 1) Encode into a .qrfs bundle
py -u qrfs_cli_ecc_lmh.py --src Z:\data\photos --out Z:\qrfs_out --block-size 1024 --ecc H --hex-mode derived

REM 2) Pack bundle(s) onto slides
py -u create_slide_qrfs_v3.py --src Z:\qrfs_out --out Z:\slide_out --progress-mode bar --verify

REM 3) Slice slide into 2×2 panes (optional)
py -u slice_slide_2x2_v2.py --svg Z:\slide_out\slide_00001.svg --out Z:\slide_out\panes --manifest --pdf --gds --oas

REM 4) Rebuild files from slides
py -u rebuild_from_slide_v3.py --slides-dir Z:\slide_out --out Z:\recovered --format auto --check-block-sha --verbose
```

---

## ECC level vs data block size (Version 40 refresher)

Maximum byte payload capacity for **QR Version 40** in byte mode (upper bound, before your headers/metadata):

- **ECC‑L:** 2953 bytes  
- **ECC‑M:** 2331 bytes  
- **ECC‑Q:** 1663 bytes  
- **ECC‑H:** 1273 bytes  

Your actual usable **data block** is lower once you include your framing (magic, IDs, sequence, embedded SHA, etc.).
Also: **inline hex halves effective capacity**. If you want maximum density, don’t hex‑inflate your payload.

---

## What “vector” means here (and why it matters)

For lithography, you typically want a representation that is stable under scaling and can integrate with mask/layout workflows.

- **SVG:** great for layout + transport; easy to inspect; good intermediate
- **PDF:** handy for print pipelines, but not the native language of litho tools
- **GDSII / OASIS:** common in IC/lithography ecosystems; good for module‑as‑polygon workflows

This repo supports exporting **panes** to GDS/OAS. Full‑slide GDS/OAS is possible, but you don’t want that file size unless you hate yourself.

---

## License

**Creative Commons Attribution 4.0 International (CC BY 4.0)**  
See `LICENSE`.

---

## Status

This is experimental engineering code aimed at a physical process test. Expect rough edges, sharp corners, and occasional swearing at decoders.
