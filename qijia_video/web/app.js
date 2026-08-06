const API = '/api/qijia-video';
const PROMPT_STORAGE_KEY = 'qijia-video-generation-settings-v1';
const SCRIPT_TARGET_MIN_CHARS = 220;
const SCRIPT_TARGET_MAX_CHARS = 300;
const SCRIPT_HARD_MAX_CHARS = 600;
const state = {
  capabilities: null,
  cards: [],
  jobs: [],
  selectedJob: null,
  activeTask: null,
  pollingTaskId: '',
  pollPromise: null,
  pollGeneration: 0,
  busy: false,
  selectedShotId: '',
  previewVersionId: '',
  previewFrameCandidateId: '',
  storyboardKey: '',
  inspectorKey: '',
  referenceImageFile: null,
  referenceImagePreviewUrl: '',
  scriptEditorDraft: null,
  workspaceTab: 'topics',
  topicRuns: [],
  selectedTopicRun: null,
  activeTopicTask: null,
  topicPollingTaskId: '',
  topicPollPromise: null,
  topicPollGeneration: 0,
  topicHandoffCandidate: null,
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[char]));

function formatCount(value) {
  const count = Number(value || 0);
  if (!Number.isFinite(count) || count <= 0) return '0';
  if (count >= 100000000) return `${(count / 100000000).toFixed(count >= 1000000000 ? 0 : 1)}亿`;
  if (count >= 10000) return `${(count / 10000).toFixed(count >= 100000 ? 0 : 1)}万`;
  return new Intl.NumberFormat('zh-CN').format(Math.round(count));
}

function formatUsd(value, fallback = '金额待账单') {
  if (value === null || value === undefined || value === '') return fallback;
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) return fallback;
  return `$${amount.toFixed(6).replace(/0+$/, '').replace(/\.$/, '') || '0'}`;
}

function formatPercent(value) {
  const ratio = Number(value);
  if (!Number.isFinite(ratio) || ratio < 0) return '';
  return `${(ratio * 100).toFixed(ratio >= 0.1 ? 1 : 2)}%`;
}

function formatDateTime(value) {
  const parsed = Date.parse(String(value || ''));
  if (!Number.isFinite(parsed)) return String(value || '');
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(parsed));
}

function switchWorkspace(tab) {
  state.workspaceTab = tab === 'production' ? 'production' : 'topics';
  const topicsActive = state.workspaceTab === 'topics';
  $('#topic-workspace').hidden = !topicsActive;
  $('#production-workspace').hidden = topicsActive;
  document.querySelectorAll('[data-workspace-tab]').forEach((button) => {
    const active = button.dataset.workspaceTab === state.workspaceTab;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
}

function narrationCharacterCount(value) {
  return Array.from(String(value || '').replace(/\s+/g, '')).length;
}

function scriptBeats(script) {
  if (Array.isArray(script?.beats)) return script.beats;
  return (script?.narration_segments || []).map((segment) => ({
    id: segment.id,
    narration: segment.text || '',
    role: segment.segment_type || 'explanation',
    visual_direction: '',
    on_screen_text: '',
    source_refs: segment.source_refs || [],
    quote_ref: segment.quote_ref ?? null,
  }));
}

function editorNarrationText() {
  return Array.from(document.querySelectorAll('[data-script-field="narration"]'))
    .map((node) => node.value.trim())
    .filter(Boolean)
    .join('\n');
}

function updateScriptLengthStatus() {
  const status = $('#script-length-status');
  if (!status) return;
  const count = narrationCharacterCount(editorNarrationText());
  const estimatedSeconds = Math.max(1, Math.round(count / 4.1));
  if (!count) {
    status.textContent = '只统计会被念出的旁白';
  } else if (count > SCRIPT_HARD_MAX_CHARS) {
    status.textContent = `旁白 ${count} 字 · 预计约 ${estimatedSeconds} 秒 · 已超过技术安全上限`;
  } else if (count >= SCRIPT_TARGET_MIN_CHARS && count <= SCRIPT_TARGET_MAX_CHARS) {
    status.textContent = `旁白 ${count} 字 · 预计约 ${estimatedSeconds} 秒 · 节奏合适`;
  } else {
    status.textContent = `旁白 ${count} 字 · 预计约 ${estimatedSeconds} 秒 · ${SCRIPT_TARGET_MIN_CHARS}-${SCRIPT_TARGET_MAX_CHARS} 字仅作建议`;
  }
  const overlong = count > SCRIPT_HARD_MAX_CHARS;
  status.classList.toggle('warning', overlong);
  const approveButton = $('#approve-script-button');
  if (approveButton) approveButton.disabled = state.busy;
}

function isNarrationRevisionFailure(job) {
  const narration = scriptBeats(job?.script)
    .map((beat) => beat.narration)
    .join('');
  const legacyProductionStall = ['script_approved', 'producing'].includes(job?.state)
    && narrationCharacterCount(narration) > SCRIPT_HARD_MAX_CHARS;
  const failedForNarration = job?.state === 'failed'
    && /完整口播稿?共|narration_duration_range|duration_range|旁白实际时长/.test(String(job.error || ''));
  return legacyProductionStall || failedForNarration;
}

function generationDefaults() {
  return state.capabilities?.generation_defaults || {
    script_prompt: '', seedance_prompt: '', shot_count: 5,
  };
}

function setPromptFields(settings) {
  const defaults = generationDefaults();
  $('#script-generation-prompt').value = settings?.script_prompt || defaults.script_prompt || '';
  $('#seedance-generation-prompt').value = settings?.seedance_prompt || defaults.seedance_prompt || '';
}

function persistPromptFields() {
  try {
    localStorage.setItem(PROMPT_STORAGE_KEY, JSON.stringify(generationSettingsPayload()));
  } catch { /* 浏览器禁用本地存储时仍可正常创建任务。 */ }
}

function initializePromptFields() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(PROMPT_STORAGE_KEY) || 'null'); } catch { saved = null; }
  const legacyFixedStructure = saved?.script_prompt?.includes('【五段结构】')
    && saved.script_prompt.includes('n01 / hook');
  const legacySeedanceStyle = saved?.seedance_prompt?.includes('写实电影感')
    && saved.seedance_prompt.includes('中国家庭生活观察镜头');
  const legacyLongScript = saved?.script_prompt?.includes('目标时长约 60 秒')
    && saved.script_prompt.includes('230-300 个汉字');
  const legacyIndependentAnimation = saved?.seedance_prompt?.includes('独立的心理隐喻')
    && saved.seedance_prompt.includes('不依赖前后镜头');
  const legacySingleTrackScript = saved?.script_prompt?.includes('总字数控制在 200-240 个汉字')
    || saved?.script_prompt?.includes('中文口播脚本，目标时长约 45-50 秒');
  const legacyPreRetentionHook = saved?.script_prompt?.includes('这不是人物简介，而是对一个观点的深入展开')
    && saved.script_prompt.includes('不要写成模板化的五段论')
    && !saved.script_prompt.includes('降低 2 秒流失率、提高 5 秒完播率');
  const legacyPreDirectorPrompt = saved?.script_prompt?.includes('降低 2 秒流失率、提高 5 秒完播率')
    && !saved.script_prompt.includes('贯穿全片的可见变化');
  if (
    legacyFixedStructure
    || legacyLongScript
    || legacySingleTrackScript
    || legacyPreRetentionHook
    || legacyPreDirectorPrompt
  ) {
    saved.script_prompt = generationDefaults().script_prompt;
  }
  if (legacySeedanceStyle || legacyIndependentAnimation) {
    saved.seedance_prompt = generationDefaults().seedance_prompt;
  }
  setPromptFields(saved);
  if (
    legacyFixedStructure
    || legacyLongScript
    || legacySingleTrackScript
    || legacyPreRetentionHook
    || legacyPreDirectorPrompt
    || legacySeedanceStyle
    || legacyIndependentAnimation
  ) persistPromptFields();
}

function generationSettingsPayload() {
  const scriptPrompt = $('#script-generation-prompt').value.trim();
  const seedancePrompt = $('#seedance-generation-prompt').value.trim();
  if (!scriptPrompt) throw new Error('脚本生成提示词不能为空');
  if (!seedancePrompt) throw new Error('全片画面导演设定不能为空');
  return {script_prompt: scriptPrompt, seedance_prompt: seedancePrompt, shot_count: 5};
}

async function api(method, path, body) {
  const response = await fetch(API + path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok || payload.code !== 0) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).filter(Boolean).join('；')
      : payload.detail;
    throw new Error(payload.message || detail || `请求失败（HTTP ${response.status}）`);
  }
  return payload.data;
}

async function apiMultipart(path, body) {
  const response = await fetch(API + path, {
    method: 'POST',
    credentials: 'same-origin',
    body,
  });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok || payload.code !== 0) {
    const detail = Array.isArray(payload.detail)
      ? payload.detail.map((item) => item.msg).filter(Boolean).join('；')
      : payload.detail;
    throw new Error(payload.message || detail || `请求失败（HTTP ${response.status}）`);
  }
  return payload.data;
}

function clearReferenceImage() {
  if (state.referenceImagePreviewUrl) URL.revokeObjectURL(state.referenceImagePreviewUrl);
  state.referenceImageFile = null;
  state.referenceImagePreviewUrl = '';
  $('#reference-image-input').value = '';
  $('#reference-preview-image').removeAttribute('src');
  $('#reference-file-name').textContent = '';
  $('#reference-preview').hidden = true;
  $('#reference-empty').hidden = false;
  $('#remove-reference-image').hidden = true;
}

function setReferenceImage(file) {
  if (!file) return;
  const allowedTypes = new Set(['image/jpeg', 'image/png', 'image/webp']);
  const hasAllowedExtension = /\.(?:jpe?g|png|webp)$/i.test(file.name || '');
  if (!allowedTypes.has(file.type) && !hasAllowedExtension) {
    notify('参考图只支持 JPG、PNG 或 WebP 格式。', true);
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    notify('参考图不能超过 10 MB。', true);
    return;
  }
  clearReferenceImage();
  state.referenceImageFile = file;
  state.referenceImagePreviewUrl = URL.createObjectURL(file);
  $('#reference-preview-image').src = state.referenceImagePreviewUrl;
  $('#reference-file-name').textContent = file.name || '已选择参考图';
  $('#reference-preview').hidden = false;
  $('#reference-empty').hidden = true;
  $('#remove-reference-image').hidden = false;
  notify('');
}

function notify(message, error = false) {
  const node = $('#notice');
  node.hidden = !message;
  node.textContent = message || '';
  node.classList.toggle('error', error);
}

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll('button').forEach((button) => { button.disabled = busy; });
  if (!busy) {
    updateScriptLengthStatus();
    renderTopicControls();
    renderTopicDetail();
  }
}

