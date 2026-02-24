#!/usr/bin/env python3
"""
magnify_pane_v3.py

Pan/zoom HTML viewer for QRFS slide artifacts (SVG/PNG).

Why v3:
- Fixes Python .format() collisions with JS template strings (like v2)
- Adds AUTO-FIT-on-load so tiny physical-unit SVGs become visible immediately
- Adds --boost to start "zoomed in" beyond fit
- Keeps optional --render-png (SVG -> PNG) for faster browser rendering

Note: Full slides are *enormously* information-dense. For human inspection of QR
modules, view 2x2 panes (from slice_slide_2x2_v3.py) rather than a whole slide.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@TITLE@@</title>
<style>
  :root {
    --bg: #0b0f14;
    --panel: #111826;
    --text: #e7eef7;
    --muted: #9fb2c7;
    --btn: #1a2637;
    --btn2: #24364e;
    --border: rgba(255,255,255,0.10);
  }
  body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
         background: var(--bg); color: var(--text); }
  header {
    position: sticky; top: 0; z-index: 10;
    background: linear-gradient(to bottom, rgba(11,15,20,0.98), rgba(11,15,20,0.85));
    border-bottom: 1px solid var(--border);
    padding: 10px 12px;
    display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  }
  .title { font-weight: 650; letter-spacing: 0.2px; margin-right: 10px; white-space: nowrap; }
  .pill  { font-size: 12px; color: var(--muted); padding: 4px 8px; border: 1px solid var(--border); border-radius: 999px; }
  button { border: 1px solid var(--border); background: var(--btn); color: var(--text);
           padding: 6px 10px; border-radius: 10px; cursor: pointer; font-weight: 600; }
  button:hover { background: var(--btn2); }
  .spacer { flex: 1; }
  #stage { height: calc(100vh - 56px); overflow: hidden; position: relative; }
  #canvas { position: absolute; left: 0; top: 0; transform-origin: 0 0; will-change: transform;
            user-select: none; -webkit-user-drag: none; image-rendering: pixelated; }
  .hint {
    position: absolute; left: 12px; bottom: 12px;
    background: rgba(17,24,38,0.75); border: 1px solid var(--border);
    color: var(--muted); padding: 8px 10px; border-radius: 12px; font-size: 12px;
    max-width: min(720px, calc(100vw - 24px));
  }
</style>
</head>
<body>
<header>
  <div class="title">@@TITLE@@</div>
  <span class="pill" id="status">scale: 1.00×</span>
  <button id="zin">Zoom +</button>
  <button id="zout">Zoom −</button>
  <button id="reset">Reset</button>
  <div class="spacer"></div>
  <span class="pill">@@FILENAME@@</span>
</header>

<div id="stage">
  <img id="canvas" src="@@REL_ASSET@@" alt="artifact"/>
  <div class="hint">
    <b>Controls:</b> Mouse wheel zooms around cursor. Click‑drag pans. Double‑click resets.
    <br><b>Tip:</b> If you are viewing a full slide, slice into 2×2 panes first — whole slides aren’t meant to be human-readable at once.
  </div>
</div>

<script>
(() => {
  const stage = document.getElementById('stage');
  const canvas = document.getElementById('canvas');
  const status = document.getElementById('status');
  const btnIn = document.getElementById('zin');
  const btnOut = document.getElementById('zout');
  const btnReset = document.getElementById('reset');

  const AUTO_FIT = @@AUTO_FIT@@;
  const BOOST = @@BOOST@@;
  const INIT_SCALE = @@INIT_SCALE@@;

  let scale = INIT_SCALE;
  let tx = 0;
  let ty = 0;
  let dragging = false;
  let lastX = 0;
  let lastY = 0;

  function apply() {
    canvas.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    status.textContent = `scale: ${scale.toFixed(2)}×`;
  }

  function clampScale(s) {
    const minS = 0.05;
    const maxS = 20000.0;
    return Math.min(maxS, Math.max(minS, s));
  }

  function zoomAt(clientX, clientY, factor) {
    const rect = stage.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;

    const worldX = (x - tx) / scale;
    const worldY = (y - ty) / scale;

    const newScale = clampScale(scale * factor);

    tx = x - worldX * newScale;
    ty = y - worldY * newScale;
    scale = newScale;
    apply();
  }

  function stageCenter() {
    const r = stage.getBoundingClientRect();
    return { cx: r.left + stage.clientWidth/2, cy: r.top + stage.clientHeight/2 };
  }

  function resetView() {
    // Use either explicit init scale, or fit-to-stage.
    if (!AUTO_FIT) {
      scale = INIT_SCALE;
      tx = 0; ty = 0;
      apply();
      return;
    }

    // Fit: image size in CSS pixels
    const iw = canvas.naturalWidth  || canvas.width  || 1;
    const ih = canvas.naturalHeight || canvas.height || 1;

    const sw = stage.clientWidth  || 1;
    const sh = stage.clientHeight || 1;

    const fit = Math.min(sw / iw, sh / ih) * 0.95;
    scale = clampScale(fit * BOOST);

    // Center the image
    tx = (sw - iw * scale) / 2.0;
    ty = (sh - ih * scale) / 2.0;

    apply();
  }

  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    const factor = (e.deltaY < 0) ? 1.12 : 1/1.12;
    zoomAt(e.clientX, e.clientY, factor);
  }, { passive: false });

  stage.addEventListener('mousedown', (e) => {
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
  });

  window.addEventListener('mouseup', () => dragging = false);
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    lastX = e.clientX;
    lastY = e.clientY;
    tx += dx;
    ty += dy;
    apply();
  });

  stage.addEventListener('dblclick', () => resetView());

  btnIn.addEventListener('click', () => { const c = stageCenter(); zoomAt(c.cx, c.cy, 1.2); });
  btnOut.addEventListener('click', () => { const c = stageCenter(); zoomAt(c.cx, c.cy, 1/1.2); });
  btnReset.addEventListener('click', () => resetView());

  canvas.addEventListener('load', () => resetView());
  resetView();
})();
</script>
</body>
</html>
"""


