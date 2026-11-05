"""Codex hook: send the current state and a short activity label to agent-status-screen (UDP).

Only sanitized activity labels are sent. No prompt, transcript, tool output,
patch contents, command arguments, or approval decisions are forwarded.
Target: AGENT_STATUS_HOST / AGENT_STATUS_PORT environment variables, default 127.0.0.1:57999.
Debug: pipe a hook event JSON into this script with --dry-run to print the packet.
"""
import argparse
import json
import ntpath
import os
import re
import socket
import sys

HOST = os.environ.get('AGENT_STATUS_HOST', '127.0.0.1')
PORT = int(os.environ.get('AGENT_STATUS_PORT', '57999'))


def short(value):
    return re.sub(r'[^\x20-\x7e]', '?', str(value)).strip()[:40]


def leaf(value):
    return ntpath.basename(str(value).rstrip('\\/'))


# A command name we are willing to show: plain file-name characters only. Anything else (a fragment of a
# quoted argument, a secret, ...) is dropped and only the tool name is shown.
SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,39}$')


def split_commands(command):
    """Split on ; && || and newlines that are outside quotes."""
    parts, buf, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
        elif c in '"\'':
            quote = c
            buf.append(c)
        elif c in ';\r\n':
            parts.append(''.join(buf))
            buf = []
        elif command.startswith(('&&', '||'), i):
            parts.append(''.join(buf))
            buf = []
            i += 1
        else:
            buf.append(c)
        i += 1
    parts.append(''.join(buf))
    return parts


def command_name(command):
    """First real command (skips cd and variable assignments), e.g. 'git'; '' when unsure."""
    for part in split_commands(command):
        part = part.strip()
        if not part or re.match(r'^\$?\w+\s*=', part):
            continue
        match = re.match(r'''^(?:&\s*)?(?:"([^"]+)"|'([^']+)'|([^\s]+))''', part)
        if match:
            name = leaf(next(group for group in match.groups() if group is not None))
            if name.lower() in ('cd', 'pushd', 'set-location'):
                continue
            return name if SAFE_NAME.match(name) else ''
    return ''


def tool_name(event):
    return str(event.get('tool_name', '')).split('__')[-1].split('.')[-1]


def activity(event):
    tool = tool_name(event)
    args = event.get('tool_input', {})
    if not isinstance(args, dict):
        args = {}
    if tool in ('Bash', 'PowerShell', 'exec_command', 'shell_command', 'shell'):
        command = args.get('command', args.get('cmd', ''))
        if isinstance(command, list):
            command = ' '.join(str(item) for item in command)
        return ('Bash ' + command_name(str(command))).strip()
    if tool in ('apply_patch', 'Edit', 'Write'):
        # Read only a patch header; never send the body.
        patch = args.get('command', args.get('patch', args.get('input', '')))
        match = re.search(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', str(patch), re.M)
        path = match.group(1).rstrip('\r') if match else args.get('file_path', '')
        return 'Edit ' + leaf(path)
    if tool in ('Read', 'read_file'):
        return 'Read ' + leaf(args.get('file_path', args.get('path', '')))
    if tool in ('Grep', 'Glob', 'search', 'search_query'):
        return 'search ' + leaf(args.get('path', ''))
    return tool or 'tool'


def event_message(event):
    name = event.get('hook_event_name')
    if name == 'UserPromptSubmit':
        return 'RUN|thinking...'
    if name == 'PermissionRequest':
        return 'WAIT|' + short('approve ' + tool_name(event))
    if name == 'PreToolUse':
        if tool_name(event) in ('request_user_input', 'request_user_input_async', 'AskUserQuestion'):
            return 'WAIT|question'
        return 'RUN|' + short(activity(event).strip())
    if name == 'PostToolUse':
        return 'RUN'
    if name == 'Stop':
        return 'DONE'
    if name == 'Interrupt':
        return 'WAIT|interrupted'
    if name == 'SessionEnd':
        return 'OFF|'
    return None


def message(event):
    payload = event_message(event)
    return 'CODEX:' + payload if payload is not None else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        event = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
        if not isinstance(event, dict):
            return
        payload = message(event)
        if payload is None:
            return
        if args.dry_run:
            print(payload)
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
                udp.settimeout(0.2)
                udp.sendto(payload.encode('ascii'), (HOST, PORT))
    except Exception:
        # Notification failure must not interfere with the agent or approvals.
        return


if __name__ == '__main__':
    main()
