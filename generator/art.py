"""Render uniform card artwork (JPEG) for every card in art.json into art/cards/.

Wide 1280x720 for everything except people (poster 600x900). Images come from TMDB (TMDB_API_KEY env, never printed).
Every render is checked (exact size, title fits in <= 2 lines, background found, file < MAX_BYTES, logo drawn when
specced; a Movie Series title logo inside the 4% safe area and legible on its photo); failures are listed and the
run exits 1. A per-card log goes to art/render-log.json.

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


TITLE_LOGO_BOX = {'wide': (0.50, 0.34), 'poster': (0.80, 0.22)}   # title logo fits in this share of the card (w, h)
TITLE_LOGO_MIN_W = 200                          # narrower after the fit = unreadable on a TV row: fail
TITLE_LOGO_MIN_H = {'wide': 60, 'poster': 40}   # a thin strip after the fit: fail
MIN_TITLE_CONTRAST = 3.0                        # WCAG ratio, each logo pixel vs the photo pixel right under it...
MIN_LEGIBLE_SHARE = 0.90                        # ...reached by at least this share of a FLAT logo's opaque pixels
MIN_LEGIBLE_SHARE_MULTI = 0.50                  # multi-colour logos (emblems, outlines) carry inner contrast: >= half
FLAT_LOGO_SD = 30                               # luminance std-dev below this = a one-colour logo (may be recoloured)
# Visual-weight cap: fitted width x height <= this share of the card. A plain height cap can't tell Alien (640x242,
# dominated the card) from Toy Story (325x245, fine); the area can. 0.13 of 1280x720 = 119,808 px2, just above the
# largest sample that looked right (The Dark Knight 640x181 = 115,840): only logos that fill the box in BOTH
# directions shrink (trimmed width/height between ~2.0 and ~3.4 on a wide card).
TITLE_LOGO_MAX_AREA = 0.13
# Strand's tvOS hero shows only the centre band of a card whose shelf fills the hero (H1 findings.md:7, observed
# y 25-75 %; H4 uses 0.246-0.754). Movie Series is such a shelf, so on wide cards the eyebrow + title logo block
# sits inside this band, HERO_PAD above its bottom edge, and is asserted there.
HERO_BAND = (0.246, 0.754)
HERO_PAD = 14
# Movie Series eyebrow: asserted legible like a flat logo (MIN_LEGIBLE_SHARE of its pixels at MIN_TITLE_CONTRAST).
# When the photo under it is too bright/busy, a soft dark scrim is laid behind the eyebrow only (no card-wide
# darkening), getting stronger step by step until the assert passes. EYEBROW_FIX = False is the seeded control.
EYEBROW_FIX = True
EYEBROW_SCRIM_ALPHAS = (110, 150, 190, 230)


def title_logo_xy(size, lw, lh, margin):
    """Left edge; bottom HERO_PAD above the hero band on wide cards, else where the typeset title sits."""
    if size[0] > size[1]:
        return margin, int(size[1] * HERO_BAND[1]) - HERO_PAD - lh
    return margin, size[1] - margin - lh


def draw_eyebrow(base, card, xy, eyebrow_f, fails, log, check):
    """Draw the eyebrow at xy; with check, assert its legibility and fix it with a local scrim. Returns its bbox."""
    text = card['eyebrow'].upper()
    draw = ImageDraw.Draw(base)
    bbox = draw.textbbox(xy, text, font=eyebrow_f)
    if check:
        layer = Image.new('RGBA', base.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(xy, text, font=eyebrow_f, fill=ACCENT)
        glyphs = layer.crop(bbox)
        share = legible_share(glyphs, base.crop(bbox))
        log['eyebrow_legible_raw'] = round(share, 3)
        orig = base.copy()                            # each step is one scrim on the original photo, not stacked
        for alpha in (EYEBROW_SCRIM_ALPHAS if EYEBROW_FIX else ()):
            if share >= MIN_LEGIBLE_SHARE:
                break
            pad_x, pad_y = 14, 6
            mask = Image.new('L', base.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((bbox[0] - pad_x, bbox[1] - pad_y, bbox[2] + pad_x, bbox[3] + pad_y),
                                                   radius=10, fill=alpha)
            mask = mask.filter(ImageFilter.GaussianBlur(4))
            scrim = Image.new('RGBA', base.size, (0, 0, 0, 0))
            scrim.putalpha(mask)
            trial = orig.copy()
            trial.alpha_composite(scrim)
            share = legible_share(glyphs, trial.crop(bbox))
            base.paste(trial)
            log['eyebrow_scrim'] = alpha
        log['eyebrow_legible'] = round(share, 3)
        if share < MIN_LEGIBLE_SHARE:
            fails.append(f'eyebrow contrast: only {share:.0%} of its pixels reach {MIN_TITLE_CONTRAST}:1 '
                         f'(need {MIN_LEGIBLE_SHARE:.0%})')
    ImageDraw.Draw(base).text(xy, text, font=eyebrow_f, fill=ACCENT)
    return bbox


def check_hero_band(boxes, size, fails, log):
    """Fail if any element (eyebrow, title logo) leaves the vertical band the tvOS hero shows."""
    lo, hi = size[1] * HERO_BAND[0], size[1] * HERO_BAND[1]
    out = [f"{name} y{int(y0)}-{int(y1)}" for name, (x0, y0, x1, y1) in boxes if y0 < lo or y1 > hi]
    log['hero_band'] = 'ok' if not out else 'FAIL'
    if out:
        fails.append(f'outside hero band y{int(lo)}-{int(hi)}: ' + '; '.join(out))


def legible_share(logo, under):
    """Share of the logo's opaque pixels whose colour reaches MIN_TITLE_CONTRAST against the photo pixel under it
    (both sampled at <= 160 px wide). A mean-vs-mean check would pass a white logo over a black-and-white stripe."""
    import art_logo
    s = min(1.0, 160 / logo.width)
    sz = (max(1, round(logo.width * s)), max(1, round(logo.height * s)))
    lg, bg = logo.resize(sz, Image.BOX), under.convert('RGB').resize(sz, Image.BOX)
    n = ok = 0
    for (r, g, b, a), px in zip(lg.getdata(), bg.getdata()):
        if a > 128:
            n += 1
            ok += art_logo.contrast((r, g, b), px) >= MIN_TITLE_CONTRAST
    return ok / n if n else 1.0


def is_flat(logo):
    """One-colour logo (a plain wordmark): its opaque pixels' luminance barely varies."""
    from PIL import ImageStat
    mask = logo.getchannel('A').point(lambda a: 255 if a > 128 else 0)
    return not mask.getbbox() or ImageStat.Stat(logo.convert('L'), mask).stddev[0] < FLAT_LOGO_SD