def try_render_svg_to_png(svg_path: Path, png_path: Path, width: int) -> bool:
    """Render SVG -> PNG using cairosvg if available."""
    try:
        import cairosvg  # type: ignore
    except Exception:
        return False
    svg_bytes = svg_path.read_bytes()
    cairosvg.svg2png(bytestring=svg_bytes, write_to=str(png_path), output_width=width)
    return True


def normalize_base_name(name: str) -> str:
    p = Path(name)
    return p.stem if p.suffix else name


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Create a pan/zoom HTML magnifier for SVG/PNG slide artifacts.")
    ap.add_argument("--in", dest="inp", required=True, help="Input artifact file (SVG or PNG).")
    ap.add_argument("--out", dest="outdir", required=True, help="Output directory.")
    ap.add_argument("--name", default=None, help="Optional base name for outputs (extension optional).")

    ap.add_argument("--auto-fit", action="store_true", default=True,
                    help="Auto-fit the image to the window on load (default: on).")
    ap.add_argument("--no-auto-fit", dest="auto_fit", action="store_false",
                    help="Disable auto-fit and use --init-scale only.")
    ap.add_argument("--boost", type=float, default=1.0,
                    help="Multiply fit scale by this factor (e.g., 5 for panes). Default: 1.0")
    ap.add_argument("--init-scale", type=float, default=2.0,
                    help="Initial scale if --no-auto-fit (default: 2.0)")

    ap.add_argument("--render-png", action="store_true",
                    help="If input is SVG, also render a PNG using cairosvg (faster for browsers).")
    ap.add_argument("--png-width", type=int, default=8192,
                    help="Rendered PNG width if --render-png (default: 8192).")

    args = ap.parse_args(argv)

    inp = Path(args.inp)
    if not inp.exists():
        ap.error(f"input not found: {inp}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    base = normalize_base_name(args.name) if args.name else inp.stem
    viewer_path = outdir / f"{base}_viewer.html"

    asset_ext = inp.suffix.lower()
    asset_copy = outdir / f"{base}{asset_ext}"
    if inp.resolve() != asset_copy.resolve():
        shutil.copy2(inp, asset_copy)

    rel_asset = asset_copy.name

    rendered_png = None
    if args.render_png and asset_ext == ".svg":
        png_path = outdir / f"{base}_render.png"
        ok = try_render_svg_to_png(asset_copy, png_path, width=args.png_width)
        if ok:
            rendered_png = png_path
            rel_asset = png_path.name

    title = f"Magnifier: {base}"

    html = (HTML_TEMPLATE
            .replace("@@TITLE@@", title)
            .replace("@@FILENAME@@", asset_copy.name)
            .replace("@@REL_ASSET@@", rel_asset)
            .replace("@@AUTO_FIT@@", "true" if args.auto_fit else "false")
            .replace("@@BOOST@@", str(float(args.boost)))
            .replace("@@INIT_SCALE@@", str(float(args.init_scale))))
    viewer_path.write_text(html, encoding="utf-8")

    print(f"[OK] wrote viewer: {viewer_path}")
    if rendered_png:
        print(f"[OK] rendered PNG: {rendered_png}")
    else:
        if args.render_png and asset_ext == ".svg":
            print("[WARN] --render-png requested but cairosvg is not installed; skipped PNG render.")
            print("       Install with: py -m pip install cairosvg  (may require Cairo runtime on Windows)")

    print("\nOpen the viewer in a browser (double-click):")
    print(f"  {viewer_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
