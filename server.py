"""agent-status-screen server: receive Claude Code / Codex hook packets and serve the dashboard page.

  py -3 server.py                     # uses config.json next to this file (all settings optional)
  py -3 server.py --config my.json
  py -3 server.py --preview out.html  # write a page with demo data, open it in a browser to check the layout

Listens on 127.0.0.1 only: UDP 57999 for hook packets, HTTP 8765 for the page (/, /status, /sprite.png).
No third-party packages are needed unless `pet` points to a full Codex pet spritesheet (then Pillow).
"""

import argparse
import base64
import io
import json
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agent_status import AgentStatus

HERE = Path(__file__).resolve().parent

DEFAULTS = {
    'lang': 'zh-TW',                 # zh-TW or en
    'http_port': 8765,
    'udp': {'host': '127.0.0.1', 'port': 57999},
    'pet': 'pets/beelu/atlas.png',   # bundled demo pet, or a Codex pet spritesheet.png
    'agent_stale_hours': 3,          # no packet for this long -> shown as offline
    'weather': None,                 # {"name", "lat", "lon", ...}; null = no weather card
    'display_hours': None,           # {"days", "start", "end", ...}; null = always on
}
WEATHER_DEFAULTS = {'rain_alert_pct': 60, 'rain_alert_mm': 0.1, 'rain_ahead_hours': 2,
                    'refresh_min': 30}
DISPLAY_DEFAULTS = {'days': [1, 2, 3, 4, 5], 'start': '08:00', 'end': '17:30',
                    'off_dates': [], 'on_dates': []}

# Codex pet spritesheet: 8 columns x 11 rows, 192x208 per cell. The atlas keeps only these rows, in this order.
CELL_W, CELL_H = 192, 208
POSES = {'idle': (0, 6), 'wave': (3, 4), 'jump': (4, 5), 'sad': (5, 8),
         'think': (6, 6), 'work': (7, 6), 'review': (8, 6), 'look': (9, 8)}

# WMO weather code (Open-Meteo); anything not listed (drizzle, rain, snow, showers) shows as rain
WMO_ICON = {0: 'sun', 1: 'partly', 2: 'partly', 3: 'cloud', 45: 'fog', 48: 'fog',
            95: 'thunder', 96: 'thunder', 99: 'thunder'}


# ---------------------------------------------------------------- config

def load_config(path):
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and Path(path).exists():
        user = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        for key, value in user.items():
            if key.startswith('_'):
                continue                 # "_comment" keys
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    if cfg['weather']:
        cfg['weather'] = {**WEATHER_DEFAULTS, **cfg['weather']}
    if cfg['display_hours']:
        cfg['display_hours'] = {**DISPLAY_DEFAULTS, **cfg['display_hours']}
    return cfg


# ---------------------------------------------------------------- display hours