const stateLabels = {
  card_verified: '观点已创建', script_generating: '生成脚本', script_review_required: '待确认脚本',
  script_approved: '脚本已确认', producing: '生产中', quality_checking: '质检中',
  final_review_required: '待确认成片', final_approved: '正在生成发布包', packaged: '发布包完成', failed: '失败',
};
const workflowStages = [
  '观点已确认', '生成脚本', '确认脚本', '生成旁白', '分镜与首帧',
  'AI 视频 1/3', 'AI 视频 2/3', 'AI 视频 3/3',
  '图像与视频合成', '确认成片', '发布包完成',
];
const progressStageIndexes = {
  material_confirmed: 0,
  script: 1,
  script_generation: 1,
  confirm_script: 2,
  tts: 3,
  production: 3,
  storyboard: 4,
  first_frames: 4,
  frame_selection: 4,
  seedance_shot_1: 5,
  seedance_shot_2: 6,
  seedance_shot_3: 7,
  // Older five-video jobs can still finish after this release.
  seedance_shot_4: 7,
  seedance_shot_5: 7,
  remotion: 8,
  quality: 8,
  confirm_final: 9,
  package: 10,
};
const domainLabels = {
  parent_education: '家庭教育', developmental_psychology: '发展心理学', educational_psychology: '教育心理学',
  parent_child_relationship: '亲子关系', parent_growth: '家长成长',
};

const topicStateLabels = {
  running: '研究中', ready: '待选择', failed: '失败',
};

function topicTaskForRun(run) {
  if (!run?.last_run_task_id || state.activeTopicTask?.task_id !== run.last_run_task_id) return null;
  return state.activeTopicTask;
}

function renderTopicControls() {
  const capability = state.capabilities?.topic_research;
  const ready = !!capability?.ready;
  const hasRunning = state.topicRuns.some((run) => run.status === 'running');
  const button = $('#topic-start-button');
  button.disabled = state.busy || !ready || hasRunning;
  if (!ready) {
    button.textContent = '选题研究尚未配置';
    $('#topic-start-hint').textContent = `缺少：${(capability?.missing_configuration || []).join('、') || 'TikHub 或模型配置'}`;
  } else if (hasRunning) {
    button.textContent = '本轮研究进行中';
    $('#topic-start-hint').textContent = '同一时间只运行一轮，避免重复产生费用。';
  } else {
    button.textContent = '开始研究今日选题';
    $('#topic-start-hint').textContent = '点击即确认本轮可能产生 API 费用；不会自动发布或自动生成视频。';
  }
  const plannedCalls = Number(capability?.planned_max_calls || 13);
  const requestBudget = Number(capability?.request_budget || 0);
  const unitPrice = Number(capability?.estimated_usd_per_success);
  const maxEstimate = Number.isFinite(unitPrice) && unitPrice > 0
    ? `，TikHub 规划上限约 ${formatUsd(plannedCalls * unitPrice)}`
    : '';
  $('#topic-cost-guard').innerHTML = `
    <strong>本轮成本保护</strong>
    <span>计划最多 ${plannedCalls} 次 TikHub 请求${requestBudget ? `，硬上限 ${requestBudget} 次` : ''} + 1 次编辑模型调用${escapeHtml(maxEstimate)}</span>`;
}

function renderTopicRuns() {
  const node = $('#topic-run-list');
  if (!state.topicRuns.length) {
    node.innerHTML = '<p class="empty">还没有选题研究记录。</p>';
    return;
  }
  node.innerHTML = state.topicRuns.map((run) => {
    const selected = state.selectedTopicRun?.id === run.id;
    const date = run.valid_through || formatDateTime(run.created_at) || '日期待确认';
    const candidateCount = (run.candidates || []).length;
    return `<button class="topic-run-card ${selected ? 'selected' : ''}" type="button" data-topic-run-id="${escapeHtml(run.id)}">
      <strong>家庭教育 · ${escapeHtml(date)}</strong>
      <span>${escapeHtml(topicStateLabels[run.status] || run.status)}${candidateCount ? ` · ${candidateCount} 个候选` : ''}${run.selected_candidate_id ? ' · 已采用' : ''}</span>
    </button>`;
  }).join('');
}

function topicMetricPills(evidence) {
  const metrics = evidence?.metrics;
  if (!metrics) return '';
  const values = [];
  if (metrics.play_count) values.push(`播放 ${formatCount(metrics.play_count)}`);
  if (metrics.like_rate !== null && metrics.like_rate !== undefined) values.push(`赞播比 ${formatPercent(metrics.like_rate)}`);
  if (metrics.comment_count) values.push(`评论 ${formatCount(metrics.comment_count)}`);
  if (metrics.share_count) values.push(`分享 ${formatCount(metrics.share_count)}`);
  if (metrics.follower_count) values.push(`作者粉丝 ${formatCount(metrics.follower_count)}`);
  const playFollowerRatio = Number(metrics.play_follower_ratio);
  if (Number.isFinite(playFollowerRatio) && playFollowerRatio > 0) {
    const precision = playFollowerRatio >= 100 ? 0 : (playFollowerRatio >= 10 ? 1 : 2);
    values.push(`播粉比 ${playFollowerRatio.toFixed(precision)}`);
  }
  return values.map((value) => `<span>${escapeHtml(value)}</span>`).join('');
}

function topicEvidenceRow(evidence) {
  const labels = (evidence.platform_labels || []).join(' · ');
  const queries = (evidence.queries || []).join(' / ');
  const publishedAt = evidence.published_at ? formatDateTime(evidence.published_at) : '';
  const duration = Number(evidence.duration_seconds);
  const subline = [
    evidence.author_name || '',
    publishedAt ? `发布 ${publishedAt}` : '',
    Number.isFinite(duration) && duration > 0 ? `${Math.round(duration)} 秒` : '',
    labels,
    queries ? `检索：${queries}` : '',
  ].filter(Boolean).join(' · ');
  const title = evidence.video_url
    ? `<a href="${escapeHtml(evidence.video_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(evidence.title)} ↗</a>`
    : `<strong>${escapeHtml(evidence.title)}</strong>`;
  return `<div class="topic-evidence-row">
    <div class="topic-evidence-copy">${title}<span>${escapeHtml(subline)}</span></div>
    <div class="topic-metrics">${topicMetricPills(evidence)}</div>
  </div>`;
}

function renderTopicCost(run) {
  const cost = run?.cost || {};
  const model = cost.model_usage || null;
  const calls = cost.tikhub_calls || [];
  const tikhubCost = cost.estimated_tikhub_cost_usd === null || cost.estimated_tikhub_cost_usd === undefined
    ? '金额待账单'
    : `约 ${formatUsd(cost.estimated_tikhub_cost_usd)}`;
  const modelState = model?.request_count
    ? (model.succeeded ? '成功' : (run?.status === 'running' ? '已调用，结果待确认' : '失败或中断'))
    : '等待调用';
  const modelTokens = model?.total_tokens ? ` · ${formatCount(model.total_tokens)} tokens` : '';
  const modelCost = model?.request_count
    ? `${modelState} · ${formatUsd(model.reported_cost_usd)}${modelTokens}`
    : modelState;
  const totalCost = formatUsd(cost.estimated_total_cost_usd, '待供应商返回完整金额');
  const unitCost = cost.estimated_cost_per_candidate_usd === null || cost.estimated_cost_per_candidate_usd === undefined
    ? ''
    : ` · 每个候选约 ${formatUsd(cost.estimated_cost_per_candidate_usd)}`;
  const callDetails = calls.length ? `<details class="topic-cost-details">
    <summary>查看 ${calls.length} 次 TikHub 调用明细</summary>
    <div>${calls.map((call, index) => {
      const endpoint = String(call.endpoint || '').split('/').filter(Boolean).pop() || call.endpoint || 'unknown';
      const requestId = call.request_id ? ` · ${call.request_id}` : '';
      const responseCode = call.response_code === null || call.response_code === undefined ? 'code —' : `code ${call.response_code}`;
      return `<p><span>${index + 1}. ${escapeHtml(endpoint)}</span><strong>${call.succeeded ? '成功' : '失败'} · ${escapeHtml(responseCode)} · ${Number(call.elapsed_ms || 0)} ms${escapeHtml(requestId)}</strong></p>`;
    }).join('')}</div>
  </details>` : '';
  const modelTokenDetail = model?.request_count
    ? `输入 ${formatCount(model.input_tokens)} / 输出 ${formatCount(model.output_tokens)}`
    : '';
  const modelDetails = model?.request_count ? `<details class="topic-cost-details">
    <summary>查看编辑模型调用明细</summary>
    <div><p><span>${escapeHtml(`${model.model || '模型待确认'} · ${modelTokenDetail}`)}</span><strong>${model.http_status_code ? `HTTP ${Number(model.http_status_code)}` : '网络状态未知'}${model.request_id ? ` · ${escapeHtml(model.request_id)}` : ''}</strong></p></div>
  </details>` : '';
  $('#topic-cost-summary').innerHTML = `
    <div class="topic-cost-item"><span>TikHub 调用</span><strong>${Number(cost.tikhub_success_count || 0)} 成功 / ${Number(cost.tikhub_request_count || 0)} 已发 / ${Number(cost.tikhub_request_budget || 0)} 上限</strong></div>
    <div class="topic-cost-item"><span>TikHub 规划成本</span><strong>${escapeHtml(tikhubCost)}</strong></div>
    <div class="topic-cost-item"><span>编辑模型</span><strong>${escapeHtml(modelCost)}</strong></div>
    <div class="topic-cost-item"><span>本轮总成本</span><strong>${escapeHtml(totalCost + unitCost)}</strong></div>
    <p class="topic-cost-basis">${escapeHtml(cost.tikhub_cost_basis || '调用后记录成本依据；实际账单以供应商为准。')}</p>
    ${callDetails}
    ${modelDetails}`;
}

