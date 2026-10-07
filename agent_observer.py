"""Best-effort native-agent telemetry; never an audit acceptance gate.

Only runtime relationships and events count as evidence. Assistant prose is
not parsed for claims of collaboration. Raw rollouts remain available when a
CLI schema is unfamiliar; absence of recognized events is not proof of absence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import time

from safeio import SafeIOError, _root_fd, safe_read


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 128
MAX_ENTRIES = 2048
MAX_SECONDS = 10
FREE_RESERVE = 256 * 1024 * 1024


def mapping(value):
    return value if isinstance(value, dict) else {}


def identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 256 else None


class Observation:
    def __init__(self):
        self.roots = set()
        self.agents = {}
        self.attempts = []
        self.problems = []
        self.files = []
        self.activities = []
        self.receipts = []

    def note(self, text):
        if len(self.problems) < 30:
            self.problems.append(str(text)[:500])

    def agent(self, child, parent, evidence, path=None):
        if (not identifier(child) or not identifier(parent)
                or child == parent or child in self.roots):
            return None
        ancestor, seen = parent, {child}
        while ancestor:
            if ancestor in seen:
                self.note(f'Cyclic parent relationship rejected for {child}')
                return None
            seen.add(ancestor)
            ancestor = self.agents.get(ancestor, {}).get('parent_thread_id')
        value = self.agents.setdefault(child, {
            'thread_id': child, 'parent_thread_id': parent, 'agent_path': path,
            'activity_observed': False, 'completed': False,
            'result_received': False, 'message_received': False,
            'result_receipt_status': 'not_observed', 'result_evidence': [],
            'evidence': [],
        })
        if value['parent_thread_id'] != parent:
            self.note(f'Conflicting parent for {child}')
            return None
        if path:
            value['agent_path'] = path
        if len(value['evidence']) < 12:
            value['evidence'].append(evidence)
        return value

    def stream(self, data, filename, deadline):
        owner = None
        created = None
        calls = {}
        for number, line in enumerate(data.splitlines(), 1):
            if time.monotonic() > deadline:
                self.note('Diagnostic time limit reached while parsing')
                return
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                self.note(f'Unreadable JSON at {filename}:{number}')
                continue
            if not isinstance(event, dict):
                self.note(f'Unsupported event at {filename}:{number}')
                continue
            kind = event.get('type')
            payload = mapping(event.get('payload'))
            evidence = {'file': filename, 'line': number, 'event': kind}
            if kind == 'thread.started':
                root = identifier(event.get('thread_id'))
                if root:
                    self.roots.add(root)
                    self.agents.pop(root, None)
            if kind == 'session_meta':
                # A fork can contain inherited parent metadata later in the file.
                if owner is not None:
                    continue
                owner = identifier(payload.get('id'))
                created = event.get('timestamp')
                source = payload.get('source')
                if source == 'exec' and owner:
                    self.roots.add(owner)
                    self.agents.pop(owner, None)
                spawned = mapping(mapping(mapping(source).get('subagent')).get('thread_spawn'))
                self.agent(owner, spawned.get('parent_thread_id'), evidence,
                           spawned.get('agent_path'))
                continue
            # Do not attribute copied parent history to a child.
            timestamp = event.get('timestamp')
            if isinstance(created, str) and isinstance(timestamp, str) and timestamp < created:
                continue
            thread = identifier(payload.get('thread_id')) or owner
            if owner in self.agents and thread != owner:
                continue
            if kind == 'response_item':
                if payload.get('type') == 'agent_message':
                    self.receipts.append({
                        'owner': thread, 'author': payload.get('author'),
                        'recipient': payload.get('recipient'),
                        'kind': self.receipt_kind(payload), 'evidence': evidence,
                    })
                name = str(payload.get('name', '')).split('.')[-1]
                if payload.get('type') == 'function_call' and name == 'spawn_agent':
                    calls[payload.get('call_id')] = thread
                    self.attempts.append(evidence)
                elif payload.get('type') == 'function_call_output' and payload.get('call_id') in calls:
                    try:
                        output = mapping(json.loads(payload.get('output', '')))
                    except (ValueError, TypeError):
                        output = {}
                    child = output.get('agent_id') or output.get('thread_id')
                    if (child and not output.get('error')
                            and output.get('status') not in {'failed', 'error', 'errored'}):
                        self.agent(child, calls[payload.get('call_id')], evidence)
            if kind == 'event_msg':
                item = mapping(payload.get('item'))
                if payload.get('type') == 'item_completed' and item.get('type') == 'SubAgentActivity':
                    # Messages and follow-ups can name the parent as their
                    # target. Activity alone must never create a child edge.
                    self.activities.append({
                        'child': item.get('agent_thread_id'), 'parent': thread,
                        'kind': item.get('kind'), 'evidence': evidence,
                        'result': item.get('result') or item.get('message'),
                    })
                own = self.agents.get(owner)
                if own and thread == owner:
                    subtype = payload.get('type')
                    if subtype == 'item_completed' and item.get('type') in {
                        'CommandExecution', 'WebSearch', 'FileChange', 'AgentMessage',
                    }:
                        own['activity_observed'] = True
                    if subtype in {'task_complete', 'task_completed', 'turn_complete', 'turn_completed'}:
                        own['completed'] = True
                        own['activity_observed'] = True
                    if subtype == 'agent_message' and payload.get('message'):
                        own['activity_observed'] = True
                    if (own['activity_observed'] or own['completed']) and len(own['evidence']) < 12:
                        own['evidence'].append(evidence)
            if kind == 'item.completed':
                self.public_item(mapping(event.get('item')), evidence)

    def public_item(self, item, evidence):
        if item.get('type') != 'collab_tool_call':
            return
        parent = item.get('sender_thread_id')
        tool = item.get('tool')
        receivers = item.get('receiver_thread_ids')
        if not isinstance(receivers, list):
            return
        if tool in {'spawn_agent', 'spawn'}:
            self.attempts.append(evidence)
            if item.get('status') == 'completed':
                for child in receivers:
                    self.agent(child, parent, evidence)
        for child, state in mapping(item.get('agents_states')).items():
            if child not in receivers:
                continue
            state = mapping(state)
            if state.get('status') == 'completed' or 'completed' in state:
                self.activities.append({
                    'child': child, 'parent': parent, 'kind': 'completed',
                    'result': state.get('message') or state.get('completed'),
                    'evidence': evidence,
                })

    @staticmethod
    def failed_result(text):
        return text.lstrip().startswith((
            'Agent errored:', 'Agent failed:', 'Agent cancelled:', 'Agent interrupted:',
        ))

    @classmethod
    def receipt_kind(cls, payload):
        if payload.get('status') in {'failed', 'error', 'errored', 'cancelled', 'interrupted'}:
            return 'failed'
        content = payload.get('content')
        if not isinstance(content, list):
            return 'unknown'
        text = '\n'.join(block['text'] for block in content
                         if isinstance(block, dict) and isinstance(block.get('text'), str))
        header, separator, body = text.partition('\nPayload:\n')
        if cls.failed_result(body if separator else text):
            return 'failed'
        if 'Message Type: FINAL_ANSWER' not in header.splitlines():
            return 'message'
        if (not separator or not body.strip()
                or any(mapping(block).get('type') == 'encrypted_content' for block in content)):
            return 'unknown'
        return 'final'

    def resolve_activity(self):
        # Resolve after all files are read so parent logs may precede metadata.
        for activity in self.activities:
            child = identifier(activity['child'])
            agent = self.agents.get(child)
            if (agent is None or child in self.roots
                    or agent['parent_thread_id'] != activity['parent']):
                continue
            if activity['kind'] != 'completed':
                continue
            agent['completed'] = True
            evidence = activity['evidence']
            if evidence not in agent['evidence']:
                agent['evidence'].append(evidence)
            result = activity['result']
            if isinstance(result, str) and result.strip():
                if '\nPayload:\n' in result:
                    kind = self.receipt_kind({'content': [{'type': 'input_text', 'text': result}]})
                elif result.lstrip().startswith('gAAAA'):
                    kind = 'unknown'
                else:
                    kind = 'failed' if self.failed_result(result) else 'final'
                self.record_receipt(agent, kind, evidence)

        for receipt in self.receipts:
            candidates = [agent for child, agent in self.agents.items()
                          if child not in self.roots
                          and receipt['author'] in {child, agent['agent_path']}
                          and agent['parent_thread_id'] == receipt['owner']]
            if len(candidates) != 1:
                continue
            agent = candidates[0]
            parent = agent['parent_thread_id']
            parent_path = ('/root' if parent in self.roots
                           else self.agents.get(parent, {}).get('agent_path'))
            if not receipt['recipient'] or receipt['recipient'] not in {parent, parent_path}:
                continue
            self.record_receipt(agent, receipt['kind'], receipt['evidence'])

    @staticmethod
    def record_receipt(agent, kind, evidence):
        agent['message_received'] = True
        if kind == 'final' and not agent['completed']:
            kind = 'unconfirmed_final'
        if kind == 'final':
            agent['result_received'] = True
        if not agent['result_received'] or kind == 'final':
            agent['result_receipt_status'] = kind
        record = {**evidence, 'receipt_kind': kind}
        if record not in agent['result_evidence']:
            agent['result_evidence'].append(record)

    def report(self):
        self.resolve_activity()
        linked = set(self.roots)
        while True:
            children = {key for key, value in self.agents.items()
                        if value['parent_thread_id'] in linked}
            expanded = linked | children
            if expanded == linked:
                break
            linked = expanded
        agents = [v for k, v in self.agents.items() if k in linked and k not in self.roots]
        completed = sum(a['completed'] for a in agents)
        active = sum(a['activity_observed'] for a in agents)
        state = ('completed' if completed else 'active' if active else 'created'
                 if agents else 'attempted' if self.attempts else 'unknown')
        return {
            'schema_version': 1, 'status': state, 'informational_only': True,
            'root_thread_ids': sorted(self.roots), 'agents': agents,
            'created_count': len(agents), 'active_count': active,
            'completed_count': completed,
            'results_received_count': sum(a['result_received'] for a in agents),
            'spawn_attempts': self.attempts, 'rollout_files': self.files,
            'limitations': self.problems,
            'interpretation': 'Counts and flags describe observed runtime evidence, not exhaustive history; '
                              'unknown does not mean no collaboration. '
                              'Completion does not verify review quality or mathematical correctness.',
        }


def rollout_paths(root, deadline):
    """Walk only pinned directories; never follow agent-created links."""
    visited = 0

    def walk(fd, relative, depth):
        nonlocal visited
        if depth > 8:
            raise SafeIOError('Rollout directory nesting limit reached')
        for name in sorted(os.listdir(fd)):
            visited += 1
            if visited > MAX_ENTRIES or time.monotonic() > deadline:
                raise SafeIOError('Rollout collection limit reached')
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            path = relative / name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    yield from walk(child, path, depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and name.endswith('.jsonl'):
                yield path
            elif stat.S_ISLNK(info.st_mode):
                raise SafeIOError(f'Linked rollout entry skipped: {path}')

    fd = _root_fd(root)
    try:
        yield from walk(fd, Path(), 0)
    finally:
        os.close(fd)


def collect(codex_home: Path, round_dir: Path):
    """Bounded collection called by the parent after the CLI exits."""
    observation = Observation()
    deadline = time.monotonic() + MAX_SECONDS
    total = 0
    for directory in ('sessions', 'archived_sessions'):
        try:
            for relative in rollout_paths(codex_home / directory, deadline):
                if len(observation.files) >= MAX_FILES:
                    raise SafeIOError('Rollout file count limit reached')
                try:
                    data = safe_read(codex_home / directory, relative,
                                     max_bytes=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
                    if shutil.disk_usage(round_dir).free < FREE_RESERVE + len(data):
                        raise SafeIOError('Insufficient spare disk space for optional diagnostics')
                    target = round_dir / 'native-rollouts' / directory / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    total += len(data)
                    filename = target.relative_to(round_dir).as_posix()
                    observation.files.append(filename)
                    observation.stream(data, filename, deadline)
                except (OSError, SafeIOError) as exc:
                    observation.note(exc)
        except FileNotFoundError:
            pass
        except (OSError, SafeIOError) as exc:
            observation.note(exc)
    # Public events can supply the root ID and parent-side completion notices.
    try:
        public = safe_read(round_dir, 'codex/events.jsonl', max_bytes=MAX_FILE_BYTES)
        observation.stream(public, 'codex/events.jsonl', deadline)
    except (OSError, SafeIOError) as exc:
        observation.note(exc)
    try:
        stderr = safe_read(round_dir, 'codex/stderr.log', max_bytes=1024 * 1024).decode('utf-8', 'replace')
        for number, line in enumerate(stderr.splitlines(), 1):
            if 'collab spawn failed' in line:
                observation.attempts.append({'file': 'codex/stderr.log', 'line': number,
                                             'event': 'spawn_failed'})
    except (OSError, SafeIOError):
        pass
    if not observation.files:
        observation.note('No raw rollout was retained; public CLI events may omit subagent activity')
    return observation.report()


def observe(codex_home: Path, round_dir: Path):
    """Contain read/parse/archive failures; callers never use this as a gate."""
    try:
        report = collect(codex_home, round_dir)
    except Exception as exc:
        report = {'schema_version': 1, 'status': 'unknown', 'informational_only': True,
                  'limitations': [f'Diagnostics unavailable: {type(exc).__name__}: {exc}']}
    try:
        target = round_dir / 'agent-diagnostics.json'
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(target)
    except Exception:
        pass
    return report
