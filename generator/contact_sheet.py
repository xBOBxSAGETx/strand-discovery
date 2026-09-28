"""Contact sheets for art QA: one sheet per Strand folder (split at 48 cards), thumbnails in folder order with the
slug and the render log's background source underneath. Review artifacts only - written outside the repo.

  python contact_sheet.py <out dir> [folder key ...]
"""
import json, sys, textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / 'art' / 'fonts' / 'Montserrat[wght].ttf'
THUMB = {'wide': (320, 180), 'poster': (160, 240)}
COLS = {'wide': 6, 'poster': 8}
PER_SHEET = 48
CAPTION_H = 44
PAD = 12


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    spec = json.loads((ROOT / 'generator' / 'spec.json').read_text(encoding='utf-8'))
    logs = {l['slug']: l for l in json.loads((ROOT / 'art' / 'render-log.json').read_text(encoding='utf-8'))}
    small = ImageFont.truetype(str(FONT), 12)
    head = ImageFont.truetype(str(FONT), 26)
    written = []
    only = set(sys.argv[2:])
    order = {f['key']: i + 1 for i, f in enumerate(spec['folders'])}
    for folder in spec['folders']:
        if only and folder['key'] not in only:
            continue
        cats = [c for c in spec['catalogs'] if folder['key'] in c['folders']]
        if folder['shape'] == 'poster':      # people folders: CSV rank order
            cats.sort(key=lambda c: c['ranks'][folder['key']])
        tw, th = THUMB[folder['shape']]
        cols = COLS[folder['shape']]
        for part, start in enumerate(range(0, len(cats), PER_SHEET), 1):
            chunk = cats[start:start + PER_SHEET]
            rows = -(-len(chunk) // cols)
            sheet = Image.new('RGB', (PAD + cols * (tw + PAD), 60 + rows * (th + CAPTION_H + PAD)), (18, 18, 22))
            d = ImageDraw.Draw(sheet)
            d.text((PAD, 16), f"{folder['title']}  ({len(cats)} cards, sheet {part})", font=head, fill=(240, 190, 80))
            for i, c in enumerate(chunk):
                x = PAD + (i % cols) * (tw + PAD)
                y = 60 + (i // cols) * (th + CAPTION_H + PAD)
                f = ROOT / 'art' / 'cards' / f"{c['slug']}.jpg"
                if f.exists():
                    sheet.paste(Image.open(f).convert('RGB').resize((tw, th)), (x, y))
                else:
                    d.rectangle((x, y, x + tw, y + th), outline=(255, 60, 60), width=3)
                log = logs.get(c['slug'], {})
                bad = bool(log.get('fails')) or not f.exists()
                cap = c['slug'] + ('  FAIL' if bad else '')
                src = str(log.get('bg') or '')
                d.text((x, y + th + 3), textwrap.shorten(cap, 44 if folder['shape'] == 'wide' else 24),
                       font=small, fill=(255, 90, 90) if bad else (220, 220, 220))
                d.text((x, y + th + 19), textwrap.shorten(src, 50 if folder['shape'] == 'wide' else 26),
                       font=small, fill=(140, 140, 150))
            p = out / f"{order[folder['key']]:02d}-{folder['key']}-{part}.jpg"
            sheet.save(p, 'JPEG', quality=85)
            written.append(p)
    print(f'{len(written)} sheets -> {out}')


if __name__ == '__main__':
    main()