function renderTopicDetail() {
  const run = state.selectedTopicRun;
  $('#topic-empty').hidden = !!run;
  $('#topic-detail').hidden = !run;
  if (!run) {
    renderTopicControls();
    return;
  }
  $('#topic-detail-title').textContent = run.status === 'running'
    ? '正在研究家庭教育选题'
    : (run.status === 'failed' ? '本轮研究未完成' : '家庭教育候选选题');
  $('#topic-run-state').textContent = topicStateLabels[run.status] || run.status;
  const meta = [
    '仅抖音',
    run.valid_through ? `数据截至 ${run.valid_through}` : '数据日期读取中',
    run.data_window_note || '',
  ].filter(Boolean);
  $('#topic-run-meta').innerHTML = meta.map((item) => `<span>${escapeHtml(item)}</span>`).join('');
  renderTopicCost(run);
  const warnings = run.warnings || [];
  const warningNode = $('#topic-warning-list');
  warningNode.hidden = !warnings.length;
  warningNode.innerHTML = warnings.map((item) => `<p>${escapeHtml(item)}</p>`).join('');
  const errorNode = $('#topic-run-error');
  errorNode.hidden = !run.error;
  errorNode.textContent = run.error || '';

  const task = topicTaskForRun(run);
  const progressNode = $('#topic-progress');
  const isRunning = run.status === 'running';
  progressNode.hidden = !isRunning;
  if (isRunning) {
    const percent = Math.max(3, Math.min(100, Number(task?.progress_meta?.percent || 5)));
    $('#topic-progress-text').textContent = task?.progress || '正在等待后台任务…';
    $('#topic-progress-bar').style.width = `${percent}%`;
    $('#topic-progress-meter').setAttribute('aria-valuenow', String(Math.round(percent)));
  }

  const evidenceById = new Map((run.evidence || []).map((item) => [item.id, item]));
  const candidates = run.candidates || [];
  if (!candidates.length) {
    $('#topic-candidate-list').innerHTML = isRunning
      ? '<p class="empty">系统正在收集和整理抖音样本。完成前不会展示半成品候选。</p>'
      : '<p class="empty">本轮没有形成可用候选。</p>';
    renderTopicControls();
    return;
  }
  $('#topic-candidate-list').innerHTML = candidates.map((candidate) => {
    const selected = run.selected_candidate_id === candidate.id;
    const evidence = candidate.evidence_refs.map((id) => evidenceById.get(id)).filter(Boolean);
    return `<article class="topic-candidate ${selected ? 'selected' : ''}">
      <header class="topic-candidate-header">
        <span class="topic-rank">${String(candidate.rank).padStart(2, '0')}</span>
        <div class="topic-candidate-title"><h3>${escapeHtml(candidate.title)}</h3><p>${escapeHtml(candidate.parent_question)}</p></div>
        <span class="topic-pillar">${escapeHtml(candidate.content_pillar)}</span>
      </header>
      <div class="topic-candidate-body">
        <div class="topic-copy-block"><span>建议切入角度</span><p>${escapeHtml(candidate.editorial_angle)}</p></div>
        <div class="topic-copy-block"><span>开场钩子</span><p class="topic-hook">${escapeHtml(candidate.opening_hook)}</p></div>
        <div class="topic-copy-block full"><span>为什么现在值得讲</span><p>${escapeHtml(candidate.why_now)}</p></div>
        <div class="topic-copy-block full"><span>内容边界</span><p class="topic-risk">${escapeHtml(candidate.risk_note)}</p></div>
        <details class="topic-evidence"><summary>查看 ${evidence.length} 条抖音研究依据</summary><div class="topic-evidence-list">${evidence.map(topicEvidenceRow).join('')}</div></details>
        <div class="topic-candidate-actions">
          <span>采用后仍需补充独立可靠资料，趋势数据不会进入脚本来源。</span>
          <button class="button ${selected ? 'secondary' : 'primary'}" type="button" data-adopt-topic="${escapeHtml(candidate.id)}" ${state.busy ? 'disabled' : ''}>${selected ? '继续补充来源' : '采用并补充来源'}</button>
        </div>
      </div>
    </article>`;
  }).join('');
  renderTopicControls();
}

function renderCapabilities() {
  const node = $('#system-status');
  const data = state.capabilities;
  if (!data) return;
  const videoReady = !!data.real_generation_ready;
  const topicReady = !!data.topic_research?.ready;
  const ready = videoReady && topicReady;
  node.classList.toggle('ready', ready);
  node.classList.toggle('warning', !ready);
  const parts = [
    topicReady
      ? '家庭教育选题研究已就绪'
      : `选题研究待配置：${(data.topic_research?.missing_configuration || []).join('、') || '配置不完整'}`,
    videoReady
      ? `视频生产已就绪 · ${data.storage} 存储`
      : `视频生产待配置：${(data.missing_configuration || []).join('、') || data.renderer?.detail || '配置不完整'}`,
  ];
  node.querySelector('span:last-child').textContent = parts.join(' ｜ ');
  renderTopicControls();
}

function renderCards() {
  const node = $('#source-card-list');
  if (!state.cards.length) { node.innerHTML = '<p class="empty">还没有保存过创作资料。</p>'; return; }
  node.innerHTML = state.cards.map((card) => `
    <article class="list-card">
      <h3>${escapeHtml(card.title)}</h3>
      <div class="meta"><span>${escapeHtml(domainLabels[card.content_domain] || card.content_domain)}</span><span>v${card.revision}</span><span>${card.status === 'verified' ? '已核验' : '草稿'}</span></div>
      ${card.status === 'verified'
        ? `<div class="list-actions"><button class="button primary" data-create-job="${escapeHtml(card.id)}">用这个观点再生成一版</button></div>`
        : '<div class="meta legacy-card-note">旧版来源草稿，仅保留记录</div>'}
    </article>`).join('');
}

function renderJobs() {
  const node = $('#job-list');
  if (!state.jobs.length) { node.innerHTML = '<p class="empty">输入人物和观点，开始第一条视频。</p>'; return; }
  node.innerHTML = state.jobs.map((job) => {
    const title = job.source_card_snapshot?.title || job.id;
    return `<article class="job-card ${state.selectedJob?.id === job.id ? 'selected' : ''}" data-job-id="${escapeHtml(job.id)}" tabindex="0">
      <h3>${escapeHtml(title)}</h3>
      <div class="meta"><span>${escapeHtml(stateLabels[job.state] || job.state)}</span><span>v${job.revision}</span><span>${escapeHtml(job.updated_at || '')}</span></div>
    </article>`;
  }).join('');
}

function taskForJob(job) {
  if (!job?.last_run_task_id || state.activeTask?.task_id !== job.last_run_task_id) return null;
  return state.activeTask;
}

function stageIndex(job) {
  if (job.state === 'script_review_required') return 2;
  if (job.state === 'final_review_required') return 9;
  if (job.state === 'packaged') return 10;
  const taskStage = taskForJob(job)?.progress_meta?.stage;
  if (taskStage in progressStageIndexes) return progressStageIndexes[taskStage];
  if (job.state === 'failed') {
    return {script: 1, production: 3, quality: 8, package: 10}[job.failed_stage] ?? 0;
  }
  return {
    card_verified: 0,
    script_generating: 1,
    script_review_required: 2,
    script_approved: 3,
    producing: 3,
    quality_checking: 8,
    final_review_required: 9,
    final_approved: 10,
    packaged: 10,
  }[job.state] ?? 0;
}

function parseTaskTime(value) {
  if (!value) return Number.NaN;
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value)
    ? `${value.replace(' ', 'T')}+08:00`
    : value;
  return Date.parse(normalized);
}

