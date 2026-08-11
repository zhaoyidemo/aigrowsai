const API = '/api/qijia-video';
const PROMPT_STORAGE_KEY = 'qijia-video-generation-settings-v2';
const LEGACY_PROMPT_STORAGE_KEY = 'qijia-video-generation-settings-v1';
const DEFAULT_CONTENT_SKILL_ID = 'explain-expert-view';
const NEWS_CONTENT_SKILL_ID = 'brief-recent-news';
const DEFAULT_VISUAL_STYLE_ID = 'content-skill-default';
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
  previewUploadId: '',
  previewFrameCandidateId: '',
  storyboardKey: '',
  inspectorKey: '',
  referenceImageFile: null,
  referenceImagePreviewUrl: '',
  scriptEditorDraft: null,
  workspaceTab: 'topics',
  productionPane: 'create',
  topicRuns: [],
  selectedTopicRun: null,
  activeTopicTask: null,
  topicPollingTaskId: '',
  topicPollPromise: null,
  topicPollGeneration: 0,
  topicHandoffCandidate: null,
  douyinRefreshFeedback: null,
  ttsPreviewKey: '',
  ttsPreviewUrl: '',
  ttsPreviewCache: new Map(),
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[char]));

function canEditResource(resource) {
  return resource?.can_edit !== false;
}

function formatCount(value) {
  const count = Number(value || 0);
  if (!Number.isFinite(count) || count <= 0) return '0';
  if (count >= 100000000) return `${(count / 100000000).toFixed(count >= 1000000000 ? 0 : 1)}亿`;
  if (count >= 10000) return `${(count / 10000).toFixed(count >= 100000 ? 0 : 1)}万`;
  return new Intl.NumberFormat('zh-CN').format(Math.round(count));
}

function usdToCnyRate() {
  const configured = Number(state.capabilities?.topic_research?.usd_to_cny_rate);
  return Number.isFinite(configured) && configured > 0 ? configured : 6.7;
}

function formatCnyFromUsd(value, fallback = '金额待账单') {
  if (value === null || value === undefined || value === '') return fallback;
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) return fallback;
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency', currency: 'CNY',
    minimumFractionDigits: 2, maximumFractionDigits: 4,
  }).format(amount * usdToCnyRate());
}

function formatCny(value, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback;
  const amount = Number(value);
  if (!Number.isFinite(amount) || amount < 0) return fallback;
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency', currency: 'CNY',
    minimumFractionDigits: 2, maximumFractionDigits: 4,
  }).format(amount);
}

function formatInteger(value, fallback = '—') {
  if (value === null || value === undefined || value === '') return fallback;
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return fallback;
  return new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 0}).format(number);
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