def _minutes(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def display_on(rule, now):
    """True while the screen should be on. rule None = always on.
    days: 1 = Monday ... 7 = Sunday. on_dates / off_dates (YYYY-MM-DD) override the weekday rule;
    the time window applies on every working day."""
    if not rule:
        return True
    t = time.localtime(now)
    date = time.strftime('%Y-%m-%d', t)
    if date in rule['off_dates']:
        return False
    if date not in rule['on_dates'] and t.tm_wday + 1 not in rule['days']:
        return False
    minute = t.tm_hour * 60 + t.tm_min
    return _minutes(rule['start']) <= minute < _minutes(rule['end'])


# ---------------------------------------------------------------- weather (Open-Meteo, no API key)

def weather_url(w):
    q = {'latitude': w['lat'], 'longitude': w['lon'],
         'current': 'temperature_2m,weather_code,is_day',
         'hourly': 'precipitation_probability,precipitation',
         # current hour + the alert window + the 3 hours the page shows
         'forecast_hours': max(4, int(w['rain_ahead_hours']) + 2), 'timezone': 'auto', 'timeformat': 'unixtime'}
    if w.get('temperature_unit'):
        q['temperature_unit'] = w['temperature_unit']
    scheme = 'http' if w.get('use_http') else 'https'     # use_http: for networks that break HTTPS
    return f'{scheme}://api.open-meteo.com/v1/forecast?' + urllib.parse.urlencode(q)


def parse_weather(j):
    c, h = j['current'], j['hourly']
    icon = WMO_ICON.get(c['weather_code'], 'rain')
    if icon == 'sun' and not c['is_day']:
        icon = 'moon'
    hourly = [[t, p or 0, mm or 0] for t, p, mm in
              zip(h['time'], h['precipitation_probability'], h['precipitation'])]
    return {'temp': round(c['temperature_2m']), 'icon': icon, 'hourly': hourly}


def rain_now(hourly, now):
    """Rain probability of the current hour (like a phone weather app), not the max of the next hours."""
    past = [p for t, p, mm in hourly if t <= now]
    return past[-1] if past else hourly[0][1]


def rain_alert(hourly, now, w):
    """Start time (unix) of the first upcoming hour with probability >= rain_alert_pct AND
    forecast rain >= rain_alert_mm, within rain_ahead_hours. None when the current hour is already
    rainy (the big percentage shows it) or nothing qualifies.
    The mm condition matters: Open-Meteo's probability comes from ensemble forecasts and can report
    70-80% when every model forecasts 0 mm."""
    if rain_now(hourly, now) >= w['rain_alert_pct']:
        return None
    for t, p, mm in hourly:
        if now < t <= now + w['rain_ahead_hours'] * 3600 and p >= w['rain_alert_pct'] and mm >= w['rain_alert_mm']:
            return t
    return None


def weather_view(data, now, w):
    if not data:
        return None
    hourly = [x for x in data['hourly'] if x[0] > now - 3600]
    if not hourly:
        return None
    return {'name': w.get('name', ''), 'temp': data['temp'], 'icon': data['icon'],
            'rain': rain_now(hourly, now), 'alert': rain_alert(hourly, now, w), 'hourly': hourly,
            'alert_pct': w['rain_alert_pct'], 'alert_mm': w['rain_alert_mm']}


# ---------------------------------------------------------------- pet sprite

def png_size(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('not a PNG file')
    return struct.unpack('>II', data[16:24])


def load_sprite(path):
    """Bundled atlas (8 rows) as is; a full Codex spritesheet (11 rows) is cut down with Pillow."""
    path = Path(path)
    if not path.is_absolute():
        path = HERE / path
    if path.is_dir():
        path = path / 'spritesheet.png'
    data = path.read_bytes()
    size = png_size(data)
    if size == (8 * CELL_W, len(POSES) * CELL_H):
        return data
    if size != (8 * CELL_W, 11 * CELL_H):
        raise ValueError(f'{path}: unexpected size {size}, expected a Codex pet spritesheet (1536x2288)')
    from PIL import Image                # only needed for a full spritesheet
    with Image.open(io.BytesIO(data)) as original:
        sheet = original.convert('RGBA')
    atlas = Image.new('RGBA', (8 * CELL_W, len(POSES) * CELL_H))
    for i, (row, _) in enumerate(POSES.values()):
        atlas.paste(sheet.crop((0, row * CELL_H, 8 * CELL_W, (row + 1) * CELL_H)), (0, i * CELL_H))
    out = io.BytesIO()
    atlas.save(out, format='PNG', optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- page

def make_page(sprite_url, cfg, demo):
    poses = {name: {'row': i, 'count': count} for i, (name, (_, count)) in enumerate(POSES.items())}
    page_cfg = {'lang': cfg['lang'], 'weather': bool(cfg['weather'])}
    template = (HERE / 'web' / 'index.html').read_text(encoding='utf-8')
    return (template.replace('__SPRITE__', json.dumps(sprite_url))
                    .replace('__POSES__', json.dumps(poses))
                    .replace('__CONFIG__', json.dumps(page_cfg))
                    .replace('__DEMO__', json.dumps(demo)))


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.agents = AgentStatus()
        self.weather = None
        self.lock = threading.Lock()

    def status(self, now=None):
        now = time.time() if now is None else now
        w = self.cfg['weather']
        with self.lock:
            agents = self.agents.snapshot()
            active = self.agents.active
            weather = weather_view(self.weather, now, w) if w else None
        return {'now': now, 'display_on': display_on(self.cfg['display_hours'], now),
                'weather': weather, 'agents': agents, 'active': active,
                'agent_stale_sec': self.cfg['agent_stale_hours'] * 3600}

    def udp_loop(self):
        udp = self.cfg['udp']
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((udp['host'], udp['port']))
        while True:
            try:
                data, _ = sock.recvfrom(65535)   # Windows raises an error instead of truncating a big datagram
            except OSError as e:
                print('udp:', e, flush=True)
                continue
            if len(data) > 512:
                continue
            text = data.decode('ascii', 'ignore')
            with self.lock:
                self.agents.receive(text, time.time())

    def weather_loop(self):
        w = self.cfg['weather']
        url = weather_url(w)
        while True:
            try:
                with urllib.request.urlopen(url, timeout=15) as r:
                    data = parse_weather(json.load(r))
                with self.lock:
                    self.weather = data
                time.sleep(w['refresh_min'] * 60)
            except Exception as e:
                print('weather:', e, flush=True)
                time.sleep(5 * 60)


def serve(cfg):
    app = App(cfg)
    page = make_page('/sprite.png', cfg, demo=False).encode('utf-8')
    sprite = load_sprite(cfg['pet'])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            cache = 'no-store'
            path = urlsplit(self.path).path      # ignore ?force=off etc. (read by the page script)
            if path == '/':
                body, content_type = page, 'text/html; charset=utf-8'
            elif path == '/status':
                body, content_type = json.dumps(app.status()).encode('utf-8'), 'application/json'
            elif path == '/sprite.png':
                body, content_type, cache = sprite, 'image/png', 'max-age=86400'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', cache)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    threading.Thread(target=app.udp_loop, daemon=True).start()
    if cfg['weather']:
        threading.Thread(target=app.weather_loop, daemon=True).start()
    # Threading: idle connections a browser opens in advance would otherwise block other requests
    with ThreadingHTTPServer(('127.0.0.1', cfg['http_port']), Handler) as server:
        print(f"http://127.0.0.1:{cfg['http_port']}/  (hook packets: UDP {cfg['udp']['host']}:{cfg['udp']['port']})",
              flush=True)
        server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, default=HERE / 'config.json')
    parser.add_argument('--preview', type=Path, metavar='HTML')
    args = parser.parse_args()
    cfg = load_config(args.config)
    if args.preview:
        url = 'data:image/png;base64,' + base64.b64encode(load_sprite(cfg['pet'])).decode('ascii')
        cfg['weather'] = cfg['weather'] or {'name': 'demo'}
        with open(args.preview, 'w', encoding='utf-8', newline='\n') as f:   # write_text(newline=) needs 3.10
            f.write(make_page(url, cfg, demo=True))
        print(f'wrote {args.preview}; add ?scene=think / done / alert / night to the URL for other states')
    else:
        serve(cfg)


if __name__ == '__main__':
    main()
