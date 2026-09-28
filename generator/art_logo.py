"""Logo cards (streaming services, networks, studios) and typographic cards (Genres · New).

A logo card is the brand's own logo, large and centred, on a field in the brand's colour: no movie imagery, so it
never goes stale. The variant is a pill badge top-right ("NEW" / "TOP RATED"; Popular has none) and the category
is a small eyebrow top-left ("STREAMING" / "NETWORK" / "STUDIO"), which also separates "Netflix" the service from
"Netflix" the network.

Colour: card['field'] (hex) if given (art_overrides), else derived automatically: the most common saturated colour of
the brand's logo, else of its streaming app icon, else a neutral dark. The field is that colour darkened into a soft
radial gradient. The logo keeps its own colours when it contrasts with the field (WCAG ratio >= 3); otherwise it is
recoloured white or near-black, whichever contrasts more. The contrast actually achieved is logged and asserted.
"""
import colorsys, io, math

from PIL import Image, ImageDraw, ImageFilter

import art

NEUTRAL = (27, 27, 34)
MIN_CONTRAST = 3.0


def luminance(rgb):
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = rgb
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def hexrgb(h):
    h = h.lstrip('#')
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def dominant(img, saturated=True):
    """Most common colour (bucketed) among opaque pixels; saturated=True ignores greys/black/white."""
    small = img.convert('RGBA').resize((96, max(1, round(96 * img.height / img.width))))
    counts = {}
    for r, g, b, a in small.getdata():
        if a < 160:
            continue
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if saturated and (s < 0.35 or v < 0.2):
            continue
        key = (r // 24, g // 24, b // 24)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    k = max(counts, key=counts.get)
    return tuple(min(255, c * 24 + 12) for c in k)


def boxiness(img):
    """Share of the logo's bounding box that is opaque: ~1.0 = a filled box/badge logo, low = a cut-out wordmark."""
    a = img.getchannel('A') if img.mode == 'RGBA' else Image.new('L', img.size, 255)
    bbox = a.point(lambda v: 255 if v > 20 else 0).getbbox()
    if not bbox:
        return 0.0
    crop = a.crop(bbox)
    return sum(1 for v in crop.getdata() if v > 128) / (crop.width * crop.height)


def icon_background(pid):
    """The streaming app icon's background colour (median of its corner pixels)."""
    ic = art.logo_image({'kind': 'provider', 'id': pid}).convert('RGB')
    w, h = ic.size
    pts = [ic.getpixel((int(w * x), int(h * y))) for x, y in ((.06, .06), (.94, .06), (.06, .94), (.94, .94), (.5, .06))]
    return tuple(sorted(c[i] for c in pts)[2] for i in range(3))


def mean_colour(img):
    px = [(r, g, b) for r, g, b, a in img.convert('RGBA').getdata() if a > 128]
    if not px:
        return (255, 255, 255)
    return tuple(sum(p[i] for p in px) // len(px) for i in range(3))


def field(size, rgb):
    """Brand colour darkened into a soft radial gradient (lighter centre), plus faint noise against banding."""
    w, h = size
    centre = tuple(min(255, int(c * 0.95)) for c in rgb)
    edge = tuple(int(c * 0.42) for c in rgb)
    small = Image.new('RGB', (64, 36))
    px = small.load()
    for y in range(36):
        for x in range(64):
            d = min(1.0, math.hypot((x - 32) / 32, (y - 18) / 18) / 1.25)
            t = d ** 1.6
            px[x, y] = tuple(int(centre[i] * (1 - t) + edge[i] * t) for i in range(3))
    img = small.resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(6))
    noise = Image.effect_noise(size, 6).convert('RGB')
    return Image.blend(img, noise, 0.025).convert('RGBA')


def recolour(logo, rgb):
    solid = Image.new('RGBA', logo.size, rgb + (255,))
    solid.putalpha(logo.getchannel('A'))
    return solid


def pill(draw, text, xy_right, font, fg, bg):
    w = draw.textlength(text, font=font)
    x1, y0 = xy_right
    pad_x, pad_y = round(font.size * 0.8), round(font.size * 0.38)
    x0 = x1 - w - 2 * pad_x
    draw.rounded_rectangle((x0, y0, x1, y0 + font.size + 2 * pad_y), radius=(font.size + 2 * pad_y) // 2, fill=bg)
    draw.text((x0 + pad_x, y0 + pad_y - 2), text, font=font, fill=fg)
    return (x0, y0, x1, y0 + font.size + 2 * pad_y)


SAFE = 0.04      # every text / badge / logo must sit >= 4% inside each edge


def text_box(draw, xy, text, font):
    return draw.textbbox(xy, text, font=font)


def check_bounds(boxes, size, fails, log):
    """Fail the card if any element's rendered box crosses the 4% safe margin (or leaves the canvas)."""
    w, h = size
    lo_x, lo_y, hi_x, hi_y = w * SAFE, h * SAFE, w * (1 - SAFE), h * (1 - SAFE)
    worst = []
    for name, (x0, y0, x1, y1) in boxes:
        if x0 < lo_x or y0 < lo_y or x1 > hi_x or y1 > hi_y:
            worst.append(f"{name} {int(x0)},{int(y0)},{int(x1)},{int(y1)}")
    log['safe_area'] = 'ok' if not worst else 'FAIL'
    if worst:
        fails.append('outside 4% safe area: ' + '; '.join(worst))


def logo_source(card):
    """(image, description) for the card's logo_src: {'kind': network|company, 'id'} -> best TMDB logo
    (largest PNG rendering), or {'kind': 'provider', 'id'} -> the streaming app icon, or {'kind': 'path', 'path'}."""
    src = card['logo_src']
    if src['kind'] == 'path':
        return art.fetch_image(src['path'], 'original'), f"path {src['path']}"
    if src['kind'] == 'provider':
        return art.logo_image({'kind': 'provider', 'id': src['id']}), f"provider {src['id']} icon"
    logos = art.tmdb(f"/{src['kind']}/{src['id']}/images").get('logos', [])
    if not logos:
        return None, f"{src['kind']} {src['id']}: no logos"
    pinned = card.get('logo_path')              # art_overrides: an exact logo file, checked by hand
    if pinned:
        best = next((l for l in logos if l['file_path'] == pinned), None)
        if best is None:                        # the pin must still be one of this brand's logos on TMDB
            return None, f"{src['kind']} {src['id']}: pinned logo {pinned} no longer listed"
        img = art.fetch_image(pinned, 'original')
        boxy = boxiness(img) > 0.8
        desc = f"{src['kind']} {src['id']} {pinned} {best.get('width')}x{best.get('height')} (PINNED)"
        return img, desc + (' BOXY' if boxy else '')
    # Pick: a cut-out wordmark over a filled "box" logo, then the largest of the 5 widest; boxy ones are kept as
    # they are. TMDB logo votes were tried as a "current brand" signal (2026-09-28) and rejected: they preferred the
    # retired Apple "tv+" and an older flat Starz. An outdated pick is fixed with a `logo_path` pin in art_overrides.
    scored = []
    for l in sorted(logos, key=lambda l: -(l.get('width') or 0))[:5]:
        img = art.fetch_image(l['file_path'], 'original')
        scored.append((boxiness(img) > 0.8, -(l.get('width') or 0), l, img))
    boxy, _, best, img = min(scored, key=lambda t: (t[0], t[1]))
    desc = f"{src['kind']} {src['id']} {best['file_path']} {best.get('width')}x{best.get('height')} ({best.get('file_type')})"
    return img, desc + (' BOXY' if boxy else '')


def render_logo(card):
    size = art.SIZES[card['shape']]
    w, h = size
    fails, log = [], {'slug': card['slug'], 'shape': card['shape'], 'style': 'logo'}
    logo, desc = logo_source(card)
    log['logo'] = desc
    if logo is None:
        fails.append('no logo')
        logo = Image.new('RGBA', (10, 10), (0, 0, 0, 0))
    bbox = logo.getchannel('A').point(lambda a: 255 if a > 20 else 0).getbbox()   # trim transparent margins
    if bbox:
        logo = logo.crop(bbox)
    boxy = desc.endswith('BOXY') or card['logo_src']['kind'] == 'provider'   # never recolour boxed logos / app icons
    logo_hue = dominant(logo)
    if card.get('field'):
        rgb, src = hexrgb(card['field']), 'override'
    elif card.get('icon_provider'):
        # the service's own app-icon background (Netflix black, Disney+ navy...); a very bright icon background
        # would wash out, so it falls back to a deep tone of the logo colour
        rgb, src = icon_background(card['icon_provider']), 'app icon background'
        if luminance(rgb) > 0.6:
            rgb, src = (tuple(int(c * 0.35) for c in logo_hue), 'dark tone of logo colour') if logo_hue else (NEUTRAL, 'neutral')
    elif logo_hue:
        rgb, src = tuple(int(c * 0.35) for c in logo_hue), 'dark tone of logo colour'
    else:
        rgb, src = NEUTRAL, 'neutral (monochrome logo)'
    log['field'] = '#%02x%02x%02x' % rgb + f' ({src})'
    base = field(size, rgb)
    centre = tuple(min(255, int(c * 0.95)) for c in rgb)
    mean = mean_colour(logo)
    ratio = contrast(mean, centre)
    if card.get('emblem'):
        # a multi-colour emblem (Warner Bros. shield) carries its own inner contrast; recolouring it to one colour
        # flattens it into a blob. It keeps its colours; its dominant (outer) colour must stand off the field.
        hue = dominant(logo) or mean
        ratio = contrast(hue, centre)
        log['contrast_basis'] = 'emblem: dominant colour vs field'
        if ratio < MIN_CONTRAST:
            fails.append(f'emblem contrast {ratio:.2f} < {MIN_CONTRAST}')
        boxy = True                                   # never recoloured below
    elif (ratio < MIN_CONTRAST and not boxy) or card.get('logo_colour'):
        target = hexrgb(card['logo_colour']) if card.get('logo_colour') else \
            max([(255, 255, 255), (18, 18, 22)], key=lambda c: contrast(c, centre))
        logo = recolour(logo, target)
        log['logo_recoloured'] = '#%02x%02x%02x' % target
        ratio = contrast(target, centre)
    log['contrast'] = round(ratio, 2)
    if ratio < MIN_CONTRAST and not boxy:             # a boxed logo carries its own background
        fails.append(f'logo contrast {ratio:.2f} < {MIN_CONTRAST}')
    icon_tile = card['logo_src']['kind'] == 'provider'     # no wordmark on TMDB: the official app icon as a tile
    sub = card.get('logo_sub')                              # e.g. "KIDS" under the Netflix wordmark
    sub_f = art.font(76, 'ExtraBold') if sub else None
    sub_gap = 26
    if icon_tile:
        side = min(logo.width, logo.height, 300)            # native size or smaller: never upscaled
        lw = lh = side
        logo = logo.resize((side, side), Image.LANCZOS)
        mask = Image.new('L', (side, side), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=int(side * 0.2), fill=255)
        logo.putalpha(mask)
        log['contrast'] = 'n/a (app icon tile carries its own background)'
        fails[:] = [f for f in fails if not f.startswith('logo contrast')]
    else:
        box_w, box_h = (w * 0.60, h * 0.36) if card['shape'] == 'wide' else (w * 0.72, h * 0.22)
        if sub:
            box_h -= sub_f.size * 0.6
        scale = min(box_w / logo.width, box_h / logo.height)
        lw, lh = max(1, round(logo.width * scale)), max(1, round(logo.height * scale))
        if lw < 200 and card['shape'] == 'wide':
            fails.append(f'logo too small after fit ({lw}px wide)')
        logo = logo.resize((lw, lh), Image.LANCZOS)
    block_h = lh + ((sub_gap + sub_f.size) if sub else 0)
    lx, ly = (w - lw) // 2, (h - block_h) // 2
    shadow = Image.new('RGBA', size, (0, 0, 0, 0))
    shadow.alpha_composite(recolour(logo, (0, 0, 0)), (lx, ly + 6))
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    shadow.putalpha(shadow.getchannel('A').point(lambda a: int(a * 0.45)))
    base.alpha_composite(shadow)
    base.alpha_composite(logo, (lx, ly))
    boxes = [('logo', (lx, ly, lx + lw, ly + lh))]
    if sub:
        sdraw = ImageDraw.Draw(base)
        stw = sdraw.textlength(sub, font=sub_f)
        sxy = ((w - stw) // 2, ly + lh + sub_gap)
        sdraw.text(sxy, sub, font=sub_f, fill=(255, 255, 255))
        boxes.append(('logo sub', text_box(sdraw, sxy, sub, sub_f)))
    draw = ImageDraw.Draw(base)
    margin = 64 if card['shape'] == 'wide' else 40
    eyebrow_f = art.font(26 if card['shape'] == 'wide' else 22, 'Bold')
    badge_f = art.font(52, 'ExtraBold')                                        # phone-legible
    pill_h = badge_f.size + 2 * round(badge_f.size * 0.38)
    ey = margin + (pill_h - eyebrow_f.size) // 2 - 4                           # eyebrow centred on the pill's row
    draw.text((margin, ey), card['eyebrow'].upper(), font=eyebrow_f, fill=(255, 255, 255, 215))
    boxes.append(('eyebrow', text_box(draw, (margin, ey), card['eyebrow'].upper(), eyebrow_f)))
    if card.get('badge'):
        boxes.append(('badge', pill(draw, card['badge'].upper(), (w - margin, margin), badge_f,
             fg=tuple(int(c * 0.5) for c in rgb), bg=(255, 255, 255))))
    check_bounds(boxes, size, fails, log)
    return base, log, fails


GENRE_COLOURS = {    # typographic Genres · New cards: deep tones that carry white type (contrast asserted)
    'Action': '#b3261e', 'Adventure': '#1f6f4a', 'Animation': '#6a3fb5', 'Anime': '#c2185b', 'Comedy': '#b54708',
    'Crime': '#37474f', 'Documentary': '#2e5e4e', 'Drama': '#5d2e46', 'Family': '#1e6fa8', 'Fantasy': '#4a2c82',
    'History': '#7a5230', 'Horror': '#6d0f16', 'Kids': '#0f7c90', 'Music': '#8e24aa', 'Mystery': '#283593',
    'Reality': '#ad1457', 'Romance': '#b0305a', 'Sci-Fi': '#0d47a1', 'Thriller': '#263238', 'War': '#4e5b31',
    'Westerns': '#8d4e1a'}


TYPE_ONE_LINE_MIN = 108


def render_type(card):
    size = art.SIZES[card['shape']]
    w, h = size
    fails, log = [], {'slug': card['slug'], 'shape': card['shape'], 'style': 'type'}
    rgb = hexrgb(card.get('field') or GENRE_COLOURS.get(card['title'], '#263238'))
    log['field'] = '#%02x%02x%02x' % rgb
    base = field(size, rgb)
    draw = ImageDraw.Draw(base)
    # Genres · New default: watermark and pill both "NEW". Date-driven cards (trending, seasonal) set card['mark'] /
    # card['badge'] (None = no pill) in art_overrides.json, since "NEW" would be wrong on them.
    mark, badge = card.get('mark', 'NEW'), card.get('badge', 'NEW')
    # oversized decorative mark, faint, fully inside the card (it used to bleed off the edge and read "NEV")
    size_px = 430
    while draw.textlength(mark, font=art.font(size_px, 'ExtraBold')) > w * card.get('mark_w', 0.62):
        size_px -= 10
    big = art.font(size_px, 'ExtraBold')
    bx0, by0, bx1, by1 = draw.textbbox((0, 0), mark, font=big)
    layer = Image.new('RGBA', size, (0, 0, 0, 0))
    wm_xy = (w - 64 - bx1, (h - (by1 - by0)) // 2 - by0 - 40)
    ImageDraw.Draw(layer).text(wm_xy, mark, font=big, fill=(255, 255, 255, 34))
    boxes = [(f'watermark {mark}', text_box(ImageDraw.Draw(layer), wm_xy, mark, big))]
    base.alpha_composite(layer)
    log['mark'], log['badge'] = mark, badge
    margin = 64
    # one line if it fits at >= TYPE_ONE_LINE_MIN px (a wrapped "Trending ·" / "Movies" left a dangling dot and pushed
    # the pill into the watermark); else up to 2 lines from 132 px. Genre titles all fit one line at 132 (unchanged).
    title_f, lines = art.fit_lines(draw, card['title'], w - 2 * margin, 132, 'ExtraBold', max_lines=1)
    if title_f is None or title_f.size < TYPE_ONE_LINE_MIN:
        title_f, lines = art.fit_lines(draw, card['title'], w - 2 * margin, 132, 'ExtraBold')
    if title_f is None:
        fails.append('title does not fit in 2 lines')
        title_f = art.font(28, 'ExtraBold')
    line_h = title_f.size * 1.06
    y = h - margin - line_h * len(lines)
    if badge:
        npill = art.font(52, 'ExtraBold')
        boxes.append(('badge', pill(draw, badge.upper(), (margin + draw.textlength(badge.upper(), font=npill)
                                                          + 2 * round(52 * 0.8), y - 104), npill, fg=rgb, bg=(255, 255, 255))))
    for i, line in enumerate(lines):
        draw.text((margin, y + i * line_h), line, font=title_f, fill=(255, 255, 255))
        boxes.append((f'title line {i + 1}', text_box(draw, (margin, y + i * line_h), line, title_f)))
    ef = art.font(26, 'Bold')
    draw.text((margin, margin), card['eyebrow'].upper(), font=ef, fill=(255, 255, 255, 215))
    boxes.append(('eyebrow', text_box(draw, (margin, margin), card['eyebrow'].upper(), ef)))
    check_bounds(boxes, size, fails, log)
    wm = boxes[0][1]                                  # the faint watermark must not run under the pill or the title
    hit = [n for n, b in boxes[1:] if n != 'eyebrow' and b[0] < wm[2] and wm[0] < b[2] and b[1] < wm[3] and wm[1] < b[3]]
    if hit:
        fails.append(f"{boxes[0][0]} overlaps: {', '.join(hit)}")
    ratio = contrast((255, 255, 255), rgb)
    log['contrast'] = round(ratio, 2)
    if ratio < 4.5:
        fails.append(f'title contrast {ratio:.2f} < 4.5')
    return base, log, fails
