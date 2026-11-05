"""Look up a place for the weather card (Open-Meteo geocoding, no API key).

  py -3 tools/find_location.py 台中              # list matches
  py -3 tools/find_location.py 台中 --pick 1     # write match no. 1 into config.json
  py -3 tools/find_location.py Taipei --lang en
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = '://geocoding-api.open-meteo.com/v1/search?'


def search(name, lang, http=False):
    q = urllib.parse.urlencode({'name': name, 'count': 10, 'language': lang, 'format': 'json'})
    with urllib.request.urlopen(('http' if http else 'https') + URL + q, timeout=15) as r:
        return json.load(r).get('results', [])


def label(r):
    parts = [r.get('name'), r.get('admin2'), r.get('admin1'), r.get('country')]
    return ', '.join(dict.fromkeys(p for p in parts if p))


def main():
    sys.stdout.reconfigure(errors='replace')    # e.g. a Big5 console cannot show every place name
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('name')
    parser.add_argument('--lang', default='zh')
    parser.add_argument('--pick', type=int, metavar='N', help='write match N into config.json')
    parser.add_argument('--http', action='store_true', help='plain http, for networks that break HTTPS')
    parser.add_argument('--config', type=Path, default=ROOT / 'config.json')
    args = parser.parse_args()

    results = search(args.name, args.lang, args.http)
    if not results:
        sys.exit(f'no match for {args.name!r}; try another spelling or --lang en')
    if args.pick is None:
        for i, r in enumerate(results, 1):
            print(f"{i:2}. {label(r)}  ({r['latitude']:.4f}, {r['longitude']:.4f})")
        print('\nrun again with --pick N to write it into', args.config.name)
        return

    r = results[args.pick - 1]
    cfg = {}
    if args.config.exists():
        cfg = json.loads(args.config.read_text(encoding='utf-8-sig'))
    weather = cfg.get('weather') or {}
    weather.update({'name': r['name'], 'lat': round(r['latitude'], 4), 'lon': round(r['longitude'], 4)})
    if args.http:
        weather['use_http'] = True
    cfg['weather'] = weather
    args.config.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'{args.config}: weather = {label(r)}; restart the server (screen.cmd -Stop, then screen.cmd)')


if __name__ == '__main__':
    main()
