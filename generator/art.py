"""Render uniform card artwork (JPEG) for every card in art.json into art/cards/.

Wide 1280x720 for everything except people (poster 600x900). Images come from TMDB (TMDB_API_KEY env, never printed).
"""
import io, json, os, sys, urllib.parse, urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / 'art' / 'fonts' / 'Montserrat[wght].ttf'
OUT = ROOT / 'art' / 'cards'
SIZES = {'wide': (1280, 720), 'poster': (600, 900)}
ACCENT = (240, 190, 80)
QUALITY = int(os.environ.get('ART_QUALITY', '84'))


def tmdb(path, **params):
    q = urllib.parse.urlencode({**params, 'api_key': os.environ['TMDB_API_KEY']})
    try:  # the key rides in the URL: report only the API path and the error class, never the URL
        with urllib.request.urlopen(urllib.request.Request(f'https://api.themoviedb.org/3{path}?{q}',
                                                           headers={'Accept-Encoding': 'identity'}), timeout=30) as r:
            return json.load(r)
    except Exception as e:
        kind = f'HTTP {e.code}' if isinstance(e, urllib.error.HTTPError) else type(e).__name__
        raise RuntimeError(f'TMDB request failed on {path}: {kind}') from None


def fetch_image(path, size='original'):
    with urllib.request.urlopen(f'https://image.tmdb.org/t/p/{size}{path}', timeout=60) as r:
        return Image.open(io.BytesIO(r.read())).convert('RGBA')


def background_path(bg):
    k = bg['kind']
    if k == 'movie':
        return tmdb(f"/movie/{bg['id']}")['backdrop_path']
    if k == 'tv':
        return tmdb(f"/tv/{bg['id']}")['backdrop_path']
    if k == 'collection':
        return tmdb(f"/collection/{bg['id']}")['backdrop_path']
    if k == 'person':
        return tmdb(f"/person/{bg['id']}")['profile_path']
    if k == 'search_movie':
        res = tmdb('/search/movie', query=bg['query'], year=bg.get('year', ''))['results']
        return res[0]['backdrop_path']
    raise SystemExit(f'unknown bg kind {k}')


def logo_image(logo):
    if logo['kind'] == 'provider':
        provs = tmdb('/watch/providers/movie', watch_region='US')['results']
        path = next(p['logo_path'] for p in provs if p['provider_id'] == logo['id'])
        return fetch_image(path, 'w300')
    raise SystemExit(f"unknown logo kind {logo['kind']}")


def cover(img, size):
    w, h = size
    scale = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 3
    return img.crop((left, top, left + w, top + h))


def gradient(size, shape):
    w, h = size
    g = Image.new('L', (w, h))
    px = g.load()
    for y in range(h):
        for x in range(w):
            v = (1 - x / w) * 0.75 + (y / h) ** 2 * 0.65 if shape == 'wide' else (y / h) ** 1.6 * 0.95
            px[x, y] = int(min(v, 0.92) * 255)
    return g


def font(size, weight):
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_name(weight)
    return f


def fit_lines(draw, text, max_w, start, weight, max_lines=2):
    size = start
    while size > 28:
        f = font(size, weight)
        words, lines, cur = text.split(), [], ''
        for word in words:
            trial = f'{cur} {word}'.strip()
            if draw.textlength(trial, font=f) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
        if len(lines) <= max_lines and all(draw.textlength(l, font=f) <= max_w for l in lines):
            return f, lines
        size -= 4
    return font(28, weight), [text]


def render(card):
    size = SIZES[card['shape']]
    w, h = size
    base = cover(fetch_image(background_path(card['bg'])), size)
    if card.get('logo'):
        base = base.filter(ImageFilter.GaussianBlur(4))
    shade = Image.new('RGBA', size, (0, 0, 0, 255))
    base = Image.composite(shade, base, gradient(size, card['shape']))
    draw = ImageDraw.Draw(base)
    margin = 64 if card['shape'] == 'wide' else 40
    title_f, lines = fit_lines(draw, card['title'], w - 2 * margin, 104 if card['shape'] == 'wide' else 72, 'ExtraBold')
    line_h = title_f.size * 1.08
    y = h - margin - line_h * len(lines)
    eyebrow_f = font(26 if card['shape'] == 'wide' else 22, 'Bold')
    draw.text((margin, y - eyebrow_f.size - 14), card['eyebrow'].upper(), font=eyebrow_f, fill=ACCENT)
    for i, line in enumerate(lines):
        draw.text((margin, y + i * line_h), line, font=title_f, fill=(255, 255, 255))
    if card.get('logo'):
        logo = logo_image(card['logo']).resize((150, 150), Image.LANCZOS)
        mask = Image.new('L', logo.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 149, 149), radius=30, fill=255)
        base.paste(logo, (margin, margin), mask)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{card['slug']}.jpg"
    base.convert('RGB').save(path, 'JPEG', quality=QUALITY, optimize=True, progressive=True)
    return path


if __name__ == '__main__':
    spec = json.loads((ROOT / 'generator' / 'art.json').read_text(encoding='utf-8'))
    only = set(sys.argv[1:])
    for c in spec['cards']:
        if not only or c['slug'] in only:
            p = render(c)
            print(f'{p.relative_to(ROOT)}  {p.stat().st_size} B')
