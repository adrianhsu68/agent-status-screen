"""Claude Code / Codex status, kept per source.

Packets look like `SOURCE:STATE` or `SOURCE:STATE|activity`, e.g.
`CLAUDE:RUN|Edit main.py`, `CODEX:WAIT|approve Bash`, `CLAUDE:DONE`.
A packet without `|` keeps the previous activity of that source.
The most recent valid packet decides which source is "active" (last takes priority).
"""

SOURCES = ('CLAUDE', 'CODEX')
STATES = ('RUN', 'WAIT', 'DONE', 'OFF')


class AgentStatus:
    def __init__(self):
        self.sources = {name: {'source': name, 'state': None, 'at': 0, 'act': '', 'done_at': 0}
                        for name in SOURCES}
        self.active = SOURCES[0]

    @property
    def current(self):
        return self.sources[self.active]

    def receive(self, text, now):
        """Apply one packet. Returns True when it was valid."""
        source = 'CLAUDE'                 # packets without a prefix count as Claude
        payload = text.strip()
        if ':' in payload.partition('|')[0]:
            source, payload = payload.split(':', 1)
            source = source.upper()
        state, sep, act = payload.partition('|')
        state = state.upper()
        if source not in self.sources or state not in STATES:
            return False
        status = self.sources[source]
        if state == 'DONE' and status['state'] != 'DONE':
            status['done_at'] = now
        status['state'] = state
        status['at'] = now
        if sep:
            status['act'] = act[:60]
        self.active = source
        return True

    def snapshot(self):
        return {k: dict(v) for k, v in self.sources.items()}
