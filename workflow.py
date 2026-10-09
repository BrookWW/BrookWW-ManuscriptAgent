"""Local workflow scheduling over the unchanged isolated manuscript CLI.

Each full stage is one runner; each segmented pass is a fresh runner and plan.
Only published revisions are handed forward, never failed workspaces or current
symlinks. Cancellation interrupts only the CLI process owned by this scheduler;
the CLI is responsible for stopping its isolated session process group.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

from bundle import BundleError, create_bundle
from manuscript import local_defaults
from i18n import LANGUAGES, LocalizedError, error_text, language, status_text, text

_CLI = Path(__file__).with_name('manuscript.py')


def _label_zh(label):
    labels = {'Manuscript path': '论文路径', 'Project root': '项目根目录',
              'Extra asset': '额外依赖', 'Model': '模型', 'Reasoning': '推理强度',
              'Session timeout': '会话超时', 'Output directory': '输出目录',
              'Codex executable': 'Codex 可执行文件', 'PDF extraction timeout': 'PDF 文字提取超时'}
    if label.startswith('Stage '):
        _, number, field = label.split(' ', 2)
        field = {'count': '次数', 'model': '模型', 'reasoning': '推理强度', 'timeout': '超时'}.get(field, field)
        return f'阶段 {number} 的{field}'
    return labels.get(label, label)


def _positive(value, label, *, integer=False):
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise LocalizedError('{label} must be a positive number.', '{zh}必须是正数。', label=label, zh=_label_zh(label)) from None
    if isinstance(value, bool) or not math.isfinite(result) or result <= 0 or (integer and not result.is_integer()):
        raise LocalizedError('{label} must be a positive {kind}.', '{zh}必须是{kind_zh}。', label=label, zh=_label_zh(label), kind='integer' if integer else 'number', kind_zh='正整数' if integer else '正数')
    return int(result) if integer else result


def _text(value, label):
    if value is None:
        return ''
    if not isinstance(value, str) or '\x00' in value:
        raise LocalizedError('{label} must be text without NUL characters.', '{zh}必须是不含空字符的文本。', label=label, zh=_label_zh(label))
    return value.strip()


def _absolute(value):
    # abspath preserves symlinks for the bundle's strict checks.
    return Path(os.path.abspath(Path(value).expanduser()))


def validate_config(config: dict) -> dict:
    """Validate and snapshot configuration without invoking a model or credentials."""
    if not isinstance(config, dict):
        raise LocalizedError('Workflow configuration must be an object.', '流程配置必须是 JSON 对象。')
    value = copy.deepcopy(config)
    locale = value.get('language', 'en')
    if locale not in LANGUAGES:
        raise LocalizedError('Choose English or Simplified Chinese.', '请选择英文或简体中文。')
    source = _text(value.get('input'), 'Manuscript path')
    if not source:
        raise LocalizedError('Choose a manuscript .tex file.', '请选择论文入口 .tex 文件。')
    source = _absolute(source)
    root = _absolute(_text(value.get('project_root'), 'Project root') or source.parent)
    assets = value.get('assets', [])
    if not isinstance(assets, list):
        raise LocalizedError('Extra assets must be a list of file paths.', '额外依赖必须是文件路径列表。')
    relative_assets = []
    for asset in assets:
        item = Path(_text(asset, 'Extra asset')).expanduser()
        if '..' in item.parts:
            raise LocalizedError('Extra asset paths must not contain parent traversal.', '额外依赖路径不能包含上级目录跳转。')
        if item.is_absolute():
            try:
                item = item.relative_to(root)
            except ValueError:
                raise LocalizedError('Extra asset is outside the project root: {item}', '额外依赖不在项目根目录内：{item}', item=item) from None
        relative_assets.append(item.as_posix())
    try:
        with tempfile.TemporaryDirectory(prefix='manuscript-validate-') as scratch:
            entry = create_bundle(source, root, Path(scratch) / 'bundle', [Path(a) for a in relative_assets])
    except (BundleError, OSError) as exc:
        raise LocalizedError('Could not load the manuscript and its dependencies: {detail}',
                             '无法读取论文或其依赖文件：{detail}', detail=str(exc)) from exc
    try:
        default_model, default_reasoning = local_defaults()
    except Exception as exc:
        raise LocalizedError('Could not read the local Codex configuration: {detail}',
                             '无法读取本机 Codex 配置：{detail}', detail=str(exc)) from exc
    model = _text(value.get('model'), 'Model') or default_model
    reasoning = _text(value.get('reasoning'), 'Reasoning') or default_reasoning
    timeout = _positive(value.get('timeout', 7200), 'Session timeout')
    stages = value.get('stages')
    if not isinstance(stages, list) or not stages:
        raise LocalizedError('At least one workflow stage is required.', '流程至少需要一个阶段。')
    normalized = []
    for index, stage in enumerate(stages, 1):
        if not isinstance(stage, dict) or stage.get('mode') not in ('full', 'segmented'):
            raise LocalizedError('Stage {index}: choose full or segmented mode.', '阶段 {index}：请选择全文或分段模式。', index=index)
        current = {'mode': stage['mode'], 'count': _positive(stage.get('count'), f'Stage {index} count', integer=True)}
        for key in ('model', 'reasoning'):
            current[key] = _text(stage.get(key), f'Stage {index} {key}')
        current['timeout'] = None if stage.get('timeout') in (None, '') else _positive(stage['timeout'], f'Stage {index} timeout')
        if not (current['model'] or model):
            raise LocalizedError('Stage {index}: choose a model or configure a local Codex default.', '阶段 {index}：请选择模型或设置本机 Codex 默认模型。', index=index)
        normalized.append(current)
    destination = _text(value.get('output'), 'Output directory')
    if destination:
        destination = str(_absolute(destination))
        if Path(destination).exists() or Path(destination).is_symlink():
            raise LocalizedError('Output directory already exists; choose a new directory.', '输出目录已存在，请选择新目录。')
    codex = _text(value.get('codex'), 'Codex executable') or 'codex'
    executable = shutil.which(os.path.expanduser(codex))
    if not executable:
        raise LocalizedError('Codex executable not found or not executable: {codex}', '找不到 Codex 可执行文件或无执行权限：{codex}', codex=codex)
    return dict(input=str(source), project_root=str(root), entry=entry, language=locale,
                assets=list(dict.fromkeys(relative_assets)), model=model, reasoning=reasoning,
                timeout=timeout, build_timeout=_positive(value.get('build_timeout', 180), 'PDF extraction timeout'),
                codex=str(_absolute(executable)), output=destination, stages=normalized)


def _save(path: Path, state: dict):
    temp = path.with_suffix('.json.tmp')
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def _read_runner(directory: Path) -> dict:
    try:
        value = json.loads((directory / 'run.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _tail(path: Path, limit=24000):
    try:
        with path.open('rb') as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


def run_workflow(config: dict, output: Path, on_update: Callable | None = None,
                 cancel_event: threading.Event | None = None) -> dict:
    """Run synchronously; return a persisted completed/failed/cancelled state.

    Validation/output errors raise ValueError/OSError before any subprocess runs.
    A callback is observational: callback failures cannot orphan a running CLI.
    """
    config = validate_config(config)
    output = _absolute(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    output = output.resolve()
    state = dict(status='preparing', config=config, output=str(output),
                 current_stage=None, current_pass=None, runner={}, runner_output=None,
                 last_revision=None, last_entry=config['entry'], error=None, logs='', stages=[],
                 report=str(output / 'report.md'))
    cancelled = cancel_event or threading.Event()

    def publish():
        _save(output / 'workflow.json', state)
        if on_update:
            try:
                on_update(copy.deepcopy(state))
            except Exception:
                # Persistence and process cleanup must not depend on a UI callback.
                pass

    def observe(directory, log):
        runner = _read_runner(directory)
        state['runner'] = runner
        state['logs'] = _tail(log)
        completed = runner.get('completed_rounds', 0)
        if isinstance(completed, int) and completed > 0:
            revision = directory / 'revisions' / f'round-{completed:04d}'
            entry = runner.get('entry')
            if revision.is_dir() and not revision.is_symlink() and isinstance(entry, str):
                if not Path(entry).is_absolute() and '..' not in Path(entry).parts and (revision / entry).is_file():
                    state.update(last_revision=str(revision), last_entry=entry)
        publish()

    publish()
    source, root = config['input'], config['project_root']
    process = None
    try:
        for number, stage in enumerate(config['stages'], 1):
            if cancelled.is_set():
                state['status'] = 'cancelled'
                break
            stage_record = dict(index=number, mode=stage['mode'], status='running', runs=[])
            state['stages'].append(stage_record)
            passes = stage['count'] if stage['mode'] == 'segmented' else 1
            for pass_number in range(1, passes + 1):
                if cancelled.is_set():
                    state['status'] = stage_record['status'] = 'cancelled'
                    break
                directory = output / f'stage-{number:03d}-pass-{pass_number:03d}'
                log = output / f'stage-{number:03d}-pass-{pass_number:03d}.log'
                state.update(status='running', current_stage=number, current_pass=pass_number,
                             runner={}, runner_output=str(directory), logs='')
                publish()
                command = [sys.executable, str(_CLI), source, '--project-root', root,
                           '--output', str(directory), '--mode', stage['mode'],
                           '--timeout', str(stage['timeout'] or config['timeout']),
                           '--build-timeout', str(config['build_timeout']), '--codex', config['codex']]
                if stage['mode'] == 'full':
                    command += ['--rounds', str(stage['count'])]
                for key in ('model', 'reasoning'):
                    if stage[key] or config[key]:
                        command += ['--' + key, stage[key] or config[key]]
                for asset in config['assets']:
                    command += ['--asset', asset]
                with log.open('wb') as stream:
                    process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                               stdin=subprocess.DEVNULL, start_new_session=True)
                    interrupted = False
                    while process.poll() is None:
                        if cancelled.is_set() and not interrupted:
                            try:
                                process.send_signal(signal.SIGINT)
                            except ProcessLookupError:
                                pass
                            interrupted = True
                            state['status'] = 'cancelling'
                        observe(directory, log)
                        time.sleep(0.2)
                    code = process.wait()
                    process = None
                observe(directory, log)
                runner = state['runner']
                stage_record['runs'].append(dict(output=str(directory), returncode=code,
                                                status=runner.get('status', 'failed')))
                if cancelled.is_set():
                    state['status'] = stage_record['status'] = 'cancelled'
                    break
                if code != 0 or runner.get('status') != 'completed':
                    raise RuntimeError(runner.get('error') or f'Runner exited with code {code}; see {log}')
                completed = runner.get('completed_rounds', 0)
                revision = directory / 'revisions' / f'round-{completed:04d}'
                if not completed or state['last_revision'] != str(revision):
                    raise RuntimeError('Runner did not publish a usable completed revision.')
                root = state['last_revision']
                source = str(Path(root) / state['last_entry'])
            if state['status'] == 'cancelled':
                break
            stage_record['status'] = 'completed'
        else:
            state['status'] = 'completed'
    except Exception as exc:
        state.update(status='failed', error=error_text(exc, config.get('language')))
        if state['stages']:
            state['stages'][-1]['status'] = 'failed'
    finally:
        # Also handles caller KeyboardInterrupt without leaving our CLI running.
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            process.wait()
            state['status'] = 'cancelled'
            observe(directory, log)
        locale = config['language']
        lines = [text(locale, '# ManuscriptAgent workflow', '# ManuscriptAgent 流程报告'), '',
                 text(locale, 'Status: {status}', '状态：{status}', status=status_text(state['status'], locale)), '',
                 text(locale, 'Last completed source: {source}', '最后完成稿：{source}',
                      source=state['last_revision'] or text(locale, 'None', '无')), '',
                 text(locale, 'Completion records source delivery, not mathematical correctness.',
                      '完成状态表示源文件已交付，不表示数学正确性已获证明。'), '']
        if state['error']:
            lines += [text(locale, 'Error: {error}', '错误：{error}', error=state['error']), '']
        for stage in state['stages']:
            mode = text(locale, 'Full review', '全文审阅') if stage['mode'] == 'full' else text(locale, 'Segmented review', '分段审阅')
            lines += [text(locale, '## Stage {index}: {mode} ({status})', '## 阶段 {index}：{mode}（{status}）',
                           index=stage['index'], mode=mode, status=status_text(stage['status'], locale)), '']
            for run in stage['runs']:
                folder = Path(run['output'])
                lines += [f"- [{folder.name}]({folder.as_uri()})"]
                for report in sorted(folder.glob('archive/round-*/outcome.txt')):
                    label = text(locale, '{round} report', '{round} 报告', round=report.parent.name)
                    lines += [f"  - [{label}]({report.as_uri()})"]
        (output / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        publish()
    return copy.deepcopy(state)
