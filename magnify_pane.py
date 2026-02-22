#!/usr/bin/env python3
"""
magnify_pane.py

A tiny "magnifier" utility for QRFS 2x2 panes.

Given a 2x2 pane (SVG or PNG), this tool writes a self-contained HTML viewer
with smooth pan/zoom controls so a human can actually inspect the tiles.

Optional: If the input is SVG and cairosvg is installed, it can also render a
large raster PNG for quick viewing.

Works on Windows/macOS/Linux (viewer is just a local HTML file).
"""

from __future__ import annotations

import argparse
import sys
import shutil
from pathlib import Path

HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #0b0f14;
    --panel: #111826;
    --text: #e7eef7;
    --muted: #9fb2c7;
    --btn: #1a2637;
    --btn2: #24364e;
    --border: rgba(255,255,255,0.10);
  }}
  body {{
    margin: 0;
    font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
  }}
  header {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: linear-gradient(to bottom, rgba(11,15,20,0.98), rgba(11,15,20,0.85));
    border-bottom: 1px solid var(--border);
    padding: 10px 12px;
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }}
  .title {{
    font-weight: 650;
    letter-spacing: 0.2px;
    margin-right: 10px;
    white-space: nowrap;
  }}
  .pill {{
    font-size: 12px;
    color: var(--muted);
    padding: 4px 8px;
    border: 1px solid var(--border);
    border-radius: 999px;
  }}
  button {{
    border: 1px solid var(--border);
    background: var(--btn);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 10px;
    cursor: pointer;
    font-weight: 600;
  }}
  button:hover {{ background: var(--btn2); }}
  .spacer {{ flex: 1; }}

  #stage {{
    height: calc(100vh - 56px);
    overflow: hidden;
    position: relative;
  }}
  #canvas {{
    position: absolute;
    left: 0;
    top: 0;
    transform-origin: 0 0;
    will-change: transform;
    user-select: none;
    -webkit-user-drag: none;
    image-rendering: pixelated; /* helps if you inspect rasterized previews */
  }}
  .hint {{
    position: absolute;
    left: 12px;
    bottom: 12px;
    background: rgba(17,24,38,0.75);
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 8px 10px;
    border-radius: 12px;
    font-size: 12px;
    max-width: min(640px, calc(100vw - 24px));
  }}
  code {{ color: var(--text); }}
</style>
</head>
<body>
<header>
  <div class="title">{title}</div>
  <span class="pill" id="status">scale: 1.00×</span>
  <button id="zin">Zoom +</button>
  <button id="zout">Zoom −</button>
  <button id="reset">Reset</button>
  <div class="spacer"></div>
  <span class="pill">{filename}</span>
</header>

<div id="stage">
  <img id="canvas" src="{rel_asset}" alt="pane"/>
  <div class="hint">
    <b>Controls:</b> Mouse wheel to zoom (around cursor). Click‑drag to pan. Double‑click to reset.
    Browser zoom also works, but this keeps the math sane.
  </div>
</div>

