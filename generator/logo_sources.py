"""Find the best logo source for every streaming service, network and studio card -> generator/logo_sources.json.

  python logo_sources.py <table.csv>

Streaming services: the TMDB *network* of the same name (found through the networks of the service's own TV shows),
else a TMDB *company* of the same name (/search/company), else the streaming app icon. Networks and studios use their
own network / company id. For each brand the chosen logo is described (resolution, transparency, boxiness) and a
weak logo (small, or only an app icon) is flagged. Colours are decided at render time (art_logo) and logged there.
"""
import csv, json, re, sys, unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import art, art_logo, build  # noqa: E402

HERE = Path(__file__).resolve().parent
MIN_WIDTH = 500           # below this a logo is flagged as low-res (it is drawn ~770 px wide on the card)
ALIASES = {'Apple TV': ['Apple TV+', 'Apple TV'], 'HBO Max': ['HBO Max', 'Max'], 'Prime Video': ['Prime Video', 'Amazon Prime Video'],
           'Starz': ['STARZ', 'Starz'], 'Discovery+': ['discovery+', 'Discovery+'], 'Rakuten Viki': ['Viki', 'Rakuten Viki'],
           'The Roku Channel': ['The Roku Channel', 'Roku Channel'], 'Criterion Channel': ['The Criterion Channel', 'Criterion Channel'],
           'AMC+': ['AMC+'], 'Netflix Kids': ['Netflix Kids'], 'Pluto TV': ['Pluto TV'], 'Acorn TV': ['Acorn TV']}


def norm(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9+]+', '', s)


def describe(kind, i):
    logos = build.tmdb(f'/{kind}/{i}/images').get('logos', [])
    if not logos:
        return None
    img, desc = art_logo.logo_source({'logo_src': {'kind': kind, 'id': i}})
    best = max(logos, key=lambda l: l.get('width') or 0)
    alpha = img.getchannel('A') if img.mode == 'RGBA' else None
    transparent = bool(alpha and alpha.getextrema()[0] < 20)
    return {'kind': kind, 'id': i, 'desc': desc, 'width': img.width, 'height': img.height,
            'transparent': transparent, 'boxy': 'BOXY' in desc, 'max_width_available': best.get('width')}


def streaming_network(card, name):
    """Network of the same name among the networks of this service's TV shows."""
    names = [norm(a) for a in ALIASES.get(name, [name])]
    shows = build.discover(dict(card, sort='votes', depth=40), 'series', 40, json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))['defaults'])
    seen = {}
    for m in shows[:40]:
        for n in build.tmdb(f"/tv/{m['id'].split(':')[1]}").get('networks', []):
            seen[n['id']] = n['name']
    for nid, nname in seen.items():
        if norm(nname) in names:
            return nid
    return None


def streaming_company(name):
    names = [norm(a) for a in ALIASES.get(name, [name])]
    for q in ALIASES.get(name, [name]):
        for r in build.tmdb('/search/company', query=q).get('results', [])[:10]:
            if norm(r['name']) in names and r.get('logo_path'):
                return r['id']
    return None


def main():
    spec = json.loads((HERE / 'spec.json').read_text(encoding='utf-8'))
    out, rows = {}, []
    for c in spec['catalogs']:
        slug = c['slug']
        if slug.startswith('streaming-') and c['sort'] == 'popular':
            name, pid = c['title'], int(c['movie']['with_watch_providers'].split('|')[0])
            src = None
            nid = streaming_network(c, name)
            if nid:
                src = describe('network', nid)
            if not src:
                cid = streaming_company(name)
                if cid:
                    src = describe('company', cid)
            if not src:
                icon = art.logo_image({'kind': 'provider', 'id': pid})
                src = {'kind': 'provider', 'id': pid, 'desc': f'provider {pid} app icon', 'width': icon.width,
                       'height': icon.height, 'transparent': False, 'boxy': True, 'max_width_available': icon.width}
            src['icon_provider'] = pid
            key = slug[len('streaming-'):]
            out[f'streaming:{key}'] = src
            group = 'streaming'
        elif slug.startswith('network-'):
            name, nid = c['title'], int(c['series']['with_networks'])
            src = describe('network', nid) or {'kind': 'network', 'id': nid, 'desc': 'no logo', 'width': 0, 'height': 0,
                                               'transparent': False, 'boxy': False, 'max_width_available': 0}
            out[slug] = src
            group = 'network'
        elif slug.startswith('studio-'):
            name, cid = c['title'], int(c['movie']['with_companies'].split('|')[0])
            src = describe('company', cid) or {'kind': 'company', 'id': cid, 'desc': 'no logo', 'width': 0, 'height': 0,
                                               'transparent': False, 'boxy': False, 'max_width_available': 0}
            out[slug] = src
            group = 'studio'
        else:
            continue
        flags = []
        if src['kind'] == 'provider':
            flags.append('app icon only (no wordmark on TMDB)')
        if src['width'] and src['width'] < MIN_WIDTH:
            flags.append(f"low-res ({src['width']}px)")
        if src['width'] == 0:
            flags.append('NO LOGO')
        if src.get('boxy') and src['kind'] != 'provider':
            flags.append('boxed logo (kept as-is)')
        rows.append([group, name, f"{src['kind']} {src['id']}", f"{src['width']}x{src['height']}",
                     'yes' if src['transparent'] else 'no', '; '.join(flags) or 'ok', src['desc']])
    (HERE / 'logo_sources.json').write_text(json.dumps(out, indent=1) + '\n', encoding='utf-8')
    with open(sys.argv[1], 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['group', 'brand', 'source', 'resolution', 'transparent', 'flags', 'asset'])
        w.writerows(rows)
    build.save_cache()
    for r in rows:
        print(f"{r[0]:<9} {r[1]:<24} {r[2]:<16} {r[3]:<10} transp={r[4]:<3} {r[5]}")


if __name__ == '__main__':
    main()
