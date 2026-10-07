"""Synthetic native telemetry; no model call or mathematical-quality claim."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_observer as observer


def event(kind, **payload):
    return {'type': kind, 'payload': payload}


def meta(identifier, parent=None):
    source = {'subagent': {'thread_spawn': {'parent_thread_id': parent,
                                           'agent_path': '/root/proof'}}} if parent else 'exec'
    return event('session_meta', id=identifier, source=source, session_id='root')


def activity(kind):
    return event('event_msg', type='item_completed', thread_id='root',
                 item={'type': 'SubAgentActivity', 'kind': kind,
                       'agent_thread_id': 'child', 'agent_path': '/root/proof'})


class ObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp', prefix='agent-observer-test-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home, self.round = self.root / 'home/.codex', self.root / 'archive/round-0001'
        self.home.mkdir(parents=True)
        (self.round / 'codex').mkdir(parents=True)
        self.write(self.round / 'codex/events.jsonl', [
            {'type': 'thread.started', 'thread_id': 'root'},
        ])
        (self.round / 'codex/stderr.log').write_text('')

    def write(self, path, events):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(e) + '\n' for e in events))

    def rollouts(self, *, complete=True):
        parent = [meta('root'),
                  event('response_item', type='function_call', namespace='collaboration',
                        name='spawn_agent', call_id='spawn'), activity('started'),
                  event('response_item', type='function_call_output', call_id='spawn',
                        output=json.dumps({'task_name': '/root/proof'}))]
        child = [meta('child', 'root'), meta('root'),
                 event('event_msg', type='item_completed', thread_id='child',
                       item={'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0})]
        if complete:
            parent.append(activity('completed'))
            child.append(event('event_msg', type='task_complete', thread_id='child'))
        self.write(self.home / 'sessions/day/root.jsonl', parent)
        self.write(self.home / 'sessions/day/child.jsonl', child)

    def test_correlated_child_execution_completion_and_parent_receipt(self):
        self.rollouts()
        self.write(self.round / 'codex/events.jsonl', [
            {'type': 'thread.started', 'thread_id': 'root'},
            {'type': 'item.completed', 'item': {
                'type': 'collab_tool_call', 'tool': 'wait', 'sender_thread_id': 'root',
                'receiver_thread_ids': ['child'], 'status': 'completed',
                'agents_states': {'child': {'status': 'completed', 'message': 'Synthetic review'}},
            }},
        ])
        (self.home / 'auth.json').write_text('credential must not be archived')
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'completed')
        self.assertEqual([report[k] for k in ['created_count', 'active_count', 'completed_count',
                                            'results_received_count']], [1, 1, 1, 1])
        self.assertEqual(report['agents'][0]['parent_thread_id'], 'root')
        self.assertTrue(report['agents'][0]['evidence'])
        self.assertEqual(json.loads((self.round / 'agent-diagnostics.json').read_text()), report)
        self.assertEqual((self.round / 'native-rollouts/sessions/day/child.jsonl').read_bytes(),
                         (self.home / 'sessions/day/child.jsonl').read_bytes())
        self.assertFalse(list(self.round.rglob('auth.json')))

    def test_active_child_is_not_reported_as_completed(self):
        self.rollouts(complete=False)
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'active')
        self.assertEqual(report['completed_count'], 0)
        self.assertEqual(report['results_received_count'], 0)

    def test_metadata_only_child_is_created_not_executed(self):
        self.write(self.home / 'sessions/root.jsonl', [meta('root')])
        self.write(self.home / 'sessions/child.jsonl', [meta('child', 'root')])
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'created')
        self.assertEqual(report['active_count'], 0)

    def test_root_prose_and_empty_wait_are_not_collaboration_evidence(self):
        self.write(self.round / 'codex/events.jsonl', [
            {'type': 'thread.started', 'thread_id': 'root'},
            {'type': 'item.completed', 'item': {'type': 'agent_message',
                'text': 'Three independent reviewers completed their work.'}},
            {'type': 'item.completed', 'item': {'type': 'collab_tool_call', 'tool': 'wait',
                'sender_thread_id': 'root', 'receiver_thread_ids': [], 'agents_states': {},
                'status': 'completed'}},
        ])
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(report['created_count'], 0)

    def test_spawn_failure_is_attempt_only(self):
        (self.round / 'codex/stderr.log').write_text('ERROR collab spawn failed: no rollout found\n')
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'attempted')
        self.assertEqual(report['created_count'], 0)
        self.assertEqual(report['spawn_attempts'][0]['event'], 'spawn_failed')

    def test_unrelated_child_is_not_attached_to_current_round(self):
        self.write(self.home / 'sessions/child.jsonl', [meta('child', 'unrelated')])
        self.assertEqual(observer.observe(self.home, self.round)['created_count'], 0)

    def test_copied_parent_work_does_not_count_as_child_activity(self):
        self.write(self.home / 'sessions/root.jsonl', [meta('root')])
        self.write(self.home / 'sessions/child.jsonl', [meta('child', 'root'), meta('root'),
            event('event_msg', type='item_completed', thread_id='root',
                  item={'type': 'CommandExecution', 'exit_code': 0}),
            event('event_msg', type='task_complete', thread_id='root')])
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'created')
        self.assertEqual(report['active_count'], 0)

    def test_truncated_log_retains_valid_evidence_and_original_bytes(self):
        self.rollouts()
        path = self.home / 'sessions/day/child.jsonl'
        path.write_bytes(path.read_bytes() + b'{broken')
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['created_count'], 1)
        self.assertTrue(any('Unreadable JSON' in s for s in report['limitations']))
        self.assertEqual((self.round / 'native-rollouts/sessions/day/child.jsonl').read_bytes(), path.read_bytes())

    def test_unrecognized_telemetry_is_archived_without_guessing(self):
        self.write(self.home / 'sessions/new.jsonl', [{'type': 'future_schema', 'reviewers': 3}])
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(len(report['rollout_files']), 1)

    def test_links_are_not_followed_or_copied(self):
        external = self.root / 'external'
        external.mkdir()
        (external / 'credentials.jsonl').write_text('private')
        (self.home / 'sessions').symlink_to(external, target_is_directory=True)
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'unknown')
        self.assertEqual(report['rollout_files'], [])
        self.assertTrue(report['limitations'])
        self.assertEqual((external / 'credentials.jsonl').read_text(), 'private')

    def test_oversized_rollout_is_skipped_without_blocking(self):
        self.rollouts()
        with patch.object(observer, 'MAX_FILE_BYTES', 32):
            report = observer.observe(self.home, self.round)
        self.assertEqual(report['rollout_files'], [])
        self.assertTrue(report['limitations'])

    def test_disk_pressure_does_not_copy_rollouts(self):
        self.rollouts()
        with patch.object(observer.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 0})()):
            report = observer.observe(self.home, self.round)
        self.assertEqual(report['rollout_files'], [])
        self.assertTrue(any('disk space' in s for s in report['limitations']))

    def test_collection_deadline_does_not_block(self):
        self.rollouts()
        with patch.object(observer, 'MAX_SECONDS', -1):
            report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'unknown')
        self.assertTrue(report['limitations'])

    def test_nested_subagents_and_archived_sessions_are_linked(self):
        self.rollouts()
        self.write(self.home / 'archived_sessions/grandchild.jsonl', [
            meta('grandchild', 'child'),
            event('event_msg', type='task_complete', thread_id='grandchild'),
        ])
        report = observer.observe(self.home, self.round)
        self.assertEqual(report['created_count'], 2)
        self.assertEqual(report['completed_count'], 2)

    def test_unexpected_parser_failure_is_unknown_and_nonfatal(self):
        with patch.object(observer, 'collect', side_effect=RuntimeError('unsupported runtime')):
            report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'unknown')
        self.assertTrue(report['informational_only'])

    def test_report_write_failure_is_nonfatal(self):
        with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
            report = observer.observe(self.home, self.round)
        self.assertEqual(report['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
