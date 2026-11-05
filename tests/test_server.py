import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from agent_status import AgentStatus  # noqa: E402

spec = importlib.util.spec_from_file_location('codex_status', ROOT / 'hooks' / 'codex-status.py')
codex_status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(codex_status)


def local(y, mo, d, h, mi):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


class AgentStatusTest(unittest.TestCase):
    def test_sources_are_separate_and_last_packet_wins(self):
        a = AgentStatus()
        self.assertTrue(a.receive('CLAUDE:RUN|Edit a.py', 1))
        self.assertTrue(a.receive('CODEX:WAIT|approve Bash', 2))
        self.assertEqual(a.active, 'CODEX')
        self.assertEqual(a.sources['CLAUDE']['act'], 'Edit a.py')
        a.receive('CLAUDE:DONE', 3)
        self.assertEqual(a.active, 'CLAUDE')
        self.assertEqual(a.sources['CLAUDE']['act'], 'Edit a.py')   # no "|" keeps the label
        self.assertEqual(a.sources['CLAUDE']['done_at'], 3)
        a.receive('CLAUDE:DONE', 9)
        self.assertEqual(a.sources['CLAUDE']['done_at'], 3)          # repeated DONE keeps the first time

    def test_invalid_packets(self):
        a = AgentStatus()
        for text in ('GEMINI:RUN', 'CLAUDE:JUMP', '', 'hello'):
            self.assertFalse(a.receive(text, 1))
        self.assertTrue(a.receive('RUN|x', 1))                       # no prefix = Claude
        self.assertEqual(a.sources['CLAUDE']['act'], 'x')


class DisplayHoursTest(unittest.TestCase):
    rule = {**server.DISPLAY_DEFAULTS, 'off_dates': ['2026-11-06'], 'on_dates': ['2026-11-07']}

    def test_rules(self):
        self.assertTrue(server.display_on(None, local(2026, 11, 8, 3, 0)))
        self.assertTrue(server.display_on(self.rule, local(2026, 11, 5, 8, 0)))     # Thursday
        self.assertFalse(server.display_on(self.rule, local(2026, 11, 5, 7, 59)))
        self.assertFalse(server.display_on(self.rule, local(2026, 11, 5, 17, 30)))
        self.assertFalse(server.display_on(self.rule, local(2026, 11, 6, 10, 0)))   # off date
        self.assertTrue(server.display_on(self.rule, local(2026, 11, 7, 10, 0)))    # Saturday, on date
        self.assertFalse(server.display_on(self.rule, local(2026, 11, 8, 10, 0)))   # Sunday


class WeatherTest(unittest.TestCase):
    w = dict(server.WEATHER_DEFAULTS)

    def sample(self):
        return {'current': {'temperature_2m': 22.6, 'weather_code': 0, 'is_day': 0},
                'hourly': {'time': [3600, 7200, 10800, 14400],
                           'precipitation_probability': [10, 70, 80, None],
                           'precipitation': [0, 0, 0.4, None]}}

    def test_parse(self):
        d = server.parse_weather(self.sample())
        self.assertEqual((d['temp'], d['icon']), (23, 'moon'))
        self.assertEqual(d['hourly'][3], [14400, 0, 0])

    def test_rain_alert_needs_probability_and_mm(self):
        hourly = server.parse_weather(self.sample())['hourly']
        # 7200 has 70% but 0 mm -> skipped; 10800 has 80% and 0.4 mm
        self.assertEqual(server.rain_alert(hourly, 3700, self.w), 10800)
        self.assertEqual(server.rain_now(hourly, 3700), 10)
        self.assertIsNone(server.rain_alert(hourly, 10900, self.w))  # current hour already rainy

    def test_view_drops_past_hours(self):
        v = server.weather_view(server.parse_weather(self.sample()), 7300, {**self.w, 'name': 'x'})
        self.assertEqual([h[0] for h in v['hourly']], [7200, 10800, 14400])
        self.assertIsNone(server.weather_view(None, 0, self.w))

    def test_url_uses_unixtime(self):
        url = server.weather_url({**self.w, 'lat': 25.0, 'lon': 121.5})
        self.assertIn('timeformat=unixtime', url)
        self.assertIn('forecast_hours=4', url)
        self.assertTrue(url.startswith('https://'))
        url = server.weather_url({**self.w, 'lat': 25.0, 'lon': 121.5, 'rain_ahead_hours': 6, 'use_http': True})
        self.assertIn('forecast_hours=8', url)
        self.assertTrue(url.startswith('http://'))


class ConfigAndPageTest(unittest.TestCase):
    def test_defaults_and_merge(self):
        cfg = server.load_config(None)
        self.assertIsNone(cfg['weather'])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'c.json'
            p.write_text(json.dumps({'udp': {'port': 1}, 'weather': {'lat': 1, 'lon': 2}}), encoding='utf-8')
            cfg = server.load_config(p)
        self.assertEqual(cfg['udp'], {'host': '127.0.0.1', 'port': 1})
        self.assertEqual(cfg['weather']['rain_alert_pct'], 60)

    def test_bundled_pet_and_page(self):
        sprite = server.load_sprite(server.DEFAULTS['pet'])
        self.assertEqual(server.png_size(sprite), (1536, 8 * 208))
        page = server.make_page('/sprite.png', server.load_config(None), demo=False)
        self.assertNotIn('__', page.split('<script>')[1].split('const params')[0])

    def test_example_config_loads(self):
        cfg = server.load_config(ROOT / 'config.example.json')
        self.assertEqual(cfg['display_hours']['days'], [1, 2, 3, 4, 5])


class CodexHookTest(unittest.TestCase):
    def test_messages(self):
        m = codex_status.message
        self.assertEqual(m({'hook_event_name': 'UserPromptSubmit', 'prompt': 'secret'}), 'CODEX:RUN|thinking...')
        self.assertEqual(m({'hook_event_name': 'PreToolUse', 'tool_name': 'exec_command',
                            'tool_input': {'cmd': 'cd x && git push --force'}}), 'CODEX:RUN|Bash git')
        patch = '*** Begin Patch\n*** Update File: C:\\a\\b.py\n+secret\n'
        self.assertEqual(m({'hook_event_name': 'PreToolUse', 'tool_name': 'apply_patch',
                            'tool_input': {'command': patch}}), 'CODEX:RUN|Edit b.py')
        self.assertEqual(m({'hook_event_name': 'PermissionRequest', 'tool_name': 'shell'}), 'CODEX:WAIT|approve shell')
        self.assertIsNone(m({'hook_event_name': 'Other'}))

    def test_command_name_never_leaks_quoted_text(self):
        name = codex_status.command_name
        self.assertEqual(name("TOKEN='prefix;sensitive_value'; curl https://x"), 'curl')
        self.assertEqual(name('TOKEN="a;b c"; curl x'), 'curl')
        self.assertEqual(name('python -c "import os;print(1)"'), 'python')
        self.assertEqual(name('"C:\\Program Files\\Git\\bin\\git.exe" log'), 'git.exe')
        self.assertEqual(name('$x = 1; npm test'), 'npm')
        self.assertEqual(name("$env:DEBUG = '1'; npm test"), 'npm')
        self.assertEqual(name('secret#$%stuff run'), '')


if __name__ == '__main__':
    unittest.main()
