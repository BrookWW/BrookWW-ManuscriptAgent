/* Interface text only. Manuscripts, model output, paths, and runner logs are not translated. */
(function (root) {
  'use strict';
  const messages = {
    'zh-CN': {
      pageTitle: 'ManuscriptAgent · 论文审阅',
      brand: '论文审阅', local: '本地工作空间', language: '界面语言',
      heading: '按阶段安排论文审阅。',
      intro: '选择稿件，编排阶段。每个阶段自动接续上一阶段交付的源文件。',
      input: '稿件入口文件', pickFile: '选择文件…',
      inputHint: '使用绝对路径；原始稿件保持不变，审阅在独立副本中进行。',
      globalModel: '全局模型', globalReasoning: '全局推理强度', inheritLocal: '继承本机配置',
      loadingDefaults: '正在读取本机默认配置…',
      defaults: '本机默认：模型 {model} · 推理强度 {reasoning}（留空时使用）', unset: '未配置',
      workflow: '审阅流程', workflowHint: '自由添加、复制与排序阶段',
      load: '载入流程', save: '保存流程', addStage: '＋ 添加阶段', order: '执行顺序',
      advanced: '高级设置', projectRoot: '项目根目录', projectRootPlaceholder: '默认：稿件入口所在目录',
      pickFolder: '选择文件夹…', projectRootHint: '包含主稿件与依赖文件的目录。嵌套 TeX 项目请填写实际项目根目录。',
      assets: '额外依赖文件', assetsPlaceholder: '每行一个项目内路径（绝对或相对项目根目录）',
      assetsHint: '项目内未被 TeX 自动引用的参考资料或资源；随稿件复制到独立工作目录。',
      output: '输出位置', outputPlaceholder: '留空自动创建新的运行目录', pickParent: '选择父文件夹…',
      outputHint: '指定本次运行的新目录，目录不可已存在。选择文件夹会在其内生成一个新目录名。',
      timeout: '单次会话超时（秒）', timeoutHint: '每次全文审阅、分段规划或分块审阅的最长时间。',
      buildTimeout: '文献文字提取超时（秒）', buildTimeoutHint: '从来源 PDF 提取文字的最长时间。',
      codex: 'Codex 程序路径', codexHint: '默认使用 PATH 中的 codex；也可填写可执行文件的绝对路径。',
      footer: '全文按轮审阅整篇；分段每遍重新规划，再依次审阅各块。启动后，本次流程将被锁定。关闭网页不会停止任务；退出启动器会取消任务并保留已完成稿件。',
      languageHint: '界面语言也用于操作提示和流程汇总报告；论文继续使用原稿语言。',
      start: '开始审阅 →', run: '运行状态', cancel: '停止运行', resume: '从最后完成稿继续',
      report: '查看报告', folder: '打开结果文件夹',
      resumeHint: '这会以最后保存的稿件建立新流程，并重新运行未完成阶段，不会恢复中断会话。',
      logs: '运行日志', configFile: '选择流程配置 JSON 文件',
      full: '全文', segmented: '分段', fullReview: '全文审阅', segmentedReview: '分段审阅',
      stage: '阶段 {index}', dragStage: '拖动阶段 {index}', stageMode: '阶段 {index} 审阅模式',
      stageCount: '阶段 {index} {unit}', fullCount: '轮数', segmentedCount: '完整遍数', roundLabel: '轮',
      decrease: '减少阶段 {index} 次数', increase: '增加阶段 {index} 次数',
      moveUp: '上移阶段 {index}', moveDown: '下移阶段 {index}',
      copy: '复制', copyStage: '复制阶段 {index}', deleteStage: '删除阶段 {index}',
      stageOverrides: '阶段设置 · 有覆盖参数', stageInherited: '阶段设置 · 默认继承全局',
      model: '模型', reasoning: '推理强度', stageTimeout: '会话超时（秒）',
      stageField: '阶段 {index} {field}', inheritGlobal: '继承全局',
      describe: '{mode} {count} {unit}', roundOne: '轮', roundOther: '轮', passOne: '遍', passOther: '遍',
      invalidResponse: '本地服务返回了无法读取的响应。', requestFailed: '请求失败（{status}）。',
      invalidConfig: '流程配置必须是 JSON 对象。', noStages: '流程至少需要一个阶段。',
      invalidMode: '阶段 {index} 的模式无效。', invalidCount: '阶段 {index} 的次数必须是正整数。',
      invalidStageTimeout: '阶段 {index} 的超时必须大于 0。',
      invalidAssets: '额外依赖必须是路径字符串数组。', invalidTimeout: '超时时间必须大于 0。',
      invalidText: '配置字段 {field} 必须是字符串。', invalidStageText: '阶段 {index} 的 {field} 必须是字符串。',
      absoluteInput: '请填写稿件入口文件的绝对路径。', invalidJSON: '文件不是有效的 JSON。',
      preparing: '准备中', running: '运行中', starting: '启动中', planning: '规划中', cancelling: '正在停止',
      cancelled: '已停止', failed: '运行失败', completed: '已完成', succeeded: '已完成',
      phaseStage: '阶段 {current} / {total}', phasePass: '第 {count} 遍', planningChunks: '正在规划分块',
      activeChunk: '当前第 {current} 块 / 共 {total} 块', finishedChunks: '已完成 {current} 块 / 共 {total} 块',
      activeRound: '第 {current} / {total} 轮', finishedRounds: '已完成 {current} / {total} 轮',
      workflowComplete: '流程已完成', laterStagesStopped: '已停止后续阶段',
      workflowCancelled: '运行已停止，已完成稿件仍保留', resultDirectory: '结果目录：{path}',
      waitingLogs: '等待运行日志…',
      connectionFailed: '连接本地服务失败：{error} 任务可能仍在后台运行，请勿重复启动。',
      configLoaded: '流程已载入。启动前请确认本机文件路径与输出目录。',
      loadFailed: '无法载入流程：{error}',
      resumeLoaded: '已载入最后完成稿。当前未完成阶段将从头执行；请检查剩余阶段和次数后点击“开始审阅”。'
    },
    en: {
      pageTitle: 'ManuscriptAgent · Manuscript review',
      brand: 'Manuscript review', local: 'Local workspace', language: 'Interface language',
      heading: 'Arrange your manuscript review in stages.',
      intro: 'Choose a manuscript and arrange the stages. Each stage continues from the source files delivered by the previous stage.',
      input: 'Manuscript entry file', pickFile: 'Choose file…',
      inputHint: 'Use an absolute path. Review runs on a separate copy, leaving your original manuscript unchanged.',
      globalModel: 'Global model', globalReasoning: 'Global reasoning effort', inheritLocal: 'Use local configuration',
      loadingDefaults: 'Reading local defaults…',
      defaults: 'Local defaults: model {model} · reasoning effort {reasoning} (used when left blank)', unset: 'not configured',
      workflow: 'Review workflow', workflowHint: 'Add, duplicate, and reorder stages',
      load: 'Load workflow', save: 'Save workflow', addStage: '＋ Add stage', order: 'Execution order',
      advanced: 'Advanced settings', projectRoot: 'Project root', projectRootPlaceholder: 'Default: directory containing the entry file',
      pickFolder: 'Choose folder…', projectRootHint: 'The directory containing your manuscript and dependencies. For a nested TeX project, enter the actual project root.',
      assets: 'Additional dependencies', assetsPlaceholder: 'One project path per line (absolute or relative to the project root)',
      assetsHint: 'Project references or assets not automatically included by TeX. They are copied with the manuscript into the separate working directory.',
      output: 'Output location', outputPlaceholder: 'Leave blank to create a new run directory automatically', pickParent: 'Choose parent folder…',
      outputHint: 'Enter a new directory for this run; it must not already exist. Choosing a folder generates a new directory name inside it.',
      timeout: 'Session timeout (seconds)', timeoutHint: 'Maximum time for each full review, segmentation plan, or chunk review.',
      buildTimeout: 'Source text extraction timeout (seconds)', buildTimeoutHint: 'Maximum time for extracting text from a source PDF.',
      codex: 'Codex executable', codexHint: 'Uses codex from PATH by default. You may also enter the executable’s absolute path.',
      footer: 'Full mode reviews the entire manuscript in rounds. Segmented mode plans each pass, then reviews its chunks in order. Starting locks the workflow. Closing this page leaves the task running; quitting the launcher cancels it and preserves completed revisions.',
      languageHint: 'The interface language also applies to messages and workflow summary reports. The manuscript keeps its original language.',
      start: 'Start review →', run: 'Run status', cancel: 'Stop run', resume: 'Continue from last revision',
      report: 'View report', folder: 'Open results folder',
      resumeHint: 'Creates a new workflow from the last saved revision and reruns unfinished stages. It does not resume an interrupted session.',
      logs: 'Run logs', configFile: 'Choose a workflow configuration JSON file',
      full: 'Full', segmented: 'Segmented', fullReview: 'Full review', segmentedReview: 'Segmented review',
      stage: 'Stage {index}', dragStage: 'Drag stage {index}', stageMode: 'Stage {index} review mode',
      stageCount: 'Stage {index}: {unit}', fullCount: 'rounds', segmentedCount: 'complete passes', roundLabel: 'rounds',
      decrease: 'Decrease stage {index} count', increase: 'Increase stage {index} count',
      moveUp: 'Move stage {index} up', moveDown: 'Move stage {index} down',
      copy: 'Copy', copyStage: 'Copy stage {index}', deleteStage: 'Delete stage {index}',
      stageOverrides: 'Stage settings · overrides set', stageInherited: 'Stage settings · using global defaults',
      model: 'Model', reasoning: 'Reasoning effort', stageTimeout: 'Session timeout (seconds)',
      stageField: 'Stage {index}: {field}', inheritGlobal: 'Use global setting',
      describe: '{mode} · {count} {unit}', roundOne: 'round', roundOther: 'rounds', passOne: 'pass', passOther: 'passes',
      invalidResponse: 'The local service returned an unreadable response.', requestFailed: 'Request failed ({status}).',
      invalidConfig: 'Workflow configuration must be a JSON object.', noStages: 'The workflow needs at least one stage.',
      invalidMode: 'Stage {index} has an invalid mode.', invalidCount: 'Stage {index} count must be a positive integer.',
      invalidStageTimeout: 'Stage {index} timeout must be greater than 0.',
      invalidAssets: 'Additional dependencies must be an array of path strings.', invalidTimeout: 'Timeouts must be greater than 0.',
      invalidText: 'Configuration field {field} must be a string.', invalidStageText: 'Stage {index} field {field} must be a string.',
      absoluteInput: 'Enter the absolute path to the manuscript entry file.', invalidJSON: 'The file is not valid JSON.',
      preparing: 'Preparing', running: 'Running', starting: 'Starting', planning: 'Planning', cancelling: 'Stopping',
      cancelled: 'Stopped', failed: 'Failed', completed: 'Completed', succeeded: 'Completed',
      phaseStage: 'Stage {current} / {total}', phasePass: 'Pass {count}', planningChunks: 'Planning chunks',
      activeChunk: 'Current chunk {current} / {total}', finishedChunks: 'Completed {current} / {total} chunks',
      activeRound: 'Round {current} / {total}', finishedRounds: 'Completed {current} / {total} rounds',
      workflowComplete: 'Workflow completed', laterStagesStopped: 'Later stages stopped',
      workflowCancelled: 'Run stopped; completed revisions are preserved', resultDirectory: 'Results directory: {path}',
      waitingLogs: 'Waiting for run logs…',
      connectionFailed: 'Could not connect to the local service: {error} The task may still be running. Do not start it again.',
      configLoaded: 'Workflow loaded. Check local file paths and the output directory before starting.',
      loadFailed: 'Could not load workflow: {error}',
      resumeLoaded: 'Last completed revision loaded. The unfinished stage will restart from the beginning. Check the remaining stages and counts, then click “Start review”.'
    }
  };
  const storageKey = 'manuscriptagent.language';
  function normalizeLanguage(value) {
    if (typeof value !== 'string') return null;
    const normalized = value.trim().toLowerCase().replace(/_/g, '-');
    if (/^zh(?:-|$)/.test(normalized)) return 'zh-CN';
    if (/^en(?:-|$)/.test(normalized)) return 'en';
    return null;
  }
  function chooseLanguage(explicit, saved, browserLanguage) {
    return normalizeLanguage(explicit) || normalizeLanguage(saved) || normalizeLanguage(browserLanguage) || 'en';
  }
  function translate(language, key, values = {}) {
    const dictionary = messages[normalizeLanguage(language) || 'en'];
    const template = dictionary[key];
    if (template === undefined) throw new Error(`Unknown interface message: ${key}`);
    return template.replace(/\{(\w+)\}/g, (placeholder, name) => values[name] === undefined ? placeholder : String(values[name]));
  }
  const api = {messages, storageKey, normalizeLanguage, chooseLanguage, translate};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.ManuscriptI18n = api;
})(typeof window !== 'undefined' ? window : null);