function elapsedText(task) {
  const started = parseTaskTime(task?.created_at);
  if (!Number.isFinite(started)) return '—';
  const finished = parseTaskTime(task?.finished_at);
  const seconds = Math.max(0, Math.floor(((Number.isFinite(finished) ? finished : Date.now()) - started) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return `${minutes} 分 ${remainder} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function formatTokens(value) {
  return new Intl.NumberFormat('zh-CN').format(Math.max(0, Number(value) || 0));
}

function requestSignature(request) {
  if (!request) return '';
  return JSON.stringify([
    request.request_id,
    request.prompt,
    request.resolution,
    request.ratio,
    request.duration_seconds,
    request.generate_audio,
    request.seed ?? null,
    request.first_frame_asset_id || '',
  ]);
}

function activeJobTaskRunning(job) {
  const task = taskForJob(job);
  return !!task && !['done', 'failed'].includes(task.status);
}

function currentTaskForShot(job, request) {
  return (job.video_tasks || []).find(
    (task) => task.request_id === request.request_id,
  ) || null;
}

function renderedVisualAsset(job, request) {
  const block = (job.render_manifest?.visual_blocks || []).find(
    (item) => item.shot_id === request.request_id,
  );
  if (block?.asset_id) {
    const asset = (job.render_manifest?.assets || []).find(
      (item) => item.asset_id === block.asset_id,
    );
    if (String(asset?.media_type || '').startsWith('video/')) return asset;
  }
  const requestIndex = (job.visual_requests || []).findIndex(
    (item) => item.request_id === request.request_id,
  );
  return requestIndex >= 0
    ? (job.render_manifest?.assets || [])
      .filter((asset) => String(asset.media_type || '').startsWith('video/'))[requestIndex] || null
    : null;
}

function versionsForShot(job, request) {
  if (request.visual_type === 'image') return [];
  const versions = (job.visual_versions || [])
    .filter((version) => version.shot_id === request.request_id)
    .sort((left, right) => left.version - right.version);
  if (versions.length) return versions;
  const task = currentTaskForShot(job, request);
  const asset = renderedVisualAsset(job, request);
  return task ? [{
    version_id: '',
    shot_id: request.request_id,
    version: 1,
    request,
    task,
    asset,
    synthetic: true,
  }] : [];
}

function currentVersionForShot(job, request) {
  const signature = requestSignature(request);
  return versionsForShot(job, request).find(
    (version) => requestSignature(version.request) === signature,
  ) || null;
}

function shotMediaUrl(job, request, version) {
  if (!version?.asset) return '';
  const root = `${API}/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(request.request_id)}`;
  return version.version_id
    ? `${root}/versions/${encodeURIComponent(version.version_id)}/media`
    : `${root}/media`;
}

function storyboardShotFor(job, shotId) {
  return (job.storyboard_plan?.shots || []).find(
    (shot) => shot.shot_id === shotId,
  ) || null;
}

function frameCandidatesForShot(job, shotId) {
  return (job.first_frame_candidates || [])
    .filter((candidate) => candidate.shot_id === shotId && candidate.asset)
    .sort((left, right) => left.variant - right.variant);
}

function frameSelectionForShot(job, shotId) {
  // Read compatibility for 0.7.x jobs that generated two frames per shot.
  return (job.frame_selections || []).find(
    (selection) => selection.shot_id === shotId,
  ) || null;
}

function frameCandidateMediaUrl(job, candidate) {
  if (!candidate?.asset) return '';
  return `${API}/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(candidate.shot_id)}/frames/${encodeURIComponent(candidate.candidate_id)}/media`;
}

function currentFrameCandidate(job, request) {
  const candidates = frameCandidatesForShot(job, request.request_id);
  const selectedId = storyboardShotFor(job, request.request_id)?.selected_candidate_id || '';
  return candidates.find(
    (candidate) => candidate.asset?.asset_id === request.first_frame_asset_id,
  ) || candidates.find(
    (candidate) => candidate.candidate_id === selectedId,
  ) || null;
}

function storyboardRequests(job) {
  const shots = job?.storyboard_plan?.shots || [];
  if (!shots.length) {
    return (job?.visual_requests || []).map((request) => ({
      ...request,
      visual_type: 'video',
    }));
  }
  const requestsById = new Map(
    (job?.visual_requests || []).map((request) => [request.request_id, request]),
  );
  return shots.map((shot) => {
    const candidates = frameCandidatesForShot(job, shot.shot_id);
    const selected = candidates.find(
      (candidate) => candidate.candidate_id === shot.selected_candidate_id,
    ) || candidates[0] || null;
    const request = requestsById.get(shot.shot_id);
    const block = (job.render_manifest?.visual_blocks || []).find(
      (item) => item.shot_id === shot.shot_id,
    );
    return {
      ...(request || {}),
      request_id: shot.shot_id,
      prompt: request?.prompt || shot.first_frame_prompt || shot.motion_prompt,
      resolution: request?.resolution || '480p',
      ratio: request?.ratio || '9:16',
      duration_seconds: request?.duration_seconds
        || (block ? Math.max(1, Math.round(block.duration_in_frames / 30)) : 6),
      generate_audio: false,
      seed: request?.seed ?? null,
      first_frame_asset_id: request?.first_frame_asset_id || selected?.asset?.asset_id || '',
      planning_preview: !request,
      visual_type: shot.visual_type || 'video',
    };
  });
}

function shotNarration(request) {
  const prompt = String(request?.prompt || '');
  for (const markerText of ['【画面语义参考】', '口播：']) {
    const marker = prompt.lastIndexOf(markerText);
    if (marker >= 0) return prompt.slice(marker + markerText.length).trim();
  }
  return prompt.trim();
}

function shotDescription(job, request) {
  return storyboardShotFor(job, request.request_id)?.visual_intent
    || shotNarration(request);
}

function shotStatus(task, hasPreview) {
  if (hasPreview) return {label: '可预览', className: 'succeeded'};
  const status = task?.state || '';
  if (status === 'failed' || status === 'cancelled') return {label: '生成失败', className: 'failed'};
  if (status === 'succeeded') return {label: '正在整理预览', className: 'running'};
  if (status === 'running') return {label: '生成中', className: 'running'};
  if (status === 'queued') return {label: '已提交', className: 'running'};
  return {label: '等待生成', className: 'waiting'};
}

function renderSeedanceUsage(job) {
  const section = $('#seedance-usage-summary');
  const requests = job?.visual_requests || [];
  const firstFrames = (job?.first_frame_candidates || []).filter(
    (candidate) => candidate.asset,
  );
  const selectedTasks = job?.video_tasks || [];
  const attempts = new Map();
  for (const task of [
    ...selectedTasks,
    ...(job?.visual_versions || []).map((version) => version.task),
  ]) {
    if (!task) continue;
    attempts.set(
      `${task.provider || ''}:${task.provider_task_id || task.request_fingerprint}`,
      task,
    );
  }
  const tasks = [...attempts.values()];
  section.hidden = requests.length === 0 && tasks.length === 0 && firstFrames.length === 0;
  if (section.hidden) return;

  const totalTokens = tasks.reduce(
    (total, task) => total + Math.max(0, Number(task.usage_total_tokens) || 0),
    0,
  );
  const recordedCount = tasks.filter((task) => Number(task.usage_total_tokens) > 0).length;
  const rate = Math.max(
    0,
    Number(state.capabilities?.seedance_pricing?.yuan_per_million_tokens) || 0,
  );
  const imageRate = Math.max(
    0,
    Number(state.capabilities?.seedream_pricing?.yuan_per_image) || 0,
  );
  const seedanceCost = totalTokens && rate ? (totalTokens * rate) / 1000000 : 0;
  const seedreamCost = firstFrames.length * imageRate;
  const firstRequest = requests[0];
  const durations = requests.map((request) => Number(request.duration_seconds) || 0).filter(Boolean);
  const minDuration = durations.length ? Math.min(...durations) : 0;
  const maxDuration = durations.length ? Math.max(...durations) : 0;
  const durationLabel = minDuration === maxDuration
    ? `${minDuration} 秒/段`
    : `${minDuration}-${maxDuration} 秒/段`;
  $('#seedance-token-total').textContent = totalTokens
    ? `${formatTokens(totalTokens)} tokens`
    : '等待方舟返回';
  $('#seedance-cost-estimate').textContent = seedanceCost || seedreamCost
    ? `约 ¥${(seedanceCost + seedreamCost).toFixed(2)}`
    : '—';
  $('#seedance-spec').textContent = firstRequest
    ? `${firstFrames.length} 张首帧 · ${requests.length} 段视频 · ${durationLabel} · ${firstRequest.resolution}`
    : `${firstFrames.length} 张首帧 · ${tasks.length} 个镜头`;
  $('#seedance-usage-status').textContent = `首帧 ${firstFrames.length} 张 · Seedance 累计 ${tasks.length} 次 · tokens 已记录 ${recordedCount}/${tasks.length}`;
  const frameUsage = firstFrames.length
    ? `<div class="usage-row"><span>Seedream 首帧 · ${firstFrames.length} 张</span><span>${imageRate ? `约 ¥${seedreamCost.toFixed(2)}` : '已生成'}</span></div>`
    : '';
  $('#seedance-usage-list').innerHTML = frameUsage + requests.map((request) => {
    const chapterIndex = (job.storyboard_plan?.shots || []).findIndex(
      (shot) => shot.shot_id === request.request_id,
    );
    const chapterLabel = chapterIndex >= 0 ? `第 ${chapterIndex + 1} 章` : request.request_id;
    const versions = (job.visual_versions || [])
      .filter((version) => version.shot_id === request.request_id)
      .sort((left, right) => left.version - right.version);
    const rows = versions.length
      ? versions.map((version) => ({label: `v${version.version}`, task: version.task}))
      : [{label: 'v1', task: currentTaskForShot(job, request)}];
    const status = rows.map(({label, task}) => {
      const tokens = Number(task?.usage_total_tokens) || 0;
      return `${label} ${tokens ? `${formatTokens(tokens)} tokens` : (task?.raw_status || task?.state || '未提交')}`;
    }).join(' · ');
    return `<div class="usage-row"><span>${escapeHtml(chapterLabel)} · Seedance 视频 · ${escapeHtml(request.resolution)} · ${request.duration_seconds} 秒</span><span>${escapeHtml(status)}</span></div>`;
  }).join('');
  $('#seedance-price-basis').textContent = [
    state.capabilities?.seedream_pricing?.basis,
    state.capabilities?.seedance_pricing?.basis,
  ].filter(Boolean).join('；');
}

function renderShotInspector(job) {
  const inspector = $('#shot-inspector');
  const layout = inspector.closest('.storyboard-layout');
  const requests = storyboardRequests(job);
  const requestIndex = requests.findIndex(
    (request) => request.request_id === state.selectedShotId,
  );
  if (requestIndex < 0) {
    inspector.hidden = true;
    layout.classList.remove('inspector-open');
    state.inspectorKey = '';
    return;
  }

  const request = requests[requestIndex];
  const isImage = request.visual_type === 'image';
  const versions = versionsForShot(job, request);
  const currentVersion = currentVersionForShot(job, request);
  const currentKey = currentVersion?.version_id || '__current';
  let previewKey = state.previewVersionId || currentKey;
  let previewVersion = versions.find(
    (version) => (version.version_id || '__current') === previewKey,
  );
  if (!previewVersion?.asset) {
    previewVersion = currentVersion;
    previewKey = currentKey;
  }
  state.previewVersionId = previewKey;
  const previewIsCurrent = isImage || (previewVersion
    && requestSignature(previewVersion.request) === requestSignature(request));
  const frameCandidates = frameCandidatesForShot(job, request.request_id);
  const showFrameChoices = frameCandidates.length > 1;
  const frameSelection = frameSelectionForShot(job, request.request_id);
  const previewRequest = previewVersion?.request || request;
  const previewFrame = frameCandidates.find(
    (candidate) => candidate.asset?.asset_id === previewRequest.first_frame_asset_id,
  ) || currentFrameCandidate(job, request);
  let chosenFrame = frameCandidates.find(
    (candidate) => candidate.candidate_id === state.previewFrameCandidateId,
  ) || previewFrame
    || frameCandidates.find(
      (candidate) => candidate.candidate_id === frameSelection?.recommended_candidate_id,
    )
    || frameCandidates[0]
    || null;
  state.previewFrameCandidateId = chosenFrame?.candidate_id || '';
  const chosenFrameIsCurrent = !!chosenFrame
    && chosenFrame.asset?.asset_id === request.first_frame_asset_id;
  const taskRunning = activeJobTaskRunning(job);
  const canEdit = !isImage
    && job.state === 'final_review_required'
    && !taskRunning
    && !state.busy;
  const key = JSON.stringify({
    job: job.id,
    shot: request.request_id,
    preview: previewKey,
    previewFrame: state.previewFrameCandidateId,
    current: requestSignature(request),
    taskRunning,
    versions: versions.map((version) => [
      version.version_id,
      version.task?.state,
      version.asset?.sha256 || '',
      requestSignature(version.request),
    ]),
    frames: frameCandidates.map((candidate) => [
      candidate.candidate_id,
      candidate.asset?.sha256 || '',
    ]),
    recommendation: frameSelection?.recommended_candidate_id || '',
  });
  inspector.hidden = false;
  layout.classList.add('inspector-open');
  if (state.inspectorKey === key) return;
  state.inspectorKey = key;

  const mediaUrl = isImage ? '' : shotMediaUrl(job, request, previewVersion);
  const previewLabel = isImage
    ? '动态图片'
    : previewVersion ? `v${previewVersion.version}` : '当前版本';
  const versionButtons = versions.map((version) => {
    const versionKey = version.version_id || '__current';
    const usable = !!version.asset && version.task?.state === 'succeeded';
    const selected = versionKey === previewKey;
    const isCurrent = requestSignature(version.request) === requestSignature(request);
    const suffix = isCurrent ? ' · 成片' : usable ? '' : ` · ${version.task?.state || '处理中'}`;
    return `<button class="version-pill ${selected ? 'previewing' : ''}" type="button" data-preview-version="${escapeHtml(versionKey)}" ${usable ? '' : 'disabled'}>v${version.version}${escapeHtml(suffix)}</button>`;
  }).join('');
  const applyButton = !previewIsCurrent && previewVersion?.asset
    ? `<button class="button secondary" type="button" data-select-version="${escapeHtml(previewVersion.version_id)}" ${canEdit ? '' : 'disabled'}>将 ${escapeHtml(previewLabel)} 用于成片</button>`
    : '';
  const frameEvaluations = new Map(
    (frameSelection?.evaluations || []).map((item) => [item.candidate_id, item]),
  );
  const frameButtons = showFrameChoices ? frameCandidates.map((candidate) => {
    const isPreviewing = candidate.candidate_id === state.previewFrameCandidateId;
    const isRecommended = candidate.candidate_id === frameSelection?.recommended_candidate_id;
    const isCurrent = candidate.asset?.asset_id === request.first_frame_asset_id;
    const evaluation = frameEvaluations.get(candidate.candidate_id);
    const badges = [
      isRecommended ? '<span>AI 推荐</span>' : '',
      isCurrent ? '<span>当前使用</span>' : '',
    ].filter(Boolean).join('');
    return `<button class="frame-candidate ${isPreviewing ? 'previewing' : ''}" type="button" data-frame-candidate="${escapeHtml(candidate.candidate_id)}" aria-label="首帧候选 ${candidate.variant}">
      <img src="${frameCandidateMediaUrl(job, candidate)}" alt="镜头 ${requestIndex + 1} 首帧候选 ${candidate.variant}">
      <strong>候选 ${candidate.variant}${evaluation ? ` · ${evaluation.total_score} 分` : ''}</strong>
      <div>${badges}</div>
    </button>`;
  }).join('') : '';
  const framePreviewUrl = frameCandidateMediaUrl(job, chosenFrame);
  const regenerateLabel = chosenFrame && !chosenFrameIsCurrent
    ? '用这张首帧换一版'
    : '按当前首帧换一版';
  const mediaKind = isImage ? '动态图片' : 'Seedance 视频';
  inspector.innerHTML = `
    <div class="shot-inspector-header">
      <div><h4>镜头 ${requestIndex + 1} · ${mediaKind}</h4><p>${isImage ? `${escapeHtml(previewLabel)} · 成片约 ${request.duration_seconds} 秒 · Remotion 动态取景` : `${escapeHtml(previewLabel)} · ${request.duration_seconds} 秒 · ${escapeHtml(request.resolution)}`}</p></div>
      <button class="icon-button" type="button" data-close-inspector aria-label="关闭镜头设置">×</button>
    </div>
    ${mediaUrl
      ? `<video class="inspector-video" src="${mediaUrl}" controls preload="metadata" playsinline></video>`
      : framePreviewUrl
        ? `<div class="inspector-frame-wrap"><img class="inspector-frame-preview" src="${framePreviewUrl}" alt="当前首帧预览"><span>${isImage ? '成片中会自动添加缓慢取景运动' : 'Seedance 视频生成后会替换此预览'}</span></div>`
        : '<div class="inspector-empty">首帧和视频生成后可在这里预览</div>'}
    ${isImage ? '' : `<div class="version-row" aria-label="镜头历史版本">${versionButtons || '<span class="field-hint">首个版本生成后会保留在这里</span>'}</div>`}
    ${frameButtons ? `<section class="frame-candidate-section"><div class="frame-candidate-heading"><strong>${isImage ? '图片候选' : '首帧候选'}</strong><span>${isImage ? '系统已推荐当前成片使用的图片，可点击查看另一构图' : '系统已自动推荐，可人工改选后重生成本镜头'}</span></div><div class="frame-candidate-grid">${frameButtons}</div></section>` : ''}
    ${isImage ? '' : `<label class="shot-prompt-field">这个镜头想呈现什么
      <textarea id="shot-prompt-editor" rows="7" maxlength="4000" spellcheck="false" ${canEdit ? '' : 'readonly'}>${escapeHtml(previewVersion?.request?.prompt || request.prompt)}</textarea>
    </label>
    <div class="shot-inspector-actions">
      ${applyButton}
      <button class="button primary" type="button" data-regenerate-shot ${canEdit ? '' : 'disabled'}>${regenerateLabel}</button>
    </div>
    <p class="cost-note">改提示词或改首帧都只会新增 1 次 ${request.duration_seconds} 秒、${escapeHtml(request.resolution)} Seedance 2.0 调用；不会重做旁白、图片章节或其他视频镜头。</p>`}
    ${isImage ? '<p class="cost-note">这个章节直接使用 Seedream 图片，由 Remotion 添加轻微推进或横移，不产生 Seedance 视频费用。</p>' : ''}`;
}

function renderShotStoryboard(job) {
  const section = $('#shot-storyboard');
  const requests = storyboardRequests(job);
  const selectionWarning = $('#frame-selection-warning');
  selectionWarning.hidden = !job?.frame_selection_warning;
  selectionWarning.textContent = job?.frame_selection_warning || '';
  section.hidden = requests.length === 0;
  if (section.hidden) {
    state.storyboardKey = '';
    state.inspectorKey = '';
    return;
  }
  if (
    state.selectedShotId
    && !requests.some((request) => request.request_id === state.selectedShotId)
  ) {
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
  }

  const rows = requests.map((request, index) => {
    const isImage = request.visual_type === 'image';
    const versions = versionsForShot(job, request);
    const currentVersion = currentVersionForShot(job, request);
    const currentFrame = currentFrameCandidate(job, request)
      || frameCandidatesForShot(job, request.request_id)[0]
      || null;
    const currentAsset = isImage
      ? currentFrame?.asset || null
      : currentVersion?.asset || renderedVisualAsset(job, request);
    const latestVersion = versions.at(-1) || null;
    const latestIsCandidate = latestVersion
      && requestSignature(latestVersion.request) !== requestSignature(request);
    let status = isImage && currentAsset
      ? {label: '动态图片已就绪', className: 'succeeded'}
      : shotStatus(currentTaskForShot(job, request), !!currentAsset);
    if (latestIsCandidate && !latestVersion.asset) {
      status = ['failed', 'cancelled'].includes(latestVersion.task?.state)
        ? {label: '新版本失败，当前版保留', className: 'failed'}
        : {label: '正在生成新版本', className: 'running'};
    }
    return {request, index, isImage, versions, currentVersion, currentAsset, currentFrame, status};
  });
  const readyCount = rows.filter((row) => row.currentAsset).length;
  const frameReadyCount = rows.filter((row) => row.currentFrame).length;
  const videoRows = rows.filter((row) => !row.isImage);
  const readyVideos = videoRows.filter((row) => row.currentAsset).length;
  $('#storyboard-summary').textContent = readyCount === requests.length
    ? `${readyCount} 个章节已就绪 · 3 段视频 + 2 段动态图片`
    : frameReadyCount
      ? `${frameReadyCount}/${requests.length} 个首帧已就绪 · ${readyVideos}/${videoRows.length} 段视频可预览`
      : `${readyCount}/${requests.length} 个章节可预览`;

  const key = JSON.stringify({
    job: job.id,
    selected: state.selectedShotId,
    rows: rows.map((row) => ({
      request: requestSignature(row.request),
      task: currentTaskForShot(job, row.request)?.state || '',
      asset: row.currentAsset?.sha256 || '',
      frame: row.currentFrame?.asset?.sha256 || '',
      status: row.status.label,
      versions: row.versions.map((version) => [
        version.version_id,
        version.task?.state,
        version.asset?.sha256 || '',
      ]),
    })),
  });
  if (state.storyboardKey !== key) {
    state.storyboardKey = key;
    $('#shot-grid').innerHTML = rows.map((row) => {
      const mediaUrl = !row.isImage && row.currentAsset
        ? shotMediaUrl(job, row.request, row.currentVersion || {
          version_id: '', asset: row.currentAsset,
        })
        : '';
      const frameUrl = frameCandidateMediaUrl(job, row.currentFrame);
      const versionNumber = row.currentVersion?.version || 1;
      const selected = state.selectedShotId === row.request.request_id;
      const placeholderClass = row.status.className === 'failed'
        ? 'failed'
        : row.status.className === 'waiting' ? 'waiting' : '';
      return `<article class="shot-card ${selected ? 'selected' : ''}" data-shot-id="${escapeHtml(row.request.request_id)}" role="listitem" tabindex="0" aria-label="镜头 ${row.index + 1}，${escapeHtml(row.status.label)}">
        <div class="shot-preview">
          <span class="shot-number">${String(row.index + 1).padStart(2, '0')}</span>
          <span class="shot-version-badge">${row.isImage ? '动态图片' : `v${versionNumber}${row.versions.length > 1 ? ` · ${row.versions.length} 版` : ''}`}</span>
          ${mediaUrl
            ? `<video src="${mediaUrl}" muted loop playsinline preload="metadata" aria-label="镜头 ${row.index + 1} 预览"></video>`
            : frameUrl
              ? `<img class="shot-frame" src="${frameUrl}" alt="镜头 ${row.index + 1} 首帧预览">`
            : `<div class="shot-placeholder ${placeholderClass}">${escapeHtml(row.status.label)}</div>`}
        </div>
        <div class="shot-copy">
          <strong>镜头 ${row.index + 1} · ${row.isImage ? '图片' : '视频'}</strong>
          <p>${escapeHtml(shotDescription(job, row.request))}</p>
          <span class="shot-status ${escapeHtml(row.status.className)}">${escapeHtml(row.status.label)}</span>
        </div>
      </article>`;
    }).join('');
  }
  renderShotInspector(job);
}

function workflowCopy(job, current) {
  const task = taskForJob(job);
  if (
    task?.progress_meta?.workflow === 'shot_edit'
    && activeJobTaskRunning(job)
  ) {
    return {
      current: task.progress || '正在更新单个 AI 镜头',
      next: '完成后重新预览并确认成片',
    };
  }
  if (isNarrationRevisionFailure(job)) {
    const reuse = (job.visual_requests || []).length ? '，现有画面会直接复用' : '';
    return {current: '口播内容需要调整', next: `返回脚本调整后重新生成${reuse}`};
  }
  if (job.state === 'failed') {
    return {current: `${workflowStages[current] || '自动流程'}失败`, next: '检查错误后，从失败阶段重试'};
  }
  if (job.state === 'script_review_required') {
    return {current: '等待你检查并确认脚本', next: '确认后自动生成旁白、首帧、三段视频和两段动态图片'};
  }
  if (job.state === 'final_review_required') {
    return {current: '等待你预览并确认成片', next: '确认后自动生成可下载的发布包'};
  }
  if (job.state === 'packaged') {
    return {current: '发布包已完成', next: '下载产物并手动发布到抖音'};
  }
  const fallbackCurrent = [
    '人物观点已确认，正在准备脚本任务', '正在生成脚本', '等待你确认脚本', '正在生成旁白',
    '正在生成分镜与 5 张首帧', '正在生成 AI 视频 1/3', '正在生成 AI 视频 2/3',
    '正在生成 AI 视频 3/3', '正在混合图片、视频、旁白与字幕', '等待你确认成片', '正在生成发布包',
  ][current];
  const next = [
    '系统自动生成脚本', '你检查并确认脚本', '系统自动生成旁白', '生成分镜和首帧',
    '用首帧生成 AI 视频 1/3', '生成 AI 视频 2/3', '生成 AI 视频 3/3',
    'Remotion 混合图片与视频', '你预览并确认成片', '系统自动生成发布包', '下载发布包',
  ][current];
  return {current: task?.progress || fallbackCurrent, next};
}

function cleanScreenplayValue(value) {
  return String(value || '')
    .replace(/\*\*/g, '')
    .replace(/(^|\n)\s*---+\s*(?=\n|$)/g, '$1')
    .replace(/\s*---+\s*$/g, '')
    .trim();
}

function screenplayRole(header, index, total) {
  const value = String(header || '');
  if (/钩子/.test(value) || index === 0) return 'hook';
  if (/悬念/.test(value)) return 'suspense';
  if (/误解|反常识|重构/.test(value)) return 'reframe';
  if (/场景|例子/.test(value)) return 'example';
  if (/应用|怎么做|行动/.test(value)) return 'application';
  if (/收束|升华|结尾/.test(value) || index === total - 1) return 'closing';
  if (/背景|语境/.test(value)) return 'context';
  return 'explanation';
}

function parseLegacyScreenplay(value) {
  const text = String(value || '').replace(/\r/g, '').trim();
  if (!/(?:画面)[：:]/.test(text) || !/(?:旁白)[：:]/.test(text)) return [];
  let sections = text.split(/(?=###\s*)/g).filter((item) => /旁白[：:]/.test(item));
  if (sections.length < 2) {
    sections = text.split(/\s*---+\s*/g).filter((item) => /旁白[：:]/.test(item));
  }
  const parsed = sections.map((section) => {
    const tokenPattern = /\*{0,2}\s*(画面|旁白|屏幕大字|屏幕文字|结尾大字)\s*[：:]\s*\*{0,2}/g;
    const tokens = Array.from(section.matchAll(tokenPattern));
    if (!tokens.length) return null;
    const fields = {};
    tokens.forEach((token, index) => {
      const start = (token.index || 0) + token[0].length;
      const end = index + 1 < tokens.length ? (tokens[index + 1].index || section.length) : section.length;
      fields[token[1]] = cleanScreenplayValue(section.slice(start, end));
    });
    const header = cleanScreenplayValue(section.slice(0, tokens[0].index || 0).replace(/^#+\s*/, ''));
    return {
      header,
      narration: fields['旁白'] || '',
      visual_direction: fields['画面'] || '',
      on_screen_text: fields['屏幕大字'] || fields['屏幕文字'] || fields['结尾大字'] || '',
    };
  }).filter((item) => item?.narration);
  return parsed.map((item, index) => ({
    ...item,
    role: screenplayRole(item.header, index, parsed.length),
  }));
}

function scriptForEditor(job) {
  const original = structuredClone(job.script);
  if (Array.isArray(original.beats)) return original;
  const legacy = scriptBeats(original);
  const parsed = parseLegacyScreenplay(legacy.map((item) => item.narration).join('\n\n'));
  const allRefs = Array.from(new Set(legacy.flatMap((item) => item.source_refs || [])));
  const quotes = job.source_card_snapshot?.verified_quotes || [];
  const beats = (parsed.length >= 3 ? parsed : legacy).map((item, index, rows) => {
    const narration = item.narration.trim();
    const quote = quotes.find((candidate) => narration.includes(candidate.text));
    const sourceRefs = Array.from(new Set([
      ...(item.source_refs || allRefs),
      ...(quote ? [quote.id] : []),
    ]));
    return {
      id: `n${String(index + 1).padStart(2, '0')}`,
      role: item.role || screenplayRole('', index, rows.length),
      narration,
      visual_direction: item.visual_direction?.trim() || `用一个具体、自然且无文字的家庭场景表达这段含义：${narration.slice(0, 120)}`,
      on_screen_text: item.on_screen_text?.trim() || '',
      source_refs: sourceRefs,
      quote_ref: quote?.id || item.quote_ref || null,
    };
  });
  delete original.narration_segments;
  original.schema_version = '2.0';
  original.beats = beats;
  original.hook = beats[0]?.narration || original.hook;
  original.closing = beats.at(-1)?.narration || original.closing;
  return original;
}

const scriptRoleLabels = {
  hook: '钩子', suspense: '悬念', context: '铺垫', reframe: '反转',
  explanation: '展开', example: '场景', application: '应用', closing: '收束',
};

function resizeScriptTextarea(node) {
  node.style.height = 'auto';
  node.style.height = `${Math.max(node.dataset.scriptField === 'narration' ? 78 : 58, node.scrollHeight)}px`;
}

function renderScriptDocument(job) {
  const script = scriptForEditor(job);
  state.scriptEditorDraft = script;
  $('#script-video-title').value = script.video_title || '';
  $('#script-beat-editor').innerHTML = script.beats.map((beat, index) => `
    <article class="script-beat" data-beat-index="${index}">
      <div class="script-beat-marker"><span>${String(index + 1).padStart(2, '0')}</span><small>${escapeHtml(scriptRoleLabels[beat.role] || '展开')}</small></div>
      <div class="script-beat-tracks">
        <label class="script-track visual-track"><span>画面</span><textarea data-script-field="visual_direction" maxlength="1200" spellcheck="false">${escapeHtml(beat.visual_direction)}</textarea></label>
        <label class="script-track narration-track"><span>旁白</span><textarea data-script-field="narration" maxlength="2000" spellcheck="false">${escapeHtml(beat.narration)}</textarea></label>
        <label class="script-track screen-track"><span>屏幕文字 <em>可留空</em></span><textarea data-script-field="on_screen_text" maxlength="80" spellcheck="false" placeholder="只保留真正值得强调的一句话">${escapeHtml(beat.on_screen_text)}</textarea></label>
      </div>
    </article>`).join('');
  document.querySelectorAll('#script-beat-editor textarea').forEach(resizeScriptTextarea);
  updateScriptLengthStatus();
}

function focusFirstNarration() {
  document.querySelector('[data-script-field="narration"]')?.focus();
}

function renderDetail() {
  const job = state.selectedJob;
  const detail = $('#job-detail');
  detail.hidden = !job;
  if (!job) return;
  $('#job-title').textContent = job.source_card_snapshot?.title || '视频详情';
  $('#job-state').textContent = stateLabels[job.state] || job.state;
  const hasReferenceImage = (job.source_card_snapshot?.reference_assets || []).length > 0;
  const referenceNode = $('#job-reference-image');
  const referencePreview = $('#job-reference-preview');
  referenceNode.hidden = !hasReferenceImage;
  if (hasReferenceImage && referencePreview.dataset.jobId !== job.id) {
    referencePreview.dataset.jobId = job.id;
    referencePreview.src = `${API}/jobs/${encodeURIComponent(job.id)}/reference-image`;
  } else if (!hasReferenceImage) {
    referencePreview.dataset.jobId = '';
    referencePreview.removeAttribute('src');
  }
  const current = stageIndex(job);
  $('#stage-track').innerHTML = workflowStages.map((label, index) => {
    const className = current > index || job.state === 'packaged' ? 'done' : current === index ? 'active' : '';
    const currentAttribute = className === 'active' ? ' aria-current="step"' : '';
    return `<li class="${className}"${currentAttribute}>${label}</li>`;
  }).join('');
  const task = taskForJob(job);
  const copy = workflowCopy(job, current);
  $('#current-action').textContent = copy.current;
  $('#stage-elapsed').textContent = elapsedText(task);
  $('#next-action').textContent = copy.next;
  renderSeedanceUsage(job);
  renderShotStoryboard(job);
  const error = $('#job-error');
  error.hidden = !job.error;
  error.textContent = job.error || '任务失败';
  const narrationFailure = isNarrationRevisionFailure(job);
  const reviseButton = $('#revise-script-button');
  reviseButton.hidden = !narrationFailure;
  reviseButton.textContent = (job.visual_requests || []).length
    ? '返回修改脚本（复用现有画面）'
    : '返回修改脚本';
  $('#retry-button').hidden = job.state !== 'failed' || narrationFailure;

  const scriptSection = $('#script-review');
  scriptSection.hidden = job.state !== 'script_review_required';
  if (!scriptSection.hidden && job.script) {
    renderScriptDocument(job);
    $('#job-seedance-prompt').value = job.generation_settings?.seedance_prompt
      || generationDefaults().seedance_prompt
      || '';
  }

  const producing = ['script_generating', 'script_approved', 'producing', 'quality_checking', 'final_approved'].includes(job.state);
  $('#production-progress').hidden = !producing;
  if (producing) {
    const percent = Number(task?.progress_meta?.percent ?? [8, 14, 28, 34, 48, 56, 60, 64, 68, 74, 82, 90, 96][current] ?? 3);
    const boundedPercent = Math.max(3, Math.min(100, percent));
    $('#task-progress').textContent = task?.progress || copy.current || '后台任务运行中…';
    $('#task-progress-bar').style.width = `${boundedPercent}%`;
    $('#task-progress-meter').setAttribute('aria-valuenow', String(Math.round(boundedPercent)));
  }
  const shotEditing = task?.progress_meta?.workflow === 'shot_edit'
    && activeJobTaskRunning(job);
  const finalSection = $('#final-review');
  const hasDraft = (job.artifacts || []).some((item) => item.name === 'draft.mp4');
  finalSection.hidden = job.state !== 'final_review_required' && !(shotEditing && hasDraft);
  if (!finalSection.hidden) {
    const draft = (job.artifacts || []).find((item) => item.name === 'draft.mp4');
    const cover = (job.artifacts || []).find((item) => item.name === 'cover.jpg');
    $('#draft-video').src = draft ? `${API}/jobs/${encodeURIComponent(job.id)}/artifacts/draft.mp4` : '';
    $('#draft-cover').src = cover ? `${API}/jobs/${encodeURIComponent(job.id)}/artifacts/cover.jpg` : '';
    const checks = job.quality_report?.checks || [];
    $('#quality-summary').innerHTML = checks.map((check) => `<div class="quality-item ${check.passed ? 'pass' : 'fail'}">${check.passed ? '通过' : '失败'} · ${escapeHtml(check.id)}</div>`).join('');
    $('#approve-final-button').disabled = state.busy || shotEditing || job.state !== 'final_review_required';
    $('#approve-final-button').textContent = shotEditing ? '镜头更新后再确认' : '确认成片包并生成发布版本';
  }
  const artifacts = job.artifacts || [];
  const packaged = job.state === 'packaged';
  const artifactSection = $('#artifact-section');
  artifactSection.hidden = !packaged;
  if (packaged) {
    const finalVideo = artifacts.find((artifact) => artifact.name === 'final.mp4');
    const finalLink = $('#download-final-video');
    finalLink.hidden = !finalVideo;
    finalLink.href = finalVideo
      ? `${API}/jobs/${encodeURIComponent(job.id)}/artifacts/final.mp4?download=1`
      : '#';
    $('#download-release-package').href = `${API}/jobs/${encodeURIComponent(job.id)}/release-package.zip`;
    const technicalArtifacts = artifacts.filter((artifact) => artifact.name !== 'final.mp4');
    $('#artifact-list').innerHTML = technicalArtifacts.map((artifact) => `<a href="${API}/jobs/${encodeURIComponent(job.id)}/artifacts/${encodeURIComponent(artifact.name)}?download=1">${escapeHtml(artifact.name)}</a>`).join('');
  } else {
    $('#artifact-list').innerHTML = '';
  }
}

async function loadAll({selectJobId = ''} = {}) {
  const [cards, jobs] = await Promise.all([api('GET', '/source-cards'), api('GET', '/jobs')]);
  state.cards = cards;
  state.jobs = jobs;
  const previousJobId = state.selectedJob?.id || '';
  const targetId = selectJobId || state.selectedJob?.id;
  state.selectedJob = targetId ? jobs.find((item) => item.id === targetId) || null : jobs[0] || null;
  if (previousJobId !== (state.selectedJob?.id || '')) {
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
  }
  if (state.activeTask?.task_id !== state.selectedJob?.last_run_task_id) state.activeTask = null;
  renderCards(); renderJobs(); renderDetail();
}

function updateVisibleTopicRun(run) {
  const index = state.topicRuns.findIndex((item) => item.id === run.id);
  if (index >= 0) state.topicRuns[index] = run;
  else state.topicRuns.unshift(run);
  if (state.selectedTopicRun?.id === run.id) state.selectedTopicRun = run;
  renderTopicRuns();
  renderTopicDetail();
}

async function loadTopicRuns({selectRunId = ''} = {}) {
  const runs = await api('GET', '/topic-research/runs');
  state.topicRuns = runs;
  const targetId = selectRunId || state.selectedTopicRun?.id || runs[0]?.id || '';
  state.selectedTopicRun = runs.find((item) => item.id === targetId) || runs[0] || null;
  if (state.activeTopicTask?.task_id !== state.selectedTopicRun?.last_run_task_id) {
    state.activeTopicTask = null;
  }
  renderTopicRuns();
  renderTopicDetail();
}

function stopTopicPolling() {
  state.topicPollGeneration += 1;
  state.topicPollingTaskId = '';
  state.topicPollPromise = null;
}

async function pollTopicTask(taskId, runId) {
  if (state.topicPollingTaskId === taskId && state.topicPollPromise) return state.topicPollPromise;
  const generation = state.topicPollGeneration + 1;
  state.topicPollGeneration = generation;
  state.topicPollingTaskId = taskId;
  const promise = (async () => {
    for (let attempt = 0; attempt < 900; attempt += 1) {
      const task = await fetchTask(taskId);
      if (generation !== state.topicPollGeneration) return null;
      state.activeTopicTask = task;
      const run = await api('GET', `/topic-research/runs/${encodeURIComponent(runId)}`);
      if (generation !== state.topicPollGeneration) return null;
      updateVisibleTopicRun(run);
      if (task.status === 'done' || task.status === 'failed') {
        const selectedRunId = state.selectedTopicRun?.id || runId;
        await loadTopicRuns({selectRunId: selectedRunId});
        state.activeTopicTask = task;
        renderTopicDetail();
        if (task.status === 'failed') throw new Error(task.error || '选题研究失败');
        return task;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    throw new Error('选题研究等待超时');
  })();
  state.topicPollPromise = promise;
  try {
    return await promise;
  } finally {
    if (generation === state.topicPollGeneration) {
      state.topicPollingTaskId = '';
      state.topicPollPromise = null;
    }
  }
}

async function resumeTopicTask(run = state.selectedTopicRun) {
  if (!run?.last_run_task_id || run.status !== 'running') return;
  if (state.topicPollingTaskId && state.topicPollingTaskId !== run.last_run_task_id) stopTopicPolling();
  const task = await fetchTask(run.last_run_task_id);
  state.activeTopicTask = task;
  if (state.selectedTopicRun?.id === run.id) renderTopicDetail();
  if (!['done', 'failed'].includes(task.status)) {
    return pollTopicTask(task.task_id, run.id);
  }
  await loadTopicRuns({selectRunId: run.id});
}

async function fetchTask(taskId) {
  const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}`, {credentials: 'same-origin'});
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok || payload.code !== 0) throw new Error(payload.message || payload.detail || '后台任务查询失败');
  return payload.data;
}