def draw_title_logo(base, card, margin, fails, log):
    """Composite card['title_logo'] (planned by art_plan) bottom-left. Returns (base, top y, boxes). Legibility: see
    legible_share(). A flat (one-colour) logo below MIN_LEGIBLE_SHARE is recoloured white or near-black, whichever
    reads better, and re-checked. A multi-colour logo is never recoloured (that turned Toy Story and the Dark Knight
    bat into white blobs in the first samples); it needs MIN_LEGIBLE_SHARE_MULTI. Below the bar = a failed card
    (fix: a title_logo_pin or a typeset fallback in art_overrides.json)."""
    import art_logo
    tl = card['title_logo']
    size = SIZES[card['shape']]
    logo = fetch_image(tl['path'], 'original')
    bbox = logo.getchannel('A').point(lambda a: 255 if a > 20 else 0).getbbox()    # trim transparent margins
    if bbox:
        logo = logo.crop(bbox)
    bw, bh = (size[0] * TITLE_LOGO_BOX[card['shape']][0], size[1] * TITLE_LOGO_BOX[card['shape']][1])
    scale = min(bw / logo.width, bh / logo.height)
    cap = TITLE_LOGO_MAX_AREA * size[0] * size[1]
    if logo.width * logo.height * scale * scale > cap:     # visual-weight cap (see TITLE_LOGO_MAX_AREA)
        scale = (cap / (logo.width * logo.height)) ** 0.5
        log['title_logo_capped'] = True
    lw, lh = max(1, round(logo.width * scale)), max(1, round(logo.height * scale))
    logo = logo.resize((lw, lh), Image.LANCZOS)
    if lw < TITLE_LOGO_MIN_W or lh < TITLE_LOGO_MIN_H[card['shape']]:
        fails.append(f'title logo too small after fit ({lw}x{lh}px)')
    x, y = title_logo_xy(size, lw, lh, margin)
    under = base.crop((x, y, x + lw, y + lh))         # the photo right under the logo (off-canvas parts read black)
    share = legible_share(logo, under)
    flat = is_flat(logo)
    need = MIN_LEGIBLE_SHARE if flat else MIN_LEGIBLE_SHARE_MULTI
    log['title_logo_kind'] = 'flat' if flat else 'multi-colour'
    if flat and share < need:                         # recolour to the flat colour that reads best, then re-check
        best = max([(255, 255, 255), (18, 18, 22)], key=lambda c: legible_share(art_logo.recolour(logo, c), under))
        logo = art_logo.recolour(logo, best)
        share = legible_share(logo, under)
        log['title_logo_recoloured'] = '#%02x%02x%02x' % best
    log['title_logo_legible'] = round(share, 3)
    if share < need:
        fails.append(f'title logo contrast: only {share:.0%} of its pixels reach {MIN_TITLE_CONTRAST}:1 '
                     f"(need {need:.0%}, {log['title_logo_kind']})")
    shadow = Image.new('RGBA', size, (0, 0, 0, 0))    # soft drop shadow, as on the logo cards
    black = art_logo.recolour(logo, (0, 0, 0))
    shadow.paste(black, (x, y + 4), black)
    shadow = shadow.filter(ImageFilter.GaussianBlur(10))
    shadow.putalpha(shadow.getchannel('A').point(lambda a: int(a * 0.5)))
    base.alpha_composite(shadow)
    layer = Image.new('RGBA', size, (0, 0, 0, 0))     # paste via a layer: the logo may be (wrongly) off-canvas
    layer.paste(logo, (x, y), logo)
    base.alpha_composite(layer)
    log['title_logo'] = f"{tl['source']} {tl['path']} ({tl.get('lang') or 'untagged'}{', PINNED' if tl.get('pinned') else ''})"
    log['title_logo_px'] = f'{lw}x{lh}'
    return base, y, [('title logo', (x, y, x + lw, y + lh))]


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
    margin = 64 if card['shape'] == 'wide' else 40
    if (card.get('title_logo') or {}).get('path'):    # Movie Series: the franchise's own title logo (#15)
        base, y, boxes = draw_title_logo(base, card, margin, fails, log)
        draw = ImageDraw.Draw(base)
    else:                                             # typeset title (also the documented title-logo fallback)
        if card.get('title_logo'):
            log['title_logo'] = f"fallback: {card['title_logo'].get('fallback')}"
        draw = ImageDraw.Draw(base)
        title_f, lines = fit_lines(draw, card['title'], w - 2 * margin, 104 if card['shape'] == 'wide' else 72, 'ExtraBold')
        if title_f is None:
            fails.append('title does not fit in 2 lines')
            title_f = font(28, 'ExtraBold')
        log['title_px'], log['title_lines'] = title_f.size, len(lines)
        line_h = title_f.size * 1.08
        y = h - margin - line_h * len(lines)
        if card.get('title_logo') and card['shape'] == 'wide':    # Movie Series fallback: inside the hero band too
            last_bottom = draw.textbbox((0, 0), lines[-1], font=title_f)[3]
            y = int(h * HERO_BAND[1]) - HERO_PAD - (len(lines) - 1) * line_h - last_bottom
        boxes = []
        for i, line in enumerate(lines):
            draw.text((margin, y + i * line_h), line, font=title_f, fill=(255, 255, 255))
            boxes.append((f'title line {i + 1}', draw.textbbox((margin, y + i * line_h), line, font=title_f)))
    eyebrow_f = font(26 if card['shape'] == 'wide' else 22, 'Bold')
    ey = (margin, y - eyebrow_f.size - 14)
    series = 'title_logo' in card                     # Movie Series (logo or fallback): eyebrow legibility asserted
    boxes.insert(0, ('eyebrow', draw_eyebrow(base, card, ey, eyebrow_f, fails, log, check=series)))
    draw = ImageDraw.Draw(base)
    if logo_kind == 'provider':                       # square app icon, rounded corners
        logo = logo_image(card['logo']).resize((150, 150), Image.LANCZOS)
        mask = Image.new('L', logo.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 149, 149), radius=30, fill=255)
        base.paste(logo, (margin, margin), mask)
        boxes.append(('logo', (margin, margin, margin + 150, margin + 150)))
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
            boxes.append(('logo', (margin, margin, margin + logo.width, margin + logo.height)))
            log['logo'] = f'{logo_kind} wordmark (luma {mean}{", whitened" if mean < 110 else ""})'
    import art_logo                                   # 4% safe-area assert, same as the logo / type cards
    if card.get('title_logo') and card['shape'] == 'wide':
        check_hero_band(boxes, size, fails, log)      # eyebrow + title (logo or typeset) inside the tvOS hero band
    art_logo.check_bounds(boxes, size, fails, log)
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


def render_any(card):
    """Photo card (render) or, for style 'logo' / 'type', a logo / typographic card (art_logo); same file checks."""
    if card.get('style') not in ('logo', 'type'):
        return render(card)
    import art_logo                                   # imported here: art_logo imports this module
    img, log, fails = (art_logo.render_logo if card['style'] == 'logo' else art_logo.render_type)(card)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{card['slug']}.jpg"
    out = img.convert('RGB')
    if out.size != SIZES[card['shape']]:
        fails.append(f"size {out.size} != {SIZES[card['shape']]}")
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
            p, log, fails = render_any(c)
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
