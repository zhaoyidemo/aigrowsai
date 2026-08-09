const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const page = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'index.html'), 'utf8');
const app = fs.readFileSync(path.join(root, 'qijia_video', 'web', 'app.js'), 'utf8');
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
  assert.match(page, /齐家 AI 家庭教育内容工作台/);
  assert.match(login, /家庭教育内容工作台/);
  assert.match(app, /const API = '\/api\/qijia-video'/);
  assert.match(host, /RedirectResponse\("\/qijia-video"/);
  assert.match(page, /action="\/logout"/);
  assert.doesNotMatch(page, /返回继续追问/);
  assert.doesNotMatch(page, /x-data=|Alpine/);
  assert.match(login, /action="\/login"/);
  assert.match(login, /autocomplete="current-password"/);
});

test('content Skills select versioned expert or recent-news workflows', () => {
  assert.match(page, /id="content-skill"/);
  assert.match(page, /id="news-topic-form"/);
  assert.match(page, /TERALAB|TERA LAB/);
  assert.match(page, /主题只是检索请求，不会被当作事实/);
  assert.match(page, /至少一条证据必须能与检索注释匹配/);
  assert.match(page, /只有单一站点时会醒目标注/);
  assert.match(app, /qijia-video-generation-settings-v2/);
  assert.match(app, /DEFAULT_CONTENT_SKILL_ID = 'explain-expert-view'/);
  assert.match(app, /NEWS_CONTENT_SKILL_ID = 'brief-recent-news'/);
  assert.match(app, /skill_id: requestedSkill\.skill_id/);
  assert.match(app, /skill_version: requestedSkill\.version/);
  assert.match(app, /\/source-cards\/news-topic/);
  assert.match(page, /id="retry-research-button"/);
  assert.match(app, /\/actions\/retry-news-research/);
  assert.match(app, /confirm_cost: true/);
  assert.match(app, /research_diagnostics/);
  assert.match(app, /web_search_requests/);
  assert.match(app, /recent_news_research: 1/);
  assert.match(app, /job\.skill_snapshot\?\.display_name/);
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

test('creation intake asks only for one person and one viewpoint', () => {
  const manualStart = page.indexOf('id="source-card-form"');
  const manualEnd = page.indexOf('id="generation-prompt-settings"');
  const manualForm = page.slice(manualStart, manualEnd);
  assert.match(page, /name="person_name"/);
  assert.match(page, /name="viewpoint"/);
  assert.match(page, /冲突、反直觉与现实意义/);
  assert.doesNotMatch(manualForm, /name="source_material"|name="rights_confirmed"|补充出处|专业模式/);
  assert.doesNotMatch(page, /name="parent_question"|name="core_idea"|name="fact_text"|name="subject_name"/);
  assert.match(app, /\/source-cards\/idea/);
  assert.ok(page.indexOf('id="generation-prompt-settings"') > page.indexOf('id="source-submit-button"'));
});

test('person viewpoint research is visible, cited, and non-blocking', () => {
  assert.match(page, /会先自动研究可追溯资料/);
  assert.match(page, /没有可靠来源时会使用原始观点继续/);
  assert.match(page, /id="person-research-brief"/);
  assert.match(app, /function renderResearchBrief\(job\)/);
  assert.match(app, /人物主题自动研究简报/);
  assert.match(app, /自动研究已降级/);
  assert.match(app, /research_warning/);
  assert.match(app, /rel="noopener noreferrer"/);
  assert.match(app, /renderResearchBrief\(job\)/);
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
  assert.match(app, /抖音趋势仅作为选题线索/);
  assert.match(app, /editorial_brief: state\.topicHandoffCandidate/);
  assert.match(app, /parent_question: state\.topicHandoffCandidate/);
  assert.match(app, /\/source-cards\/quick/);
  assert.doesNotMatch(app, /topic-research[\s\S]{0,240}自动发布/);
});

test('creation intake offers one optional global reference image without extra fields', () => {
  assert.match(page, /id="reference-image-input"/);
  assert.match(page, /accept="image\/jpeg,image\/png,image\/webp"/);
  assert.match(page, /上传 1 张参考图（可选）/);
  assert.match(page, /最高优先级视觉基准，但不作为观点依据/);
  assert.match(page, /优先统一全部视觉章节的画风、色彩与人物造型/);
  assert.doesNotMatch(page, /id="reference-image-input"[^>]*multiple/);
  assert.match(app, /referenceImageFile/);
  assert.match(app, /\/source-cards\/idea-with-reference/);
  assert.match(app, /apiMultipart/);
  assert.match(page, /id="job-reference-image"/);
});

test('workflow exposes three videos and configurable image chapters with action context', () => {
  for (const label of [
    '观点已确认', '生成脚本', '确认脚本', '生成旁白', '分镜与首帧', 'AI 视频 1/3',
    'AI 视频 2/3', 'AI 视频 3/3', '图像与视频合成', '确认成片', '发布包完成',
  ]) assert.match(app, new RegExp(label.replace('/', '\\/')));
  assert.match(page, /id="current-action"/);
  assert.match(page, /id="stage-elapsed"/);
  assert.match(page, /总计 \/ 当前阶段/);
  assert.match(app, /phaseElapsedText\(task\)/);
  assert.match(app, /seedance_parallel: 5/);
  assert.match(app, /remotion_render: 8/);
  assert.match(app, /remotion_normalize: 8/);
  assert.match(page, /id="next-action"/);
  assert.match(page, /3 张继续生成 8-10 秒、所选画质的 AI 视频/);
  assert.match(page, /其余按所选动态图片数量直接进入成片/);
});

test('video quality is selectable and frozen into each new task', () => {
  assert.match(page, /id="video-resolution"/);
  assert.match(page, /value="480p"[^>]*>480P · 480×854/);
  assert.match(page, /value="720p"[^>]*>720P · 720×1280/);
  assert.match(page, /value="1080p"[^>]*selected[^>]*>1080P · 1080×1920（默认）/);
  assert.match(page, /画质越高，Token、耗时和文件体积通常越大/);
  assert.match(page, /默认使用 Seedance 1\.0 Pro Fast 无声模式/);
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
  assert.match(app, /legacyPreTtsSpeedPrompt/);
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
  assert.match(page, /3 段视频和 10 段动态图片会在这里逐个出现/);
  assert.match(page, /id="image-count"[^>]+min="2"[^>]+max="10"[^>]+value="10"/);
  assert.match(page, /默认 10 段/);
  assert.match(page, /10 段动态图片 \+ 3 张视频首帧 = 13 张 Seedream/);
  assert.match(page, /id="image-count-cost"/);
  assert.match(app, /image_count: rawImageCount/);
  assert.match(app, /shot_count: rawImageCount \+ 3/);
  assert.match(app, /updateImageCountCost/);
  assert.match(app, /renderShotStoryboard\(job\)/);
  assert.match(app, /data-regenerate-shot/);
  assert.match(app, /data-frame-candidate/);
  assert.match(app, /const showFrameChoices = frameCandidates\.length > 1/);
  assert.match(app, /data-select-version/);
  assert.match(app, /用这张首帧换一版/);
  assert.match(app, /id="shot-seedance-model"/);
  assert.match(app, /默认 1\.0 Pro Fast 保持原生 1080P/);
  assert.match(app, /seedance_model: seedanceModel/);
  assert.match(app, /刊例价预估约/);
  assert.match(app, /first_frame_candidate_id/);
  assert.match(app, /\/frames\/\$\{encodeURIComponent\(candidate\.candidate_id\)\}\/media/);
  assert.match(app, /\/shots\/\$\{encodeURIComponent\(state\.selectedShotId\)\}\/actions\/regenerate/);
  assert.match(app, /\/versions\/\$\{encodeURIComponent\(select\.dataset\.selectVersion\)\}\/actions\/select/);
  assert.doesNotMatch(page, /无限画布|模型市场|工作流节点/);
});

test('final video mixes generated videos and motion images without narration text cards', () => {
  assert.match(page, /口播原句不会作为画面文字/);
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

test('person viewpoint flow starts a real job and polling resumes after refresh', () => {
  assert.match(app, /card = await api\('POST', '\/source-cards\/idea', body\)/);
  assert.match(app, /source_card_id: card\.id, generation_settings: generationSettings/);
  assert.match(app, /job\?\.last_run_task_id/);
  assert.match(app, /resumeSelectedTask/);
  assert.match(app, /fetchTask\(job\.last_run_task_id\)/);
});

test('script and Seedance prompts are configurable without exposing transport contracts', () => {
  assert.match(page, /id="script-generation-prompt"/);
  assert.match(page, /id="seedance-generation-prompt"/);
  assert.match(page, /来源卡和 JSON 输出结构由系统自动附加/);
  assert.match(page, /上传参考图后，由参考图接管画风与人物造型/);
  assert.match(page, /为每个视觉章节生成 1 张画面/);
  assert.match(page, /id="job-seedance-prompt"/);
  assert.match(app, /generation_settings: generationSettings/);
  assert.match(app, /seedance_prompt: seedancePrompt/);
  assert.match(app, /video_resolution: videoResolution/);
  assert.match(app, /restore-prompt-defaults/);
  assert.match(app, /legacyFixedStructure/);
  assert.match(app, /legacySeedanceStyle/);
  assert.match(app, /legacyLongScript/);
  assert.match(app, /legacyIndependentAnimation/);
  assert.match(app, /legacySingleTrackScript/);
  assert.match(app, /legacyPreRetentionHook/);
  assert.match(app, /legacyPreDirectorPrompt/);
  assert.match(app, /降低 2 秒流失率、提高 5 秒完播率/);
  assert.match(app, /贯穿全片的可见变化/);
});

test('final approval packages in the background task flow', () => {
  assert.match(app, /approve-final[\s\S]*result\.task_id/);
  assert.match(app, /正在生成发布包/);
});

test('script review edits one flowing multi-track screenplay', () => {
  assert.match(page, /id="script-beat-editor"/);
  assert.match(page, /id="script-video-title"/);
  assert.match(page, /id="script-length-status"/);
  assert.match(page, /每一段都包含画面、旁白和可选的屏幕文字/);
  assert.match(page, /只有旁白会进入配音/);
  assert.match(app, /data-script-field="visual_direction"/);
  assert.match(app, /data-script-field="narration"/);
  assert.match(app, /data-script-field="on_screen_text"/);
  assert.match(app, /parseLegacyScreenplay/);
  assert.match(app, /scriptForEditor/);
  assert.match(app, /narrationCharacterCount/);
  assert.match(app, /SCRIPT_HARD_MAX_CHARS/);
  assert.doesNotMatch(page, /完整口播稿|建议 200-240 字|精简至 200-240 字/);
  assert.doesNotMatch(app, /actions\/condense-script/);
  assert.doesNotMatch(app, /splitNarrationText|确认时系统会先整理/);
});

test('legacy markdown screenplay is separated into visual, narration and screen-text tracks', () => {
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
  assert.equal(context.result[0].visual_direction, '孩子推开一只受伤的小鸟。');
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