function switchProductionPane(pane) {
  state.productionPane = pane === 'jobs' ? 'jobs' : 'create';
  document.querySelectorAll('[data-production-pane]').forEach((button) => {
    const active = button.dataset.productionPane === state.productionPane;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll('[data-production-column]').forEach((column) => {
    column.classList.toggle(
      'mobile-pane-hidden',
      column.dataset.productionColumn !== state.productionPane,
    );
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
  const speedRatio = selectedJobTtsSpeedRatio();
  const [targetMinChars, targetMaxChars] = scriptTargetRange(speedRatio);
  const estimatedSeconds = Math.max(1, Math.round(count / (4.1 * speedRatio)));
  if (!count) {
    status.textContent = '只统计会被念出的旁白';
  } else if (count > SCRIPT_HARD_MAX_CHARS) {
    status.textContent = `旁白 ${count} 字 · ${speedRatio.toFixed(1)}x 预计约 ${estimatedSeconds} 秒 · 已超过技术安全上限`;
  } else if (count >= targetMinChars && count <= targetMaxChars) {
    status.textContent = `旁白 ${count} 字 · ${speedRatio.toFixed(1)}x 预计约 ${estimatedSeconds} 秒 · 节奏合适`;
  } else {
    status.textContent = `旁白 ${count} 字 · ${speedRatio.toFixed(1)}x 预计约 ${estimatedSeconds} 秒 · ${targetMinChars}-${targetMaxChars} 字仅作建议`;
  }
  const overlong = count > SCRIPT_HARD_MAX_CHARS;
  status.classList.toggle('warning', overlong);
  const approveButton = $('#approve-script-button');
  if (approveButton) {
    approveButton.disabled = state.busy || !canEditResource(state.selectedJob);
  }
  if (!$('#script-review')?.hidden && state.selectedJob) {
    renderScriptCostEstimate(state.selectedJob);
  }
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
    script_prompt: '', video_resolution: '1080p',
    seedance_model: 'doubao-seedance-1-0-pro-fast-251015',
    visual_style_id: DEFAULT_VISUAL_STYLE_ID,
    image_count: 10, shot_count: 13,
    tts_voice_id: 'zh_female_vv_uranus_bigtts', tts_speed_ratio: 1.2,
  };
}

function updateScriptApprovalAction() {
  const choice = $('#prepare-media-first');
  const button = $('#approve-script-button');
  if (!choice || !button) return;
  button.textContent = choice.checked
    ? '确认脚本并先安排自有素材'
    : '确认脚本并生成';
}

function isNewsResearchFailure(job) {
  return job?.state === 'failed'
    && job?.failed_stage === 'script'
    && job?.skill_snapshot?.research_mode === 'recent_news_required'
    && !job?.research_brief;
}

function contentSkills() {
  return Array.isArray(state.capabilities?.content_skills)
    ? state.capabilities.content_skills
    : [];
}

function contentSkill(skillId = '') {
  const skills = contentSkills();
  const requested = String(skillId || '').trim();
  return skills.find((item) => item.skill_id === requested)
    || skills.find((item) => item.skill_id === DEFAULT_CONTENT_SKILL_ID)
    || skills[0]
    || null;
}

function selectedContentSkill() {
  return contentSkill($('#content-skill')?.value || '');
}

function skillGenerationDefaults(skillId = '') {
  return {
    ...generationDefaults(),
    ...(contentSkill(skillId)?.generation_defaults || {}),
  };
}

function skillIdForCard(card) {
  return contentSkills().find(
    (item) => (item.compatible_formats || []).includes(card?.content_format),
  )?.skill_id || DEFAULT_CONTENT_SKILL_ID;
}

function updateContentSkillIntake() {
  const skill = selectedContentSkill();
  const isNews = skill?.input_mode === 'recent_news_topic';
  const handoffOpen = !$('#topic-handoff').hidden;
  $('#content-skill-description').textContent = skill
    ? `${skill.description} · v${skill.version}`
    : '选择一套有版本、研究规则、提示词和质量边界的内容工作流。';
  $('#manual-intake-intro').hidden = handoffOpen;
  $('#manual-intake-intro').textContent = isNews
    ? '输入一个新闻主题和关注角度，系统会以任务创建时间为边界检索公开来源，核对事件时间，再生成可追溯的口播脚本。'
    : '输入一个人物和他的核心观点，系统会先自动研究可追溯资料，再把观点中的冲突、反直觉与现实意义展开成完整视频脚本。';
  $('#source-card-form').hidden = handoffOpen || isNews;
  $('#news-topic-form').hidden = handoffOpen || !isNews;
  renderOrchestrationSelection();
}

function renderContentSkillSelector(preferredSkillId = '') {
  const selector = $('#content-skill');
  const skills = contentSkills();
  selector.innerHTML = skills.map((item) => (
    `<option value="${escapeHtml(item.skill_id)}">${escapeHtml(item.display_name)} · v${escapeHtml(item.version)}</option>`
  )).join('');
  const selected = contentSkill(preferredSkillId);
  if (selected) selector.value = selected.skill_id;
  updateContentSkillIntake();
}

function selectContentSkill(skillId, {resetPrompts = false} = {}) {
  const selected = contentSkill(skillId);
  if (!selected) return;
  $('#content-skill').value = selected.skill_id;
  updateContentSkillIntake();
  if (resetPrompts) {
    setPromptFields(skillGenerationDefaults(selected.skill_id));
  }
}

function visualStyles() {
  return Array.isArray(state.capabilities?.visual_styles)
    ? state.capabilities.visual_styles
    : [];
}

function visualStyle(styleId = '') {
  const styles = visualStyles();
  const requested = String(styleId || '').trim();
  return styles.find((item) => item.style_id === requested)
    || styles.find((item) => item.style_id === DEFAULT_VISUAL_STYLE_ID)
    || styles.find((item) => item.default)
    || styles[0]
    || null;
}

function selectedVisualStyle() {
  return visualStyle($('#visual-style')?.value || '');
}

function visualStylePreviewKey(styleId) {
  return [
    DEFAULT_VISUAL_STYLE_ID,
    'paper-collage-explainer',
    'papercraft-stop-motion',
  ].includes(styleId) ? styleId : DEFAULT_VISUAL_STYLE_ID;
}

function renderVisualStylePreviews() {
  const node = $('#visual-style-previews');
  if (!node) return;
  const selectedId = selectedVisualStyle()?.style_id || DEFAULT_VISUAL_STYLE_ID;
  node.innerHTML = visualStyles().map((style) => {
    const selected = style.style_id === selectedId;
    return `<button class="visual-style-preview ${selected ? 'selected' : ''}" type="button" data-style-preview="${escapeHtml(visualStylePreviewKey(style.style_id))}" data-visual-style-id="${escapeHtml(style.style_id)}" aria-pressed="${String(selected)}">
      <span class="style-preview-art" aria-hidden="true"><i></i><i></i><i></i></span>
      <strong>${escapeHtml(style.display_name)}</strong>
    </button>`;
  }).join('');
}

function updateVisualStyleDescription() {
  const style = selectedVisualStyle();
  $('#visual-style-description').textContent = style
    ? `${style.description} · v${style.version}`
    : '视觉风格与内容 Skill、生成模型相互独立。';
  renderVisualStylePreviews();
  renderOrchestrationSelection();
}

function renderVisualStyleSelector(preferredStyleId = '') {
  const selector = $('#visual-style');
  const styles = visualStyles();
  selector.innerHTML = styles.map((item) => (
    `<option value="${escapeHtml(item.style_id)}">${escapeHtml(item.display_name)} · v${escapeHtml(item.version)}</option>`
  )).join('');
  const selected = visualStyle(preferredStyleId);
  if (selected) selector.value = selected.style_id;
  updateVisualStyleDescription();
}

function promptWritingProfile() {
  const profile = state.capabilities?.prompt_writing_profile;
  return profile && typeof profile === 'object' ? profile : null;
}

function renderOrchestrationSelection() {
  const contentNode = $('#orchestration-content-name');
  const styleNode = $('#orchestration-style-name');
  const profileNode = $('#orchestration-profile-summary');
  const referenceNode = $('#reference-priority-state');
  if (!contentNode || !styleNode || !profileNode || !referenceNode) return;
  contentNode.textContent = selectedContentSkill()?.display_name || '内容 Skill';
  styleNode.textContent = selectedVisualStyle()?.display_name || '视觉风格';
  profileNode.textContent = promptWritingProfile()?.display_name || 'H3 统一编排';
  const supportsReference = selectedContentSkill()?.input_mode !== 'recent_news_topic';
  const hasReference = supportsReference && !!state.referenceImageFile;
  referenceNode.textContent = !supportsReference
    ? '参考图属性（当前 Skill 不使用）'
    : hasReference
      ? '参考图属性（已接管）'
      : '参考图属性（未上传）';
  referenceNode.classList.toggle('active', hasReference);
}

function renderPromptWritingProfile() {
  const profile = promptWritingProfile();
  $('#prompt-profile-name').textContent = profile?.display_name || 'H3 提示词编排';
  $('#prompt-profile-version').textContent = profile?.version
    ? '自动启用 · v' + profile.version
    : '自动启用';
  $('#prompt-profile-description').textContent = profile?.description
    || 'H3 是唯一编排层：从原始输入统一生成研究、脚本、分镜、首帧和首帧驱动视频提示词。';
  renderOrchestrationSelection();
}

const SEEDANCE_EFFICIENT_MODEL = 'doubao-seedance-1-0-pro-fast-251015';
const SEEDANCE_FLAGSHIP_MODEL = 'doubao-seedance-2-0-260128';
const MIN_IMAGE_CHAPTER_COUNT = 2;
const MAX_IMAGE_CHAPTER_COUNT = 10;
const DEFAULT_TTS_VOICE_ID = 'zh_female_vv_uranus_bigtts';
const DEFAULT_TTS_SPEED_RATIO = 1.2;
const FALLBACK_TTS_VOICES = [
  {id: DEFAULT_TTS_VOICE_ID, label: 'Vivi 2.0', description: '亲和自然'},
  {id: 'zh_female_santongyongns_saturn_bigtts', label: '流畅女声', description: '清晰利落'},
  {id: 'zh_male_ruyayichen_saturn_bigtts', label: '儒雅逸辰', description: '沉稳克制'},
];
const TTS_SPEED_RATIOS = [1.0, 1.1, 1.2];

function ttsVoices() {
  const configured = state.capabilities?.tts_pricing?.voices;
  return Array.isArray(configured) && configured.length === 3
    ? configured
    : FALLBACK_TTS_VOICES;
}

function normalizedTtsVoiceId(value) {
  const voices = ttsVoices();
  const requested = String(value || '');
  return voices.some((item) => item.id === requested)
    ? requested
    : (state.capabilities?.tts_pricing?.default_voice_id
      || voices.find((item) => item.default)?.id
      || DEFAULT_TTS_VOICE_ID);
}

function normalizedTtsSpeedRatio(value) {
  const parsed = Number(value);
  return TTS_SPEED_RATIOS.includes(parsed) ? parsed : DEFAULT_TTS_SPEED_RATIO;
}

function scriptTargetRange(speedRatio) {
  return {
    '1.0': [SCRIPT_TARGET_MIN_CHARS, SCRIPT_TARGET_MAX_CHARS],
    '1.1': [245, 325],
    '1.2': [265, 355],
  }[normalizedTtsSpeedRatio(speedRatio).toFixed(1)];
}

function ttsVoiceLabel(voiceId) {
  return ttsVoices().find((item) => item.id === voiceId)?.label || 'Seed-TTS 2.0';
}

function populateTtsVoiceSelect(select, voiceId) {
  const selected = normalizedTtsVoiceId(voiceId);
  select.innerHTML = ttsVoices().map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)} · ${escapeHtml(item.description)}</option>`
  )).join('');
  select.value = selected;
}

function setTtsSettingsFields(settings) {
  const defaults = generationDefaults();
  populateTtsVoiceSelect(
    $('#tts-voice-id'),
    settings?.tts_voice_id || defaults.tts_voice_id || DEFAULT_TTS_VOICE_ID,
  );
  $('#tts-speed-ratio').value = String(normalizedTtsSpeedRatio(
    settings?.tts_speed_ratio ?? defaults.tts_speed_ratio,
  ));
  updateProductionSpecSummary();
}

function selectedJobTtsSpeedRatio() {
  const field = $('#job-tts-speed-ratio');
  if (field && !$('#script-review')?.hidden) {
    return normalizedTtsSpeedRatio(field.value);
  }
  return normalizedTtsSpeedRatio(
    state.selectedJob?.generation_settings
      ? state.selectedJob.generation_settings.tts_speed_ratio
      : 1.0,
  );
}

function setJobTtsFields(job) {
  const settings = job?.generation_settings || {
    ...generationDefaults(),
    tts_speed_ratio: 1.0,
  };
  populateTtsVoiceSelect(
    $('#job-tts-voice-id'),
    settings.tts_voice_id || DEFAULT_TTS_VOICE_ID,
  );
  $('#job-tts-speed-ratio').value = String(normalizedTtsSpeedRatio(
    settings.tts_speed_ratio,
  ));
  const maxCost = Number(
    state.capabilities?.tts_pricing?.preview_max_estimated_cost_cny,
  );
  $('#tts-preview-cost').textContent = Number.isFinite(maxCost)
    ? `每次最多约 ${formatCny(maxCost)}，费用计入本任务`
    : '试听费用按实际字符数计入本任务';
}

function clearTtsPreview({resetStatus = true, clearCache = true} = {}) {
  const audio = $('#tts-preview-audio');
  audio.pause();
  audio.removeAttribute('src');
  audio.load();
  audio.hidden = true;
  if (clearCache) {
    state.ttsPreviewCache.forEach((url) => URL.revokeObjectURL(url));
    state.ttsPreviewCache.clear();
  }
  state.ttsPreviewUrl = '';
  state.ttsPreviewKey = '';
  if (resetStatus) {
    $('#tts-preview-status').textContent = '重复播放同一试听不会重复计费。';
  }
}

function ttsPreviewKey(job) {
  const settings = job?.generation_settings || {};
  return JSON.stringify([
    job?.id || '',
    narrationPreviewText(job?.script),
    settings.tts_voice_id || '',
    normalizedTtsSpeedRatio(settings.tts_speed_ratio).toFixed(1),
  ]);
}

function audioObjectUrl(base64Value, mediaType) {
  const binary = atob(String(base64Value || ''));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return URL.createObjectURL(new Blob([bytes], {type: mediaType || 'audio/mpeg'}));
}

function narrationPreviewText(script) {
  const opening = Array.from(
    String(scriptBeats(script)[0]?.narration || '').replace(/\s+/g, ' ').trim(),
  );
  const maxCharacters = 60;
  let preview = opening;
  if (opening.length > maxCharacters) {
    const window = opening.slice(0, maxCharacters);
    let breakAt = -1;
    window.forEach((character, index) => {
      if ('，。！？；,.!?;'.includes(character)) breakAt = index;
    });
    preview = breakAt >= Math.floor(maxCharacters / 2)
      ? window.slice(0, breakAt + 1)
      : window;
  }
  let value = preview.join('').trim();
  if (
    Array.from(value).length >= maxCharacters
    && !/[。！？….!?]$/.test(value)
  ) {
    value = Array.from(value).slice(0, maxCharacters - 1).join('').trimEnd();
  }
  return value;
}

function seedanceModels() {
  const configured = state.capabilities?.seedance_pricing?.models;
  if (Array.isArray(configured) && configured.length) return configured;
  return [
    {
      id: SEEDANCE_EFFICIENT_MODEL, label: 'Seedance 1.0 Pro Fast',
      short_label: '1.0 Fast', yuan_per_million_tokens: 4.2, default: true,
    },
    {
      id: SEEDANCE_FLAGSHIP_MODEL, label: 'Seedance 2.0',
      short_label: '2.0', yuan_per_million_tokens: 46, default: false,
    },
  ];
}

function seedanceModelInfo(modelId) {
  const models = seedanceModels();
  return models.find((item) => item.id === modelId)
    || models.find((item) => item.default)
    || models[0];
}

function defaultSeedanceModel() {
  return generationDefaults().seedance_model
    || state.capabilities?.seedance_pricing?.default_model
    || SEEDANCE_EFFICIENT_MODEL;
}

function seedanceModelForRequest(job, request, task = null) {
  return request?.model_id
    || task?.model_id
    || currentTaskForShot(job, request || {})?.model_id
    || job?.generation_settings?.seedance_model
    || defaultSeedanceModel();
}

function estimatedSeedanceCost(request, modelId) {
  const dimensions = {
    '480p': [480, 854], '720p': [720, 1280], '1080p': [1080, 1920],
  }[String(request?.resolution || '').toLowerCase()];
  const duration = Math.max(0, Number(request?.duration_seconds) || 0);
  const rate = Math.max(
    0,
    Number(seedanceModelInfo(modelId)?.yuan_per_million_tokens) || 0,
  );
  if (!dimensions || !duration || !rate) return null;
  const tokens = dimensions[0] * dimensions[1] * 24 * duration / 1024;
  return tokens * rate / 1000000;
}

function taskSeedanceCost(task) {
  const snapshot = task?.estimated_cost_cny;
  if (snapshot !== null && snapshot !== undefined && Number.isFinite(Number(snapshot))) {
    return Math.max(0, Number(snapshot));
  }
  const tokens = Math.max(0, Number(task?.usage_total_tokens) || 0);
  const snapshotRate = task?.pricing_rate_cny_per_million;
  const rate = snapshotRate !== null && snapshotRate !== undefined
    ? Math.max(0, Number(snapshotRate) || 0)
    : Math.max(0, Number(seedanceModelInfo(task?.model_id)?.yuan_per_million_tokens) || 0);
  return tokens && rate ? tokens * rate / 1000000 : 0;
}

function customScriptPromptEnabled() {
  return !!$('#enable-custom-script-prompt')?.checked;
}

function setCustomScriptPromptMode(enabled, {resetToDefault = false} = {}) {
  const checkbox = $('#enable-custom-script-prompt');
  const fields = $('#custom-script-prompt-fields');
  const textarea = $('#script-generation-prompt');
  const settings = $('#generation-prompt-settings');
  const badge = $('#script-prompt-mode-badge');
  if (!checkbox || !fields || !textarea || !settings || !badge) return;
  checkbox.checked = !!enabled;
  fields.hidden = !enabled;
  textarea.readOnly = !enabled;
  settings.classList.toggle('custom-active', !!enabled);
  badge.textContent = enabled ? '自定义 · 仅下一条' : '系统默认';
  if (resetToDefault) {
    setPromptFields(skillGenerationDefaults(
      selectedContentSkill()?.skill_id || '',
    ));
  }
}

function setPromptFields(settings) {
  const defaults = skillGenerationDefaults(
    settings?.skill_id || $('#content-skill')?.value,
  );
  $('#script-generation-prompt').value = settings?.script_prompt || defaults.script_prompt || '';
}

function updateProductionSpecSummary() {
  const node = $('#production-spec-summary');
  if (!node) return;
  const resolution = String($('#video-resolution')?.value || '1080p').toUpperCase();
  const imageCount = normalizedImageCount($('#image-count')?.value || 10);
  const voiceId = normalizedTtsVoiceId($('#tts-voice-id')?.value);
  const speed = normalizedTtsSpeedRatio($('#tts-speed-ratio')?.value).toFixed(1);
  node.textContent = `${resolution} · 3 段视频 + ${imageCount} 段动态图片 · ${ttsVoiceLabel(voiceId)} ${speed}x`;
}

function setResolutionField(settings) {
  const defaults = generationDefaults();
  const resolution = String(
    settings?.video_resolution || defaults.video_resolution || '1080p',
  ).toLowerCase();
  $('#video-resolution').value = ['480p', '720p', '1080p'].includes(resolution)
    ? resolution
    : '1080p';
  updateProductionSpecSummary();
}

function normalizedImageCount(value, fallback = 10) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) return fallback;
  return Math.min(MAX_IMAGE_CHAPTER_COUNT, Math.max(MIN_IMAGE_CHAPTER_COUNT, parsed));
}

function updateImageCountCost() {
  const imageCount = normalizedImageCount($('#image-count').value);
  const totalImages = imageCount + 3;
  const rate = Math.max(
    0,
    Number(state.capabilities?.seedream_pricing?.yuan_per_image) || 0,
  );
  $('#image-count-cost').textContent = [
    imageCount + ' 段动态图片 + 3 张视频首帧 = ' + totalImages + ' 张 Seedream',
    rate ? '图片刊例价预估约 ¥' + (totalImages * rate).toFixed(2) : '',
  ].filter(Boolean).join(' · ');
  updateProductionSpecSummary();
}

function setImageCountField(settings) {
  const defaults = generationDefaults();
  const inferred = settings?.image_count
    ?? (Number(settings?.shot_count) ? Number(settings.shot_count) - 3 : null)
    ?? defaults.image_count
    ?? 10;
  $('#image-count').value = normalizedImageCount(inferred);
  updateImageCountCost();
}

function jobResolution(job) {
  return job?.visual_requests?.[0]?.resolution
    || job?.generation_settings?.video_resolution
    || '480p';
}

function persistPromptFields() {
  try {
    const settings = generationSettingsPayload();
    // Custom script prompts are intentionally one-task-only and never survive
    // a reload or silently affect another Content Skill.
    delete settings.script_prompt;
    localStorage.setItem(PROMPT_STORAGE_KEY, JSON.stringify(settings));
    localStorage.removeItem(LEGACY_PROMPT_STORAGE_KEY);
  } catch { /* 浏览器禁用本地存储时仍可正常创建任务。 */ }
}

function initializePromptFields() {
  let saved = null;
  try {
    let raw = localStorage.getItem(PROMPT_STORAGE_KEY);
    if (!raw) {
      raw = localStorage.getItem(LEGACY_PROMPT_STORAGE_KEY);
    }
    saved = JSON.parse(raw || 'null');
  } catch { saved = null; }
  const selectedSkillId = contentSkills().some((item) => item.skill_id === saved?.skill_id)
    ? saved.skill_id
    : DEFAULT_CONTENT_SKILL_ID;
  const selectedStyleId = visualStyles().some(
    (item) => item.style_id === saved?.visual_style_id,
  )
    ? saved.visual_style_id
    : DEFAULT_VISUAL_STYLE_ID;
  const migratedVisualStyle = !!saved
    && saved.visual_style_id !== selectedStyleId;
  renderContentSkillSelector(selectedSkillId);
  renderVisualStyleSelector(selectedStyleId);
  renderPromptWritingProfile();
  const selectedDefaults = skillGenerationDefaults(selectedSkillId);
  setPromptFields(selectedDefaults);
  setCustomScriptPromptMode(false);
  setResolutionField(saved);
  setImageCountField(saved);
  setTtsSettingsFields(saved);
  if (saved?.script_prompt || migratedVisualStyle) persistPromptFields();
}

function generationSettingsPayload(skillOverride = '') {
  const selected = selectedContentSkill();
  const requestedSkill = contentSkill(skillOverride || selected?.skill_id || '');
  const requestedStyle = selectedVisualStyle();
  const useEditorPrompts = (
    customScriptPromptEnabled()
    && (!skillOverride || requestedSkill?.skill_id === selected?.skill_id)
  );
  const defaults = skillGenerationDefaults(requestedSkill?.skill_id || '');
  const scriptPrompt = useEditorPrompts
    ? $('#script-generation-prompt').value.trim()
    : String(defaults.script_prompt || '').trim();
  const videoResolution = $('#video-resolution').value;
  const rawImageCount = Number($('#image-count').value);
  const ttsVoiceId = normalizedTtsVoiceId($('#tts-voice-id').value);
  const ttsSpeedRatio = normalizedTtsSpeedRatio($('#tts-speed-ratio').value);
  if (!scriptPrompt) throw new Error('脚本生成提示词不能为空');
  if (!['480p', '720p', '1080p'].includes(videoResolution)) {
    throw new Error('请选择有效的视频画质');
  }
  if (
    !Number.isInteger(rawImageCount)
    || rawImageCount < MIN_IMAGE_CHAPTER_COUNT
    || rawImageCount > MAX_IMAGE_CHAPTER_COUNT
  ) {
    throw new Error('动态图片数量必须是 2–10 之间的整数');
  }
  return {
    ...(requestedSkill ? {
      skill_id: requestedSkill.skill_id,
      skill_version: requestedSkill.version,
    } : {}),
    ...(requestedStyle ? {
      visual_style_id: requestedStyle.style_id,
      visual_style_version: requestedStyle.version,
    } : {}),
    script_prompt: scriptPrompt,
    video_resolution: videoResolution,
    seedance_model: defaultSeedanceModel(),
    image_count: rawImageCount,
    shot_count: rawImageCount + 3,
    tts_voice_id: ttsVoiceId,
    tts_speed_ratio: ttsSpeedRatio,
  };
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


async function sha256File(file) {
  if (!globalThis.crypto?.subtle) {
    throw new Error('当前浏览器不支持安全文件校验，请升级浏览器后重试。');
  }
  const digest = await globalThis.crypto.subtle.digest(
    'SHA-256',
    await file.arrayBuffer(),
  );
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('');
}

function uploadFileDirect(grant, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(grant.upload_method || 'PUT', grant.upload_url, true);
    Object.entries(grant.upload_headers || {}).forEach(([name, value]) => {
      if (String(name).toLowerCase() !== 'host') {
        request.setRequestHeader(name, String(value));
      }
    });
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable && typeof onProgress === 'function') {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    });
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) {
        resolve();
        return;
      }
      reject(new Error(`素材存储上传失败（HTTP ${request.status || '未知'}）`));
    });
    request.addEventListener('error', () => {
      reject(new Error('无法直传素材，请检查网络或素材存储跨域配置后重试。'));
    });
    request.addEventListener('abort', () => {
      reject(new Error('素材上传已取消。'));
    });
    request.send(file);
  });
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
  renderOrchestrationSelection();
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
  renderOrchestrationSelection();
  notify('');
}

function notify(message, error = false) {
  const node = $('#notice');
  node.hidden = !message;
  node.textContent = message || '';
  node.classList.toggle('error', error);
}

function renderDouyinRefreshFeedback(job) {
  const node = $('#douyin-refresh-status');
  const button = $('#douyin-refresh-button');
  if (!node || !button) return;
  const feedback = state.douyinRefreshFeedback;
  const visible = !!(
    feedback?.message
    && feedback.jobId === job?.id
  );
  const tone = visible ? feedback.tone : '';
  const loading = tone === 'loading';
  node.hidden = !visible;
  node.textContent = visible ? feedback.message : '';
  node.classList.toggle('loading', loading);
  node.classList.toggle('success', tone === 'success');
  node.classList.toggle('error', tone === 'error');
  button.textContent = loading
    ? '正在刷新作品数据…'
    : (button.dataset.defaultLabel || '手动刷新作品数据');
  button.setAttribute('aria-busy', String(loading));
}

function setDouyinRefreshFeedback(jobId, tone, message) {
  state.douyinRefreshFeedback = {
    jobId: String(jobId || ''),
    tone: String(tone || ''),
    message: String(message || ''),
  };
  renderDouyinRefreshFeedback(state.selectedJob);
}

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll('[data-busy-lock]').forEach((control) => {
    control.disabled = busy;
    control.setAttribute('aria-busy', String(busy));
  });
  document.querySelectorAll('[data-shot-upload]').forEach((input) => {
    input.disabled = busy || !canEditResource(state.selectedJob);
  });
  if (!busy) {
    updateScriptLengthStatus();
    renderTopicControls();
    renderTopicDetail();
    applyJobReadOnly(state.selectedJob);
  }
  renderOrchestrationSelection();
}

const stateLabels = {
  card_verified: '观点已创建', script_generating: '生成脚本', script_review_required: '待确认脚本',
  script_approved: '脚本已确认', producing: '生产中', media_review_required: '安排镜头素材', quality_checking: '质检中',
  final_review_required: '待确认成片', final_approved: '正在生成发布包', packaged: '发布包完成', failed: '失败',
};
const workflowStages = [
  '准备内容', '确认脚本', '制作画面', '确认成片', '发布包',
];
const progressStageIndexes = {
  material_confirmed: 0,
  research_prompt_compilation: 0,
  person_research: 0,
  recent_news_research: 0,
  script: 0,
  script_generation: 0,
  confirm_script: 1,
  tts: 2,
  production: 2,
  storyboard: 2,
  confirm_media: 2,
  first_frames: 2,
  first_frames_video: 2,
  frame_selection: 2,
  seedance_parallel: 2,
  seedance_shot_1: 2,
  seedance_shot_2: 2,
  seedance_shot_3: 2,
  seedance_shot_4: 2,
  seedance_shot_5: 2,
  visual_assets: 2,
  remotion: 2,
  remotion_render: 2,
  remotion_normalize: 2,
  quality: 2,
  artifact_upload: 2,
  confirm_final: 3,
  package: 4,
};
const domainLabels = {
  parent_education: '家庭教育', developmental_psychology: '发展心理学', educational_psychology: '教育心理学',
  parent_child_relationship: '亲子关系', parent_growth: '家长成长',
  general_knowledge: '通识观点',
  technology: '科技', business: '商业', general_news: '通用新闻',
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
  const actorUsername = state.capabilities?.actor?.username || '';
  const hasRunning = state.topicRuns.some(
    (run) => run.status === 'running' && run.created_by === actorUsername,
  );
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
  const plannedEstimate = Number.isFinite(unitPrice) && unitPrice > 0
    ? `（约 ${formatCnyFromUsd(plannedCalls * unitPrice)}）`
    : '';
  const hardEstimate = Number.isFinite(unitPrice) && unitPrice > 0 && requestBudget
    ? `（约 ${formatCnyFromUsd(requestBudget * unitPrice)}）`
    : '';
  $('#topic-cost-guard').innerHTML = `
    <strong>本轮成本保护</strong>
    <span>当前计划最多 ${plannedCalls} 次 TikHub 请求${escapeHtml(plannedEstimate)} + 1 次编辑模型调用${requestBudget ? `；TikHub 硬上限 ${requestBudget} 次${escapeHtml(hardEstimate)}` : ''}。不会为了用满预算而增加调用</span>`;

  const policy = capability?.evidence_policy || {};
  const low = policy.low_follower_breakout || {};
  const emerging = policy.emerging_low_follower_breakout || {};
  const high = policy.high_heat_breakout || {};
  const ranking = policy.ranking || {};
  const gate = policy.research_gate || {};
  const lowFollowers = Number(low.max_followers || 50000);
  const lowPlays = Number(low.min_plays || 500000);
  const lowRatio = Number(low.min_play_follower_ratio || 20);
  const lowLike = Number(low.min_like_rate || 0.05);
  const deepRate = Number(low.min_deep_engagement_rate || 0.008);
  const emergingFollowers = Number(emerging.max_followers || 100000);
  const emergingFreshHours = Number(emerging.freshest_age_hours || 24);
  const emergingFreshPlays = Number(emerging.min_plays_fresh || 100000);
  const emergingPlays = Number(emerging.min_plays || 200000);
  const emergingRatio = Number(emerging.min_play_follower_ratio || 10);
  const emergingLike = Number(emerging.min_like_rate || 0.03);
  const emergingDeepRate = Number(emerging.min_deep_engagement_rate || 0.003);
  const highPlays = Number(high.min_plays || 1000000);
  const highLike = Number(high.min_like_rate || 0.08);
  const freshHours = Number(ranking.fresh_priority_hours || 72);
  const recentHours = Number(ranking.recent_priority_hours || 168);
  $('#topic-quality-policy').innerHTML = `
    <p><strong>榜单决定入池</strong><span>TikHub 低粉爆款榜或高点赞率榜 + 有效作品 ID/标题 + 家庭教育相关；继续排除泛母婴和带货内容。</span></p>
    <p><strong>指标只加复核标签</strong><span>强复核参考：粉丝 ≤ ${formatCount(lowFollowers)}、播放 ≥ ${formatCount(lowPlays)}、播粉比 ≥ ${lowRatio}、赞播比 ≥ ${formatPercent(lowLike)}、深互动 ≥ ${formatPercent(deepRate)}；潜力复核参考：粉丝 ≤ ${formatCount(emergingFollowers)}、${emergingFreshHours} 小时内播放 ≥ ${formatCount(emergingFreshPlays)}，之后 ≥ ${formatCount(emergingPlays)}、播粉比 ≥ ${emergingRatio}、赞播比 ≥ ${formatPercent(emergingLike)}、深互动 ≥ ${formatPercent(emergingDeepRate)}。未达到或字段缺失仍可作为平台榜单样本。</span></p>
    <p><strong>新近优先，不按时间淘汰</strong><span>发布 ${freshHours} 小时内最优先，其次 ${Math.round(recentHours / 24)} 天内；更早作品按当前榜单的回潮线索排序。高点赞强复核参考播放 ≥ ${formatCount(highPlays)}、赞播比 ≥ ${formatPercent(highLike)}。</span></p>
    <p><strong>交叉验证，不用普通搜索凑数</strong><span>至少 ${Number(gate.min_usable_videos || 8)} 条不同榜单视频，其中至少 ${Number(gate.min_low_follower_videos || 5)} 条来自低粉榜；最多批量补齐 ${Number(gate.batch_detail_limit || 50)} 条详情，每个选题仍需两条独立视频共同验证。</span></p>`;
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
    const access = canEditResource(run) ? '' : ' · 团队内容，只读';
    return `<button class="topic-run-card ${selected ? 'selected' : ''}" type="button" data-topic-run-id="${escapeHtml(run.id)}">
      <strong>家庭教育 · ${escapeHtml(date)}</strong>
      <span>${escapeHtml(topicStateLabels[run.status] || run.status)}${candidateCount ? ` · ${candidateCount} 个候选` : ''}${run.selected_candidate_id ? ' · 已采用' : ''} · ${escapeHtml(run.created_by || '创建者未知')}${access}</span>
    </button>`;
  }).join('');
}

function topicMetricPills(evidence) {
  const metrics = evidence?.metrics;
  if (!metrics) return '';
  const values = [];
  values.push(evidence.metrics_enriched ? '批量详情已补齐' : '榜单快照');
  const hasPublishedAge = (
    metrics.published_age_hours !== null
    && metrics.published_age_hours !== undefined
  );
  const ageHours = Number(metrics.published_age_hours);
  if (hasPublishedAge && Number.isFinite(ageHours) && ageHours >= 0) {
    const ageLabel = ageHours < 1
      ? '发布不足 1 小时'
      : (ageHours <= 72 ? `发布 ${Math.round(ageHours)} 小时` : `发布 ${(ageHours / 24).toFixed(1)} 天`);
    values.push(ageLabel);
  }
  if (metrics.average_daily_plays) values.push(`日均播放 ${formatCount(metrics.average_daily_plays)}`);
  if (metrics.play_count) values.push(`播放 ${formatCount(metrics.play_count)}`);
  if (metrics.like_rate !== null && metrics.like_rate !== undefined) values.push(`赞播比 ${formatPercent(metrics.like_rate)}`);
  if (metrics.deep_engagement_rate !== null && metrics.deep_engagement_rate !== undefined) values.push(`深互动 ${formatPercent(metrics.deep_engagement_rate)}`);
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
  const tier = {
    low_follower_billboard: ['平台低粉榜样本', 'emerging'],
    low_follower_breakout: ['已复核强低粉爆款', ''],
    emerging_low_follower_breakout: ['已复核潜力低粉爆款', 'emerging'],
    high_like_billboard: ['平台高点赞榜样本', 'high-heat'],
    high_heat_breakout: ['已复核高热爆款', 'high-heat'],
    trend_signal: ['趋势信号', 'trend'],
    unassessed: ['未按新标准复核', 'trend'],
  }[evidence.quality_tier || 'unassessed'] || ['研究证据', 'trend'];
  const qualification = (evidence.qualification_reasons || []).join('；');
  return `<div class="topic-evidence-row">
    <div class="topic-evidence-copy"><span class="topic-evidence-tier ${tier[1]}">${tier[0]}</span>${title}<span>${escapeHtml(subline)}</span>${qualification ? `<span>依据：${escapeHtml(qualification)}</span>` : ''}</div>
    <div class="topic-metrics">${topicMetricPills(evidence)}</div>
  </div>`;
}

function renderTopicDiagnostics(run) {
  const node = $('#topic-evidence-diagnostics');
  const diagnostics = run?.low_follower_diagnostics || {};
  const received = Number(diagnostics.received_count || 0);
  const qualified = Number(diagnostics.unique_qualified_count || 0);
  const emptyQueries = Number(diagnostics.empty_query_count || 0);
  const unrecognizedQueries = Number(diagnostics.unrecognized_query_count || 0);
  const queryFailures = Number(
    diagnostics.empty_or_unrecognized_query_count || 0,
  );
  if (!received && !qualified && !queryFailures && !emptyQueries && !unrecognizedQueries) {
    node.hidden = true;
    node.innerHTML = '';
    return;
  }
  const legacyUnclassifiedQueries = Math.max(
    0,
    queryFailures
      - emptyQueries
      - unrecognizedQueries,
  );
  const missingVideoIds = Number(diagnostics.rejected_missing_video_id_count || 0);
  const missingTitles = Number(diagnostics.rejected_missing_title_count || 0);
  const legacyMissingIdentity = Math.max(
    0,
    Number(diagnostics.rejected_missing_identity_count || 0)
      - missingVideoIds
      - missingTitles,
  );
  const historicalRejectionLabels = [
    ['空结果查询', emptyQueries],
    ['响应结构未识别', unrecognizedQueries],
    ['未分类的空/结构异常查询', legacyUnclassifiedQueries],
    ['作品 ID 缺失', missingVideoIds],
    ['标题缺失', missingTitles],
    ['其他身份字段缺失', legacyMissingIdentity],
    ['作品 ID 异常', Number(diagnostics.rejected_invalid_video_id_count || 0)],
    ['偏离家庭教育', Number(diagnostics.rejected_off_topic_count || 0)],
    ['历史淘汰：发布时间异常', Number(diagnostics.rejected_invalid_publish_time_count || 0)],
    ['历史淘汰：超过 72 小时', Number(diagnostics.rejected_too_old_count || 0)],
    ['历史淘汰：粉丝数缺失', Number(diagnostics.rejected_missing_followers_count || 0)],
    ['历史淘汰：粉丝超过 10 万', Number(diagnostics.rejected_follower_ceiling_count || 0)],
    ['历史淘汰：播放不足', Number(diagnostics.rejected_insufficient_plays_count || 0)],
    ['历史淘汰：播粉比不足', Number(diagnostics.rejected_play_follower_ratio_count || 0)],
    ['历史淘汰：赞播比不足', Number(diagnostics.rejected_like_rate_count || 0)],
    ['历史淘汰：深度互动不足', Number(diagnostics.rejected_deep_engagement_rate_count || 0)],
  ];
  const observationLabels = [
    ['批量详情已补齐', Number(diagnostics.detail_enriched_count || 0)],
    ['粉丝数未补齐（不淘汰）', Number(diagnostics.missing_follower_metrics_count || 0)],
    ['发布时间未补齐（不淘汰）', Number(diagnostics.missing_publish_time_count || 0)],
    ['发布超过 72 小时（仅降序）', Number(diagnostics.older_than_72h_count || 0)],
    ['指标强复核', Number(diagnostics.strong_qualified_count || 0)],
    ['指标潜力复核', Number(diagnostics.emerging_qualified_count || 0)],
  ];
  const rejectionPills = [...observationLabels, ...historicalRejectionLabels]
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `<span>${escapeHtml(label)} ${formatCount(count)}</span>`)
    .join('');
  const duplicateCount = Number(diagnostics.duplicate_qualified_count || 0);
  const duplicatePill = duplicateCount > 0
    ? `<span>跨检索重复 ${formatCount(duplicateCount)}</span>`
    : '';
  node.hidden = false;
  node.innerHTML = `
    <div class="topic-diagnostics-heading"><strong>低粉榜样本整理</strong><span>指标缺失与发布时间只影响标签和排序</span></div>
    <div class="topic-diagnostics-counts">
      <p><span>已检查</span><strong>${formatCount(received)}</strong></p>
      <p><span>唯一可用</span><strong>${formatCount(qualified)}</strong></p>
      <p><span>指标已复核</span><strong>${formatCount(Number(diagnostics.strong_qualified_count || 0) + Number(diagnostics.emerging_qualified_count || 0))}</strong></p>
      <p><span>平台榜单待补</span><strong>${formatCount(Number(diagnostics.billboard_only_count || 0))}</strong></p>
    </div>
    ${(duplicatePill || rejectionPills) ? `<div class="topic-diagnostics-rejections">${duplicatePill}${rejectionPills}</div>` : ''}`;
}

function renderTopicCost(run) {
  const cost = run?.cost || {};
  const model = cost.model_usage || null;
  const calls = cost.tikhub_calls || [];
  const tikhubCost = cost.estimated_tikhub_cost_usd === null || cost.estimated_tikhub_cost_usd === undefined
    ? '金额待账单'
    : `约 ${formatCnyFromUsd(cost.estimated_tikhub_cost_usd)}`;
  const modelState = model?.request_count
    ? (model.succeeded ? '成功' : (run?.status === 'running' ? '已调用，结果待确认' : '失败或中断'))
    : '等待调用';
  const modelTokens = model?.total_tokens ? ` · ${formatCount(model.total_tokens)} tokens` : '';
  const modelCost = model?.request_count
    ? `${modelState} · ${formatCnyFromUsd(model.reported_cost_usd)}${modelTokens}`
    : modelState;
  const totalCost = formatCnyFromUsd(cost.estimated_total_cost_usd, '待供应商返回完整金额');
  const unitCost = cost.estimated_cost_per_candidate_usd === null || cost.estimated_cost_per_candidate_usd === undefined
    ? ''
    : ` · 每个候选约 ${formatCnyFromUsd(cost.estimated_cost_per_candidate_usd)}`;
  const successCount = Number(cost.tikhub_success_count || 0);
  const hasTikhubEstimate = (
    cost.estimated_tikhub_cost_usd !== null
    && cost.estimated_tikhub_cost_usd !== undefined
  );
  const tikhubEstimate = Number(cost.estimated_tikhub_cost_usd);
  const fallbackUnitPrice = Number(state.capabilities?.topic_research?.estimated_usd_per_success);
  const snapshotUnitPrice = successCount > 0 && hasTikhubEstimate && Number.isFinite(tikhubEstimate)
    ? tikhubEstimate / successCount
    : fallbackUnitPrice;
  const costBasis = Number.isFinite(snapshotUnitPrice) && snapshotUnitPrice > 0
    ? `TikHub 按 ${formatCnyFromUsd(snapshotUnitPrice)}/成功请求估算，失败响应按 ¥0 估算；美元成本固定按 1 USD = ¥${usdToCnyRate()} 换算，供应商账单优先。`
    : `美元成本固定按 1 USD = ¥${usdToCnyRate()} 换算；当前金额待供应商账单。`;
  const callDetails = calls.length ? `<details class="topic-cost-details">
    <summary>查看 ${calls.length} 次 TikHub 调用明细</summary>
    <div>${calls.map((call, index) => {
      const endpoint = String(call.endpoint || '').split('/').filter(Boolean).pop() || call.endpoint || 'unknown';
      const requestLabel = call.request_label ? ` · ${call.request_label}` : '';
      const dataShape = call.data_shape ? ` · 返回结构 ${call.data_shape}` : '';
      const requestId = call.request_id ? ` · ${call.request_id}` : '';
      const responseCode = call.response_code === null || call.response_code === undefined ? 'code —' : `code ${call.response_code}`;
      return `<p><span>${index + 1}. ${escapeHtml(endpoint + requestLabel + dataShape)}</span><strong>${call.succeeded ? '成功' : '失败'} · ${escapeHtml(responseCode)} · ${Number(call.elapsed_ms || 0)} ms${escapeHtml(requestId)}</strong></p>`;
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
    <p class="topic-cost-basis">${escapeHtml(costBasis)}</p>
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
    `创建者 ${run.created_by || '未知'}`,
    canEditResource(run) ? '' : '团队内容 · 只读',
    run.valid_through ? `榜单采集 ${run.valid_through}` : '榜单采集日期待写入',
    run.data_window_note || '',
  ].filter(Boolean);
  $('#topic-run-meta').innerHTML = meta.map((item) => `<span>${escapeHtml(item)}</span>`).join('');
  renderTopicCost(run);
  renderTopicDiagnostics(run);
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
    const editable = canEditResource(run);
    const actionDisabled = state.busy || (!editable && !selected);
    const actionLabel = selected
      ? (editable ? '继续补充来源' : '用这个选题创作')
      : (editable ? '采用并补充来源' : '创建者尚未采用');
    const evidence = candidate.evidence_refs.map((id) => evidenceById.get(id)).filter(Boolean);
    const lowFollowerCount = evidence.filter((item) => [
      'low_follower_billboard',
      'low_follower_breakout',
      'emerging_low_follower_breakout',
    ].includes(item.quality_tier)).length;
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
        <details class="topic-evidence"><summary>查看 ${evidence.length} 条抖音研究依据${lowFollowerCount ? ` · ${lowFollowerCount} 条低粉榜视频` : ''}</summary><div class="topic-evidence-list">${evidence.map(topicEvidenceRow).join('')}</div></details>
        <div class="topic-candidate-actions">
          <span>采用后仍需补充独立可靠资料，趋势数据不会进入脚本来源。</span>
          <button class="button ${selected ? 'secondary' : 'primary'}" type="button" data-adopt-topic="${escapeHtml(candidate.id)}" data-busy-lock ${actionDisabled ? 'disabled' : ''}>${actionLabel}</button>
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
  $('#account-management-link').hidden = data.actor?.role !== 'admin';
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
      ? `视频生产已就绪 · ${data.storage} 存储 · ${contentSkills().length} 个内容 Skill`
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
      <div class="meta"><span>${escapeHtml(domainLabels[card.content_domain] || card.content_domain)}</span><span>v${card.revision}</span><span>${card.status === 'verified' ? '已核验' : '草稿'}</span><span>${escapeHtml(card.created_by || '创建者未知')}</span>${canEditResource(card) ? '' : '<span>团队内容 · 只读</span>'}</div>
      ${card.status === 'verified' && canEditResource(card)
        ? `<div class="list-actions"><button class="button primary" type="button" data-create-job="${escapeHtml(card.id)}" data-busy-lock ${state.busy ? 'disabled' : ''}>用这份资料再生成一版</button></div>`
        : `<div class="meta legacy-card-note">${card.status === 'verified' ? '可查看，只有创建者可以继续生成' : '旧版来源草稿，仅保留记录'}</div>`}
    </article>`).join('');
}

function renderJobs() {
  const node = $('#job-list');
  const countNode = $('#production-task-count');
  if (countNode) countNode.textContent = String(state.jobs.length);
  if (!state.jobs.length) { node.innerHTML = '<p class="empty">选择内容 Skill，开始第一条视频。</p>'; return; }
  node.innerHTML = state.jobs.map((job) => {
    const title = job.source_card_snapshot?.title || job.id;
    const skillName = job.skill_snapshot?.display_name || '旧版工作流';
    return `<button class="job-card ${state.selectedJob?.id === job.id ? 'selected' : ''}" data-job-id="${escapeHtml(job.id)}" type="button" aria-pressed="${String(state.selectedJob?.id === job.id)}">
      <span class="job-card-title">${escapeHtml(title)}</span>
      <span class="meta"><span>${escapeHtml(skillName)}</span><span>${escapeHtml(stateLabels[job.state] || job.state)}</span><span>v${job.revision}</span><span>${escapeHtml(job.created_by || '创建者未知')}</span><span>${escapeHtml(formatDateTime(job.updated_at || ''))}</span>${canEditResource(job) ? '' : '<span>只读</span>'}</span>
    </button>`;
  }).join('');
}

function taskForJob(job) {
  if (!job?.last_run_task_id || state.activeTask?.task_id !== job.last_run_task_id) return null;
  return state.activeTask;
}

function stageIndex(job) {
  if (job.state === 'script_review_required') return 1;
  if (job.state === 'media_review_required') return 2;
  if (job.state === 'final_review_required') return 3;
  if (job.state === 'packaged') return 4;
  const taskStage = taskForJob(job)?.progress_meta?.stage;
  if (taskStage in progressStageIndexes) return progressStageIndexes[taskStage];
  if (job.state === 'failed') {
    return {script: 0, production: 2, quality: 2, package: 4}[job.failed_stage] ?? 0;
  }
  return {
    card_verified: 0,
    script_generating: 0,
    script_review_required: 1,
    script_approved: 2,
    producing: 2,
    media_review_required: 2,
    quality_checking: 2,
    final_review_required: 3,
    final_approved: 4,
    packaged: 4,
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
  return durationText(seconds);
}

function durationText(rawSeconds) {
  const seconds = Math.max(0, Math.floor(Number(rawSeconds) || 0));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return `${minutes} 分 ${remainder} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

function phaseElapsedText(task) {
  const meta = task?.progress_meta || {};
  const started = parseTaskTime(meta.phase_started_at);
  if (task?.status === 'running' && Number.isFinite(started)) {
    return durationText((Date.now() - started) / 1000);
  }
  const recorded = Number(meta.phase_elapsed_seconds);
  return Number.isFinite(recorded) ? durationText(recorded) : '—';
}

function formatTokens(value) {
  return new Intl.NumberFormat('zh-CN').format(Math.max(0, Number(value) || 0));
}

function requestSignature(request) {
  if (!request) return '';
  return JSON.stringify([
    request.request_id,
    request.prompt,
    request.revision_intent || '',
    request.resolution,
    request.model_id || '',
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

function uploadedMediaForShot(job, shotId) {
  return (job?.shot_media_versions || [])
    .filter((item) => item.shot_id === shotId && item.asset)
    .sort((left, right) => left.version - right.version);
}

function selectedUploadedMedia(job, shotId) {
  const selectedId = storyboardShotFor(job, shotId)?.selected_media_id || '';
  return uploadedMediaForShot(job, shotId).find(
    (item) => item.media_id === selectedId,
  ) || null;
}

function pendingShotMediaEdit(job, shotId) {
  return (job?.pending_shot_media_edits || []).find(
    (item) => item.shot_id === shotId,
  ) || null;
}

function pendingUploadedMedia(job, shotId) {
  const pending = pendingShotMediaEdit(job, shotId);
  if (!pending?.media_id) return null;
  return uploadedMediaForShot(job, shotId).find(
    (item) => item.media_id === pending.media_id,
  ) || null;
}

function uploadedMediaUrl(job, media) {
  if (!media?.asset) return '';
  return API + '/jobs/' + encodeURIComponent(job.id)
    + '/shots/' + encodeURIComponent(media.shot_id)
    + '/uploads/' + encodeURIComponent(media.media_id) + '/media';
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
      model_id: request
        ? (request.model_id || '')
        : (job?.generation_settings?.seedance_model || defaultSeedanceModel()),
      resolution: request?.resolution || jobResolution(job),
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
  const imageRate = Math.max(
    0,
    Number(state.capabilities?.seedream_pricing?.yuan_per_image) || 0,
  );
  const seedanceCost = tasks.reduce(
    (total, task) => total + taskSeedanceCost(task),
    0,
  );
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
    ? `${firstFrames.length} 张首帧 · ${requests.length} 段视频 · ${durationLabel} · ${firstRequest.resolution} · ${seedanceModelInfo(seedanceModelForRequest(job, firstRequest))?.short_label || 'Seedance'}`
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
      ? versions.map((version) => ({
        label: `v${version.version}`, task: version.task, request: version.request,
      }))
      : [{label: 'v1', task: currentTaskForShot(job, request), request}];
    const status = rows.map(({label, task, request: versionRequest}) => {
      const tokens = Number(task?.usage_total_tokens) || 0;
      const model = seedanceModelInfo(
        seedanceModelForRequest(job, versionRequest || request, task),
      );
      const cost = taskSeedanceCost(task);
      const usage = tokens
        ? `${formatTokens(tokens)} tokens${cost ? ` / ¥${cost.toFixed(2)}` : ''}`
        : (task?.raw_status || task?.state || '未提交');
      return `${label} · ${model?.short_label || 'Seedance'} · ${usage}`;
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
  const preGeneration = job.state === 'media_review_required';
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
  const uploads = uploadedMediaForShot(job, request.request_id);
  const activeUpload = selectedUploadedMedia(job, request.request_id);
  const pendingEdit = pendingShotMediaEdit(job, request.request_id);
  const pendingUpload = pendingUploadedMedia(job, request.request_id);
  const pendingPreviewId = pendingEdit
    ? pendingEdit.media_id || '__generated'
    : activeUpload?.media_id || '__generated';
  if (!state.previewUploadId) {
    state.previewUploadId = pendingPreviewId;
  }
  let previewUpload = state.previewUploadId === '__generated'
    ? null
    : uploads.find((item) => item.media_id === state.previewUploadId) || null;
  if (!previewUpload && state.previewUploadId !== '__generated') {
    state.previewUploadId = pendingPreviewId;
    previewUpload = pendingUpload || activeUpload;
  }

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
  const previewIsCurrent = previewUpload
    ? previewUpload.media_id === activeUpload?.media_id
    : !activeUpload && (preGeneration || isImage || (previewVersion
      && requestSignature(previewVersion.request) === requestSignature(request)));
  const previewIsPending = !!pendingEdit && (
    pendingEdit.media_id
      ? previewUpload?.media_id === pendingEdit.media_id
      : !previewUpload && (isImage || (previewVersion
        && requestSignature(previewVersion.request) === requestSignature(request)))
  );

  const frameCandidates = frameCandidatesForShot(job, request.request_id);
  const showFrameChoices = !previewUpload && frameCandidates.length > 1;
  const frameSelection = frameSelectionForShot(job, request.request_id);
  const previewRequest = previewVersion?.request || request;
  const previewFrame = frameCandidates.find(
    (candidate) => candidate.asset?.asset_id === previewRequest.first_frame_asset_id,
  ) || currentFrameCandidate(job, request);
  const chosenFrame = frameCandidates.find(
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
  const canManageMedia = canEditResource(job)
    && ['media_review_required', 'final_review_required'].includes(job.state)
    && !taskRunning
    && !state.busy;
  const canEditAi = !isImage
    && job.state === 'final_review_required'
    && canManageMedia;
  const key = JSON.stringify({
    job: job.id,
    shot: request.request_id,
    preview: previewKey,
    previewUpload: state.previewUploadId,
    selectedUpload: activeUpload?.media_id || '',
    pendingUpload: pendingEdit?.media_id ?? null,
    previewFrame: state.previewFrameCandidateId,
    current: requestSignature(request),
    taskRunning,
    uploads: uploads.map((item) => [
      item.media_id,
      item.asset?.sha256 || '',
      item.original_filename || '',
    ]),
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

  const mediaUrl = previewUpload
    ? uploadedMediaUrl(job, previewUpload)
    : (isImage ? '' : shotMediaUrl(job, request, previewVersion));
  const previewIsImage = previewUpload
    ? previewUpload.media_kind === 'image'
    : isImage;
  const selectedModelId = seedanceModelForRequest(
    job,
    previewRequest,
    previewVersion?.task,
  );
  const selectedModel = seedanceModelInfo(selectedModelId);
  const modelOptions = seedanceModels().map((model) => {
    const suffix = model.id === SEEDANCE_EFFICIENT_MODEL
      ? ' · 默认省成本'
      : ' · 复杂镜头升级';
    return `<option value="${escapeHtml(model.id)}" ${model.id === selectedModelId ? 'selected' : ''}>${escapeHtml(model.label + suffix)}</option>`;
  }).join('');
  const selectedModelCost = estimatedSeedanceCost(previewRequest, selectedModelId);
  const previewLabel = previewUpload
    ? `上传 v${previewUpload.version}`
    : preGeneration
      ? `待生成 AI ${isImage ? '动态图片' : '视频'}`
    : isImage
      ? 'AI 动态图片'
      : previewVersion ? `AI v${previewVersion.version}` : 'AI 当前版本';
  const pendingGenerated = !!pendingEdit && !pendingEdit.media_id;

  const generatedImageButton = isImage || preGeneration
    ? `<button class="version-pill ${!previewUpload ? 'previewing' : ''}" type="button" data-preview-generated>${preGeneration ? 'AI 生成' : 'AI 动态图片'}${pendingGenerated ? ' · 待应用' : !activeUpload ? ` · ${preGeneration ? '已选' : '成片'}` : ''}</button>`
    : '';
  const versionButtons = versions.map((version) => {
    const versionKey = version.version_id || '__current';
    const usable = !!version.asset && version.task?.state === 'succeeded';
    const selected = !previewUpload && versionKey === previewKey;
    const isCurrent = !activeUpload
      && requestSignature(version.request) === requestSignature(request);
    const isPending = pendingGenerated
      && requestSignature(version.request) === requestSignature(request);
    const suffix = isPending
      ? ' · 待应用'
      : isCurrent ? ' · 成片' : usable ? '' : ` · ${version.task?.state || '处理中'}`;
    const versionModel = seedanceModelInfo(
      seedanceModelForRequest(job, version.request, version.task),
    );
    return `<button class="version-pill ${selected ? 'previewing' : ''}" type="button" data-preview-version="${escapeHtml(versionKey)}" ${usable ? '' : 'disabled'}>AI v${version.version} · ${escapeHtml(versionModel?.short_label || 'Seedance')}${escapeHtml(suffix)}</button>`;
  }).join('');
  const uploadButtons = uploads.map((item) => {
    const selected = previewUpload?.media_id === item.media_id;
    const current = activeUpload?.media_id === item.media_id;
    const pending = pendingEdit?.media_id === item.media_id;
    const kind = item.media_kind === 'video' ? '视频' : '图片';
    return `<button class="version-pill uploaded ${selected ? 'previewing' : ''}" type="button" data-preview-upload="${escapeHtml(item.media_id)}">上传 v${item.version} · ${kind}${pending ? ' · 待应用' : current ? ` · ${preGeneration ? '已选' : '成片'}` : ''}</button>`;
  }).join('');
  const historyButtons = [generatedImageButton || versionButtons, uploadButtons]
    .filter(Boolean)
    .join('');

  let applyButton = '';
  if (preGeneration && previewUpload && !previewIsCurrent) {
    applyButton = `<button class="button secondary" type="button" data-select-upload="${escapeHtml(previewUpload.media_id)}" data-busy-lock ${canManageMedia ? '' : 'disabled'}>选用上传 v${previewUpload.version}</button>`;
  } else if (preGeneration && !previewUpload && activeUpload) {
    applyButton = `<button class="button secondary" type="button" data-restore-generated data-busy-lock ${canManageMedia ? '' : 'disabled'}>改为使用 AI 生成</button>`;
  } else if (pendingEdit && (previewIsPending || previewIsCurrent)) {
    applyButton = `<button class="button secondary" type="button" data-discard-pending-media data-busy-lock ${canManageMedia ? '' : 'disabled'}>撤销这个镜头的待应用修改</button>`;
  } else if (previewUpload && !previewIsCurrent) {
    applyButton = `<button class="button secondary" type="button" data-select-upload="${escapeHtml(previewUpload.media_id)}" data-busy-lock ${canManageMedia ? '' : 'disabled'}>暂存上传 v${previewUpload.version}</button>`;
  } else if (!previewUpload && activeUpload) {
    const previewIsOlderAiVersion = !isImage
      && previewVersion?.asset
      && requestSignature(previewVersion.request) !== requestSignature(request);
    applyButton = previewIsOlderAiVersion
      ? `<button class="button secondary" type="button" data-select-version="${escapeHtml(previewVersion.version_id)}" data-busy-lock ${canEditAi ? '' : 'disabled'}>将 ${escapeHtml(previewLabel)} 用于成片</button>`
      : `<button class="button secondary" type="button" data-restore-generated data-busy-lock ${canManageMedia ? '' : 'disabled'}>暂存恢复 AI 素材</button>`;
  } else if (!previewUpload && !previewIsCurrent && previewVersion?.asset) {
    applyButton = `<button class="button secondary" type="button" data-select-version="${escapeHtml(previewVersion.version_id)}" data-busy-lock ${canEditAi ? '' : 'disabled'}>将 ${escapeHtml(previewLabel)} 用于成片</button>`;
  }

  const frameEvaluations = new Map(
    (frameSelection?.evaluations || []).map((item) => [item.candidate_id, item]),
  );
  const frameButtons = showFrameChoices ? frameCandidates.map((candidate) => {
    const isPreviewing = candidate.candidate_id === state.previewFrameCandidateId;
    const isRecommended = candidate.candidate_id === frameSelection?.recommended_candidate_id;
    const isCurrent = !activeUpload
      && candidate.asset?.asset_id === request.first_frame_asset_id;
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
  const framePreviewUrl = previewUpload ? '' : frameCandidateMediaUrl(job, chosenFrame);
  const regenerateLabel = chosenFrame && !chosenFrameIsCurrent
    ? '用这张首帧换一版'
    : '按当前首帧换一版';
  const mediaKind = previewUpload
    ? `自有${previewUpload.media_kind === 'video' ? '视频' : '图片'}`
    : isImage ? 'AI 动态图片' : 'Seedance 视频';
  const headerDetail = previewUpload
    ? `${escapeHtml(previewUpload.original_filename || previewLabel)} · 成片约 ${request.duration_seconds} 秒`
    : isImage
      ? `${escapeHtml(previewLabel)} · 成片约 ${request.duration_seconds} 秒 · Remotion 动态取景`
      : `${escapeHtml(previewLabel)} · ${request.duration_seconds} 秒 · ${escapeHtml(request.resolution)} · ${escapeHtml(selectedModel?.short_label || 'Seedance')}`;
  const sourceStatusBadge = pendingEdit
    ? `<span class="source-active-badge">待应用：${pendingUpload ? `上传 v${pendingUpload.version}` : 'AI 素材'}</span>`
    : activeUpload
      ? `<span class="source-active-badge">${preGeneration ? '本次将使用自有素材' : '当前使用自有素材'}</span>`
      : '';
  const semanticShot = storyboardShotFor(job, request.request_id);
  const semanticIntent = semanticShot?.visual_intent
    || shotDescription(job, request)
    || '沿用已确认脚本与当前首帧。';
  const currentRevisionIntent = String(previewRequest.revision_intent || '').trim();
  const compiledFirstFramePrompt = chosenFrame?.prompt
    || semanticShot?.first_frame_prompt
    || '';
  const compiledVideoPrompt = isImage
    ? ''
    : String(previewRequest.prompt || semanticShot?.motion_prompt || '');
  const promptMethodName = job.prompt_writing_profile_snapshot?.display_name
    || '历史兼容编译器';
  const semanticPanel = `
    <section class="shot-intent-panel" aria-label="镜头语义与编译提示词">
      <div class="shot-intent-heading">
        <strong>镜头语义</strong>
        <span>这是内容层要表达的画面含义，不是 Provider 最终提示词</span>
      </div>
      <p>${escapeHtml(semanticIntent)}</p>
      ${currentRevisionIntent ? `<p class="shot-current-revision"><strong>当前版本修改意图：</strong>${escapeHtml(currentRevisionIntent)}</p>` : ''}
      <details class="compiled-prompts">
        <summary>查看 ${escapeHtml(promptMethodName)} 编译后的只读提示词</summary>
        <div class="compiled-prompt-grid">
          <label>首帧提示词 · 只读
            <textarea rows="5" readonly spellcheck="false">${escapeHtml(compiledFirstFramePrompt || '该历史任务没有保存首帧提示词。')}</textarea>
          </label>
          ${compiledVideoPrompt ? `<label>I2V 动作提示词 · 只读
            <textarea rows="7" readonly spellcheck="false">${escapeHtml(compiledVideoPrompt)}</textarea>
          </label>` : ''}
        </div>
      </details>
    </section>`;

  inspector.innerHTML = `
    <div class="shot-inspector-header">
      <div><h4>镜头 ${requestIndex + 1} · ${mediaKind}</h4><p>${headerDetail}</p></div>
      <button class="icon-button" type="button" data-close-inspector aria-label="关闭镜头设置">×</button>
    </div>
    ${mediaUrl && !previewIsImage
      ? `<video class="inspector-video" src="${mediaUrl}" controls preload="metadata" playsinline></video>`
      : mediaUrl && previewIsImage
        ? `<div class="inspector-frame-wrap"><img class="inspector-frame-preview" src="${mediaUrl}" alt="上传图片预览"><span>成片中会自动裁切为竖屏并添加缓慢取景运动</span></div>`
        : framePreviewUrl
          ? `<div class="inspector-frame-wrap"><img class="inspector-frame-preview" src="${framePreviewUrl}" alt="当前首帧预览"><span>${isImage ? '成片中会自动添加缓慢取景运动' : 'Seedance 视频生成后会替换此预览'}</span></div>`
          : '<div class="inspector-empty">素材生成或上传后可在这里预览</div>'}
    ${historyButtons ? `<div class="version-row source-version-row" aria-label="镜头素材历史">${historyButtons}</div>` : ''}
    ${applyButton ? `<div class="shot-source-selection-actions">${applyButton}</div>` : ''}
    ${semanticPanel}
    <section class="shot-source-panel" aria-label="上传自己的镜头素材">
      <div class="shot-source-heading">
        <div><strong>换成自己的素材</strong><span>适合产品实拍、人物采访、操作录屏、品牌与数据画面</span></div>
        ${sourceStatusBadge}
      </div>
      <div class="shot-source-actions">
        <label class="button secondary shot-upload-button ${canManageMedia ? '' : 'disabled'}">
          上传图片
          <input type="file" data-shot-upload accept="image/jpeg,image/png,image/webp" data-media-kind="image" ${canManageMedia ? '' : 'disabled'}>
        </label>
        <label class="button secondary shot-upload-button ${canManageMedia ? '' : 'disabled'}">
          上传视频
          <input type="file" data-shot-upload accept=".mp4,.mov,.webm,video/mp4,video/quicktime,video/webm" data-media-kind="video" ${canManageMedia ? '' : 'disabled'}>
        </label>
      </div>
      <p>${preGeneration
        ? '图片支持 JPG、PNG、WebP，最大 20 MB；视频支持 MP4、MOV、WebM，最大 200 MB。上传后直接加入本次素材安排；确认后只生成其余 AI 画面，首版成片只渲染一次。'
        : '图片支持 JPG、PNG、WebP，最大 20 MB；视频支持 MP4、MOV、WebM，最大 200 MB。每次上传只做安全校验、转码和暂存；完成全部替换后再一次应用并重新生成成片。原 AI 素材会完整保留。'}</p>
    </section>
    ${frameButtons ? `<section class="frame-candidate-section"><div class="frame-candidate-heading"><strong>${isImage ? '图片候选' : '首帧候选'}</strong><span>${isImage ? '系统已推荐当前成片使用的图片，可点击查看另一构图' : '系统已自动推荐，可人工改选后重生成本镜头'}</span></div><div class="frame-candidate-grid">${frameButtons}</div></section>` : ''}
    ${preGeneration ? `<section class="ai-shot-panel"><div class="shot-source-heading"><div><strong>AI 生成方案</strong><span>${activeUpload ? '当前已跳过这个镜头的 AI 生成；改回 AI 后才会产生费用' : '当前镜头未上传素材，确认后才会开始 AI 生成'}</span></div></div></section>` : isImage ? '' : `<section class="ai-shot-panel"><div class="shot-source-heading"><div><strong>AI 生成方案</strong><span>只填写想改变的动作或镜头效果；H3 会保留事实、风格、参考图和首帧边界</span></div></div>
      <label class="shot-revision-field">这次想调整什么
        <textarea id="shot-revision-intent" rows="4" maxlength="600" placeholder="例如：孩子先犹豫半秒，再把积木轻轻放稳；镜头只做一次缓慢推进。" ${canEditAi ? '' : 'readonly'}>${escapeHtml(currentRevisionIntent)}</textarea>
        <span class="field-hint">这里是语义修改意图，不会直接发送给 Seedance。需要新构图时，先选择另一张首帧候选。</span>
      </label>
      <label class="shot-model-field">生成模型
        <select id="shot-seedance-model" ${canEditAi ? '' : 'disabled'}>${modelOptions}</select>
        <span class="field-hint">默认 1.0 Pro Fast 保持原生 1080P；仅在复杂动作、人物一致性不理想时升级 2.0。</span>
      </label>
      <div class="shot-inspector-actions">
        <button class="button primary" type="button" data-regenerate-shot data-busy-lock ${canEditAi ? '' : 'disabled'}>${regenerateLabel}</button>
      </div>
      <p class="cost-note" data-shot-model-cost>本次只会新增 1 次 ${previewRequest.duration_seconds} 秒、${escapeHtml(previewRequest.resolution)} ${escapeHtml(selectedModel?.label || 'Seedance')} 调用${selectedModelCost === null ? '' : `，刊例价预估约 ¥${selectedModelCost.toFixed(2)}`}；不会重做旁白、图片章节或其他视频镜头。</p>
    </section>`}
    ${!preGeneration && isImage ? '<p class="cost-note">AI 图片章节由 Remotion 添加轻微推进或横移，不产生 Seedance 视频费用。</p>' : ''}`;
}

function renderShotStoryboard(job) {
  const section = $('#shot-storyboard');
  const requests = storyboardRequests(job);
  const preGeneration = job.state === 'media_review_required';
  const selectionWarning = $('#frame-selection-warning');
  selectionWarning.hidden = !job?.frame_selection_warning;
  selectionWarning.textContent = job?.frame_selection_warning || '';
  section.hidden = requests.length === 0;
  if (section.hidden) {
    state.storyboardKey = '';
    state.inspectorKey = '';
    $('#pre-generation-media-bar').hidden = true;
    $('#pending-shot-media-bar').hidden = true;
    return;
  }
  if (
    state.selectedShotId
    && !requests.some((request) => request.request_id === state.selectedShotId)
  ) {
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewUploadId = '';
    state.previewFrameCandidateId = '';
  }

  const rows = requests.map((request, index) => {
    const activeUpload = selectedUploadedMedia(job, request.request_id);
    const pendingEdit = pendingShotMediaEdit(job, request.request_id);
    const pendingUpload = pendingUploadedMedia(job, request.request_id);
    const displayUpload = pendingEdit ? pendingUpload : activeUpload;
    const isImage = displayUpload
      ? displayUpload.media_kind === 'image'
      : request.visual_type === 'image';
    const versions = versionsForShot(job, request);
    const currentVersion = currentVersionForShot(job, request);
    const currentFrame = currentFrameCandidate(job, request)
      || frameCandidatesForShot(job, request.request_id)[0]
      || null;
    const currentAsset = displayUpload?.asset || (
      isImage
        ? currentFrame?.asset || null
        : currentVersion?.asset || renderedVisualAsset(job, request)
    );
    const latestVersion = versions.at(-1) || null;
    const latestIsCandidate = latestVersion
      && requestSignature(latestVersion.request) !== requestSignature(request);
    let status = pendingEdit
      ? {label: '待应用 · 当前成片尚未更新', className: 'pending'}
      : activeUpload
      ? {label: preGeneration ? '已选择自有素材 · 跳过 AI 生成' : '已使用上传素材', className: 'succeeded'}
      : preGeneration
        ? {label: '将由 AI 生成', className: 'waiting'}
      : isImage && currentAsset
        ? {label: '动态图片已就绪', className: 'succeeded'}
        : shotStatus(currentTaskForShot(job, request), !!currentAsset);
    if (!pendingEdit && latestIsCandidate && !latestVersion.asset) {
      status = ['failed', 'cancelled'].includes(latestVersion.task?.state)
        ? {label: '新版本失败，当前版保留', className: 'failed'}
        : {label: '正在生成新版本', className: 'running'};
    }
    return {
      request, index, isImage, versions, currentVersion,
      currentAsset, currentFrame, activeUpload, pendingEdit,
      pendingUpload, displayUpload, status,
    };
  });
  const readyCount = rows.filter((row) => row.currentAsset).length;
  const frameReadyCount = rows.filter((row) => row.currentFrame).length;
  const videoRows = rows.filter((row) => !row.isImage);
  const imageRows = rows.filter((row) => row.isImage);
  const readyVideos = videoRows.filter((row) => row.currentAsset).length;
  const pendingCount = (job.pending_shot_media_edits || []).length;
  const ownMediaCount = rows.filter((row) => row.activeUpload).length;
  const aiMediaCount = Math.max(0, rows.length - ownMediaCount);
  $('#storyboard-summary').textContent = preGeneration
    ? `${ownMediaCount} 个自有素材已选 · ${aiMediaCount} 个镜头将由 AI 生成`
    : pendingCount
    ? `${pendingCount} 处修改已暂存 · 当前成片尚未改变`
    : readyCount === requests.length
    ? readyCount + ' 个章节已就绪 · ' + videoRows.length
      + ' 段视频 + ' + imageRows.length + ' 段动态图片'
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
      upload: row.activeUpload?.media_id || '',
      pending: row.pendingEdit?.media_id ?? null,
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
        ? row.displayUpload
          ? uploadedMediaUrl(job, row.displayUpload)
          : shotMediaUrl(job, row.request, row.currentVersion || {
            version_id: '', asset: row.currentAsset,
          })
        : '';
      const frameUrl = row.displayUpload?.media_kind === 'image'
        ? uploadedMediaUrl(job, row.displayUpload)
        : frameCandidateMediaUrl(job, row.currentFrame);
      const versionNumber = row.currentVersion?.version || 1;
      const selected = state.selectedShotId === row.request.request_id;
      const placeholderClass = row.status.className === 'failed'
        ? 'failed'
        : row.status.className === 'waiting' ? 'waiting' : '';
      return `<article class="shot-card ${selected ? 'selected' : ''} ${row.pendingEdit ? 'pending' : ''}" data-shot-id="${escapeHtml(row.request.request_id)}" role="listitem" tabindex="0" aria-label="镜头 ${row.index + 1}，${escapeHtml(row.status.label)}">
        <div class="shot-preview">
          <span class="shot-number">${String(row.index + 1).padStart(2, '0')}</span>
          <span class="shot-version-badge">${row.pendingEdit ? `待应用 · ${row.displayUpload ? `自有${row.isImage ? '图片' : '视频'}` : 'AI 素材'}` : row.activeUpload ? `自有${row.isImage ? '图片' : '视频'}` : preGeneration ? `AI ${row.request.visual_type === 'image' ? '图片' : '视频'} · 待生成` : row.isImage ? '动态图片' : `v${versionNumber}${row.versions.length > 1 ? ` · ${row.versions.length} 版` : ''}`}</span>
          ${mediaUrl
            ? `<video src="${mediaUrl}" muted loop playsinline preload="metadata" aria-label="镜头 ${row.index + 1} 预览"></video>`
            : frameUrl
              ? `<img class="shot-frame" src="${frameUrl}" alt="镜头 ${row.index + 1} 首帧预览">`
            : `<div class="shot-placeholder ${placeholderClass}">${escapeHtml(row.status.label)}</div>`}
        </div>
        <div class="shot-copy">
          <strong>镜头 ${row.index + 1} · ${row.displayUpload ? '自有' : 'AI '}${row.isImage ? '图片' : '视频'}</strong>
          <p>${escapeHtml(shotDescription(job, row.request))}</p>
          <span class="shot-status ${escapeHtml(row.status.className)}">${escapeHtml(row.status.label)}</span>
        </div>
      </article>`;
    }).join('');
  }
  const pendingBar = $('#pending-shot-media-bar');
  const canApplyPending = canEditResource(job)
    && job.state === 'final_review_required'
    && !activeJobTaskRunning(job)
    && !state.busy;
  pendingBar.hidden = pendingCount === 0;
  if (pendingCount) {
    $('#pending-shot-media-title').textContent = `${pendingCount} 处镜头修改待应用`;
    $('#pending-shot-media-detail').textContent = '素材均已准备；继续替换不会重复渲染，最后只需生成一次成片。';
    $('#apply-pending-shot-media-button').textContent = `一次应用 ${pendingCount} 处修改并重新生成成片`;
    $('#apply-pending-shot-media-button').disabled = !canApplyPending;
    $('#discard-pending-shot-media-button').disabled = !canApplyPending;
  }
  const preGenerationBar = $('#pre-generation-media-bar');
  const canConfirmMedia = canEditResource(job)
    && preGeneration
    && !activeJobTaskRunning(job)
    && !state.busy;
  preGenerationBar.hidden = !preGeneration;
  if (preGeneration) {
    const aiVideoCount = rows.filter(
      (row) => !row.activeUpload && row.request.visual_type === 'video',
    ).length;
    const aiImageCount = aiMediaCount - aiVideoCount;
    $('#pre-generation-media-title').textContent = `${ownMediaCount} 个自有素材 · ${aiMediaCount} 个 AI 镜头`;
    $('#pre-generation-media-detail').textContent = aiMediaCount
      ? `确认后只生成 ${aiVideoCount} 段 AI 视频和 ${aiImageCount} 张 AI 图片；已上传镜头不产生对应画面费用。`
      : '所有镜头都已使用自有素材；确认后跳过 AI 画面生成，直接合成一次成片。';
    $('#confirm-pre-generation-media-button').textContent = aiMediaCount
      ? `确认素材并生成剩余 ${aiMediaCount} 个 AI 画面`
      : '确认素材并直接合成成片';
    $('#confirm-pre-generation-media-button').disabled = !canConfirmMedia;
  }
  renderShotInspector(job);
}

function jobVisualChapterCounts(job) {
  const shots = job?.storyboard_plan?.shots || [];
  if (shots.length) {
    const videoCount = shots.filter((shot) => shot.visual_type === 'video').length;
    return {
      videoCount,
      imageCount: shots.length - videoCount,
      total: shots.length,
    };
  }
  if ((job?.visual_requests || []).length && !job?.generation_settings) {
    return {
      videoCount: job.visual_requests.length,
      imageCount: 0,
      total: job.visual_requests.length,
    };
  }
  const imageCount = normalizedImageCount(
    job?.generation_settings?.image_count
      ?? (Number(job?.generation_settings?.shot_count)
        ? Number(job.generation_settings.shot_count) - 3
        : 2),
    2,
  );
  return {videoCount: 3, imageCount, total: imageCount + 3};
}

function usageRecordCostCny(record) {
  const hasReported = record?.reported_cost !== null
    && record?.reported_cost !== undefined
    && record?.reported_cost !== '';
  const hasEstimated = record?.estimated_cost !== null
    && record?.estimated_cost !== undefined
    && record?.estimated_cost !== '';
  const reported = hasReported ? Number(record.reported_cost) : Number.NaN;
  const estimated = hasEstimated ? Number(record.estimated_cost) : Number.NaN;
  const amount = Number.isFinite(reported)
    ? reported
    : Number.isFinite(estimated) ? estimated : null;
  const currency = Number.isFinite(reported)
    ? record?.reported_currency
    : record?.estimated_currency;
  if (amount === null || amount < 0) return null;
  if (currency === 'CNY') return amount;
  if (currency === 'USD') return amount * usdToCnyRate();
  return null;
}

function renderScriptCostEstimate(job) {
  const totalNode = $('#script-cost-total');
  const breakdownNode = $('#script-cost-breakdown');
  const basisNode = $('#script-cost-basis');
  if (!totalNode || !breakdownNode || !basisNode || !job) return;
  const records = job.usage_records || [];
  const knownCosts = records.map(usageRecordCostCny).filter((value) => value !== null);
  const incurred = knownCosts.reduce((total, value) => total + value, 0);
  const unpricedCount = records.length - knownCosts.length;
  const narration = editorNarrationText()
    || scriptBeats(job.script).map((beat) => beat.narration).join('');
  const characterCount = narrationCharacterCount(narration);
  const ttsRate = Math.max(
    0,
    Number(state.capabilities?.tts_pricing?.yuan_per_10000_characters) || 0,
  );
  const ttsCost = characterCount * ttsRate / 10000;
  const counts = jobVisualChapterCounts(job);
  const reusesVisuals = (job.visual_requests || []).length > 0;
  const imageRate = Math.max(
    0,
    Number(state.capabilities?.seedream_pricing?.yuan_per_image) || 0,
  );
  const imageCost = reusesVisuals ? 0 : counts.total * imageRate;
  const modelId = job.generation_settings?.seedance_model || defaultSeedanceModel();
  const requestBase = {resolution: jobResolution(job)};
  const videoLow = reusesVisuals ? 0 : counts.videoCount * (
    estimatedSeedanceCost({...requestBase, duration_seconds: 8}, modelId) || 0
  );
  const videoHigh = reusesVisuals ? 0 : counts.videoCount * (
    estimatedSeedanceCost({...requestBase, duration_seconds: 10}, modelId) || 0
  );
  const low = incurred + ttsCost + imageCost + videoLow;
  const high = incurred + ttsCost + imageCost + videoHigh;
  totalNode.textContent = Math.abs(high - low) < 0.005
    ? `约 ${formatCny(high)}`
    : `约 ${formatCny(low)}–${formatCny(high)}`;
  breakdownNode.textContent = [
    `已发生且可计价 ${formatCny(incurred)}`,
    `完整旁白 ${characterCount} 字约 ${formatCny(ttsCost)}`,
    reusesVisuals
      ? '现有画面直接复用'
      : `${counts.total} 张 Seedream 约 ${formatCny(imageCost)}`,
    reusesVisuals
      ? ''
      : `${counts.videoCount} 段 Seedance（每段 8–10 秒）约 ${formatCny(videoLow)}–${formatCny(videoHigh)}`,
  ].filter(Boolean).join(' · ');
  basisNode.textContent = [
    '这是整单刊例价区间，不是新增确认门槛；最终按真实旁白、视频时长和供应商账单核算。',
    $('#prepare-media-first')?.checked
      ? '你已选择先安排自有素材，上传后未生成的 AI 画面会从实际费用中扣除。'
      : '',
    unpricedCount ? `${unpricedCount} 条调用没有可用价格，未计入区间。` : '',
  ].filter(Boolean).join(' ');
}

function workflowCopy(job, current) {
  const task = taskForJob(job);
  const chapterCounts = jobVisualChapterCounts(job);
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
    return {
      current: '等待你检查并确认脚本',
      next: '确认后自动生成旁白、' + chapterCounts.total + ' 张首帧、'
        + chapterCounts.videoCount + ' 段视频和 '
        + chapterCounts.imageCount + ' 段动态图片',
    };
  }
  if (job.state === 'media_review_required') {
    const shots = job.storyboard_plan?.shots || [];
    const ownCount = shots.filter((shot) => shot.selected_media_id).length;
    const aiCount = Math.max(0, shots.length - ownCount);
    return {
      current: `等待你安排镜头素材 · 已选 ${ownCount} 个自有素材`,
      next: aiCount
        ? `确认后只生成剩余 ${aiCount} 个 AI 画面，并渲染一次成片`
        : '确认后跳过 AI 画面生成，直接渲染一次成片',
    };
  }
  if (job.state === 'final_review_required') {
    return {current: '等待你预览并确认成片', next: '确认后自动生成可下载的发布包'};
  }
  if (job.state === 'packaged') {
    return {current: '发布包已完成', next: '下载产物并手动发布到抖音'};
  }
  const fallbackCurrent = [
    '正在研究资料并生成脚本',
    '等待你确认脚本',
    '正在生成旁白、' + chapterCounts.total + ' 个画面章节并合成',
    '等待你确认成片',
    '正在生成发布包',
  ][current];
  const next = [
    '你检查并确认脚本',
    '系统自动完成旁白、画面与合成',
    '你预览并确认成片',
    '系统自动生成发布包',
    '下载发布包',
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

function renderResearchBrief(job) {
  const node = $('#person-research-brief');
  const brief = job?.research_brief;
  const warning = String(job?.research_warning || '').trim();
  node.hidden = !brief && !warning;
  if (node.hidden) {
    node.innerHTML = '';
    return;
  }
  if (!brief) {
    node.innerHTML = [
      '<div class="research-brief-heading"><div><strong>自动研究已降级</strong>',
      '<span>不阻断本次创作</span></div></div>',
      '<p class="research-warning">' + escapeHtml(warning) + '</p>',
    ].join('');
    return;
  }

  const list = (values) => (
    '<ul>' + (Array.isArray(values) ? values : []).map(
      (value) => '<li>' + escapeHtml(value) + '</li>',
    ).join('') + '</ul>'
  );
  const evidence = Array.isArray(brief.evidence) ? brief.evidence : [];
  const isNews = brief.kind === 'recent_news';
  const evidenceRows = evidence.map((item) => {
    const timing = [
      item.event_at ? `事件 ${formatDateTime(item.event_at)}` : '',
      item.published_at ? `发布 ${formatDateTime(item.published_at)}` : '',
    ].filter(Boolean).join(' · ');
    return [
      '<li><a href="' + escapeHtml(item.source_url || '') + '" target="_blank" rel="noopener noreferrer">',
      escapeHtml(item.source_title || item.source_url || '研究来源'),
      '</a><span>' + escapeHtml(item.claim || '') + '</span>',
      timing ? '<small>' + escapeHtml(timing) + '</small>' : '',
      '</li>',
    ].join('');
  }).join('');
  const uncertainties = Array.isArray(brief.uncertainties)
    && brief.uncertainties.length
    ? '<div class="research-uncertainties"><strong>需保留的边界</strong>'
      + list(brief.uncertainties) + '</div>'
    : '';
  const attributionLabels = {
    verified: '已核验',
    partially_supported: '部分支持',
    unverified: '尚未核验',
    not_applicable: '不适用',
  };
  const inputTypeLabels = {
    attributed_quote: '候选人物引语',
    paraphrased_viewpoint: '人物观点转述',
    conceptual_claim: '观点命题',
    unknown: '待确认输入',
  };
  const personResearchRows = isNews ? '' : [
    '<article><span>输入判断</span><p>'
      + escapeHtml(inputTypeLabels[brief.input_type] || '待确认输入')
      + (brief.research_focus ? ' · ' + escapeHtml(brief.research_focus) : '')
      + '</p></article>',
    '<article><span>出处核验 · '
      + escapeHtml(attributionLabels[brief.attribution_status] || '尚未核验')
      + '</span><p>' + escapeHtml(brief.attribution_note || '未返回额外出处说明') + '</p></article>',
    brief.verified_wording
      ? '<article><span>可靠原文</span><p>' + escapeHtml(brief.verified_wording) + '</p></article>'
      : '',
    brief.source_context
      ? '<article><span>原始语境</span><p>' + escapeHtml(brief.source_context) + '</p></article>'
      : '',
  ].join('');
  node.innerHTML = [
    '<div class="research-brief-heading"><div><strong>'
      + (isNews ? '最新新闻研究简报' : 'H3 输入驱动研究简报') + '</strong>',
    '<span>原始输入已冻结 · 有可追溯来源 · 已用于本次脚本'
      + (isNews && brief.as_of ? ' · 截至 ' + escapeHtml(formatDateTime(brief.as_of)) : '')
      + '</span></div>',
    '<small>' + escapeHtml(brief.model_id || '') + '</small></div>',
    '<p class="research-summary">' + escapeHtml(brief.summary || '') + '</p>',
    '<div class="research-brief-grid">',
    personResearchRows,
    '<article><span>最值得讲的张力</span><p>' + escapeHtml(brief.core_tension || '') + '</p></article>',
    '<article><span>受众现实关联</span>' + list(brief.audience_relevance) + '</article>',
    '<article><span>可展开角度</span>' + list(brief.content_angles) + '</article>',
    '<article><span>互动切口</span><p>' + escapeHtml(brief.interaction_opportunity || '—') + '</p></article>',
    '</div>',
    '<details class="research-evidence"><summary>查看 ' + evidence.length + ' 条研究证据与边界</summary>',
    '<ol>' + evidenceRows + '</ol>' + uncertainties + '</details>',
    warning ? '<p class="research-warning">' + escapeHtml(warning) + '</p>' : '',
  ].join('');
}

function focusFirstNarration() {
  document.querySelector('[data-script-field="narration"]')?.focus();
}

function renderDouyinPerformance(job) {
  const section = $('#douyin-performance-section');
  const packaged = job.state === 'packaged';
  section.hidden = !packaged;
  if (!packaged) return;

  const performance = job.douyin_performance;
  const analysis = job.douyin_performance_analysis || {};
  const snapshots = Array.isArray(performance?.snapshots)
    ? performance.snapshots
    : [];
  const latestSnapshot = snapshots[snapshots.length - 1] || {};
  const metricValue = (key) => (
    analysis[key] !== null && analysis[key] !== undefined
      ? analysis[key]
      : latestSnapshot[key]
  );
  const capability = state.capabilities?.douyin_performance || {};
  const unitCost = Number(capability.estimated_cny_per_success);
  const costLabel = Number.isFinite(unitCost) && unitCost > 0
    ? formatCny(unitCost)
    : '金额待账单';
  const shortLinkCost = Number(capability.short_link_estimated_cny);
  const shortLinkCostLabel = Number.isFinite(shortLinkCost) && shortLinkCost > 0
    ? formatCny(shortLinkCost)
    : '金额待账单';
  const form = $('#douyin-performance-form');
  const input = $('#douyin-link-input');
  form.hidden = !!performance;
  $('#douyin-change-link-button').hidden = !performance;
  if (document.activeElement !== input) {
    input.value = performance?.video_url || '';
  }
  $('#douyin-bind-button').textContent = performance
    ? '更新链接并读取作品数据（约 ' + costLabel + ' 起）'
    : '绑定并读取作品数据（约 ' + costLabel + ' 起）';
  const refreshButton = $('#douyin-refresh-button');
  refreshButton.hidden = !performance;
  refreshButton.dataset.defaultLabel = '手动刷新作品数据（约 ' + costLabel + '）';
  renderDouyinRefreshFeedback(job);
  $('#douyin-refresh-hint').textContent = (
    '播放、点赞、评论、分享和收藏不会自动更新；需要最新数据时请点击按钮。'
    + '每次刷新会发起 1 次 TikHub 星图请求，预计成本 ' + costLabel + '。'
  );
  $('#douyin-cost-note').textContent = [
    '刷新已绑定作品只发起 1 次 TikHub 请求，成功请求规划成本约 ' + costLabel + '，点击按钮即确认本次费用。',
    '首次绑定完整作品链接约 ' + costLabel + '；抖音短链接还需 1 次解析请求，最多约 ' + shortLinkCostLabel + '。',
    '播放量采用星图总播放口径，包含可能的投流播放；互动指标来自同一次完整指标响应。',
    'ROI 按每千次播放 ¥10、目标 10 倍计算；读取费用计入该视频成本。',
    '不含未绑定到本视频的选题研究公摊。',
  ].join(' ');

  const result = $('#douyin-performance-result');
  result.hidden = !performance;
  if (performance) {
    $('#douyin-video-title').textContent = performance.video_title || '已绑定抖音作品';
    $('#douyin-video-author').textContent = performance.author_name
      ? '作者：' + performance.author_name
      : '作品 ID：' + performance.video_id;
    $('#douyin-video-link').href = performance.video_url;
    $('#douyin-play-count').textContent = formatInteger(metricValue('play_count'));
    $('#douyin-like-count').textContent = formatInteger(metricValue('like_count'), '未返回');
    $('#douyin-comment-count').textContent = formatInteger(metricValue('comment_count'), '未返回');
    $('#douyin-share-count').textContent = formatInteger(metricValue('share_count'), '未返回');
    $('#douyin-collect-count').textContent = formatInteger(metricValue('collect_count'), '未返回');
    $('#douyin-accounted-cost').textContent = formatCny(analysis.accounted_cost_cny);
    $('#douyin-playback-value').textContent = formatCny(analysis.playback_value_cny);
    $('#douyin-roi').textContent = (
      analysis.roi_multiple !== null
      && analysis.roi_multiple !== undefined
      && Number.isFinite(Number(analysis.roi_multiple))
    )
      ? Number(analysis.roi_multiple).toFixed(2) + '×'
      : '—';
    $('#douyin-target-views').textContent = formatInteger(analysis.target_views);
    const remaining = $('#douyin-remaining-views');
    remaining.textContent = analysis.target_achieved
      ? '已达到 10 倍'
      : formatInteger(analysis.remaining_views);
    remaining.classList.toggle('target-met', !!analysis.target_achieved);
    const observedAt = analysis.observed_at || latestSnapshot.observed_at;
    const snapshotCount = analysis.snapshot_count ?? snapshots.length;
    $('#douyin-observed-at').textContent = observedAt
      ? '更新于 ' + formatDateTime(observedAt) + ' · 已记录 ' + formatInteger(snapshotCount, '0') + ' 次'
      : '尚未读取作品数据';
  }

  const warnings = [];
  if (capability.ready === false) {
    warnings.push('效果回流待配置：' + ((capability.missing_configuration || []).join('、') || 'TikHub 配置不完整'));
  }
  if (analysis.cost_complete === false) {
    warnings.push('当前有 ' + formatInteger(analysis.unpriced_event_count, '0') + ' 笔费用待对账，成本与 10 倍目标为暂估值');
  }
  const warning = $('#douyin-cost-warning');
  warning.hidden = warnings.length === 0;
  warning.textContent = warnings.join('；');
}

function applyJobReadOnly(job) {
  if (!job) return;
  const readOnly = !canEditResource(job);
  const shotTaskRunning = activeJobTaskRunning(job);
  const mediaPlanning = job.state === 'media_review_required';
  const mediaControlsDisabled = readOnly
    || state.busy
    || !['media_review_required', 'final_review_required'].includes(job.state)
    || shotTaskRunning;
  const aiControlsDisabled = mediaControlsDisabled
    || job.state !== 'final_review_required';
  document.querySelectorAll([
    '#script-review input',
    '#script-review textarea',
    '#shot-inspector input',
    '#shot-inspector textarea',
    '#douyin-link-input',
  ].join(',')).forEach((node) => { node.readOnly = readOnly; });
  document.querySelectorAll('#shot-inspector select').forEach((node) => {
    node.disabled = aiControlsDisabled;
  });
  document.querySelectorAll('#shot-inspector [data-shot-upload]').forEach((node) => {
    node.disabled = mediaControlsDisabled;
  });
  document.querySelectorAll('#script-review select').forEach((node) => {
    node.disabled = readOnly || state.busy;
  });
  document.querySelectorAll([
    '#save-script-button',
    '#approve-script-button',
    '#retry-button',
    '#retry-research-button',
    '#revise-script-button',
    '#preview-tts-button',
  ].join(',')).forEach((node) => {
    node.disabled = readOnly || state.busy;
  });
  $('#prepare-media-first').disabled = readOnly || state.busy;
  const performanceUnavailable = (
    state.capabilities?.douyin_performance?.ready !== true
  );
  $('#douyin-bind-button').disabled = (
    readOnly || state.busy || performanceUnavailable
  );
  $('#douyin-change-link-button').disabled = readOnly || state.busy;
  // Keep refresh actionable so permission or Provider errors can be explained
  // beside the button. The server remains the source of truth and blocks the
  // paid call before it happens when the actor or configuration is invalid.
  $('#douyin-refresh-button').disabled = state.busy;
  document.querySelectorAll([
    '[data-regenerate-shot]',
    '[data-select-version]',
  ].join(',')).forEach((node) => { node.disabled = aiControlsDisabled; });
  document.querySelectorAll([
    '[data-select-upload]',
    '[data-restore-generated]',
    '[data-discard-pending-media]',
  ].join(',')).forEach((node) => { node.disabled = mediaControlsDisabled; });
  const pendingCount = (job.pending_shot_media_edits || []).length;
  const pendingActionsDisabled = mediaControlsDisabled
    || job.state !== 'final_review_required'
    || pendingCount === 0;
  $('#apply-pending-shot-media-button').disabled = pendingActionsDisabled;
  $('#discard-pending-shot-media-button').disabled = pendingActionsDisabled;
  $('#confirm-pre-generation-media-button').disabled = (
    mediaControlsDisabled || !mediaPlanning
  );
  $('#approve-final-button').disabled = readOnly
    || state.busy
    || shotTaskRunning
    || pendingCount > 0
    || job.state !== 'final_review_required';
}

function jobGenerationInputCard(label, responsibility, snapshot, legacyName) {
  const frozen = !!snapshot;
  const name = snapshot?.display_name || legacyName;
  const status = frozen
    ? 'v' + (snapshot.version || '未知') + ' · 已随任务冻结'
    : '兼容模式 · 未使用新版快照';
  return [
    '<article class="job-generation-method">',
    '<span>' + escapeHtml(label) + '</span>',
    '<strong>' + escapeHtml(name) + '</strong>',
    '<p>' + escapeHtml(responsibility) + '</p>',
    '<small>' + escapeHtml(status) + '</small>',
    '</article>',
  ].join('');
}

function renderJobGenerationMethods(job) {
  const profile = job.prompt_writing_profile_snapshot;
  const hasReference = (job.source_card_snapshot?.reference_assets || []).length > 0;
  const profileName = profile?.display_name || '旧版提示词逻辑';
  const profileStatus = profile
    ? 'v' + (profile.version || '未知') + ' · 已随任务冻结'
    : '兼容模式 · 未使用新版快照';
  const profileDescription = profile?.description
    || '该任务创建于 H3 编排快照启用前，继续使用历史提示词逻辑。';
  $('#job-generation-methods').innerHTML = [
    '<summary>',
    '<span><strong>本任务的生成方法</strong><small>输入与版本已冻结，重试不会漂移</small></span>',
    '<span>查看 H3 架构</span>',
    '</summary>',
    '<div class="job-generation-methods-body">',
    '<div class="job-orchestration-heading">',
    '<div><span>本任务的生成架构</span><strong>输入已冻结，重试不会漂移方法</strong></div>',
    '<small>' + escapeHtml(hasReference ? '参考图优先级已启用' : '未使用参考图') + '</small>',
    '</div>',
    '<div class="job-orchestration-inputs">',
    jobGenerationInputCard(
      '输入 01 · 内容约束',
      '冻结事实依据、叙事目标与视觉禁区',
      job.skill_snapshot,
      '旧版内容流程',
    ),
    jobGenerationInputCard(
      '输入 02 · 视觉语言',
      '只补全画风、材质、色彩与造型',
      job.visual_style_snapshot,
      '旧版视觉逻辑',
    ),
    '</div>',
    '<div class="job-orchestration-merge" aria-hidden="true"><span></span><strong>交给编排层</strong><span></span></div>',
    '<article class="job-orchestration-core' + (profile ? '' : ' legacy') + '">',
    '<div><span>' + escapeHtml(profile ? '任务冻结的唯一编排层' : '历史兼容编排') + '</span>',
    '<strong>' + escapeHtml(profileName) + '</strong>',
    '<small>' + escapeHtml(profileStatus) + '</small></div>',
    '<p>' + escapeHtml(profileDescription) + '</p>',
    '<ol aria-label="本任务提示词冲突优先级">',
    '<li>事实与安全</li><li>镜头语义</li>',
    '<li>' + escapeHtml(hasReference ? '参考图属性' : '无参考图') + '</li>',
    '<li>视觉风格补全</li><li>Provider 语法</li>',
    '</ol>',
    '</article>',
    '<div class="job-orchestration-outputs">',
    '<span>最终产物</span>',
    '<div><strong>分镜语义</strong><strong>首帧提示词</strong><strong>I2V 动作提示词</strong></div>',
    '</div>',
    '</div>',
  ].join('');
}

function renderDetail() {
  const job = state.selectedJob;
  const detail = $('#job-detail');
  detail.hidden = !job;
  if (!job) return;
  $('#job-title').textContent = job.source_card_snapshot?.title || '视频详情';
  $('#job-state').textContent = stateLabels[job.state] || job.state;
  const accessNote = $('#job-access-note');
  accessNote.hidden = canEditResource(job);
  accessNote.textContent = canEditResource(job)
    ? ''
    : `这是 ${job.created_by || '其他同事'} 创建的团队内容，你可以查看和下载，但不能修改、重试或继续产生费用。`;
  renderJobGenerationMethods(job);
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
  $('#stage-elapsed').textContent = `总 ${elapsedText(task)} · 当前 ${phaseElapsedText(task)}`;
  $('#next-action').textContent = copy.next;
  renderSeedanceUsage(job);
  renderShotStoryboard(job);
  const error = $('#job-error');
  error.hidden = !job.error;
  error.textContent = job.error || '任务失败';
  const narrationFailure = isNarrationRevisionFailure(job);
  const newsResearchFailure = isNewsResearchFailure(job);
  const reviseButton = $('#revise-script-button');
  reviseButton.hidden = !narrationFailure;
  reviseButton.textContent = (job.visual_requests || []).length
    ? '返回修改脚本（复用现有画面）'
    : '返回修改脚本';
  $('#retry-button').hidden = (
    job.state !== 'failed' || narrationFailure || newsResearchFailure
  );
  const retryResearchButton = $('#retry-research-button');
  retryResearchButton.hidden = !newsResearchFailure;
  const researchRetryDetail = $('#research-retry-detail');
  const diagnostics = job.research_diagnostics || {};
  const rejectionLabels = {
    invalid_item: '无效证据结构',
    invalid_url: '无效 URL',
    citation_not_matched: '未匹配检索注释',
    missing_claim: '缺少事实描述',
    duplicate_url: '重复 URL',
    url_too_long: 'URL 过长',
    missing_host: '缺少站点',
  };
  const rejected = Object.entries(diagnostics.rejected_counts || {})
    .filter(([, count]) => Number(count) > 0)
    .map(([reason, count]) => `${rejectionLabels[reason] || reason} ${count}`)
    .join('、');
  const webSearchDetail = diagnostics.web_search_requests == null
    ? '实际联网检索次数：供应商未回传'
    : `实际联网检索 ${Number(diagnostics.web_search_requests)} 次`;
  researchRetryDetail.hidden = !newsResearchFailure;
  researchRetryDetail.textContent = newsResearchFailure
    ? [
      `第 ${Number(diagnostics.attempt_count || 1)} 次研究未形成可用简报`,
      webSearchDetail,
      `检索注释 ${Number(diagnostics.citation_count || 0)} 条`,
      `候选证据 ${Number(diagnostics.candidate_evidence_count || 0)} 条`,
      `引用 URL 已匹配 ${Number(diagnostics.matched_citation_count || 0)} 条`,
      `通过完整性校验 ${Number(diagnostics.accepted_evidence_count || 0)} 条`,
      diagnostics.accepted_timed_evidence_count == null
        ? ''
        : `带事件或发布时间 ${Number(diagnostics.accepted_timed_evidence_count)} 条`,
      Number(diagnostics.citation_excerpt_claim_count || 0) > 0
        ? `由检索摘录补全事实描述 ${Number(diagnostics.citation_excerpt_claim_count)} 条`
        : '',
      `可追溯站点 ${Number(diagnostics.accepted_site_count || 0)} 个`,
      (diagnostics.unexpected_response_fields || []).length
        ? `已隔离上游额外字段：${diagnostics.unexpected_response_fields.join('、')}`
        : '',
      (diagnostics.validation_errors || []).length
        ? `字段校验：${diagnostics.validation_errors.join('；')}`
        : '',
      rejected ? `被过滤：${rejected}` : '',
      diagnostics.detail || '',
    ].filter(Boolean).join(' · ')
    : '';

  const scriptSection = $('#script-review');
  scriptSection.hidden = job.state !== 'script_review_required';
  if (!scriptSection.hidden && job.script) {
    setJobTtsFields(job);
    renderResearchBrief(job);
    renderScriptDocument(job);
    const ttsSettings = job.generation_settings || {
      ...generationDefaults(),
      tts_speed_ratio: 1.0,
    };
    $('#script-review-spec').textContent = [
      job.skill_snapshot?.display_name || '旧版工作流',
      job.visual_style_snapshot?.display_name || '旧版视觉设定',
      job.prompt_writing_profile_snapshot?.display_name || '旧版提示词方法',
      `本任务 ${jobResolution(job).toUpperCase()}`,
      ttsVoiceLabel(ttsSettings.tts_voice_id),
      `${normalizedTtsSpeedRatio(ttsSettings.tts_speed_ratio).toFixed(1)}x`,
      '确认后开始配音与画面生成',
    ].join(' · ');
    const mediaChoice = $('#prepare-media-first');
    if (mediaChoice.dataset.jobId !== job.id) {
      mediaChoice.dataset.jobId = job.id;
      mediaChoice.checked = false;
    }
    updateScriptApprovalAction();
    renderScriptCostEstimate(job);
  }
  if (
    scriptSection.hidden
    && (state.ttsPreviewUrl || state.ttsPreviewCache.size)
  ) clearTtsPreview();

  const producing = ['script_generating', 'script_approved', 'producing', 'quality_checking', 'final_approved'].includes(job.state);
  $('#production-progress').hidden = !producing;
  if (producing) {
    const percent = Number(task?.progress_meta?.percent ?? [8, 20, 60, 90, 96][current] ?? 3);
    const boundedPercent = Math.max(3, Math.min(100, percent));
    $('#task-progress').textContent = task?.progress || copy.current || '后台任务运行中…';
    $('#task-progress-bar').style.width = `${boundedPercent}%`;
    $('#task-progress-meter').setAttribute('aria-valuenow', String(Math.round(boundedPercent)));
  }
  const shotEditing = task?.progress_meta?.workflow === 'shot_edit'
    && activeJobTaskRunning(job);
  const pendingMediaCount = (job.pending_shot_media_edits || []).length;
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
    $('#approve-final-button').disabled = state.busy
      || shotEditing
      || pendingMediaCount > 0
      || job.state !== 'final_review_required';
    $('#approve-final-button').textContent = pendingMediaCount
      ? `先应用或撤销 ${pendingMediaCount} 处镜头修改`
      : shotEditing ? '镜头更新后再确认' : '确认成片包并生成发布版本';
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
  renderDouyinPerformance(job);
  applyJobReadOnly(job);
}

async function loadAll({selectJobId = ''} = {}) {
  const [cards, listedJobs] = await Promise.all([api('GET', '/source-cards'), api('GET', '/jobs')]);
  const jobs = [...listedJobs];
  if (selectJobId && !jobs.some((item) => item.id === selectJobId)) {
    try {
      const linkedJob = await api('GET', `/jobs/${encodeURIComponent(selectJobId)}`);
      jobs.unshift(linkedJob);
    } catch {
      // The list remains usable when a stale or inaccessible deep link is opened.
    }
  }
  state.cards = cards;
  state.jobs = jobs;
  const previousJobId = state.selectedJob?.id || '';
  const targetId = selectJobId || state.selectedJob?.id;
  state.selectedJob = targetId ? jobs.find((item) => item.id === targetId) || null : jobs[0] || null;
  if (selectJobId && state.selectedJob) switchProductionPane('jobs');
  if (previousJobId !== (state.selectedJob?.id || '')) {
    clearTtsPreview();
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewUploadId = '';
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
  selectContentSkill(DEFAULT_CONTENT_SKILL_ID, {resetPrompts: true});
  persistPromptFields();
  const form = $('#topic-source-form');
  form.reset();
  $('#topic-source-title').value = candidate.title || '';
  $('#topic-handoff-title').textContent = candidate.title || '';
  $('#topic-handoff-angle').textContent = candidate.editorial_angle || '';
  $('#topic-handoff').hidden = false;
  $('#manual-intake-intro').hidden = true;
  $('#source-card-form').hidden = true;
  $('#news-topic-form').hidden = true;
  switchWorkspace('production');
  switchProductionPane('create');
  $('#topic-handoff').scrollIntoView({behavior: 'smooth', block: 'start'});
  form.elements.source_material.focus();
}

function closeTopicHandoff() {
  state.topicHandoffCandidate = null;
  $('#topic-source-form').reset();
  $('#topic-handoff').hidden = true;
  updateContentSkillIntake();
}

document.querySelector('.workspace-tabs').addEventListener('click', (event) => {
  const button = event.target.closest('[data-workspace-tab]');
  if (!button) return;
  switchWorkspace(button.dataset.workspaceTab);
});
document.querySelector('.workspace-tabs').addEventListener('keydown', (event) => {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  event.preventDefault();
  const tab = state.workspaceTab === 'topics' ? 'production' : 'topics';
  switchWorkspace(tab);
  document.querySelector(`[data-workspace-tab="${tab}"]`)?.focus();
});
$('.production-view-tabs').addEventListener('click', (event) => {
  const button = event.target.closest('[data-production-pane]');
  if (button) switchProductionPane(button.dataset.productionPane);
});
$('.production-view-tabs').addEventListener('keydown', (event) => {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
  event.preventDefault();
  const pane = state.productionPane === 'create' ? 'jobs' : 'create';
  switchProductionPane(pane);
  document.querySelector(`[data-production-pane="${pane}"]`)?.focus();
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
  if (!canEditResource(run)) {
    notify('只有创建者可以采用这个候选。', true);
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
  event.preventDefault();
  if (state.busy) return;
  notify(''); setBusy(true);
  const form = new FormData(event.currentTarget);
  try {
    if (form.get('rights_confirmed') !== 'on') throw new Error('请先确认资料已经核对且可以引用');
    const generationSettings = generationSettingsPayload(DEFAULT_CONTENT_SKILL_ID);
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
    clearOneTaskPromptOverride();
    await loadAll({selectJobId: result.job.id});
    setBusy(false);
    await pollTask(result.task_id, result.job.id);
    notify('脚本已生成，请人工确认。');
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#source-card-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.busy) return;
  notify(''); setBusy(true);
  const form = new FormData(event.currentTarget);
  const body = {
    schema_version: '1.0',
    person_name: form.get('person_name'),
    viewpoint: form.get('viewpoint'),
  };
  try {
    const generationSettings = generationSettingsPayload(DEFAULT_CONTENT_SKILL_ID);
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
    clearOneTaskPromptOverride();
    await loadAll({selectJobId: result.job.id});
    setBusy(false);
    await pollTask(result.task_id, result.job.id);
    notify('脚本已生成，请人工确认。');
    return;
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#news-topic-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (state.busy) return;
  notify(''); setBusy(true);
  const form = new FormData(event.currentTarget);
  try {
    const generationSettings = generationSettingsPayload(NEWS_CONTENT_SKILL_ID);
    const card = await api('POST', '/source-cards/news-topic', {
      schema_version: '1.0',
      topic: String(form.get('topic') || '').trim(),
      focus: String(form.get('focus') || '').trim(),
      content_domain: String(form.get('content_domain') || 'technology'),
      target_audience: String(form.get('target_audience') || '').trim()
        || '关注该主题的普通用户',
    });
    let result;
    try {
      result = await api('POST', '/jobs', {
        source_card_id: card.id,
        generation_settings: generationSettings,
      });
    } catch (error) {
      await loadAll();
      throw new Error(`新闻主题已保存，但视频任务未启动：${error.message}`);
    }
    resetSourceForm();
    clearOneTaskPromptOverride();
    await loadAll({selectJobId: result.job.id});
    setBusy(false);
    await pollTask(result.task_id, result.job.id);
    notify('最新新闻研究和脚本已完成，请人工确认。');
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#source-card-list').addEventListener('click', async (event) => {
  const create = event.target.closest('[data-create-job]');
  if (!create) return;
  const card = state.cards.find((item) => item.id === create.dataset.createJob);
  if (!canEditResource(card)) {
    notify('只有创建者可以继续使用这份来源卡。', true);
    return;
  }
  notify(''); setBusy(true);
  try {
    const result = await api('POST', '/jobs', {
      source_card_id: create.dataset.createJob,
      generation_settings: generationSettingsPayload(skillIdForCard(card)),
    });
    clearOneTaskPromptOverride();
    await loadAll({selectJobId: result.job.id});
    setBusy(false); await pollTask(result.task_id, result.job.id); notify('脚本已生成，请人工确认。'); return;
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

function resetSourceForm() {
  $('#source-card-form').reset();
  $('#news-topic-form').reset();
  clearReferenceImage();
  $('#source-submit-button').textContent = '生成脚本';
  updateContentSkillIntake();
}

function clearOneTaskPromptOverride() {
  setCustomScriptPromptMode(false, {resetToDefault: true});
  persistPromptFields();
}

$('#job-list').addEventListener('click', async (event) => {
  const card = event.target.closest('[data-job-id]');
  if (!card) return;
  switchProductionPane('jobs');
  try {
    if (state.selectedJob?.id !== card.dataset.jobId) {
      stopPolling();
      clearTtsPreview();
    }
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewUploadId = '';
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
  const narrationText = script.beats.map((item) => item.narration).join('');
  const previousNarrationText = scriptBeats(job.script)
    .map((item) => item.narration)
    .join('');
  if (narrationText !== previousNarrationText) {
    script.estimated_duration_seconds = Math.max(
      45,
      Math.min(
        75,
        Math.round(
          narrationCharacterCount(narrationText)
          / (4.1 * selectedJobTtsSpeedRatio()),
        ),
      ),
    );
  }
  return script;
}

async function saveScriptEdits(job) {
  const script = editedScript(job);
  const characterCount = narrationCharacterCount(script.beats.map((item) => item.narration).join(''));
  if (characterCount > SCRIPT_HARD_MAX_CHARS) {
    throw new Error(`纯旁白共 ${characterCount} 字，超过技术安全上限 ${SCRIPT_HARD_MAX_CHARS} 字`);
  }
  const ttsVoiceId = normalizedTtsVoiceId($('#job-tts-voice-id').value);
  const ttsSpeedRatio = normalizedTtsSpeedRatio($('#job-tts-speed-ratio').value);
  const currentSettings = job.generation_settings || {
    ...generationDefaults(),
    tts_speed_ratio: 1.0,
  };
  const scriptChanged = JSON.stringify(script) !== JSON.stringify(job.script);
  const voiceChanged = ttsVoiceId !== currentSettings.tts_voice_id;
  const speedChanged = ttsSpeedRatio !== normalizedTtsSpeedRatio(
    currentSettings.tts_speed_ratio,
  );
  if (!scriptChanged && !voiceChanged && !speedChanged) return {job};
  const updated = await api('PUT', `/jobs/${encodeURIComponent(job.id)}/script`, {
    expected_revision: job.revision,
    script,
    tts_voice_id: ttsVoiceId,
    tts_speed_ratio: ttsSpeedRatio,
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
    notify('完整脚本和旁白设置已保存。');
  }
  catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#preview-tts-button').addEventListener('click', async () => {
  const originalJob = state.selectedJob;
  if (!originalJob?.script || !canEditResource(originalJob)) return;
  setBusy(true);
  notify('');
  try {
    const saved = await saveScriptEdits(originalJob);
    updateVisibleJob(saved.job);
    const key = ttsPreviewKey(saved.job);
    const audio = $('#tts-preview-audio');
    const cachedUrl = state.ttsPreviewCache.get(key);
    if (cachedUrl) {
      state.ttsPreviewKey = key;
      state.ttsPreviewUrl = cachedUrl;
      audio.src = cachedUrl;
      audio.hidden = false;
      $('#tts-preview-status').textContent = '正在重播本页已生成的试听，不会重复计费。';
      await audio.play().catch(() => {});
      return;
    }
    const result = await api(
      'POST',
      `/jobs/${encodeURIComponent(saved.job.id)}/narration-preview`,
      {expected_revision: saved.job.revision, confirm_cost: true},
    );
    updateVisibleJob(result.job);
    clearTtsPreview({resetStatus: false, clearCache: false});
    state.ttsPreviewUrl = audioObjectUrl(
      result.audio_base64,
      result.media_type,
    );
    state.ttsPreviewKey = ttsPreviewKey(result.job);
    state.ttsPreviewCache.set(state.ttsPreviewKey, state.ttsPreviewUrl);
    audio.src = state.ttsPreviewUrl;
    audio.hidden = false;
    const cost = formatCny(result.estimated_cost_cny, '金额已记账');
    const duration = Number(result.duration_seconds);
    $('#tts-preview-status').textContent = [
      `已生成：${ttsVoiceLabel(result.job.generation_settings?.tts_voice_id)}`,
      `${normalizedTtsSpeedRatio(result.job.generation_settings?.tts_speed_ratio).toFixed(1)}x`,
      Number.isFinite(duration) && duration > 0 ? `${duration.toFixed(1)} 秒` : '',
      `本次约 ${cost}`,
      '本页重复播放不再调用',
    ].filter(Boolean).join(' · ');
    await audio.play().catch(() => {
      $('#tts-preview-status').textContent += ' · 请点击播放器开始';
    });
  } catch (error) {
    try { await loadAll({selectJobId: originalJob.id}); } catch { /* 保留原错误。 */ }
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

$('#approve-script-button').addEventListener('click', async () => {
  const job = state.selectedJob; if (!job?.script_hash) return;
  const prepareMediaFirst = $('#prepare-media-first').checked;
  setBusy(true); notify('');
  try {
    const saved = await saveScriptEdits(job);
    const latestJob = saved.job;
    const result = await api('POST', `/jobs/${encodeURIComponent(latestJob.id)}/actions/approve-script`, {
      expected_revision: latestJob.revision,
      script_hash: latestJob.script_hash,
      prepare_media_first: prepareMediaFirst,
    });
    await loadAll({selectJobId: job.id});
    setBusy(false);
    await pollTask(result.task_id, job.id);
    notify(
      state.selectedJob?.state === 'media_review_required'
        ? '旁白和文字分镜已就绪，请为需要替换的镜头上传素材。'
        : '真实成片已生成，请检查并确认。',
    );
    return;
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#prepare-media-first').addEventListener('change', () => {
  updateScriptApprovalAction();
  renderScriptCostEstimate(state.selectedJob);
});

$('#script-beat-editor').addEventListener('input', (event) => {
  if (event.target.matches('textarea')) resizeScriptTextarea(event.target);
  if (event.target.dataset.scriptField === 'narration') {
    if (state.ttsPreviewKey || state.ttsPreviewUrl) {
      clearTtsPreview({clearCache: false});
    }
    updateScriptLengthStatus();
  }
});

$('#job-tts-voice-id').addEventListener('change', () => {
  if (state.ttsPreviewKey || state.ttsPreviewUrl) {
    clearTtsPreview({clearCache: false});
  }
});
$('#job-tts-speed-ratio').addEventListener('change', () => {
  if (state.ttsPreviewKey || state.ttsPreviewUrl) {
    clearTtsPreview({clearCache: false});
  }
  updateScriptLengthStatus();
});

$('#shot-grid').addEventListener('click', (event) => {
  const card = event.target.closest('[data-shot-id]');
  if (!card || !state.selectedJob) return;
  state.selectedShotId = card.dataset.shotId;
  state.previewVersionId = '';
  state.previewUploadId = '';
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

$('#shot-inspector').addEventListener('change', (event) => {
  if (event.target.id !== 'shot-seedance-model' || !state.selectedJob) return;
  const request = storyboardRequests(state.selectedJob).find(
    (item) => item.request_id === state.selectedShotId,
  );
  const note = $('#shot-inspector [data-shot-model-cost]');
  if (!request || !note) return;
  const model = seedanceModelInfo(event.target.value);
  const cost = estimatedSeedanceCost(request, event.target.value);
  note.textContent = `本次只会新增 1 次 ${request.duration_seconds} 秒、${request.resolution} ${model?.label || 'Seedance'} 调用${cost === null ? '' : `，刊例价预估约 ¥${cost.toFixed(2)}`}；不会重做旁白、图片章节或其他视频镜头。`;
});

$('#shot-inspector').addEventListener('change', async (event) => {
  const input = event.target.closest('[data-shot-upload]');
  const job = state.selectedJob;
  const preGeneration = job?.state === 'media_review_required';
  const shotId = state.selectedShotId;
  const file = input?.files?.[0];
  if (!input || !job || !shotId || !file) return;
  const mediaKind = input.dataset.mediaKind;
  const extension = file.name.toLowerCase().split('.').pop() || '';
  const validImage = mediaKind === 'image'
    && ['jpg', 'jpeg', 'png', 'webp'].includes(extension);
  const validVideo = mediaKind === 'video'
    && ['mp4', 'mov', 'webm'].includes(extension);
  if (!validImage && !validVideo) {
    notify(
      mediaKind === 'image'
        ? '请选择 JPG、PNG 或 WebP 图片。'
        : '请选择 MP4、MOV 或 WebM 视频。',
      true,
    );
    input.value = '';
    return;
  }
  const maxBytes = mediaKind === 'image' ? 20 * 1024 * 1024 : 200 * 1024 * 1024;
  if (file.size <= 0 || file.size > maxBytes) {
    notify(
      mediaKind === 'image'
        ? '图片必须小于 20 MB。'
        : '视频必须小于 200 MB。',
      true,
    );
    input.value = '';
    return;
  }
  const uploadPath = `/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(shotId)}/media`;
  const previousTaskId = job.last_run_task_id || '';
  let taskMayHaveStarted = false;
  setBusy(true); notify('正在安全校验素材…');
  try {
    const sha256 = await sha256File(file);
    const grant = await api(
      'POST',
      `${uploadPath}/uploads`,
      {
        expected_revision: job.revision,
        original_filename: file.name,
        media_kind: mediaKind,
        size_bytes: file.size,
        sha256,
      },
    );
    let result;
    if (grant.upload_mode === 'direct') {
      notify('正在直传素材… 0%');
      try {
        await uploadFileDirect(grant, file, (percent) => {
          notify(`正在直传素材… ${percent}%`);
        });
      } catch (uploadError) {
        api(
          'POST',
          `${uploadPath}/uploads/cancel`,
          {upload_token: grant.upload_token},
        ).catch(() => {});
        throw uploadError;
      }
      notify('素材已上传，正在确认完整性…');
      taskMayHaveStarted = true;
      result = await api(
        'POST',
        `${uploadPath}/uploads/complete`,
        {upload_token: grant.upload_token},
      );
    } else {
      const body = new FormData();
      body.append('expected_revision', String(job.revision));
      body.append('media', file, file.name);
      taskMayHaveStarted = true;
      result = await apiMultipart(uploadPath, body);
    }
    await loadAll({selectJobId: job.id});
    await pollTask(result.task_id, job.id);
    state.previewUploadId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderDetail();
    notify(preGeneration
      ? `${mediaKind === 'image' ? '图片' : '视频'}已加入素材安排；这个镜头将跳过 AI 画面生成。`
      : `${mediaKind === 'image' ? '图片' : '视频'}已暂存；可继续替换其他镜头，最后一次应用全部修改。`);
  } catch (error) {
    await loadAll({selectJobId: job.id}).catch(() => {});
    const refreshed = state.selectedJob;
    if (
      taskMayHaveStarted
      && refreshed?.id === job.id
      && refreshed.last_run_task_id
      && refreshed.last_run_task_id !== previousTaskId
    ) {
      notify('素材已进入后台校验，正在恢复任务进度…');
      await pollTask(refreshed.last_run_task_id, job.id);
      state.previewUploadId = '';
      state.storyboardKey = '';
      state.inspectorKey = '';
      renderDetail();
      notify(preGeneration
        ? '素材已加入生成前安排；这个镜头将跳过 AI 画面生成。'
        : '素材已暂存；可继续替换其他镜头，最后一次应用全部修改。');
      return;
    }
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

$('#shot-inspector').addEventListener('click', async (event) => {
  const job = state.selectedJob;
  if (!job) return;
  if (event.target.closest('[data-close-inspector]')) {
    state.selectedShotId = '';
    state.previewVersionId = '';
    state.previewUploadId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderShotStoryboard(job);
    return;
  }
  const preview = event.target.closest('[data-preview-version]');
  if (preview) {
    state.previewVersionId = preview.dataset.previewVersion;
    state.previewUploadId = '__generated';
    state.previewFrameCandidateId = '';
    state.inspectorKey = '';
    renderShotInspector(job);
    return;
  }
  if (event.target.closest('[data-preview-generated]')) {
    state.previewUploadId = '__generated';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.inspectorKey = '';
    renderShotInspector(job);
    return;
  }
  const previewUpload = event.target.closest('[data-preview-upload]');
  if (previewUpload) {
    state.previewUploadId = previewUpload.dataset.previewUpload;
    state.previewFrameCandidateId = '';
    state.inspectorKey = '';
    renderShotInspector(job);
    return;
  }
  const frameCandidate = event.target.closest('[data-frame-candidate]');
  if (frameCandidate) {
    state.previewUploadId = '__generated';
    state.previewFrameCandidateId = frameCandidate.dataset.frameCandidate;
    state.inspectorKey = '';
    renderShotInspector(job);
    return;
  }
  if (event.target.closest('[data-discard-pending-media]')) {
    setBusy(true); notify('');
    try {
      await api(
        'POST',
        `/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(state.selectedShotId)}/media/pending/actions/discard`,
        {expected_revision: job.revision},
      );
      await loadAll({selectJobId: job.id});
      state.previewUploadId = '';
      state.previewVersionId = '';
      state.previewFrameCandidateId = '';
      state.storyboardKey = '';
      state.inspectorKey = '';
      renderDetail();
      notify('已撤销这个镜头的待应用修改，当前成片未改变。');
    } catch (error) {
      await loadAll({selectJobId: job.id}).catch(() => {});
      notify(error.message, true);
    } finally {
      setBusy(false);
    }
    return;
  }
  const selectUpload = event.target.closest('[data-select-upload]');
  if (selectUpload) {
    setBusy(true); notify('');
    try {
      await api(
        'POST',
        `/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(state.selectedShotId)}/uploads/${encodeURIComponent(selectUpload.dataset.selectUpload)}/actions/select`,
        {expected_revision: job.revision},
      );
      await loadAll({selectJobId: job.id});
      state.previewUploadId = selectUpload.dataset.selectUpload;
      state.previewVersionId = '';
      state.previewFrameCandidateId = '';
      state.storyboardKey = '';
      state.inspectorKey = '';
      renderDetail();
      notify(job.state === 'media_review_required'
        ? '已选用这个上传版本；对应 AI 画面不会生成。'
        : '上传素材版本已加入待应用修改，可继续调整其他镜头。');
    } catch (error) {
      await loadAll({selectJobId: job.id}).catch(() => {});
      notify(error.message, true);
    } finally {
      setBusy(false);
    }
    return;
  }
  if (event.target.closest('[data-restore-generated]')) {
    setBusy(true); notify('');
    try {
      await api(
        'POST',
        `/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(state.selectedShotId)}/actions/restore-generated-media`,
        {expected_revision: job.revision},
      );
      await loadAll({selectJobId: job.id});
      state.previewUploadId = '__generated';
      state.previewVersionId = '';
      state.previewFrameCandidateId = '';
      state.storyboardKey = '';
      state.inspectorKey = '';
      renderDetail();
      notify(job.state === 'media_review_required'
        ? '这个镜头已改回 AI 生成；上传版本仍保留在素材历史中。'
        : '恢复 AI 素材已加入待应用修改，上传版本仍保留在历史中。');
    } catch (error) {
      await loadAll({selectJobId: job.id}).catch(() => {});
      notify(error.message, true);
    } finally {
      setBusy(false);
    }
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
      state.previewUploadId = '__generated';
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
  const revisionIntent = $('#shot-revision-intent')?.value.trim() || '';
  if (!revisionIntent) { notify('请填写这次想调整的动作或镜头效果。', true); return; }
  const request = storyboardRequests(job).find(
    (item) => item.request_id === state.selectedShotId,
  );
  const seedanceModel = $('#shot-seedance-model')?.value
    || seedanceModelForRequest(job, request);
  const model = seedanceModelInfo(seedanceModel);
  const cost = estimatedSeedanceCost(request, seedanceModel);
  const confirmed = window.confirm(
    `H3 会把这次修改意图重新编译为只读视频提示词，并新增 1 次真实的 ${model?.label || 'Seedance'} 镜头生成费用${cost === null ? '' : `，刊例价预估约 ¥${cost.toFixed(2)}`}。首帧、旁白、图片章节和其他视频镜头不会重做，是否继续？`,
  );
  if (!confirmed) return;
  setBusy(true); notify('');
  try {
    const result = await api(
      'POST',
      `/jobs/${encodeURIComponent(job.id)}/shots/${encodeURIComponent(state.selectedShotId)}/actions/regenerate`,
      {
        expected_revision: job.revision,
        revision_intent: revisionIntent,
        first_frame_candidate_id: state.previewFrameCandidateId || '',
        seedance_model: seedanceModel,
      },
    );
    await loadAll({selectJobId: job.id});
    setBusy(false);
    await pollTask(result.task_id, job.id);
    state.previewVersionId = '';
    state.previewUploadId = '__generated';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderDetail();
    notify('这个镜头的新版本已生成，并已更新到成片。');
    return;
  } catch (error) { notify(error.message, true); }
  finally { setBusy(false); }
});

$('#confirm-pre-generation-media-button').addEventListener('click', async () => {
  const job = state.selectedJob;
  if (
    !job
    || job.state !== 'media_review_required'
    || state.busy
    || !canEditResource(job)
  ) return;
  const shots = job.storyboard_plan?.shots || [];
  const ownMediaCount = shots.filter((shot) => shot.selected_media_id).length;
  const aiMediaCount = Math.max(0, shots.length - ownMediaCount);
  setBusy(true);
  notify(aiMediaCount
    ? `素材已确认，正在生成剩余 ${aiMediaCount} 个 AI 画面并合成一次成片…`
    : '素材已确认，正在跳过 AI 画面生成并直接合成一次成片…');
  try {
    const result = await api(
      'POST',
      `/jobs/${encodeURIComponent(job.id)}/actions/confirm-media-plan`,
      {expected_revision: job.revision},
    );
    await loadAll({selectJobId: job.id});
    setBusy(false);
    await pollTask(result.task_id, job.id);
    state.previewUploadId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderDetail();
    notify(`${ownMediaCount} 个自有素材已直接采用，其余 ${aiMediaCount} 个 AI 画面已生成；首版成片只渲染了 1 次。`);
  } catch (error) {
    await loadAll({selectJobId: job.id}).catch(() => {});
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

$('#apply-pending-shot-media-button').addEventListener('click', async () => {
  const job = state.selectedJob;
  const pendingCount = (job?.pending_shot_media_edits || []).length;
  if (!job || !pendingCount || state.busy || !canEditResource(job)) return;
  setBusy(true); notify(`正在一次应用 ${pendingCount} 处镜头修改…`);
  try {
    const result = await api(
      'POST',
      `/jobs/${encodeURIComponent(job.id)}/shot-media/pending/actions/apply`,
      {expected_revision: job.revision},
    );
    await loadAll({selectJobId: job.id});
    setBusy(false);
    await pollTask(result.task_id, job.id);
    state.previewUploadId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderDetail();
    notify(`${pendingCount} 处镜头修改已一次应用，成片只重新生成了 1 次。`);
  } catch (error) {
    await loadAll({selectJobId: job.id}).catch(() => {});
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

$('#discard-pending-shot-media-button').addEventListener('click', async () => {
  const job = state.selectedJob;
  const pendingCount = (job?.pending_shot_media_edits || []).length;
  if (!job || !pendingCount || state.busy || !canEditResource(job)) return;
  setBusy(true); notify('');
  try {
    await api(
      'POST',
      `/jobs/${encodeURIComponent(job.id)}/shot-media/pending/actions/discard`,
      {expected_revision: job.revision},
    );
    await loadAll({selectJobId: job.id});
    state.previewUploadId = '';
    state.previewVersionId = '';
    state.previewFrameCandidateId = '';
    state.storyboardKey = '';
    state.inspectorKey = '';
    renderDetail();
    notify(`已撤销 ${pendingCount} 处待应用修改；已上传版本仍保留在素材历史中。`);
  } catch (error) {
    await loadAll({selectJobId: job.id}).catch(() => {});
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
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

$('#douyin-change-link-button').addEventListener('click', () => {
  const job = state.selectedJob;
  if (!job?.douyin_performance || state.busy || !canEditResource(job)) return;
  $('#douyin-performance-form').hidden = false;
  $('#douyin-link-input').value = '';
  $('#douyin-link-input').focus();
});

$('#douyin-performance-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const job = state.selectedJob;
  if (!job || job.state !== 'packaged' || state.busy) return;
  if (!canEditResource(job)) {
    notify('只有创建者或管理员可以发起付费的播放量读取。', true);
    return;
  }
  const douyinUrl = $('#douyin-link-input').value.trim();
  if (!douyinUrl) {
    notify('请先粘贴这条视频发布后的抖音作品链接。', true);
    return;
  }
  setBusy(true); notify('');
  try {
    const updated = await api(
      'PUT',
      '/jobs/' + encodeURIComponent(job.id) + '/douyin-performance',
      {
        expected_revision: job.revision,
        douyin_url: douyinUrl,
        confirm_cost: true,
      },
    );
    updateVisibleJob(updated);
    notify('抖音作品已绑定并完成首次读取；以后可点击“手动刷新作品数据”更新五项指标。');
  } catch (error) {
    await loadAll({selectJobId: job.id}).catch(() => {});
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

$('#douyin-refresh-button').addEventListener('click', async () => {
  const job = state.selectedJob;
  if (!job?.douyin_performance || state.busy) return;
  if (!canEditResource(job)) {
    const message = '只有创建者或管理员可以发起付费的作品数据刷新。';
    setDouyinRefreshFeedback(job.id, 'error', message);
    notify(message, true);
    return;
  }
  const startedAt = Date.now();
  setBusy(true);
  setDouyinRefreshFeedback(
    job.id,
    'loading',
    '正在连接 TikHub 并读取最新作品数据，请勿重复点击。',
  );
  notify('');
  try {
    const updated = await api(
      'POST',
      '/jobs/' + encodeURIComponent(job.id) + '/douyin-performance/actions/refresh',
      {expected_revision: job.revision, confirm_cost: true},
    );
    updateVisibleJob(updated);
    const analysis = updated.douyin_performance_analysis || {};
    const elapsedSeconds = Math.max(1, Math.ceil((Date.now() - startedAt) / 1000));
    const successSummary = [
      '刷新成功',
      '播放量 ' + formatInteger(analysis.play_count, '未返回'),
      '点赞 ' + formatInteger(analysis.like_count, '未返回'),
      '评论 ' + formatInteger(analysis.comment_count, '未返回'),
      '分享 ' + formatInteger(analysis.share_count, '未返回'),
      '收藏 ' + formatInteger(analysis.collect_count, '未返回'),
      analysis.observed_at ? '数据时间 ' + formatDateTime(analysis.observed_at) : '',
      '耗时 ' + elapsedSeconds + ' 秒',
    ].filter(Boolean).join(' · ');
    setDouyinRefreshFeedback(job.id, 'success', successSummary);
    notify('抖音作品数据已刷新，本次 TikHub 成本已保存。');
  } catch (error) {
    await loadAll({selectJobId: job.id}).catch(() => {});
    const message = error?.message || '未知错误';
    setDouyinRefreshFeedback(job.id, 'error', '刷新失败：' + message);
    notify(message, true);
  } finally {
    setBusy(false);
  }
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

$('#retry-research-button').addEventListener('click', async () => {
  const job = state.selectedJob; if (!job) return;
  if (!window.confirm('重新研究会产生一次新的 OpenRouter 检索费用，确认继续吗？')) return;
  setBusy(true); notify('');
  try {
    const result = await api(
      'POST',
      `/jobs/${encodeURIComponent(job.id)}/actions/retry-news-research`,
      {expected_revision: job.revision, confirm_cost: true},
    );
    if (result.job) updateVisibleJob(result.job);
    setBusy(false);
    await pollTask(result.task_id, job.id);
    notify('最新新闻已重新研究，脚本已更新。');
    return;
  }
  catch (error) {
    await loadAll({selectJobId: job.id}).catch(() => {});
    notify(error.message, true);
  }
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
$('#enable-custom-script-prompt').addEventListener('change', (event) => {
  setCustomScriptPromptMode(event.target.checked);
});
$('#content-skill').addEventListener('change', () => {
  const skill = selectedContentSkill();
  updateContentSkillIntake();
  setPromptFields(skillGenerationDefaults(skill?.skill_id || ''));
  setCustomScriptPromptMode(false);
  persistPromptFields();
});
$('#visual-style').addEventListener('change', () => {
  updateVisualStyleDescription();
  persistPromptFields();
});
$('#visual-style-previews').addEventListener('click', (event) => {
  const button = event.target.closest('[data-visual-style-id]');
  if (!button) return;
  $('#visual-style').value = button.dataset.visualStyleId;
  updateVisualStyleDescription();
  persistPromptFields();
});
$('#video-resolution').addEventListener('change', () => {
  updateProductionSpecSummary();
  persistPromptFields();
});
$('#tts-voice-id').addEventListener('change', () => {
  updateProductionSpecSummary();
  persistPromptFields();
});
$('#tts-speed-ratio').addEventListener('change', () => {
  updateProductionSpecSummary();
  persistPromptFields();
});
$('#image-count').addEventListener('input', updateImageCountCost);
$('#image-count').addEventListener('change', (event) => {
  event.target.value = normalizedImageCount(event.target.value);
  updateImageCountCost();
  persistPromptFields();
});
$('#restore-prompt-defaults').addEventListener('click', () => {
  setCustomScriptPromptMode(false, {resetToDefault: true});
  notify('已退出自定义模式，下一条任务使用 Content Skill 默认提示词。');
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
async function init() {
  try {
    const initialJobId = new URLSearchParams(window.location.search).get('job') || '';
    switchWorkspace(initialJobId ? 'production' : 'topics');
    switchProductionPane(initialJobId ? 'jobs' : 'create');
    state.capabilities = await api('GET', '/capabilities');
    renderCapabilities();
    initializePromptFields();
    await Promise.all([loadAll({selectJobId: initialJobId}), loadTopicRuns()]);
    resumeSelectedTask().catch((error) => notify(error.message, true));
    const runningTopic = state.topicRuns.find(
      (run) => run.status === 'running'
        && run.created_by === state.capabilities?.actor?.username,
    );
    if (runningTopic) resumeTopicTask(runningTopic).catch((error) => notify(error.message, true));
  }
  catch (error) { notify(error.message, true); $('#system-status').classList.add('warning'); }
}

init();
