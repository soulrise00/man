#!/usr/bin/env python
"""Reproducible usability benchmark for the 누끼 models.

Runs one or more rembg models over a folder of images, measuring per-image
latency, peak memory, and the foreground ratio, then writes:

  * ``<out>/comparison.png`` — a side-by-side grid (original + each model) on a
    checkerboard so edge quality is visible, and
  * a Markdown table to stdout (paste it into BENCHMARK.md).

This is the script behind the numbers reported in BENCHMARK.md.

    python -m scripts.evaluate samples/input \
        --models u2net isnet-general-use birefnet-general-lite birefnet-general \
        --out samples

Each model runs in its **own subprocess** (``--isolate``, the default) so a
heavy model cannot leave residual memory that OOMs the next one.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def list_images(folder: Path) -> list[Path]:
    return [p for p in sorted(folder.iterdir()) if p.suffix.lower() in IMAGE_EXTS]


def run_model(model: str, folder: Path, out_root: Path) -> dict:
    """Process every image in ``folder`` with one model. Returns metrics."""
    os.environ.setdefault("OMP_NUM_THREADS", "4")
    from PIL import Image
    from rembg import new_session, remove

    outdir = out_root / f"out_{model}"
    outdir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    session = new_session(model)
    load_s = time.perf_counter() - t0

    times, ratios = [], []
    for p in list_images(folder):
        img = Image.open(p).convert("RGB")
        t = time.perf_counter()
        cut = remove(img, session=session, post_process_mask=True)
        dt = time.perf_counter() - t
        if cut.mode != "RGBA":
            cut = cut.convert("RGBA")
        cut.save(outdir / f"{p.stem}.png")
        times.append(dt)
        alpha = cut.split()[-1]
        hist = alpha.histogram()
        total = sum(hist) or 1
        ratios.append(sum(i * c for i, c in enumerate(hist)) / (255.0 * total))
        gc.collect()

    peak_gb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    return {
        "model": model,
        "load_s": round(load_s, 1),
        "avg_s": round(sum(times) / len(times), 2) if times else 0,
        "min_s": round(min(times), 2) if times else 0,
        "max_s": round(max(times), 2) if times else 0,
        "peak_gb": round(peak_gb, 2),
        "avg_fg": round(sum(ratios) / len(ratios), 3) if ratios else 0,
        "n": len(times),
    }


def build_grid(folder: Path, models: list[str], out_root: Path) -> Path:
    from PIL import Image, ImageDraw

    names = [p.stem for p in list_images(folder)]
    cols = [("original", folder / "{}")] + [
        (m, out_root / f"out_{m}" / "{}.png") for m in models
    ]
    cell, pad, header = 230, 8, 24

    def checker(size, s=12):
        bg = Image.new("RGB", size, (235, 235, 235))
        d = ImageDraw.Draw(bg)
        for y in range(0, size[1], s):
            for x in range(0, size[0], s):
                if (x // s + y // s) % 2:
                    d.rectangle([x, y, x + s, y + s], fill=(205, 205, 205))
        return bg

    def fit(im):
        im = im.copy(); im.thumbnail((cell, cell)); return im

    grid = Image.new(
        "RGB",
        (len(cols) * (cell + pad) + pad, len(names) * (cell + pad) + pad + header),
        (255, 255, 255),
    )
    d = ImageDraw.Draw(grid)
    for ci, (title, _) in enumerate(cols):
        d.text((ci * (cell + pad) + pad + 4, 6), title, fill=(0, 0, 0))
    for ri, n in enumerate(names):
        for ci, (title, tpl) in enumerate(cols):
            x, y = ci * (cell + pad) + pad, ri * (cell + pad) + pad + header
            if ci == 0:
                candidates = list(folder.glob(n + ".*"))
                if not candidates:
                    continue
                p = candidates[0]
            else:
                p = Path(str(tpl).format(n))
            if not p.exists():
                continue
            im = Image.open(p)
            if im.mode == "RGBA":
                base = checker((cell, cell)); t = fit(im)
                base.paste(t, ((cell - t.width) // 2, (cell - t.height) // 2), t); c = base
            else:
                c = checker((cell, cell)); t = fit(im.convert("RGB"))
                c.paste(t, ((cell - t.width) // 2, (cell - t.height) // 2))
            grid.paste(c, (x, y))
    out = out_root / "comparison.png"
    grid.save(out)
    return out


def print_table(rows: list[dict]) -> None:
    print("\n| model | load | avg/img | min | max | peak RAM | avg fg | n |")
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| `{r['model']}` | {r['load_s']}s | **{r['avg_s']}s** | {r['min_s']}s "
            f"| {r['max_s']}s | {r['peak_gb']} GB | {r['avg_fg']} | {r['n']} |"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark 누끼 models")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--models", nargs="+", default=["u2net", "isnet-general-use", "birefnet-general"])
    ap.add_argument("--out", type=Path, default=Path("samples"))
    ap.add_argument("--no-isolate", action="store_true", help="Run all models in one process")
    ap.add_argument("--_single", help=argparse.SUPPRESS)  # internal: child mode
    args = ap.parse_args()

    # Child mode: run one model and emit JSON metrics.
    if args._single:
        print("__METRICS__" + json.dumps(run_model(args._single, args.folder, args.out)))
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for model in args.models:
        print(f"→ {model} ...", flush=True)
        if args.no_isolate:
            rows.append(run_model(model, args.folder, args.out))
        else:
            cmd = [sys.executable, __file__, str(args.folder), "--out", str(args.out), "--_single", model]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            line = next((l for l in proc.stdout.splitlines() if l.startswith("__METRICS__")), None)
            if line is None:
                print(proc.stdout[-500:]); print(proc.stderr[-500:])
                print(f"  (failed: {model})")
                continue
            m = json.loads(line[len("__METRICS__"):])
            rows.append(m)
            print(f"  avg={m['avg_s']}s peak={m['peak_gb']}GB")

    grid = build_grid(args.folder, [r["model"] for r in rows], args.out)
    print_table(rows)
    print(f"\nGrid: {grid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
