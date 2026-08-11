const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'app.js'), 'utf8');
const styles = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'app.css'), 'utf8');
const host = fs.readFileSync(path.join(root, 'main.py'), 'utf8');
const renderer = fs.readFileSync(path.join(root, 'video_renderer', 'src', 'KnowledgeVideoV1.tsx'), 'utf8');
const renderEntry = fs.readFileSync(path.join(root, 'video_renderer', 'render.mjs'), 'utf8');
const login = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'login.html'), 'utf8');
const accountsPage = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'accounts.html'), 'utf8');
const accountsApp = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'accounts.js'), 'utf8');
const costsPage = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'costs.html'), 'utf8');
const costsApp = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'costs.js'), 'utf8');
const costsApi = fs.readFileSync(path.join(root, 'qijia_video', 'cost_api.py'), 'utf8');
const ttsProvider = fs.readFileSync(
  path.join(root, 'qijia_video', 'infrastructure', 'tts_providers.py'),
  'utf8',
);

test('qijia video uses an independent page and API namespace', () => {
  assert.match(page, /齐家 AI 知识视频工作台/);
  assert.match(login, /家庭教育内容工作台/);
  assert.match(app, /const API = '\/api\/qijia-video'/);
  assert.match(host, /RedirectResponse\("\/qijia-video"/);
  assert.match(page, /action="\/logout"/);
  assert.doesNotMatch(page, /返回继续追问/);
  assert.doesNotMatch(page, /x-data=|Alpine/);
  assert.match(login, /action="\/login"/);
  assert.match(login, /autocomplete="current-password"/);
});

test('video creation uses model knowledge without external retrieval', () => {
  assert.match(page, /id="creative-request-form"/);
  assert.match(page, /系统直接生成口播稿 · 不联网 · 不生成前置研究简报/);
  assert.match(page, /精确出处、人物归属、版本、日期和最新动态会在脚本确认时标记为需要复核/);
  assert.match(page, />生成口播稿<\/button>/);
  assert.match(app, /qijia-video-generation-settings-v3/);
  assert.doesNotMatch(page, /id="content-skill"|id="script-skill"|id="director-skill"/);
  assert.doesNotMatch(app, /DEFAULT_CONTENT_SKILL_ID|DEFAULT_SCRIPT_SKILL_ID|DEFAULT_DIRECTOR_SKILL_ID/);
  assert.doesNotMatch(page, /id="news-topic-form"/);
  assert.doesNotMatch(page, /id="retry-research-button"/);
  assert.doesNotMatch(app, /NEWS_CONTENT_SKILL_ID|\/source-cards\/news-topic/);
  assert.doesNotMatch(app, /\/actions\/retry-news-research/);
  assert.doesNotMatch(app, /research_diagnostics|web_search_requests/);
  assert.match(app, /job\.pipeline_version === 'v3'[\s\S]*'直接创作'/);
});

test('creator selects only visual style while internal roles stay server-owned', () => {
  assert.match(page, /id="visual-style"/);
  assert.match(app, /DEFAULT_VISUAL_STYLE_ID = 'content-skill-default'/);
  assert.doesNotMatch(app, /function visualStyleGenerationDefaults/);
  assert.match(app, /visual_style_id: requestedVisualStyle\.style_id/);
  assert.match(app, /visual_style_version: requestedVisualStyle\.version/);
  assert.doesNotMatch(app, /director_skill_id:|script_skill_id:|provider_adapter_id:/);
  assert.match(page, /id="visual-style-previews"/);
  assert.match(app, /function renderVisualStylePreviews/);
  assert.match(app, /function visualStylePreviewAsset/);
  assert.match(app, /data-visual-style-id/);
  assert.match(styles, /\.visual-style-setting/);
  assert.match(styles, /\.visual-style-preview/);
  assert.match(
    styles,
    /\.visual-style-preview img \{[^}]*height: auto;[^}]*min-height: 0;/s,
  );
  for (const asset of [
    'modern-editorial.webp',
    'paper-collage.webp',
    'papercraft-stop-motion.webp',
  ]) {
    const assetPath = path.join(
      root,
      'qijia_video',
      'web',
      'style-previews',
      asset,
    );
    assert.ok(fs.existsSync(assetPath));
    assert.ok(fs.statSync(assetPath).size < 100000);
  }
});

