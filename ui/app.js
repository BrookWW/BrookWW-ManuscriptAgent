'use strict';
const $ = id => document.getElementById(id);
const {messages, storageKey, normalizeLanguage, chooseLanguage, translate} = window.ManuscriptI18n;
let savedLanguage;
try { savedLanguage = localStorage.getItem(storageKey); } catch { /* Storage may be unavailable. */ }
let language = chooseLanguage(new URLSearchParams(location.search).get('language'), savedLanguage, navigator.language);
let stages = [{mode: 'full', count: 3}, {mode: 'segmented', count: 1}, {mode: 'full', count: 1}];
let job = null, locked = false, dragIndex = null, polling = false, pending = false, initialState = true;
let defaults = null, notice = null, connectionError = null;
const expandedStages = new WeakSet();
const active = j => Boolean(j && ['running', 'planning', 'cancelling', 'starting', 'preparing'].includes(j.status));
const t = (key, values) => translate(language, key, values);
// Remove the launch token and initial-language query after reading them.
if (location.search) history.replaceState(null, '', location.pathname);

class InterfaceError extends Error {
  constructor(key, values = {}) { super(t(key, values)); this.key = key; this.values = values; }
}
function errorText(error) {
  return error instanceof InterfaceError ? t(error.key, error.values) : (error?.message || String(error));
}
function renderMessage() {
  const text = notice ? (notice.key ? t(notice.key, notice.error ? {error: errorText(notice.error)} : notice.values) : errorText(notice.error)) : '';
  $('message').textContent = text;
  $('message').classList.toggle('hidden', !text);
}
function message(key, values) { notice = key ? {key, values} : null; renderMessage(); }
function messageError(error, key) { notice = {error, key}; renderMessage(); }
function renderDefaults() {
  $('defaults').textContent = defaults ? t('defaults', {model: defaults.model || t('unset'), reasoning: defaults.reasoning || t('unset')}) : t('loadingDefaults');
}
function renderConnection() {
  $('connection').textContent = connectionError ? t('connectionFailed', {error: errorText(connectionError)}) : '';
}
function setLanguage(value) {
  language = normalizeLanguage(value) || 'en';
  try { localStorage.setItem(storageKey, language); } catch { /* Keep the selection for this page. */ }
  document.documentElement.lang = language;
  document.title = t('pageTitle');
  $('language').value = language;
  document.querySelectorAll('[data-i18n]').forEach(node => { node.textContent = t(node.getAttribute('data-i18n')); });
  for (const attribute of ['placeholder', 'aria-label', 'title']) {
    document.querySelectorAll(`[data-i18n-${attribute}]`).forEach(node => {
      node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`)));
    });
  }
  renderStages();
  renderDefaults();
  renderMessage();
  renderConnection();
  showJob(job);
}
async function api(path, body) {
  const headers = {'X-Manuscript-Language': language};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST', headers,
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  let data;
  try { data = await response.json(); } catch { throw new InterfaceError('invalidResponse'); }
  if (!response.ok) {
    if (data.error || data.message) throw Error(data.error || data.message);
    throw new InterfaceError('requestFailed', {status: response.status});
  }
  return data;
}
function stageSettingsLabel(stage) {
  return t([stage.model, stage.reasoning, stage.timeout].some(value => value !== undefined && value !== null && String(value).trim() !== '') ? 'stageOverrides' : 'stageInherited');
}
function describe(stage) {
  const unit = (stage.mode === 'segmented' ? 'pass' : 'round') + (Number(stage.count) === 1 ? 'One' : 'Other');
  return t('describe', {mode: t(stage.mode), count: stage.count, unit: t(unit)});
}
function summary() { $('overview').textContent = stages.map((stage, index) => `${index + 1}. ${describe(stage)}`).join('  →  '); }
function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else node.setAttribute(key, value);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(text, title, action) {
  const node = el('button', {type: 'button', title, 'aria-label': title}, text);
  node.addEventListener('click', action);
  return node;
}
function reorder(from, to) {
  if (locked || to < 0 || to >= stages.length) return;
  stages.splice(to, 0, stages.splice(from, 1)[0]);
  renderStages();
}
function renderStages() {
  const parent = $('stages');
  // Preserve expanded controls even if a toggle event has not yet been delivered.
  for (const card of parent.children) {
    const details = card.querySelector('details');
    if (details?.open) expandedStages.add(card.stage);
    else expandedStages.delete(card.stage);
  }
  parent.replaceChildren();
  stages.forEach((stage, index) => {
    const n = {index: index + 1};
    const card = el('article', {class: 'stage', 'aria-label': t('stage', n)});
    card.stage = stage;
    const top = el('div', {class: 'stage-top'});
    const grip = button('⠿', t('dragStage', n), () => {});
    grip.className = 'grip';
    grip.draggable = !locked;
    grip.addEventListener('dragstart', event => {
      if (locked) { event.preventDefault(); return; }
      dragIndex = index;
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', String(index));
    });
    grip.addEventListener('dragend', () => {
      dragIndex = null;
      document.querySelectorAll('.drag-over').forEach(node => node.classList.remove('drag-over'));
    });
    card.addEventListener('dragover', event => {
      if (dragIndex !== null && !locked) { event.preventDefault(); card.classList.add('drag-over'); }
    });
    card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
    card.addEventListener('drop', event => {
      event.preventDefault(); card.classList.remove('drag-over');
      if (dragIndex !== null) { reorder(dragIndex, index); dragIndex = null; }
    });
    top.append(grip, el('span', {class: 'number'}, String(index + 1)));
    const mode = el('select', {class: 'mode', 'aria-label': t('stageMode', n)});
    mode.append(el('option', {value: 'full'}, t('fullReview')), el('option', {value: 'segmented'}, t('segmentedReview')));
    mode.value = stage.mode;
    mode.onchange = () => { stage.mode = mode.value; renderStages(); };
    top.append(mode);
    const count = el('div', {class: 'counter'});
    const num = el('input', {type: 'number', min: '1', step: '1', required: '', 'aria-label': t('stageCount', {...n, unit: t(stage.mode === 'segmented' ? 'segmentedCount' : 'fullCount')})});
    num.value = stage.count;
    num.oninput = () => { stage.count = num.value === '' ? '' : Number(num.value); summary(); };
    count.append(
      button('−', t('decrease', n), () => { stage.count = Math.max(1, (Number(stage.count) || 1) - 1); num.value = stage.count; summary(); }),
      num,
      button('+', t('increase', n), () => { stage.count = (Number(stage.count) || 0) + 1; num.value = stage.count; summary(); })
    );
    top.append(count, el('span', {class: 'count-label'}, t(stage.mode === 'segmented' ? 'segmentedCount' : 'roundLabel')));
    const controls = el('div', {class: 'stage-tools'});
    const up = button('↑', t('moveUp', n), () => reorder(index, index - 1));
    up.disabled = index === 0;
    const down = button('↓', t('moveDown', n), () => reorder(index, index + 1));
    down.disabled = index === stages.length - 1;
    const copy = button(t('copy'), t('copyStage', n), () => { stages.splice(index + 1, 0, {...stage}); renderStages(); });
    const remove = button('×', t('deleteStage', n), () => { if (stages.length > 1) { stages.splice(index, 1); renderStages(); } });
    remove.disabled = stages.length === 1;
    controls.append(up, down, copy, remove);
    top.append(controls);
    card.append(top);
    const details = el('details'), settingsSummary = el('summary', {}, stageSettingsLabel(stage));
    details.open = expandedStages.has(stage);
    details.ontoggle = () => { if (details.open) expandedStages.add(stage); else expandedStages.delete(stage); };
    details.append(settingsSummary);
    const grid = el('div', {class: 'grid overrides'});
    [['model', 'model', 'models'], ['reasoning', 'reasoning', 'efforts'], ['timeout', 'stageTimeout', null]].forEach(([key, labelKey, list]) => {
      const label = t(labelKey), wrap = el('label', {}, label);
      const input = el('input', {'aria-label': t('stageField', {...n, field: label}), placeholder: t('inheritGlobal')});
      if (list) input.setAttribute('list', list);
      if (key === 'timeout') { input.type = 'number'; input.min = '0'; input.step = 'any'; }
      input.value = stage[key] ?? '';
      input.oninput = () => {
        stage[key] = key === 'timeout' && input.value !== '' ? Number(input.value) : input.value;
        settingsSummary.textContent = stageSettingsLabel(stage);
      };
      wrap.append(input);
      grid.append(wrap);
    });
    details.append(grid); card.append(details); parent.append(card);
  });
  summary();
}
function config() {
  const result = {language, stages: stages.map(stage => ({...stage})), assets: $('assets').value.split('\n').map(value => value.trim()).filter(Boolean)};
  for (const key of ['input', 'project_root', 'output', 'model', 'reasoning', 'codex']) result[key] = $(key).value.trim();
  for (const key of ['timeout', 'build_timeout']) result[key] = Number($(key).value);
  return result;
}
function validate(c) {
  if (!c || typeof c !== 'object' || Array.isArray(c)) throw new InterfaceError('invalidConfig');
  if (!Array.isArray(c.stages) || !c.stages.length) throw new InterfaceError('noStages');
  c.stages.forEach((stage, index) => {
    const n = {index: index + 1};
    if (!stage || !['full', 'segmented'].includes(stage.mode)) throw new InterfaceError('invalidMode', n);
    if (!Number.isInteger(stage.count) || stage.count < 1) throw new InterfaceError('invalidCount', n);
    if (stage.timeout !== undefined && stage.timeout !== '' && stage.timeout !== null && (!Number.isFinite(Number(stage.timeout)) || Number(stage.timeout) <= 0)) throw new InterfaceError('invalidStageTimeout', n);
    for (const key of ['model', 'reasoning']) {
      if (stage[key] !== undefined && stage[key] !== null && typeof stage[key] !== 'string') throw new InterfaceError('invalidStageText', {...n, field: key});
    }
  });
  if (c.assets !== undefined && (!Array.isArray(c.assets) || c.assets.some(value => typeof value !== 'string'))) throw new InterfaceError('invalidAssets');
  for (const key of ['timeout', 'build_timeout']) {
    if (c[key] !== undefined && (!Number.isFinite(Number(c[key])) || Number(c[key]) <= 0)) throw new InterfaceError('invalidTimeout');
  }
  for (const key of ['input', 'project_root', 'output', 'model', 'reasoning', 'codex']) {
    if (c[key] !== undefined && c[key] !== null && typeof c[key] !== 'string') throw new InterfaceError('invalidText', {field: key});
  }
}
function setConfig(c, {useLanguage = true} = {}) {
  validate(c);
  stages = c.stages.map(stage => ({...stage}));
  for (const key of ['input', 'project_root', 'output', 'model', 'reasoning', 'codex']) $(key).value = c[key] || '';
  $('assets').value = (c.assets || []).join('\n');
  $('timeout').value = c.timeout ?? 7200;
  $('build_timeout').value = c.build_timeout ?? 180;
  if (useLanguage && normalizeLanguage(c.language)) setLanguage(c.language);
  else renderStages();
}
function lock(value) {
  locked = Boolean(value);
  $('editor').disabled = locked;
  $('cancel').disabled = !locked || job?.status === 'cancelling';
}
function showJob(j) {
  job = j;
  lock(pending || active(j));
  $('run').classList.toggle('hidden', !j);
  if (!j) return;
  $('status').textContent = Object.hasOwn(messages.en, j.status) ? t(j.status) : j.status;
  const runner = j.runner || {};
  let phase = t('phaseStage', {current: j.current_stage || 1, total: j.config?.stages?.length || stages.length});
  const stage = j.config?.stages?.[(j.current_stage || 1) - 1];
  if (stage) {
    phase += ` · ${t(stage.mode)}`;
    if (stage.mode === 'segmented') {
      phase += ` · ${t('phasePass', {count: j.current_pass || 1})}`;
      if ((!['running', 'completed'].includes(runner.status) || !runner.planned_rounds) && active(j)) {
        phase += ` · ${t('planningChunks')}`;
      } else if (runner.planned_rounds && (['running', 'completed'].includes(runner.status) || runner.completed_rounds > 0)) {
        phase += ` · ${t(runner.active_round ? 'activeChunk' : 'finishedChunks', {current: runner.active_round || runner.completed_rounds || 0, total: runner.planned_rounds})}`;
      }
    } else if (runner.active_round) {
      phase += ` · ${t('activeRound', {current: runner.active_round, total: stage.count})}`;
    } else if (runner.completed_rounds) {
      phase += ` · ${t('finishedRounds', {current: runner.completed_rounds, total: stage.count})}`;
    }
  }
  if (['completed', 'succeeded'].includes(j.status)) phase = t('workflowComplete');
  if (j.status === 'failed') phase += ` · ${t('laterStagesStopped')}`;
  if (j.status === 'cancelled') phase = t('workflowCancelled');
  $('phase').textContent = phase;
  $('run-detail').textContent = j.output ? t('resultDirectory', {path: j.output}) : '';
  $('run-error').textContent = j.error || '';
  $('run-error').classList.toggle('hidden', !j.error);
  // Preserve runner/model output exactly; only the empty-state message is translated.
  $('logs').textContent = Array.isArray(j.logs) ? (j.logs.length ? j.logs.join('\n') : t('waitingLogs')) : (j.logs || t('waitingLogs'));
  $('cancel').classList.toggle('hidden', !active(j));
  const canResume = ['failed', 'cancelled'].includes(j.status) && j.last_revision && j.last_entry;
  $('resume').classList.toggle('hidden', !canResume);
  $('resume-hint').classList.toggle('hidden', !canResume);
  $('folder').disabled = !j.output;
  $('report').disabled = !j.output;
}
async function refresh() {
  if (polling) return;
  polling = true;
  try {
    const state = await api('/api/state');
    defaults = state.defaults || {};
    renderDefaults();
    $('models').replaceChildren(...(defaults.model ? [el('option', {value: defaults.model})] : []));
    // A running job must restore its form without replacing the browser's language preference.
    if (initialState && active(state.job) && state.job.config) setConfig(state.job.config, {useLanguage: false});
    initialState = false;
    showJob(state.job);
    connectionError = null;
  } catch (error) { connectionError = error; }
  finally { polling = false; renderConnection(); }
}
$('language').onchange = () => setLanguage($('language').value);
$('add-stage').onclick = () => { stages.push({mode: 'full', count: 1}); renderStages(); };
async function pick(target, kind) {
  message();
  try {
    const result = await api('/api/pick', {kind});
    if (result.path) $(target).value = target === 'output' ? result.path.replace(/\/$/, '') + '/review-' + new Date().toISOString().replace(/[:.]/g, '-') : result.path;
  } catch (error) { messageError(error); }
}
$('pick-input').onclick = () => pick('input', 'file');
$('pick-root').onclick = () => pick('project_root', 'folder');
$('pick-output').onclick = () => pick('output', 'folder');
$('save-config').onclick = () => {
  try {
    const c = config(); validate(c);
    const blob = new Blob([JSON.stringify(c, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob), link = el('a', {href: url, download: 'manuscript-workflow.json'});
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); message();
  } catch (error) { messageError(error); }
};
$('load-config').onclick = () => $('config-file').click();
$('config-file').onchange = async event => {
  try {
    if (locked) return;
    const file = event.target.files[0];
    if (file) {
      let parsed;
      try { parsed = JSON.parse(await file.text()); } catch (error) {
        if (error instanceof SyntaxError) throw new InterfaceError('invalidJSON');
        throw error;
      }
      setConfig(parsed?.config || parsed);
      message('configLoaded');
    }
  } catch (error) { messageError(error, 'loadFailed'); }
  finally { event.target.value = ''; }
};
$('workflow-form').onsubmit = async event => {
  event.preventDefault();
  if (locked) return;
  message();
  try {
    const c = config(); validate(c);
    if (!c.input.startsWith('/')) throw new InterfaceError('absoluteInput');
    pending = true; lock(true);
    const result = await api('/api/start', c);
    pending = false; showJob(result.job || result);
    $('run').scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch (error) { pending = false; lock(active(job)); messageError(error); }
};
$('cancel').onclick = async () => {
  message(); $('cancel').disabled = true;
  try { await api('/api/cancel', {}); await refresh(); }
  catch (error) { messageError(error); $('cancel').disabled = false; }
};
for (const kind of ['report', 'folder']) {
  $(kind).onclick = async () => {
    message();
    try { await api('/api/open', {kind}); } catch (error) { messageError(error); }
  };
}
$('resume').onclick = () => {
  if (!job?.last_revision || !job.last_entry) return;
  const c = JSON.parse(JSON.stringify(job.config || config()));
  c.input = job.last_revision.replace(/\/$/, '') + '/' + job.last_entry.replace(/^\//, '');
  c.project_root = job.last_revision;
  c.output = '';
  c.stages = c.stages.slice(Math.max(0, (job.current_stage || 1) - 1));
  if (!c.stages.length) c.stages = [{mode: 'full', count: 1}];
  setConfig(c, {useLanguage: false}); message('resumeLoaded');
  $('input').focus(); $('workflow-form').scrollIntoView({behavior: 'smooth'});
};
window.addEventListener('beforeunload', event => {
  if (locked) { event.preventDefault(); event.returnValue = ''; }
});
setLanguage(language);
refresh();
setInterval(refresh, 1500);
