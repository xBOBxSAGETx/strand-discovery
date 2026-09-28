"""Render the logo-card / typographic-card samples into one review sheet (outside the repo). No art/cards changes.

  python logo_samples.py <out dir>
"""
import json, sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import art, art_logo

SAMPLES = [  # (slug, eyebrow, title, badge, logo_src, icon_provider)
    ('streaming-netflix', 'Streaming', 'Netflix', None, {'kind': 'network', 'id': 213}, 8),
    ('streaming-netflix-new', 'Streaming', 'Netflix', 'New', {'kind': 'network', 'id': 213}, 8),
    ('streaming-netflix-top', 'Streaming', 'Netflix', 'Top Rated', {'kind': 'network', 'id': 213}, 8),
    ('streaming-hulu', 'Streaming', 'Hulu', None, {'kind': 'network', 'id': 453}, 15),
    ('streaming-hulu-new', 'Streaming', 'Hulu', 'New', {'kind': 'network', 'id': 453}, 15),
    ('streaming-hulu-top', 'Streaming', 'Hulu', 'Top Rated', {'kind': 'network', 'id': 453}, 15),
    ('streaming-disneyplus', 'Streaming', 'Disney+', None, {'kind': 'network', 'id': 2739}, 337),
    ('streaming-disneyplus-new', 'Streaming', 'Disney+', 'New', {'kind': 'network', 'id': 2739}, 337),
    ('streaming-disneyplus-top', 'Streaming', 'Disney+', 'Top Rated', {'kind': 'network', 'id': 2739}, 337),
    ('network-hbo', 'Network', 'HBO', None, {'kind': 'network', 'id': 49}, None),
    ('network-amc', 'Network', 'AMC', None, {'kind': 'network', 'id': 174}, None),
    ('studio-a24', 'Studio', 'A24', None, {'kind': 'company', 'id': 41077}, None),
    ('studio-pixar', 'Studio', 'Pixar', None, {'kind': 'company', 'id': 3}, None),
]
GENRES = [('genre-horror-new', 'Genre', 'Horror'), ('genre-comedy-new', 'Genre', 'Comedy')]


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    tiles, rows = [], []
    for slug, eyebrow, title, badge, src, icon in SAMPLES:
        card = {'slug': slug, 'shape': 'wide', 'eyebrow': eyebrow, 'title': title, 'badge': badge,
                'logo_src': src, 'icon_provider': icon}
        img, log, fails = art_logo.render_logo(card)
        tiles.append((slug, img.convert('RGB'), log, fails))
    for slug, eyebrow, title in GENRES:
        img, log, fails = art_logo.render_type({'slug': slug, 'shape': 'wide', 'eyebrow': eyebrow, 'title': title})
        tiles.append((slug, img.convert('RGB'), log, fails))
    tw, th, pad, cols = 400, 225, 14, 3
    rows_n = -(-len(tiles) // cols)
    sheet = Image.new('RGB', (pad + cols * (tw + pad), 60 + rows_n * (th + 60 + pad)), (18, 18, 22))
    d = ImageDraw.Draw(sheet)
    small = ImageFont.truetype(str(art.FONT), 13)
    d.text((pad, 16), 'Logo + typographic card samples', font=ImageFont.truetype(str(art.FONT), 26), fill=(240, 190, 80))
    for i, (slug, img, log, fails) in enumerate(tiles):
        x = pad + (i % cols) * (tw + pad)
        y = 60 + (i // cols) * (th + 60 + pad)
        sheet.paste(img.resize((tw, th)), (x, y))
        img.save(out / f'{slug}.jpg', 'JPEG', quality=art.QUALITY, optimize=True, progressive=True)
        d.text((x, y + th + 4), slug + ('  FAIL ' + '; '.join(fails) if fails else ''), font=small,
               fill=(255, 90, 90) if fails else (220, 220, 220))
        d.text((x, y + th + 22), f"field {log.get('field', '')}  contrast {log.get('contrast')}"
               + (f"  logo->{log['logo_recoloured']}" if log.get('logo_recoloured') else ''), font=small, fill=(150, 150, 160))
        d.text((x, y + th + 40), str(log.get('logo', ''))[:60], font=small, fill=(120, 120, 130))
        rows.append({k: v for k, v in log.items()} | {'fails': fails})
    sheet.save(out / 'logo-samples.jpg', 'JPEG', quality=88)
    (out / 'logo-samples.json').write_text(json.dumps(rows, indent=1), encoding='utf-8')
    print(out / 'logo-samples.jpg')


if __name__ == '__main__':
    main()
