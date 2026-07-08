# ctx2img.py - optical context compression: render long text into PNG "pages" that Claude
# reads visually at fewer tokens than the raw text would cost.
# Technique: DeepSeek-OCR "Contexts Optical Compression" / Zhipu Glyph (2025), adapted for
# Claude's native vision. Claude image cost ~= ceil(w*h/750) tokens, long edge capped 1568px,
# so each page is rendered at 1088x1088 (~1578 tokens) and packed with monospace text.
# HONESTY: compression ratio is MEASURED and printed per run (text-token estimate vs image
# tokens), never assumed. Expect ~2-4x with Claude as reader; lossy for tiny glyphs - keep
# font >= 11px for reliable reading. Tables/code survive (monospace); don't use for text
# where a single-character error is fatal (keys, hashes).
#
# CLI:
#   python ctx2img.py <input.txt|.md|.log> [outdir]       -> pages + manifest, prints stats
#   python ctx2img.py --stats-only <input>                -> just the token math
import json, math, sys, time
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

PAGE_W, PAGE_H = 1088, 1088          # 1088*1088/750 ~= 1578 tokens/page, under the 1568 long-edge cap
MARGIN = 12
FONT_SIZE = 10                        # Consolas 10px: the density/legibility knee for Claude vision
LINE_H = FONT_SIZE + 2
FONT = "consola.ttf"

def compact(text):
    """--dense mode: reflow prose to full-width, collapse blank runs. Short structural lines
    (headings, bullets, code) are kept as-is; consecutive prose lines are joined so pages fill."""
    out, para = [], []
    def flush():
        if para:
            out.append(" ".join(para)); para.clear()
    for raw in text.splitlines():
        s = raw.rstrip()
        st = s.strip()
        if not st:
            flush()
            if out and out[-1] != "":
                out.append("")
        elif st.startswith(("#", "-", "*", "|", ">", "```")) or raw[:1] in (" ", "\t") or len(st) < 40:
            flush(); out.append(s)
        else:
            para.append(st)
    flush()
    return "\n".join(out)

def _wrap(text, chars_per_line):
    out = []
    for raw in text.splitlines():
        raw = raw.replace("\t", "    ")
        if not raw:
            out.append("")
            continue
        while len(raw) > chars_per_line:
            out.append(raw[:chars_per_line])
            raw = raw[chars_per_line:]
        out.append(raw)
    return out

def render(text, outdir, stem="ctx"):
    outdir = Path(outdir); outdir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.truetype(FONT, FONT_SIZE)
    char_w = font.getlength("M")                      # monospace: constant advance
    cols = int((PAGE_W - 2 * MARGIN) // char_w)
    rows = int((PAGE_H - 2 * MARGIN) // LINE_H)
    lines = _wrap(text, cols)
    pages = []
    for p in range(0, len(lines), rows):
        chunk = lines[p:p + rows]
        img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        d = ImageDraw.Draw(img)
        y = MARGIN
        for ln in chunk:
            d.text((MARGIN, y), ln, fill="black", font=font)
            y += LINE_H
        # page footer for provenance (costs one line of density, saves confusion)
        d.text((MARGIN, PAGE_H - MARGIN + 1), f"[{stem} p{len(pages)+1}]", fill="#888888", font=font)
        f = outdir / f"{stem}-p{len(pages)+1:02d}.png"
        img.save(f, optimize=True)
        pages.append(str(f))
    return pages, cols, rows

def stats(text, n_pages):
    text_tokens = math.ceil(len(text) / 4)            # standard ~4 chars/token estimate
    img_tokens = n_pages * math.ceil(PAGE_W * PAGE_H / 750)
    return {"chars": len(text), "estTextTokens": text_tokens,
            "pages": n_pages, "imgTokens": img_tokens,
            "ratio": round(text_tokens / img_tokens, 2) if img_tokens else 0}

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__ or "usage: ctx2img.py <input> [outdir]"); return 1
    src = Path(args[0])
    text = src.read_text(encoding="utf-8", errors="replace")
    if "--dense" in sys.argv:
        text = compact(text)
    font = ImageFont.truetype(FONT, FONT_SIZE)
    cols = int((PAGE_W - 2 * MARGIN) // font.getlength("M"))
    rows = int((PAGE_H - 2 * MARGIN) // LINE_H)
    n_pages_est = max(1, math.ceil(len(_wrap(text, cols)) / rows))
    if "--stats-only" in sys.argv:
        print(json.dumps(stats(text, n_pages_est), indent=1)); return 0
    outdir = args[1] if len(args) > 1 else (Path(__file__).parent / "ctx-pages" / src.stem)
    pages, cols, rows = render(text, outdir, src.stem[:24])
    s = stats(text, len(pages))
    manifest = {"source": str(src), "created": time.strftime("%Y-%m-%d %H:%M"),
                "grid": f"{cols}x{rows}", **s, "pages": pages}
    (Path(outdir) / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(json.dumps(manifest, indent=1))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
