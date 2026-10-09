'use strict';
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const {messages, normalizeLanguage, chooseLanguage, translate} = require('../ui/i18n.js');

test('launch language overrides the saved language and browser locale', () => {
  assert.equal(chooseLanguage('en', 'zh-CN', 'zh-TW'), 'en');
  assert.equal(chooseLanguage('zh-CN', 'en', 'en-US'), 'zh-CN');
});

test('saved choice wins when the launch language is absent or unsupported', () => {
  assert.equal(chooseLanguage(null, 'zh-CN', 'en-US'), 'zh-CN');
  assert.equal(chooseLanguage('invalid', 'en', 'zh-CN'), 'en');
});

test('a Chinese browser locale uses Chinese, while other locales fall back to English', () => {
  for (const locale of ['zh', 'zh-CN', 'zh-HK', 'zh-TW', 'zh_Hans_CN']) {
    assert.equal(chooseLanguage(null, null, locale), 'zh-CN', locale);
  }
  for (const locale of ['en', 'en-US', 'en-GB', 'ja-JP', 'de-DE', undefined]) {
    assert.equal(chooseLanguage(null, null, locale), 'en', String(locale));
  }
});

test('locale parsing rejects nonlanguage prefixes and nonstrings', () => {
  for (const invalid of ['english', 'zhongwen', '', false, {}, 4]) {
    assert.equal(normalizeLanguage(invalid), null);
  }
  assert.equal(normalizeLanguage(' ZH-cn '), 'zh-CN');
});

test('both catalogs cover the same messages and interpolation variables', () => {
  assert.deepEqual(Object.keys(messages.en).sort(), Object.keys(messages['zh-CN']).sort());
  const parameters = value => [...new Set(value.match(/\{\w+\}/g) || [])].sort();
  for (const key of Object.keys(messages.en)) {
    assert.ok(messages.en[key].trim(), `Empty English message: ${key}`);
    assert.ok(messages['zh-CN'][key].trim(), `Empty Chinese message: ${key}`);
    assert.deepEqual(parameters(messages.en[key]), parameters(messages['zh-CN'][key]), key);
  }
});

test('English catalog contains no untranslated Chinese text', () => {
  for (const [key, value] of Object.entries(messages.en)) assert.doesNotMatch(value, /\p{Script=Han}/u, key);
});

test('progress and validation messages interpolate in either language', () => {
  assert.equal(translate('en', 'activeChunk', {current: 2, total: 8}), 'Current chunk 2 / 8');
  assert.equal(translate('zh-CN', 'activeChunk', {current: 2, total: 8}), '当前第 2 块 / 共 8 块');
  assert.equal(translate('en', 'invalidCount', {index: 3}), 'Stage 3 count must be a positive integer.');
  assert.equal(translate('zh-CN', 'invalidCount', {index: 3}), '阶段 3 的次数必须是正整数。');
});

test('user paths and raw error details are interpolated literally without translation', () => {
  const value = '/Users/test/论文/{current}/$&/main.tex';
  assert.equal(translate('en', 'resultDirectory', {path: value}), `Results directory: ${value}`);
  const error = 'Runner stderr: 中文 <tag> $&';
  assert.equal(translate('en', 'loadFailed', {error}), `Could not load workflow: ${error}`);
});

test('all static translation attributes and literal app translation keys exist', () => {
  const html = readFileSync(path.join(__dirname, '../ui/index.html'), 'utf8');
  const app = readFileSync(path.join(__dirname, '../ui/app.js'), 'utf8');
  const keys = [
    ...Array.from(html.matchAll(/data-i18n(?:-[\w-]+)?="([^"]+)"/g), match => match[1]),
    ...Array.from(app.matchAll(/\b(?:t|InterfaceError|message)\('([^']+)'/g), match => match[1])
  ];
  for (const key of keys) assert.ok(Object.hasOwn(messages.en, key), `Missing translation: ${key}`);
  assert.ok(keys.length > 70, 'Expected static and dynamic interface text coverage');
});

test('unknown translation keys fail explicitly rather than displaying undefined', () => {
  assert.throws(() => translate('en', 'notARealMessage'), /Unknown interface message/);
});