function updateVisibleJob(job) {
  const index = state.jobs.findIndex((item) => item.id === job.id);
  if (index >= 0) state.jobs[index] = job;
  if (state.selectedJob?.id === job.id) state.selectedJob = job;
  renderJobs(); renderDetail();
}

function stopPolling() {
  state.pollGeneration += 1;
  state.pollingTaskId = '';
  state.pollPromise = null;
}

async function pollTask(taskId, jobId) {
  if (state.pollingTaskId === taskId && state.pollPromise) return state.pollPromise;
  const generation = state.pollGeneration + 1;
  state.pollGeneration = generation;
  state.pollingTaskId = taskId;
  const promise = (async () => {
    for (let attempt = 0; attempt < 3600; attempt += 1) {
      const task = await fetchTask(taskId);
      if (generation !== state.pollGeneration) return null;
      if (state.selectedJob?.id === jobId) state.activeTask = task;

      const latestJob = await api('GET', `/jobs/${encodeURIComponent(jobId)}`);
      if (generation !== state.pollGeneration) return null;
      updateVisibleJob(latestJob);

      if (task.status === 'done' || task.status === 'failed') {
        await loadAll({selectJobId: jobId});
        state.activeTask = task;
        renderDetail();
        if (task.status === 'failed') throw new Error(task.error || '后台任务失败');
        return task;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    throw new Error('后台任务等待超时');
  })();
  state.pollPromise = promise;
  try {
    return await promise;
  } finally {
    if (generation === state.pollGeneration) {
      state.pollingTaskId = '';
      state.pollPromise = null;
    }
  }
}

async function resumeSelectedTask() {
  const job = state.selectedJob;
  if (!job?.last_run_task_id) return;
  if (state.pollingTaskId && state.pollingTaskId !== job.last_run_task_id) stopPolling();
  const task = await fetchTask(job.last_run_task_id);
  if (state.selectedJob?.id !== job.id) return;
  state.activeTask = task;
  renderDetail();
  if (!['done', 'failed'].includes(task.status)) {
    return pollTask(task.task_id, job.id);
  }
}

function openTopicHandoff(candidate) {
  state.topicHandoffCandidate = candidate;
  const form = $('#topic-source-form');
  form.reset();
  $('#topic-source-title').value = candidate.title || '';
  $('#topic-handoff-title').textContent = candidate.title || '';
  $('#topic-handoff-angle').textContent = candidate.editorial_angle || '';
  $('#topic-handoff').hidden = false;
  $('#manual-intake-intro').hidden = true;
  $('#source-card-form').hidden = true;
  switchWorkspace('production');
  $('#topic-handoff').scrollIntoView({behavior: 'smooth', block: 'start'});
  form.elements.source_material.focus();
}

function closeTopicHandoff() {
  state.topicHandoffCandidate = null;
  $('#topic-source-form').reset();
  $('#topic-handoff').hidden = true;
  $('#manual-intake-intro').hidden = false;
  $('#source-card-form').hidden = false;
}

document.querySelector('.workspace-tabs').addEventListener('click', (event) => {
  const button = event.target.closest('[data-workspace-tab]');
  if (!button || state.busy) return;
  switchWorkspace(button.dataset.workspaceTab);
});
document.querySelector('.workspace-tabs').addEventListener('keydown', (event) => {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  event.preventDefault();
  const tab = state.workspaceTab === 'topics' ? 'production' : 'topics';
  switchWorkspace(tab);
  document.querySelector(`[data-workspace-tab="${tab}"]`)?.focus();
});

$('#topic-start-button').addEventListener('click', async () => {
  notify(''); setBusy(true);
  try {
    const result = await api('POST', '/topic-research/runs', {confirm_cost: true});
    updateVisibleTopicRun(result.run);
    state.selectedTopicRun = result.run;
    renderTopicRuns();
    renderTopicDetail();
    setBusy(false);
    await pollTopicTask(result.task_id, result.run.id);
    notify('本轮已形成 5 个候选，请选择最值得继续验证的方向。');
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#topic-run-list').addEventListener('click', (event) => {
  const button = event.target.closest('[data-topic-run-id]');
  if (!button) return;
  const run = state.topicRuns.find((item) => item.id === button.dataset.topicRunId);
  if (!run) return;
  state.selectedTopicRun = run;
  if (state.activeTopicTask?.task_id !== run.last_run_task_id) state.activeTopicTask = null;
  renderTopicRuns();
  renderTopicDetail();
  resumeTopicTask(run).catch((error) => notify(error.message, true));
});

$('#topic-candidate-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-adopt-topic]');
  const run = state.selectedTopicRun;
  if (!button || !run || state.busy) return;
  const candidate = (run.candidates || []).find((item) => item.id === button.dataset.adoptTopic);
  if (!candidate) return;
  if (run.selected_candidate_id === candidate.id) {
    openTopicHandoff(candidate);
    return;
  }
  notify(''); setBusy(true);
  try {
    const updated = await api('POST', `/topic-research/runs/${encodeURIComponent(run.id)}/actions/select`, {
      candidate_id: candidate.id,
      expected_revision: run.revision,
    });
    updateVisibleTopicRun(updated);
    openTopicHandoff(candidate);
    notify('选题已采用。请补充独立可靠资料，再进入脚本生成。');
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#cancel-topic-handoff').addEventListener('click', () => {
  if (!state.busy) closeTopicHandoff();
});

$('#topic-source-form').addEventListener('submit', async (event) => {
  event.preventDefault(); notify(''); setBusy(true);
  const form = new FormData(event.currentTarget);
  try {
    if (form.get('rights_confirmed') !== 'on') throw new Error('请先确认资料已经核对且可以引用');
    const generationSettings = generationSettingsPayload();
    const card = await api('POST', '/source-cards/quick', {
      schema_version: '1.0',
      title: String(form.get('title') || '').trim(),
      source_material: String(form.get('source_material') || '').trim(),
      rights_confirmed: true,
      editorial_brief: state.topicHandoffCandidate?.editorial_angle || '',
      parent_question: state.topicHandoffCandidate?.parent_question || '',
      content_domain: 'parent_education',
      content_format: 'concept_explainer',
      source_type: form.get('source_url') ? 'article' : 'other',
      source_url: String(form.get('source_url') || '').trim(),
      boundary: [
        '抖音趋势仅作为选题线索；脚本只可使用本来源卡中的已核对资料，不得把播放量、平台标签或视频标题表述为家庭教育事实。',
        state.topicHandoffCandidate?.risk_note || '',
      ].filter(Boolean).join(' '),
    });
    let result;
    try {
      result = await api('POST', '/jobs', {
        source_card_id: card.id,
        generation_settings: generationSettings,
      });
    } catch (error) {
      await loadAll();
      closeTopicHandoff();
      throw new Error(`资料已保存，但视频任务未启动：${error.message}`);
    }
    closeTopicHandoff();
    await loadAll({selectJobId: result.job.id});
    setBusy(false);
    await pollTask(result.task_id, result.job.id);
    notify('脚本已生成，请人工确认。');
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#source-card-form').addEventListener('submit', async (event) => {
  event.preventDefault(); notify(''); setBusy(true);
  const form = new FormData(event.currentTarget);
  const body = {
    schema_version: '1.0',
    person_name: form.get('person_name'),
    viewpoint: form.get('viewpoint'),
  };
  try {
    const generationSettings = generationSettingsPayload();
    let card;
    if (state.referenceImageFile) {
      const upload = new FormData();
      upload.append('person_name', String(body.person_name || ''));
      upload.append('viewpoint', String(body.viewpoint || ''));
      upload.append(
        'reference_image',
        state.referenceImageFile,
        state.referenceImageFile.name || 'reference-image',
      );
      card = await apiMultipart('/source-cards/idea-with-reference', upload);
    } else {
      card = await api('POST', '/source-cards/idea', body);
    }
    resetSourceForm();
    let result;
    try {
      result = await api('POST', '/jobs', {source_card_id: card.id, generation_settings: generationSettings});
    } catch (error) {
      await loadAll();
      throw new Error(`观点已保存，但视频任务未启动：${error.message}`);
    }
    await loadAll({selectJobId: result.job.id});
    setBusy(false);
    await pollTask(result.task_id, result.job.id);
    notify('脚本已生成，请人工确认。');
    return;
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#source-card-list').addEventListener('click', async (event) => {
  const create = event.target.closest('[data-create-job]');
  if (!create) return;
  notify(''); setBusy(true);
  try {
    const result = await api('POST', '/jobs', {
      source_card_id: create.dataset.createJob,
      generation_settings: generationSettingsPayload(),
    });
    await loadAll({selectJobId: result.job.id});
    setBusy(false); await pollTask(result.task_id, result.job.id); notify('脚本已生成，请人工确认。'); return;
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

function resetSourceForm() {
  $('#source-card-form').reset();
  clearReferenceImage();
  $('#source-submit-button').textContent = '生成脚本';
}

$('#job-list').addEventListener('click', async (event) => {
  const card = event.target.closest('[data-job-id]');
  if (!card) return;
  try {
    if (state.selectedJob?.id !== card.dataset.jobId) stopPolling();
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    state.selectedJob = await api('GET', `/jobs/${encodeURIComponent(card.dataset.jobId)}`);
    state.activeTask = null;
    renderJobs(); renderDetail();
    resumeSelectedTask().catch((error) => notify(error.message, true));
  }
  catch (error) { notify(error.message, true); }
});

function editedScript(job) {
  const script = structuredClone(state.scriptEditorDraft || scriptForEditor(job));
  const rows = Array.from(document.querySelectorAll('#script-beat-editor .script-beat'));
  if (rows.length < 5) throw new Error('完整脚本至少需要五个自然叙事段');
  rows.forEach((row, index) => {
    const beat = script.beats[index];
    ['visual_direction', 'narration', 'on_screen_text'].forEach((field) => {
      beat[field] = row.querySelector(`[data-script-field="${field}"]`).value.trim();
    });
    if (!beat.narration) throw new Error(`第 ${index + 1} 段旁白不能为空`);
    if (!beat.visual_direction) throw new Error(`第 ${index + 1} 段画面不能为空`);
  });
  script.video_title = $('#script-video-title').value.trim();
  if (!script.video_title) throw new Error('视频标题不能为空');
  script.hook = script.beats[0].narration;
  script.closing = script.beats[script.beats.length - 1].narration;
  script.estimated_duration_seconds = Math.max(
    45,
    Math.min(75, Math.round(narrationCharacterCount(script.beats.map((item) => item.narration).join('')) / 4.1)),
  );
  return script;
}

async function saveScriptEdits(job) {
  const script = editedScript(job);
  const characterCount = narrationCharacterCount(script.beats.map((item) => item.narration).join(''));
  if (characterCount > SCRIPT_HARD_MAX_CHARS) {
    throw new Error(`纯旁白共 ${characterCount} 字，超过技术安全上限 ${SCRIPT_HARD_MAX_CHARS} 字`);
  }
  const seedancePrompt = $('#job-seedance-prompt').value.trim();
  if (!seedancePrompt) throw new Error('全片画面导演设定不能为空');
  const scriptChanged = JSON.stringify(script) !== JSON.stringify(job.script);
  const promptChanged = seedancePrompt !== (
    job.generation_settings?.seedance_prompt || generationDefaults().seedance_prompt
  );
  if (!scriptChanged && !promptChanged) return {job};
  const updated = await api('PUT', `/jobs/${encodeURIComponent(job.id)}/script`, {
    expected_revision: job.revision,
    script,
    seedance_prompt: seedancePrompt,
  });
  return {job: updated};
}

$('#save-script-button').addEventListener('click', async () => {
  const job = state.selectedJob; if (!job?.script) return;
  setBusy(true); notify('');
  try {
    const saved = await saveScriptEdits(job);
    state.selectedJob = saved.job;
    await loadAll({selectJobId: job.id});
    notify('完整脚本和画面导演设定已保存。');
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#approve-script-button').addEventListener('click', async () => {
  const job = state.selectedJob; if (!job?.script_hash) return;
  setBusy(true); notify('');
  try {
    const saved = await saveScriptEdits(job);
    const latestJob = saved.job;
    const result = await api('POST', `/jobs/${encodeURIComponent(latestJob.id)}/actions/approve-script`, {expected_revision: latestJob.revision, script_hash: latestJob.script_hash});
    await loadAll({selectJobId: job.id}); setBusy(false); await pollTask(result.task_id, job.id); notify('真实成片已生成，请检查并确认。'); return;
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#script-beat-editor').addEventListener('input', (event) => {
  if (event.target.matches('textarea')) resizeScriptTextarea(event.target);
  if (event.target.dataset.scriptField === 'narration') updateScriptLengthStatus();
});

$('#shot-grid').addEventListener('click', (event) => {
  const card = event.target.closest('[data-shot-id]');
  if (!card || !state.selectedJob) return;
  state.selectedShotId = card.dataset.shotId;
  state.previewVersionId = '';
  const request = storyboardRequests(state.selectedJob).find(
    (item) => item.request_id === state.selectedShotId,
  );
  state.previewFrameCandidateId = request
    ? currentFrameCandidate(state.selectedJob, request)?.candidate_id || ''
    : '';
  state.storyboardKey = '';
  state.inspectorKey = '';
  renderShotStoryboard(state.selectedJob);
});

$('#shot-grid').addEventListener('keydown', (event) => {
  if (!['Enter', ' '].includes(event.key)) return;
  const card = event.target.closest('[data-shot-id]');
  if (!card) return;
  event.preventDefault();
  card.click();
});

$('#shot-grid').addEventListener('mouseover', (event) => {
  const video = event.target.closest('.shot-preview video');
  if (video) video.play().catch(() => {});
});

$('#shot-grid').addEventListener('mouseout', (event) => {
  const video = event.target.closest('.shot-preview video');
  if (video && !video.contains(event.relatedTarget)) {
    video.pause();
    video.currentTime = 0;
  }
});

$('#shot-inspector').addEventListener('click', async (event) => {
  const job = state.selectedJob;
  if (!job) return;
  if (event.target.closest('[data-close-inspector]')) {
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderShotStoryboard(job);
    return;
  }
  const preview = event.target.closest('[data-preview-version]');
  if (preview) {
    state.previewVersionId = preview.dataset.previewVersion;
    state.previewFrameCandidateId = '';
    state.inspectorKey = '';
    renderShotInspector(job);
    return;
  }
  const frameCandidate = event.target.closest('[data-frame-candidate]');
  if (frameCandidate) {
    state.previewFrameCandidateId = frameCandidate.dataset.frameCandidate;
    state.inspectorKey = '';
    renderShotInspector(job);
    return;
  }
  const select = event.target.closest('[data-select-version]');
  if (select) {
    setBusy(true); notify('');
    try {
      const result = await api(
        'POST',
        `/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(state.selectedShotId)}/versions/${encodeURIComponent(select.dataset.selectVersion)}/actions/select`,
        {expected_revision: job.revision},
      );
      await loadAll({selectJobId: job.id});
      setBusy(false);
      await pollTask(result.task_id, job.id);
      state.previewVersionId = select.dataset.selectVersion;
      state.previewFrameCandidateId = '';
      state.storyboardKey = '';
      state.inspectorKey = '';
      renderDetail();
      notify('镜头版本已切换，成片也已同步更新。');
      return;
    } catch (error) { notify(error.message, true); }
    finally { setBusy(false); }
    return;
  }
  if (!event.target.closest('[data-regenerate-shot]')) return;
  const prompt = $('#shot-prompt-editor')?.value.trim() || '';
  if (!prompt) { notify('镜头提示词不能为空。', true); return; }
  const confirmed = window.confirm(
    '这会新增 1 次真实的 Seedance 2.0 镜头生成费用。首帧、旁白、图片章节和其他视频镜头不会重做，是否继续？',
  );
  if (!confirmed) return;
  setBusy(true); notify('');
  try {
    const result = await api(
      'POST',
      `/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(state.selectedShotId)}/actions/regenerate`,
      {
        expected_revision: job.revision,
        prompt,
        first_frame_candidate_id: state.previewFrameCandidateId || '',
      },
    );
    await loadAll({selectJobId: job.id});
    setBusy(false);
    await pollTask(result.task_id, job.id);
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderDetail();
    notify('这个镜头的新版本已生成，并已更新到成片。');
    return;
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#approve-final-button').addEventListener('click', async () => {
  const job = state.selectedJob; if (!job?.review_bundle_hash) return;
  setBusy(true); notify('');
  try {
    const result = await api('POST', `/jobs/${encodeURIComponent(job.id)}/actions/approve-final`, {expected_revision: job.revision, review_bundle_hash: job.review_bundle_hash});
    await loadAll({selectJobId: job.id});
    setBusy(false);
    await pollTask(result.task_id, job.id);
    notify('成片、封面、字幕与来源已共同确认，发布包已生成。');
    $('#artifact-section').scrollIntoView({behavior: 'smooth', block: 'start'});
    return;
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#retry-button').addEventListener('click', async () => {
  const job = state.selectedJob; if (!job) return;
  setBusy(true); notify('');
  try {
    const result = await api('POST', `/jobs/${encodeURIComponent(job.id)}/actions/retry`, {});
    await loadAll({selectJobId: job.id});
    if (result.requires_review) {
      notify('已返回脚本修改，请调整后重新确认。');
      focusFirstNarration();
      return;
    }
    setBusy(false);
    await pollTask(result.task_id, job.id);
    notify('失败阶段已重试完成。');
    return;
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#revise-script-button').addEventListener('click', async () => {
  const job = state.selectedJob; if (!job) return;
  const willReuseVisuals = (job.visual_requests || []).length > 0;
  setBusy(true); notify('');
  try {
    await api('POST', `/jobs/${encodeURIComponent(job.id)}/actions/revise-script`, {expected_revision: job.revision});
    await loadAll({selectJobId: job.id});
    notify(willReuseVisuals
      ? '已返回脚本修改。确认后只重做旁白和合成，现有画面直接复用。'
      : '已返回脚本修改，请调整后重新确认。');
    focusFirstNarration();
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#refresh-button').addEventListener('click', async () => {
  try {
    await loadAll();
    await resumeSelectedTask();
  } catch (error) { notify(error.message, true); }
});
$('#script-generation-prompt').addEventListener('change', persistPromptFields);
$('#seedance-generation-prompt').addEventListener('change', persistPromptFields);
$('#restore-prompt-defaults').addEventListener('click', () => {
  setPromptFields(generationDefaults());
  persistPromptFields();
  notify('已恢复默认提示词。');
});
$('#reference-image-input').addEventListener('change', (event) => {
  setReferenceImage(event.target.files?.[0]);
});
$('#reference-dropzone').addEventListener('click', () => {
  if (!state.busy) $('#reference-image-input').click();
});
$('#reference-dropzone').addEventListener('keydown', (event) => {
  if (!state.busy && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault();
    $('#reference-image-input').click();
  }
});
$('#reference-dropzone').addEventListener('dragover', (event) => {
  event.preventDefault();
  if (!state.busy) event.currentTarget.classList.add('dragging');
});
$('#reference-dropzone').addEventListener('dragleave', (event) => {
  event.currentTarget.classList.remove('dragging');
});
$('#reference-dropzone').addEventListener('drop', (event) => {
  event.preventDefault();
  event.currentTarget.classList.remove('dragging');
  if (!state.busy) setReferenceImage(event.dataTransfer?.files?.[0]);
});
$('#remove-reference-image').addEventListener('click', () => {
  if (!state.busy) clearReferenceImage();
});
$('#new-card-button').addEventListener('click', () => {
  closeTopicHandoff();
  resetSourceForm();
  $('#source-card-form').scrollIntoView({behavior: 'smooth'});
  $('#source-card-form').elements.person_name.focus();
});

async function init() {
  try {
    switchWorkspace('topics');
    state.capabilities = await api('GET', '/capabilities');
    renderCapabilities();
    initializePromptFields();
    await Promise.all([loadAll(), loadTopicRuns()]);
    resumeSelectedTask().catch((error) => notify(error.message, true));
    const runningTopic = state.topicRuns.find((run) => run.status === 'running');
    if (runningTopic) resumeTopicTask(runningTopic).catch((error) => notify(error.message, true));
  }
  catch (error) { notify(error.message, true); $('#system-status').classList.add('warning'); }
}

init();