<script>
(() => {{
  const stage = document.getElementById('stage');
  const canvas = document.getElementById('canvas');
  const status = document.getElementById('status');
  const btnIn = document.getElementById('zin');
  const btnOut = document.getElementById('zout');
  const btnReset = document.getElementById('reset');

  let scale = {init_scale};
  let tx = 0;
  let ty = 0;
  let dragging = false;
  let lastX = 0;
  let lastY = 0;

  function apply() {{
    canvas.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    status.textContent = `scale: ${scale.toFixed(2)}×`;
  }}

  function clampScale(s) {{
    const minS = 0.1;
    const maxS = 50.0;
    return Math.min(maxS, Math.max(minS, s));
  }}

  function zoomAt(clientX, clientY, factor) {{
    const rect = stage.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;

    // Convert screen point to "world" point before zoom
    const worldX = (x - tx) / scale;
    const worldY = (y - ty) / scale;

    const newScale = clampScale(scale * factor);

    // Keep the world point under cursor fixed after zoom
    tx = x - worldX * newScale;
    ty = y - worldY * newScale;
    scale = newScale;
    apply();
  }}

  stage.addEventListener('wheel', (e) => {{
    e.preventDefault();
    const factor = (e.deltaY < 0) ? 1.12 : 1/1.12;
    zoomAt(e.clientX, e.clientY, factor);
  }}, {{ passive: false }});

  stage.addEventListener('mousedown', (e) => {{
    dragging = true;
    lastX = e.clientX;
    lastY = e.clientY;
  }});
  window.addEventListener('mouseup', () => dragging = false);
  window.addEventListener('mousemove', (e) => {{
    if (!dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    lastX = e.clientX;
    lastY = e.clientY;
    tx += dx;
    ty += dy;
    apply();
  }});

  stage.addEventListener('dblclick', () => {{
    scale = {init_scale};
    tx = 0; ty = 0;
    apply();
  }});

  btnIn.addEventListener('click', () => zoomAt(stage.getBoundingClientRect().left + stage.clientWidth/2,
                                              stage.getBoundingClientRect().top + stage.clientHeight/2, 1.2));
  btnOut.addEventListener('click', () => zoomAt(stage.getBoundingClientRect().left + stage.clientWidth/2,
                                               stage.getBoundingClientRect().top + stage.clientHeight/2, 1/1.2));
  btnReset.addEventListener('click', () => {{
    scale = {init_scale};
    tx = 0; ty = 0;
    apply();
  }});

  // Ensure the image loads before applying transforms (helps some browsers)
  canvas.addEventListener('load', () => apply());
  apply();
}})();
</script>
</body>
</html>
"""


def try_render_svg_to_png(svg_path: Path, png_path: Path, width: int) -> bool:
    """
    Render SVG -> PNG using cairosvg if available.
    Returns True if rendered, False if cairosvg missing.
    """
    try:
        import cairosvg  # type: ignore
    except Exception:
        return False

    svg_bytes = svg_path.read_bytes()
    cairosvg.svg2png(bytestring=svg_bytes, write_to=str(png_path), output_width=width)
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Create a pan/zoom HTML 'magnifier' viewer for a 2x2 pane (SVG or PNG)."
    )
    ap.add_argument("--in", dest="inp", required=True, help="Input pane file (SVG or PNG).")
    ap.add_argument("--out", dest="outdir", required=True, help="Output directory.")
    ap.add_argument("--name", default=None, help="Optional base name for outputs.")
    ap.add_argument("--init-scale", type=float, default=2.0, help="Initial zoom scale in the viewer (default: 2.0).")
    ap.add_argument("--render-png", action="store_true", help="If input is SVG, also render a large PNG (needs cairosvg).")
    ap.add_argument("--png-width", type=int, default=8192, help="Rendered PNG width if --render-png (default: 8192).")
    args = ap.parse_args(argv)

    inp = Path(args.inp)
    if not inp.exists():
        ap.error(f"input not found: {inp}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    base = args.name or inp.stem
    viewer_path = outdir / f"{base}_viewer.html"

    # Copy the asset into the outdir so the HTML is portable
    asset_ext = inp.suffix.lower()
    asset_copy = outdir / f"{base}{asset_ext}"
    if inp.resolve() != asset_copy.resolve():
        shutil.copy2(inp, asset_copy)

    rel_asset = asset_copy.name  # same directory as HTML

    # Optional raster render for SVG (handy for quick preview tools)
    rendered_png = None
    if args.render_png and asset_ext == ".svg":
        png_path = outdir / f"{base}_render.png"
        ok = try_render_svg_to_png(asset_copy, png_path, width=args.png_width)
        if ok:
            rendered_png = png_path
            # prefer PNG for viewing if generated (faster than SVG in some browsers)
            rel_asset = png_path.name

    html = HTML_TEMPLATE.format(
        title=f"Magnifier: {base}",
        filename=asset_copy.name,
        rel_asset=rel_asset,
        init_scale=float(args.init_scale),
    )
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
