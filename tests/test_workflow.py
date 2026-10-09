"""Exercise real workflow subprocess boundaries with a synthetic CLI (no model)."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workflow

FAKE = r'''
import argparse, json, pathlib, shutil, signal, sys, time
p=argparse.ArgumentParser()
p.add_argument('input')
for name in ['project-root','output','mode','rounds','timeout','build-timeout','codex','model','reasoning']:
    p.add_argument('--'+name)
p.add_argument('--asset',action='append',default=[])
a=p.parse_args()
o=pathlib.Path(a.output); o.mkdir()
(o/'args.json').write_text(json.dumps(vars(a)))
entry=pathlib.Path(a.input).relative_to(a.project_root).as_posix()
s={'status':'planning' if a.mode=='segmented' else 'running','entry':entry,'completed_rounds':0,'planned_rounds':None if a.mode=='segmented' else int(a.rounds),'active_round':None}
def save():
    q=o/'run.tmp'; q.write_text(json.dumps(s)); q.replace(o/'run.json')
save()
time.sleep(.25)
s.update(status='running',planned_rounds=int(a.rounds) if a.rounds else 2,active_round=1)
revision=o/'revisions'/'round-0001'
shutil.copytree(a.project_root,revision)
with (revision/entry).open('a') as f: f.write('\n% reviewed '+a.mode+'\n')
s['completed_rounds']=1; save()
if a.model=='wait':
    try: time.sleep(60)
    except KeyboardInterrupt:
        s.update(status='interrupted',error='KeyboardInterrupt'); save(); sys.exit(130)
if a.model=='fail':
    s.update(status='failed',error='Synthetic failure'); save(); sys.exit(1)
s.update(status='completed',active_round=None); save()
'''


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'
        (self.project / 'nested').mkdir(parents=True)
        (self.project / 'nested/main.tex').write_text('\\documentclass{article}\n\\input{section}\n')
        (self.project / 'section.tex').write_text('Section')
        (self.project / 'asset.dat').write_text('asset')
        self.cli = self.root / 'fake.py'
        self.cli.write_text(FAKE)
        self.config = dict(input=str(self.project / 'nested/main.tex'), project_root=str(self.project),
                           assets=[str(self.project / 'asset.dat')], codex=sys.executable, stages=[dict(mode='full', count=1)])
        self.defaults = patch.object(workflow, 'local_defaults', return_value=('local-model', 'high'))
        self.defaults.start()
        self.addCleanup(self.defaults.stop)

    def run_config(self, callback=None, event=None):
        with patch.object(workflow, '_CLI', self.cli):
            return workflow.run_workflow(self.config, self.root / 'output', callback, event)

    def test_arbitrary_order_replans_passes_and_preserves_nested_dependencies(self):
        self.config['stages'] = [dict(mode='segmented', count=2), dict(mode='full', count=3),
                                 dict(mode='full', count=1), dict(mode='segmented', count=1)]
        updates=[]
        result=self.run_config(updates.append)
        self.assertEqual(result['status'], 'completed')
        runs=[run for stage in result['stages'] for run in stage['runs']]
        self.assertEqual(len(runs),5)
        previous=None
        for run in runs:
            folder=Path(run['output']); args=json.loads((folder/'args.json').read_text())
            self.assertEqual(args['asset'], ['asset.dat'])
            self.assertEqual(args['model'], 'local-model')
            if previous:
                self.assertEqual(args['project_root'], str(previous/'revisions/round-0001'))
                self.assertTrue(args['input'].endswith('revisions/round-0001/nested/main.tex'))
            if args['mode']=='segmented': self.assertIsNone(args['rounds'])
            previous=folder
        final=Path(result['last_revision'])
        self.assertEqual((final/'asset.dat').read_text(), 'asset')
        self.assertTrue((final/'section.tex').exists())
        self.assertEqual((final/'nested/main.tex').read_text().count('% reviewed'),5)
        self.assertNotIn('% reviewed', (self.project/'nested/main.tex').read_text())
        planning=[s for s in updates if s['runner'].get('status')=='planning']
        self.assertTrue(planning)
        self.assertTrue(all(s['runner']['planned_rounds'] is None for s in planning))

    def test_global_and_per_stage_overrides_and_failure_stops_subsequent(self):
        self.config.update(model='global',reasoning='medium',timeout=222)
        self.config['stages']=[dict(mode='full',count=2),dict(mode='segmented',count=3,model='fail',reasoning='low',timeout=7),dict(mode='full',count=1)]
        result=self.run_config()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['stages']),2)
        second=Path(result['stages'][1]['runs'][0]['output'])
        args=json.loads((second/'args.json').read_text())
        self.assertEqual((args['model'],args['reasoning'],float(args['timeout'])),('fail','low',7))
        first=Path(result['stages'][0]['runs'][0]['output'])
        args=json.loads((first/'args.json').read_text())
        self.assertEqual((args['model'],args['reasoning'],float(args['timeout'])),('global','medium',222))
        self.assertEqual(result['last_revision'], str(second/'revisions/round-0001'))
        self.assertEqual(result['last_entry'],'nested/main.tex')
        self.assertFalse((self.root/'output/stage-003-pass-001').exists())
        persisted=json.loads((self.root/'output/workflow.json').read_text())
        self.assertEqual(persisted,result)

    def test_cancel_interrupts_owned_process_and_retains_revision(self):
        self.config['stages']=[dict(mode='full',count=1,model='wait'),dict(mode='full',count=1)]
        event=threading.Event()
        def update(state):
            if state['last_revision']: event.set()
        result=self.run_config(update,event)
        self.assertEqual(result['status'],'cancelled')
        self.assertTrue(Path(result['last_revision']).is_dir())
        self.assertEqual(len(result['stages']),1)
        self.assertEqual(result['runner']['status'],'interrupted')

    def test_validation_rejects_bad_counts_paths_and_modes(self):
        for count in [0,-1,1.5,True,'nan','inf','oops',None]:
            self.config['stages']=[dict(mode='full',count=count)]
            with self.subTest(count=count),self.assertRaises(ValueError): workflow.validate_config(self.config)
        self.config['stages']=[dict(mode='bad',count=1)]
        with self.assertRaises(ValueError): workflow.validate_config(self.config)
        self.config['stages']=[dict(mode='full',count=1)]
        self.config['assets']=['../elsewhere']
        with self.assertRaises(ValueError): workflow.validate_config(self.config)
        self.config['assets']=[]
        link=self.project/'link.tex'; link.symlink_to('nested/main.tex')
        self.config['input']=str(link)
        with self.assertRaises(ValueError): workflow.validate_config(self.config)

    def test_existing_workflow_directory_is_never_reused(self):
        (self.root/'output').mkdir()
        with self.assertRaises(FileExistsError): self.run_config()

    def test_callback_failure_does_not_abandon_run(self):
        def broken(_): raise RuntimeError('UI disconnected')
        self.assertEqual(self.run_config(broken)['status'],'completed')

    def test_language_is_optional_and_invalid_values_are_rejected(self):
        self.assertEqual(workflow.validate_config(self.config)['language'], 'en')
        self.config['language'] = 'fr'
        with self.assertRaises(ValueError):
            workflow.validate_config(self.config)

    def test_chinese_workflow_summary_preserves_cli_arguments_and_sources(self):
        self.config['language'] = 'zh-CN'
        result = self.run_config()
        self.assertEqual(result['status'], 'completed', result.get('error'))
        report = Path(result['report']).read_text()
        self.assertIn('状态：已完成', report)
        self.assertIn('阶段 1：全文审阅', report)
        self.assertEqual(result['config']['language'], 'zh-CN')
        folder = Path(result['stages'][0]['runs'][0]['output'])
        args = json.loads((folder / 'args.json').read_text())
        self.assertNotIn('language', args)
        self.assertTrue((Path(result['last_revision']) / 'nested/main.tex').read_text().startswith('\\documentclass{article}'))


if __name__=='__main__': unittest.main()