test('video jobs expose guarded deletion without erasing audit history', () => {
  assert.match(app, /data-delete-job-id/);
  assert.match(app, /window\.confirm/);
  assert.match(app, /await api\('DELETE', `\/jobs\/\$\{encodeURIComponent\(job\.id\)\}`/);
  assert.match(app, /expected_revision: job\.revision/);
  assert.match(app, /已产生的费用和生成资产仍会保留/);
  assert.match(app, /任务运行完成后才能删除/);
  assert.match(app, /data-locked="\$\{String\(deleteLocked\)\}"/);
  assert.match(app, /busy \|\| control\.dataset\.locked === 'true'/);
  assert.match(styles, /\.job-delete-button/);
  assert.match(costsApi, /include_deleted=True/);
});

test('creation UI removes the old planning and technical pipeline experience', () => {
  assert.doesNotMatch(page, /generation-orchestration|creation-engine-summary|creation-method-choices/);
  assert.doesNotMatch(page, /job-generation-methods|content-planning-brief/);
  assert.doesNotMatch(app, /renderContentPlanning|renderJobGenerationMethods|renderOrchestrationSelection/);
  assert.doesNotMatch(page + app, /ContextPack|EditorialPlan|比较过的脚本角度|完整脚本|x-ai\/grok/);
  assert.match(app, /VisualBible 全片视觉宪法/);
  assert.match(app, /色彩与材质/);
  assert.equal((page.match(/id="reference-image-input"/g) || []).length, 1);
  assert.doesNotMatch(styles, /\.job-generation-methods|\.job-orchestration-core|\.content-planning-brief/);
});

test('administrator can manage bounded colleague access without exposing passwords', () => {
  assert.match(page, /id="account-management-link"/);
  assert.match(accountsPage, /同事账号与使用权限/);
  assert.match(accountsPage, /只能修改或继续执行自己创建的内容/);
  assert.match(accountsPage, /autocomplete="new-password"/);
  assert.match(accountsApp, /\/actions\/reset-password/);
  assert.match(accountsApp, /can_use_workbench/);
  assert.match(accountsApp, /Array\.isArray\(detail\)/);
  assert.match(accountsApp, /账号名.*初始密码.*新密码/s);
  assert.doesNotMatch(accountsApp, /new Error\(payload\.detail \|\|/);
  const createHandler = accountsApp.slice(
    accountsApp.indexOf("$('#account-create-form').addEventListener"),
    accountsApp.indexOf("$('#account-list').addEventListener"),
  );
  assert.ok(createHandler.indexOf('new FormData(form)') < createHandler.indexOf('setBusy(true)'));
  const updateHandler = accountsApp.slice(
    accountsApp.indexOf("$('#account-list').addEventListener"),
    accountsApp.indexOf("$('#account-refresh-button').addEventListener"),
  );
  assert.ok(updateHandler.indexOf('new FormData(form)') < updateHandler.indexOf('setBusy(true)'));
  assert.doesNotMatch(accountsApp, /localStorage|sessionStorage/);
});

test('team cost and Douyin performance dashboard has explicit audited refresh actions', () => {
  assert.match(page, /href="\/qijia-video\/costs">成本与效果/);
  assert.match(costsPage, /内容成本与效果分析/);
  assert.match(costsPage, /人民币已计成本 = 供应商回传金额 \+ 有计价依据的估算/);
  assert.match(costsPage, /1 USD = ¥6\.7/);
  assert.doesNotMatch(costsPage, />USD</);
  assert.match(costsPage, /团队抖音内容效果/);
  assert.match(costsPage, />整体 ROI</);
  assert.match(costsPage, /单条视频 ROI/);
  assert.match(costsPage, /单条 ROI \/ 10 倍目标/);
  assert.match(costsPage, /class="cost-advanced"/);
  assert.match(costsPage, /播放价值 = 播放量 ÷ 1000 × ¥10/);
  assert.match(costsPage, /id="performance-tracked-videos"/);
  assert.match(costsPage, /id="performance-target-meter"/);
  assert.match(costsPage, /id="performance-table-body"/);
  assert.match(costsPage, /<th>互动<\/th>/);
  assert.match(costsPage, /id="performance-export-button"/);
  assert.match(costsPage, /id="cost-refresh-button"[^>]*>重新载入看板</);
  assert.match(costsPage, /id="performance-refresh-button"/);
  assert.match(costsPage, /id="performance-refresh-status"[^>]*aria-live="polite"/);
  assert.match(costsPage, /id="cost-by-provider"/);
  assert.match(costsPage, /id="cost-by-stage"/);
  assert.match(costsPage, /id="cost-by-creator"/);
  assert.match(costsPage, /id="cost-event-body"/);
  assert.match(costsApp, /const API = '\/api\/qijia-video\/costs'/);
  assert.match(costsApp, /const WORKBENCH_API = '\/api\/qijia-video'/);
  assert.match(costsApp, /reported_cny/);
  assert.match(costsApp, /estimated_cny/);
  assert.match(costsApp, /unpriced_event_count/);
  assert.match(costsApp, /function renderPerformance/);
  assert.match(costsApp, /function formatOptionalInteger/);
  assert.match(costsApp, /class="performance-engagement-cell"/);
  assert.match(costsApp, /点赞.*评论.*分享.*收藏/s);
  assert.match(costsApp, /performance-target-gap/);
  assert.match(costsApp, /class="performance-roi-cell"/);
  assert.match(costsApp, /performance\.period\?\.cohort_basis/);
  assert.match(costsApp, /target_achievement_rate/);
  assert.match(costsApp, /target_achieved_provisional/);
  assert.match(costsApp, /duplicate_binding/);
  assert.match(costsApp, /exportPerformanceCsv/);
  assert.match(costsApp, /'like_count', 'comment_count', 'share_count', 'collect_count'/);
  assert.match(costsApp, /qijia-douyin-performance-/);
  assert.match(costsApp, /\/qijia-video\?job=/);
  assert.match(costsApp, /function refreshPerformanceRow/);
  assert.match(costsApp, /douyin-performance\/actions\/refresh/);
  assert.match(costsApp, /confirm_cost: true/);
  assert.match(costsApp, /rows\.length !== 1/);
  assert.match(costsApp, /data-refresh-performance-job/);
  assert.match(costsApi, /row\["revision"\] = int\(job\.revision\)/);
  assert.match(costsApi, /row\["can_refresh"\]/);
  assert.match(costsApi, /runtime\.capabilities\(\)\.get\("douyin_performance"/);
  assert.match(app, /new URLSearchParams\(window\.location\.search\)\.get\('job'\)/);
  assert.match(app, /api\('GET', `\/jobs\/\$\{encodeURIComponent\(selectJobId\)\}`\)/);
  assert.match(costsApp, /exportCsv/);
  assert.match(costsApp, /URL\.createObjectURL/);
  assert.match(costsApp, /\^\[\\t\\r\\n \]\*\[=\+\\-@\]/);
  assert.doesNotMatch(costsApp, /method:\s*['"](?:PUT|PATCH|DELETE)/);
  assert.doesNotMatch(costsApp, /reported_usd|estimated_usd|accounted_usd|自动发布/);
});

test('final approval binds the whole review bundle', () => {
  assert.match(app, /review_bundle_hash: job\.review_bundle_hash/);
  assert.doesNotMatch(app, /draft_hash:/);
  assert.match(page, /封面同时属于本次确认对象/);
});

test('generated video UI does not expose an automatic publish action', () => {
  assert.doesNotMatch(page, /<button[^>]*>[^<]*(?:自动发布|发布到抖音)/);
  assert.match(page, /不会自动发布或自动生成视频/);
  assert.match(page, /生成发布版本/);
});

test('creation intake freezes one unified natural-language creative request', () => {
  const manualStart = page.indexOf('id="creative-request-form"');
  const manualEnd = page.indexOf('class="visual-style-setting');
  const manualForm = page.slice(manualStart, manualEnd);
  assert.match(manualForm, /name="creative_request"/);
  assert.equal((manualForm.match(/name="creative_request"/g) || []).length, 1);
  assert.doesNotMatch(manualForm, /name="person_name"|name="viewpoint"/);
  assert.match(page, /系统直接生成口播稿 · 不联网 · 不生成前置研究简报/);
  assert.ok(page.indexOf('name="creative_request"') < page.indexOf('id="visual-style-previews"'));
  assert.ok(page.indexOf('id="visual-style-previews"') < page.indexOf('id="manual-reference-input"'));
  assert.ok(page.indexOf('id="manual-reference-input"') < page.indexOf('id="source-submit-button"'));
  assert.doesNotMatch(manualForm, /阿尔弗雷德·阿德勒|真正影响孩子/);
  assert.doesNotMatch(manualForm, /name="source_material"|name="rights_confirmed"|补充出处|专业模式/);
  assert.doesNotMatch(page, /name="parent_question"|name="core_idea"|name="fact_text"|name="subject_name"/);
  assert.match(app, /\/jobs\/creative-request/);
  assert.match(app, /\/jobs\/creative-request-with-reference/);
  assert.match(app, /upload\.append\('creative_request'/);
  assert.doesNotMatch(app, /\/source-cards\/creative-request/);
});

test('model knowledge boundary is visible without a pre-script planning artifact', () => {
  assert.match(page, /精确出处、人物归属、版本、日期和最新动态会在脚本确认时标记为需要复核/);
  assert.match(page, /不会假装成已经核验/);
  assert.match(page, /不生成前置研究简报/);
  assert.doesNotMatch(page + app, /content-planning-brief|EditorialPlan|比较过的脚本角度/);
});

test('creation intake hides internal architecture behind creator-facing choices', () => {
  assert.match(page, /同一场景的真实样片，只比较视觉语言/);
  assert.match(page, /有需要保持一致的人物、场景或画面风格吗/);
  assert.match(page, /人物、服装、物件、场景还是风格/);
  const referenceStart = page.indexOf('id="manual-reference-input"');
  const referenceEnd = page.indexOf('id="manual-intake-actions"');
  assert.doesNotMatch(page.slice(referenceStart, referenceEnd), /ShotContextIR|Director Skill/);
  assert.doesNotMatch(page, /创作链路与技术信息/);
  assert.doesNotMatch(page, /Prompt Adapter|Script Skill|Provider Adapter/);
});

test('topic research is Douyin-only, cost-bounded, and human-gated', () => {
  assert.match(page, /data-workspace-tab="topics"[^>]*>今日选题/);
  assert.match(page, /主题<\/dt><dd>家庭教育/);
  assert.match(page, /数据<\/dt><dd>抖音 · TikHub/);
  assert.match(page, /5 个可供人工判断的内容角度/);
  assert.match(page, /榜单证据与排序口径/);
  assert.match(page, /72 小时内优先 · 老视频可回潮/);
  assert.match(page, /不足不凑数/);
  assert.match(page, /本轮成本保护/);
  assert.match(app, /\/topic-research\/runs', \{confirm_cost: true\}/);
  assert.match(app, /estimated_usd_per_success/);
  assert.match(app, /formatCnyFromUsd/);
  assert.match(app, /usdToCnyRate/);
  assert.match(app, /1 USD = ¥\$\{usdToCnyRate\(\)\}/);
  assert.doesNotMatch(app, /function formatUsd/);
  assert.match(app, /plannedCalls \* unitPrice/);
  assert.match(app, /requestBudget \* unitPrice/);
  assert.match(app, /不会为了用满预算而增加调用/);
  assert.match(app, /evidence_policy/);
  assert.match(app, /low_follower_billboard/);
  assert.match(app, /low_follower_breakout/);
  assert.match(app, /emerging_low_follower_breakout/);
  assert.match(app, /deep_engagement_rate/);
  assert.match(app, /published_age_hours/);
  assert.match(app, /average_daily_plays/);
  assert.match(app, /日均播放/);
  assert.match(page, /id="topic-evidence-diagnostics"/);
  assert.match(app, /低粉榜样本整理/);
  assert.match(app, /指标缺失与发布时间只影响标签和排序/);
  assert.match(app, /空结果查询/);
  assert.match(app, /响应结构未识别/);
  assert.match(app, /rejected_missing_title_count/);
  assert.match(app, /rejected_insufficient_plays_count/);
  assert.match(app, /tikhub_success_count/);
  assert.match(app, /TikHub 调用明细/);
  assert.match(app, /request_label/);
  assert.match(app, /data_shape/);
  assert.match(app, /estimated_total_cost_usd/);
  assert.match(app, /reported_cost_usd/);
  assert.match(app, /data-adopt-topic/);
  assert.match(page, /name="source_material"/);
  assert.match(page, /name="rights_confirmed"/);
  assert.match(app, /抖音趋势只用于选题，不作为脚本事实/);
  assert.match(app, /state\.topicHandoffCandidate\?\.editorial_angle/);
  assert.match(app, /state\.topicHandoffCandidate\?\.parent_question/);
  assert.match(app, /verified_materials:/);
  assert.match(app, /\/jobs\/creative-request/);
  assert.doesNotMatch(app, /\/source-cards\/quick/);
  assert.doesNotMatch(app, /topic-research[\s\S]{0,240}自动发布/);
});

test('creation intake offers one optional global reference image without extra fields', () => {
  assert.match(page, /id="reference-image-input"/);
  assert.match(page, /accept="image\/jpeg,image\/png,image\/webp"/);
  assert.match(page, /上传 1 张参考图（可选）/);
  assert.match(page, /系统会逐个镜头判断参考图用于人物、服装、物件、场景还是风格/);
  assert.match(page, /不会自动照搬全部属性，也不会把图片内容当作事实依据/);
  assert.doesNotMatch(page, /id="reference-image-input"[^>]*multiple/);
  assert.match(app, /referenceImageFile/);
  assert.match(app, /\/jobs\/creative-request-with-reference/);
  assert.match(app, /apiMultipart/);
  assert.match(page, /id="job-reference-image"/);
});

test('workflow exposes five understandable stages with detailed action context', () => {
  for (const label of [
    '准备内容', '确认脚本', '制作画面', '确认成片', '发布包',
  ]) assert.match(app, new RegExp(label.replace('/', '\\/')));
  assert.match(app, /const workflowStages = \[[\s\S]*'准备内容'[\s\S]*'发布包'[\s\S]*\];/);
  assert.match(page, /id="current-action"/);
  assert.match(page, /id="stage-elapsed"/);
  assert.match(page, /总计 \/ 当前阶段/);
  assert.match(app, /phaseElapsedText\(task\)/);
  assert.match(app, /seedance_parallel: 2/);
  assert.match(app, /remotion_render: 2/);
  assert.match(app, /remotion_normalize: 2/);
  assert.match(styles, /grid-template-columns: repeat\(5, minmax\(0, 1fr\)\)/);
  assert.match(page, /id="next-action"/);
  assert.match(page, /系统按确认脚本的真实语义变化决定章节数量/);
  assert.match(page, /最多三段但不要求凑满/);
});

test('video quality is selectable and frozen into each new task', () => {
  assert.match(page, /id="video-resolution"/);
  assert.match(page, /value="480p"[^>]*>480P · 480×854/);
  assert.match(page, /value="720p"[^>]*>720P · 720×1280/);
  assert.match(page, /value="1080p"[^>]*selected[^>]*>1080P · 1080×1920（默认）/);
  assert.match(page, /画质越高，生成成本、耗时和文件体积通常越大/);
  assert.doesNotMatch(page, /默认使用 Seedance 1\.0 Pro Fast 无声模式/);
  assert.match(app, /video_resolution: '1080p'/);
  assert.match(app, /video_resolution: videoResolution/);
  assert.match(app, /job\?\.generation_settings\?\.video_resolution/);
});

test('Seed-TTS uses three verified voices and explicit speed choices with 1.2x default', () => {
  assert.match(page, /id="tts-voice-id"/);
  assert.equal((page.match(/zh_female_vv_uranus_bigtts/g) || []).length, 2);
  assert.equal((page.match(/zh_female_santongyongns_saturn_bigtts/g) || []).length, 2);
  assert.equal((page.match(/zh_male_ruyayichen_saturn_bigtts/g) || []).length, 2);
  assert.match(page, /id="tts-speed-ratio"[\s\S]*value="1\.2" selected>1\.2x（默认）/);
  assert.match(page, /id="job-tts-speed-ratio"/);
  assert.match(page, /id="preview-tts-button"/);
  assert.match(page, /费用计入本任务/);
  assert.match(app, /tts_voice_id: ttsVoiceId/);
  assert.match(app, /tts_speed_ratio: ttsSpeedRatio/);
  assert.match(app, /'1\.2': \[265, 355\]/);
  assert.match(app, /setTtsSettingsFields/);
  assert.match(app, /confirm_cost: true/);
  assert.match(app, /ttsPreviewKey/);
  assert.match(app, /narrationPreviewText/);
  assert.match(ttsProvider, /"speech_rate": speech_rate/);
  assert.match(ttsProvider, /speed_ratio=normalized_speed/);
  assert.match(ttsProvider, /probe_duration=False/);
});

test('Seedance usage and estimated cost are visible per job', () => {
  assert.match(page, /id="seedance-usage-summary"/);
  assert.match(page, /id="seedance-token-total"/);
  assert.match(page, /id="seedance-cost-estimate"/);
  assert.match(page, /id="seedance-spec"/);
  assert.match(app, /usage_total_tokens/);
  assert.match(app, /yuan_per_million_tokens/);
  assert.match(app, /yuan_per_image/);
  assert.match(app, /Seedream 首帧/);
  assert.match(app, /renderSeedanceUsage\(job\)/);
  assert.match(app, /visual_versions/);
  assert.match(app, /Seedance 累计 .* 次/);
  assert.match(app, /taskSeedanceCost/);
  assert.match(app, /estimated_cost_cny/);
});

test('AI shots are visible storyboard cards with isolated version controls', () => {
  assert.match(page, /id="shot-storyboard"/);
  assert.match(page, /id="shot-grid"/);
  assert.match(page, /id="shot-inspector"/);
  assert.match(page, /Director Skill 会按语义变化规划图片与必要的视频镜头/);
  assert.doesNotMatch(page, /id="image-count"|默认 10 段|13 张 Seedream/);
  assert.doesNotMatch(app, /image_count:|shot_count:|updateImageCountCost/);
  assert.match(app, /renderShotStoryboard\(job\)/);
  assert.match(app, /data-regenerate-shot/);
  assert.match(app, /data-frame-candidate/);
  assert.match(app, /const showFrameChoices = !previewUpload && frameCandidates\.length > 1/);
  assert.match(app, /data-select-version/);
  assert.match(app, /用这张首帧换一版/);
  assert.match(app, /id="shot-seedance-model"/);
  assert.match(app, /默认使用 Seedance 2\.0/);
  assert.match(app, /seedance_model: seedanceModel/);
  assert.match(app, /刊例价预估约/);
  assert.match(app, /first_frame_candidate_id/);
  assert.match(app, /id="shot-revision-intent"/);
  assert.match(app, /revision_intent: revisionIntent/);
  assert.match(app, /这是内容层要表达的画面含义/);
  assert.match(app, /编译后的只读提示词/);
  assert.match(app, /不会直接发送给 Seedance/);
  assert.doesNotMatch(app, /id="shot-prompt-editor"/);
  assert.match(app, /\/frames\/\$\{encodeURIComponent\(candidate\.candidate_id\)\}\/media/);
  assert.match(app, /\/shots\/\$\{encodeURIComponent\(state\.selectedShotId\)\}\/actions\/regenerate/);
  assert.match(app, /\/versions\/\$\{encodeURIComponent\(select\.dataset\.selectVersion\)\}\/actions\/select/);
  assert.doesNotMatch(page, /无限画布|模型市场|工作流节点/);
});

test('final video mixes generated videos and motion images without narration text cards', () => {
  assert.match(page, /屏幕文字由 Remotion 后期排版，不会让 AI 直接画字/);
  assert.match(renderer, /playbackRate=\{playbackRate\}/);
  assert.match(renderer, /assetDurationSeconds/);
  assert.match(renderer, /translate3d/);
  assert.match(renderer, /generated_image/);
  assert.match(renderer, /<Img/);
  assert.match(app, /visual_type === 'image'/);
  assert.match(app, /imageRows\.length/);
  assert.match(app, /jobVisualChapterCounts/);
  assert.match(renderer, /screen_text_cues/);
  assert.match(renderer, /EditorialText/);
  assert.match(renderEntry, /pixelFormat: 'yuv420p'/);
  assert.match(renderEntry, /colorSpace: 'bt709'/);
  assert.match(renderEntry, /sampleRate: 48000/);
  assert.match(renderEntry, /--cover-output/);
  assert.match(renderEntry, /KnowledgeCoverV1/);
  assert.equal((renderEntry.match(/await bundle\(/g) || []).length, 1);
  assert.match(renderer, /whiteSpace: 'nowrap'/);
  assert.match(renderer, /padding: '0 74px 220px'/);
  assert.match(renderer, /Array\.from\(cue\.text\)\.length/);
});

test('v4 visual development is selected before bulk AI generation', () => {
  assert.match(page, /id="style-frame-review"/);
  assert.match(page, /先确认全片视觉方向/);
  assert.match(app, /function renderStyleFrameReview\(job\)/);
  assert.match(app, /data-select-style-frame/);
  assert.match(app, /style-frames\/\$\{encodeURIComponent\(button\.dataset\.selectStyleFrame\)\}\/actions\/select/);
  assert.match(app, /job\.pipeline_version === 'v4'[\s\S]*!job\.selected_style_frame_id/);
  assert.match(app, /确认脚本并开发视觉方案/);
  assert.match(app, /mediaChoiceContainer\.hidden = qualityFirst/);
  assert.match(app, /含 3 张视觉样片/);
});

test('storyboard supports editor images and videos without losing AI versions', () => {
  assert.match(page, /混合制作故事板/);
  assert.match(page, /上传自己的图片或视频，也可恢复 AI 素材/);
  assert.match(app, /data-shot-upload/);
  assert.match(app, /image\/jpeg,image\/png,image\/webp/);
  assert.match(app, /video\/quicktime,video\/webm/);
  assert.match(app, /sha256File\(file\)/);
  assert.match(app, /uploadFileDirect\(grant, file/);
  assert.match(app, /`\$\{uploadPath\}\/uploads`/);
  assert.match(app, /`\$\{uploadPath\}\/uploads\/complete`/);
  assert.match(app, /`\$\{uploadPath\}\/uploads\/cancel`/);
  assert.match(app, /apiMultipart\(uploadPath, body\)/);
  assert.match(app, /正在直传素材… \$\{percent\}%/);
  assert.match(app, /restore-generated-media/);
  assert.match(app, /data-preview-upload/);
  assert.match(app, /selectedUploadedMedia/);
  assert.match(app, /pendingShotMediaEdit/);
  assert.match(app, /pending_shot_media_edits/);
  assert.match(page, /id="pending-shot-media-bar"/);
  assert.match(page, /id="apply-pending-shot-media-button"/);
  assert.match(page, /id="discard-pending-shot-media-button"/);
  assert.match(app, /shot-media\/pending\/actions\/apply/);
  assert.match(app, /shot-media\/pending\/actions\/discard/);
  assert.match(app, /media\/pending\/actions\/discard/);
  assert.match(app, /成片只重新生成了 1 次/);
  assert.match(app, /每次上传只做安全校验、转码和暂存/);
  assert.match(app, /原 AI 素材会完整保留/);
  assert.match(styles, /\.shot-source-panel/);
  assert.match(styles, /\.shot-upload-button/);
  assert.match(styles, /\.pending-shot-media-bar/);
  assert.match(styles, /\.shot-card\.pending/);
});

test('script approval can arrange owned media before paid visual generation', () => {
  assert.match(page, /id="prepare-media-first"/);
  assert.match(page, /我有自己的图片或视频，先安排素材/);
  assert.match(page, /只为未上传的镜头调用 AI/);
  assert.match(page, /id="pre-generation-media-bar"/);
  assert.match(page, /id="confirm-pre-generation-media-button"/);
  assert.match(app, /prepare_media_first: prepareMediaFirst/);
  assert.match(app, /media_review_required/);
  assert.match(app, /actions.confirm-media-plan/);
  assert.match(app, /上传后直接加入本次素材安排/);
  assert.match(app, /跳过这个镜头的 AI 生成/);
  assert.match(app, /首版成片只渲染了 1 次/);
  assert.match(styles, /pre-generation-media-choice/);
  assert.match(styles, /pre-generation-media-bar/);

  const workflowSource = app.slice(
    app.indexOf('function workflowCopy'),
    app.indexOf('function cleanScreenplayValue'),
  );
  assert.match(workflowSource, /job.state === 'media_review_required'/);
  assert.match(workflowSource, /只生成剩余/);
  assert.doesNotMatch(app, /function renderContentPlanning/);
});

test('unified creative request starts a real job and polling resumes after refresh', () => {
  assert.match(app, /result = await api\('POST', '\/jobs\/creative-request'/);
  assert.match(app, /creative_request: body\.creative_request/);
  assert.match(app, /generation_settings: generationSettings/);
  assert.doesNotMatch(app, /source_card_id:|\/source-cards\/creative-request/);
  assert.match(app, /job\?\.last_run_task_id/);
  assert.match(app, /resumeSelectedTask/);
  assert.match(app, /fetchTask\(job\.last_run_task_id\)/);
});

test('Script Skill owns writing and creation payload exposes no competing controls', () => {
  assert.doesNotMatch(page, /id="script-generation-prompt"|id="enable-custom-script-prompt"/);
  assert.doesNotMatch(page, /id="seedance-generation-prompt"/);
  assert.doesNotMatch(page, /id="job-seedance-prompt"/);
  assert.match(app, /generation_settings: generationSettings/);
  assert.match(app, /video_resolution: videoResolution/);
  const settingsSource = app.slice(
    app.indexOf('function generationSettingsPayload'),
    app.indexOf('async function api'),
  );
  assert.doesNotMatch(settingsSource, /script_prompt|seedance_prompt|image_count|shot_count/);
  assert.match(settingsSource, /visual_style_id: requestedVisualStyle\.style_id/);
  assert.doesNotMatch(settingsSource, /content_skill|script_skill|director_skill|provider_adapter/);
  assert.match(app, /localStorage\.removeItem\(LEGACY_PROMPT_STORAGE_KEY\)/);
  assert.doesNotMatch(app, /function customScriptPromptEnabled|restore-prompt-defaults|job-seedance-prompt/);
});

test('production UI prioritizes the current task and stays usable on mobile', () => {
  assert.match(page, /class="production-view-tabs"/);
  assert.match(page, /data-production-pane="create"/);
  assert.match(page, /data-production-pane="jobs"/);
  assert.match(app, /productionPane: 'create'/);
  assert.match(app, /function switchProductionPane/);
  assert.match(app, /\['ArrowLeft', 'ArrowRight'\][\s\S]*data-production-pane/);
  assert.match(app, /<button class="job-card/);
  assert.match(app, /formatDateTime\(job\.updated_at/);
  assert.match(styles, /\.job-list \{ max-height:/);
  assert.match(styles, /\.workspace-grid > \.mobile-pane-hidden/);
  assert.match(styles, /@media \(max-width: 900px\)[\s\S]*\.production-view-tabs \{ display: flex/);
  assert.match(styles, /@media \(max-width: 560px\)[\s\S]*\.section-title \{ align-items: flex-start; flex-direction: column/);
  assert.doesNotMatch(page, /id="job-generation-methods"/);
  assert.match(app, /querySelectorAll\('\[data-busy-lock\]'\)/);
  for (const formId of ['topic-source-form', 'creative-request-form']) {
    const handler = app.slice(
      app.indexOf(`$('#${formId}').addEventListener('submit'`),
      app.indexOf('\n});', app.indexOf(`$('#${formId}').addEventListener('submit'`)) + 4,
    );
    assert.match(handler, /if \(state\.busy\) return/);
  }
  assert.doesNotMatch(page, /id="new-card-button"/);
});

test('script review shows a whole-job cost range before confirmation', () => {
  assert.match(page, /id="script-cost-estimate"/);
  assert.match(page, /整单成本预估/);
  assert.match(app, /function renderScriptCostEstimate/);
  assert.match(app, /usageRecordCostCny/);
  assert.match(app, /每段 8–10 秒/);
  assert.match(app, /未生成的 AI 画面会从实际费用中扣除/);
  assert.match(styles, /\.script-cost-estimate/);
});

test('final approval packages in the background task flow', () => {
  assert.match(app, /approve-final[\s\S]*result\.task_id/);
  assert.match(app, /正在生成发布包/);
});

test('script review edits narration and screen text without a competing visual track', () => {
  assert.match(page, /id="script-beat-editor"/);
  assert.match(page, /id="script-video-title"/);
  assert.match(page, /id="script-length-status"/);
  assert.match(page, /这里只编辑会被念出的旁白和可选屏幕文字/);
  assert.match(page, /确认后，系统再根据真实配音时长设计视觉章节/);
  assert.doesNotMatch(app, /data-script-field="visual_direction"/);
  assert.match(app, /data-script-field="narration"/);
  assert.match(app, /data-script-field="on_screen_text"/);
  assert.match(app, /parseLegacyScreenplay/);
  assert.match(app, /scriptForEditor/);
  assert.match(app, /narrationCharacterCount/);
  assert.match(app, /SCRIPT_HARD_MAX_CHARS/);
  assert.match(app, /script\.schema_version = '3\.0'/);
  assert.match(app, /口播稿至少需要三个自然叙事段/);
  assert.doesNotMatch(app, /口播稿至少需要五个自然叙事段/);
  assert.doesNotMatch(page, /完整口播稿|建议 200-240 字|精简至 200-240 字/);
  assert.doesNotMatch(app, /actions\/condense-script/);
  assert.doesNotMatch(app, /splitNarrationText|确认时系统会先整理/);
});

test('legacy markdown screenplay discards the old visual track during v3 migration', () => {
  const start = app.indexOf('function cleanScreenplayValue');
  const end = app.indexOf('const scriptRoleLabels');
  const source = app.slice(start, end);
  const input = `## 《蔡元培：美育为什么如此重要？》
### 0—2秒｜钩子 **画面：** 孩子推开一只受伤的小鸟。**旁白：** 一个孩子成绩很好，为什么还可能教育失败？ **屏幕大字：** 会做题，不等于会感受。---
### 2—5秒｜制造悬念 **画面：** 满分试卷逐渐虚化。**旁白：** 蔡元培认为，我们可能少教了他一样东西——美育。 **屏幕文字：** 美育，到底在教育什么？---
### 5—16秒｜打破误解 **画面：** 画笔和钢琴退到画面边缘。**旁白：** 美育，不只是学画画、弹钢琴。---
### 16—32秒｜解释核心 **画面：** 孩子看到花后慢慢收回手。**旁白：** 美育让他感受到，美的东西不忍心被破坏。---
### 32—44秒｜回答为什么 **画面：** 孩子主动扶起同伴。**旁白：** 一个人的行为，也由他会被什么打动决定。---
### 44—55秒｜升华与收束 **画面：** 孩子照顾受伤的小鸟。**旁白：** 教育还在帮助他决定要成为怎样的人。**结尾大字：** 不忍心伤害世界。`;
  const context = {input};
  vm.runInNewContext(`${source}\nresult = parseLegacyScreenplay(input);`, context);
  assert.equal(context.result.length, 6);
  assert.equal(context.result[0].narration, '一个孩子成绩很好，为什么还可能教育失败？');
  assert.equal(context.result[0].visual_direction, undefined);
  assert.equal(context.result[0].on_screen_text, '会做题，不等于会感受。');
  assert.equal(context.result[5].role, 'closing');
  assert.equal(context.result[5].on_screen_text, '不忍心伤害世界。');
});

test('narration failures return to script instead of repeating production', () => {
  assert.match(page, /id="revise-script-button"/);
  assert.match(app, /isNarrationRevisionFailure/);
  assert.match(app, /完整口播稿\?共/);
  assert.match(app, /\['script_approved', 'producing'\]/);
  assert.match(app, /actions\/revise-script/);
  assert.match(app, /result\.requires_review/);
  assert.match(app, /返回脚本调整后重新生成/);
  assert.match(app, /复用现有画面/);
});

test('packaged jobs expose two primary downloads and collapse technical artifacts', () => {
  assert.match(page, /下载成片 MP4/);
  assert.match(page, /下载完整发布包 ZIP/);
  assert.match(page, /查看技术产物/);
  assert.match(app, /job\.state === 'packaged'/);
  assert.match(app, /release-package\.zip/);
});

test('packaged jobs expose Douyin-only manual playback feedback and ROI', () => {
  assert.match(page, /id="douyin-performance-section"/);
  assert.match(page, /本版本仅记录抖音，不采集小红书或视频号/);
  assert.match(page, /id="douyin-link-input"/);
  assert.match(page, /id="douyin-change-link-button"/);
  assert.match(page, /id="douyin-refresh-button"[^>]*>手动刷新作品数据/);
  assert.match(page, /id="douyin-refresh-status"[^>]*aria-live="polite"/);
  assert.match(page, /id="douyin-play-count"/);
  assert.match(page, /id="douyin-like-count"/);
  assert.match(page, /id="douyin-comment-count"/);
  assert.match(page, /id="douyin-share-count"/);
  assert.match(page, /id="douyin-collect-count"/);
  assert.match(page, /作品数据不会自动更新/);
  assert.match(page, /10 倍目标播放/);
  assert.match(app, /douyin-performance\/actions\/refresh/);
  assert.match(app, /refreshButton\.hidden = !performance/);
  assert.match(app, /setDouyinRefreshFeedback/);
  assert.match(app, /正在连接 TikHub 并读取最新作品数据/);
  assert.match(app, /刷新成功/);
  assert.match(app, /刷新失败：/);
  assert.match(app, /\$\('#douyin-refresh-button'\)\.disabled = state\.busy/);
  assert.match(app, /只有创建者或管理员可以发起付费的作品数据刷新/);
  assert.match(app, /latestSnapshot\[key\]/);
  assert.match(app, /confirm_cost: true/);
  assert.match(app, /每千次播放 ¥10、目标 10 倍/);
  assert.match(app, /canEditResource\(job\)/);
});
