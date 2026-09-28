"""Render uniform card artwork (JPEG) for every card in art.json into art/cards/.

Wide 1280x720 for everything except people (poster 600x900). Images come from TMDB (TMDB_API_KEY env, never printed).
Every render is checked (exact size, title fits in <= 2 lines, background found, file < MAX_BYTES, logo drawn when
specced); failures are listed and the run exits 1. A per-card log goes to art/render-log.json.

  python art.py [slug ...]
"""
import io, json, os, sys, urllib.error, urllib.parse, urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / 'art' / 'fonts' / 'Montserrat[wght].ttf'
OUT = ROOT / 'art' / 'cards'
SIZES = {'wide': (1280, 720), 'poster': (600, 900)}
ACCENT = (240, 190, 80)
QUALITY = int(os.environ.get('ART_QUALITY', '84'))
MAX_BYTES = 250_000
LOGO_BOX = {'wide': (300, 120), 'poster': (220, 90)}   # wordmark logos (networks/studios) fit inside this box
_memo = {}


def tmdb(path, **params):
    key = (path, tuple(sorted(params.items())))
    if key not in _memo:
        _memo[key] = _tmdb(path, **params)
    return _memo[key]


def _tmdb(path, **params):
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
    if k == 'path':
        return bg['path']
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
        provs = tmdb('/watch/providers/movie', watch_region='US')['results'] + \
            tmdb('/watch/providers/tv', watch_region='US')['results']
        path = next(p['logo_path'] for p in provs if p['provider_id'] == logo['id'])
        return fetch_image(path, 'original')
    if logo['kind'] in ('network', 'company'):
        path = tmdb(f"/{logo['kind']}/{logo['id']}").get('logo_path')
        return fetch_image(path, 'w500') if path else None
    raise SystemExit(f"unknown logo kind {logo['kind']}")


def wordmark(img):
    """Transparent wordmark logos are often dark: if the visible pixels are dark, render them white."""
    alpha = img.getchannel('A')
    px = [l for l, a in zip(img.convert('L').getdata(), alpha.getdata()) if a > 128]
    mean = sum(px) / len(px) if px else 255
    if mean < 110:
        white = Image.new('RGBA', img.size, (255, 255, 255, 255))
        white.putalpha(alpha)
        return white, round(mean)
    return img, round(mean)


def cover(img, size):
    w, h = size
    scale = max(w / img.width, h / img.height)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    left, top = (img.width - w) // 2, (img.height - h) // 3
    return img.crop((left, top, left + w, top + h))


_gradients = {}


def gradient(size, shape):
    if shape not in _gradients:
        w, h = size
        g = Image.new('L', (w, h))
        px = g.load()
        for y in range(h):
            for x in range(w):
                v = (1 - x / w) * 0.75 + (y / h) ** 2 * 0.65 if shape == 'wide' else (y / h) ** 1.6 * 0.95
                px[x, y] = int(min(v, 0.92) * 255)
        _gradients[shape] = g
    return _gradients[shape]


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
    return None, [text]                       # does not fit: reported as a failure by render()


def render(card):
    """Returns (path, log dict, list of failed checks)."""
    size = SIZES[card['shape']]
    w, h = size
    fails, log = [], {'slug': card['slug'], 'shape': card['shape']}
    bg_path = background_path(card['bg']) if card.get('bg') else None
    if bg_path:
        base = cover(fetch_image(bg_path), size)
    else:
        fails.append('no background')
        base = Image.new('RGBA', size, (28, 28, 34, 255))
    log['bg'] = card.get('bg', {}).get('from') or bg_path
    logo_kind = card.get('logo', {}).get('kind')
    if logo_kind == 'provider':
        base = base.filter(ImageFilter.GaussianBlur(4))
    shade = Image.new('RGBA', size, (0, 0, 0, 255))
    base = Image.composite(shade, base, gradient(size, card['shape']))
    draw = ImageDraw.Draw(base)
    margin = 64 if card['shape'] == 'wide' else 40
    title_f, lines = fit_lines(draw, card['title'], w - 2 * margin, 104 if card['shape'] == 'wide' else 72, 'ExtraBold')
    if title_f is None:
        fails.append('title does not fit in 2 lines')
        title_f = font(28, 'ExtraBold')
    log['title_px'], log['title_lines'] = title_f.size, len(lines)
    line_h = title_f.size * 1.08
    y = h - margin - line_h * len(lines)
    eyebrow_f = font(26 if card['shape'] == 'wide' else 22, 'Bold')
    draw.text((margin, y - eyebrow_f.size - 14), card['eyebrow'].upper(), font=eyebrow_f, fill=ACCENT)
    for i, line in enumerate(lines):
        draw.text((margin, y + i * line_h), line, font=title_f, fill=(255, 255, 255))
    if logo_kind == 'provider':                       # square app icon, rounded corners
        logo = logo_image(card['logo']).resize((150, 150), Image.LANCZOS)
        mask = Image.new('L', logo.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 149, 149), radius=30, fill=255)
        base.paste(logo, (margin, margin), mask)
        log['logo'] = 'provider icon'
    elif logo_kind:                                   # transparent wordmark, fitted inside the box, top-left
        logo = logo_image(card['logo'])
        if logo is None:
            fails.append(f'{logo_kind} has no logo on TMDB')
        else:
            logo, mean = wordmark(logo)
            bw, bh = LOGO_BOX[card['shape']]
            scale = min(bw / logo.width, bh / logo.height)
            logo = logo.resize((max(1, round(logo.width * scale)), max(1, round(logo.height * scale))), Image.LANCZOS)
            base.alpha_composite(logo, (margin, margin))
            log['logo'] = f'{logo_kind} wordmark (luma {mean}{", whitened" if mean < 110 else ""})'
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{card['slug']}.jpg"
    out = base.convert('RGB')
    if out.size != size:
        fails.append(f'size {out.size} != {size}')
    out.save(path, 'JPEG', quality=QUALITY, optimize=True, progressive=True)
    log['bytes'] = path.stat().st_size
    if log['bytes'] >= MAX_BYTES:
        fails.append(f"{log['bytes']} B >= {MAX_BYTES}")
    return path, log, fails


if __name__ == '__main__':
    spec = json.loads((ROOT / 'generator' / 'art.json').read_text(encoding='utf-8'))
    only = set(sys.argv[1:])
    logs, failed = [], []
    for c in spec['cards']:
        if only and c['slug'] not in only:
            continue
        try:
            p, log, fails = render(c)
        except Exception as e:                        # one bad card must not stop the other renders
            log, fails = {'slug': c['slug']}, [f'render error {type(e).__name__}']
        log['fails'] = fails
        logs.append(log)
        if fails:
            failed.append(f"{c['slug']}: {'; '.join(fails)}")
        print(f"{c['slug']:<45} {log.get('bytes', 0):>7} B  {'FAIL ' + '; '.join(fails) if fails else 'ok'}", flush=True)
    log_file = ROOT / 'art' / 'render-log.json'          # merged by slug, so partial renders keep earlier entries
    try:
        merged = {l['slug']: l for l in json.loads(log_file.read_text(encoding='utf-8'))}
    except (OSError, ValueError):
        merged = {}
    merged.update({l['slug']: l for l in logs})
    log_file.write_text(json.dumps(list(merged.values()), indent=1, ensure_ascii=False), encoding='utf-8')
    total = sum(l.get('bytes', 0) for l in logs)
    print(f'rendered {len(logs)}, total {total:,} B, failed {len(failed)}')
    for f in failed:
        print('FAIL', f)
    sys.exit(1 if failed else 0)
