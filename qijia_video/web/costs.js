const API = '/api/qijia-video/costs';
const WORKBENCH_API = '/api/qijia-video';
const state = {
  data: null,
  busy: false,
  performanceSort: 'roi',
  performanceRefreshProgress: null,
};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[character]));

const STATUS_LABELS = {
  card_verified: '材料已确认', script_generating: '脚本生成中',
  script_review_required: '待确认脚本', script_approved: '脚本已确认',
  producing: '生产中', quality_checking: '质检中',
  final_review_required: '待确认成片', final_approved: '成片已确认',
  packaged: '已打包', failed: '失败', running: '研究中', ready: '选题已就绪',
};
const VALUATION_LABELS = {
  reported: '供应商回传', estimated_snapshot: '发生时估算',
  estimated_current_price: '历史补算', unpriced: '待对账',
};

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function formatMoney(value) {
  const amount = number(value);
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency', currency: 'CNY',
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(amount);
}

function formatInteger(value) {
  return new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 0}).format(number(value));
}

function formatOptionalInteger(value) {
  if (value === null || value === undefined || value === '') return '未返回';
  return formatInteger(value);
}

function formatMultiple(value) {
  if (value === null || value === undefined || value === '') return '—';
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? `${parsed.toFixed(2)}×` : '—';
}

function formatPercent(value) {
  const ratio = number(value);
  return `${(ratio * 100).toFixed(ratio >= 0.1 ? 1 : 2)}%`;
}

function formatDateTime(value) {
  const parsed = Date.parse(String(value || ''));
  if (!Number.isFinite(parsed)) return String(value || '—');
  return new Intl.DateTimeFormat('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(parsed));
}

function formatPeriod(value, bucket) {
  const raw = String(value || '');
  if (bucket === 'month' && /^\d{4}-\d{2}$/.test(raw)) {
    const [year, month] = raw.split('-');
    return `${year} 年 ${Number(month)} 月`;
  }
  if (bucket === 'week') return `${raw} 当周`;
  return raw;
}

function moneyBreakdown(row) {
  return `回传 ${formatMoney(row?.reported_cny)} · 估算 ${formatMoney(row?.estimated_cny)}`;
}

function apiErrorMessage(payload) {
  const detail = payload?.detail ?? payload?.message;
  if (Array.isArray(detail)) {
    return detail.map((item) => item?.msg || item?.message || '参数格式不正确').join('；');
  }
  if (detail && typeof detail === 'object') return detail.msg || '请求失败';
  return String(detail || '请求失败');
}

function setBusy(busy) {
  state.busy = busy;
  $('#cost-refresh-button').disabled = busy;
  $('#cost-period-select').disabled = busy;
  $('#cost-export-button').disabled = busy || !state.data?.content?.length;
  $('#performance-sort-select').disabled = busy;
  $('#performance-export-button').disabled = busy || !state.data?.performance?.rows?.length;
  document.querySelectorAll('[data-refresh-performance-job]').forEach((button) => {
    button.disabled = busy;
  });
  updatePerformanceRefreshControls();
}

function performanceRefreshMeta(data = state.data) {
  return data?.performance?.refresh || {};
}

function refreshablePerformanceRows(data = state.data) {
  const rows = data?.performance?.rows || [];
  const seenVideoIds = new Set();
  return rows.filter((row) => {
    const videoId = String(row.video_id || '');
    if (
      row.duplicate_binding
      || row.can_refresh !== true
      || number(row.revision) < 1
      || !videoId
      || seenVideoIds.has(videoId)
    ) return false;
    seenVideoIds.add(videoId);
    return true;
  });
}

function setPerformanceRefreshStatus(message, tone = '') {
  const node = $('#performance-refresh-status');
  node.hidden = !message;
  node.textContent = message || '';
  node.classList.toggle('loading', tone === 'loading');
  node.classList.toggle('success', tone === 'success');
  node.classList.toggle('warning', tone === 'warning');
  node.classList.toggle('error', tone === 'error');
}

function updatePerformanceRefreshControls(data = state.data) {
  const button = $('#performance-refresh-button');
  const hint = $('#performance-refresh-hint');
  if (!button || !hint) return;
  const meta = performanceRefreshMeta(data);
  const rows = refreshablePerformanceRows(data);
  const progress = state.performanceRefreshProgress;
  const unitCost = Number(meta.estimated_cny_per_success);
  const hasUnitCost = Number.isFinite(unitCost) && unitCost > 0;
  const unitCostLabel = hasUnitCost ? formatMoney(unitCost) : '金额以账单为准';

  button.textContent = progress
    ? '正在更新这条抖音数据…'
    : rows.length === 1
      ? `更新这条抖音数据（约 ${unitCostLabel}）`
      : rows.length > 1
        ? '请在下方选择视频'
        : '更新抖音最新数据';
  button.disabled = state.busy || meta.ready !== true || rows.length !== 1;
  if (meta.ready === false) {
    hint.textContent = `TikHub 暂不可用：${(meta.missing_configuration || []).join('、') || '配置不完整'}`;
  } else if (!rows.length) {
    hint.textContent = (data?.performance?.rows || []).length
      ? '当前账号没有可付费更新的唯一作品；成员只能更新自己创建的内容。'
      : '当前时间范围内尚无已绑定抖音作品。';
  } else if (rows.length === 1) {
    hint.textContent = `当前只有 1 个可更新作品；点击后调用 1 次 TikHub，预计成本 ${unitCostLabel}。`;
  } else {
    hint.textContent = `当前有 ${rows.length} 个可更新作品。为控制费用，请在下方逐条点击“更新此视频”；每次只调用 1 次 TikHub，预计 ${unitCostLabel}。`;
  }
}

function renderSummary(data) {
  const summary = data.summary || {};
  $('#cost-accounted-cny').textContent = formatMoney(summary.accounted_cny);
  $('#cost-cny-breakdown').textContent = moneyBreakdown(summary);
  $('#cost-coverage').textContent = summary.event_count ? formatPercent(summary.coverage_ratio) : '暂无调用';
  $('#cost-coverage-detail').textContent = `${formatInteger(summary.priced_event_count)} 已计价 · ${formatInteger(summary.unpriced_event_count)} 待对账`;
  $('#cost-request-count').textContent = formatInteger(summary.request_count);
  $('#cost-token-count').textContent = `${formatInteger(summary.total_tokens)} tokens`;
  $('#cost-completed-videos').textContent = formatInteger(summary.completed_video_count);
  $('#cost-work-count').textContent = `${formatInteger(summary.video_job_count)} 个视频任务 · ${formatInteger(summary.topic_run_count)} 轮选题`;

  const perVideo = summary.video_cost_per_packaged || {};
  $('#cost-per-video').textContent = perVideo.denominator
    ? formatMoney(perVideo.accounted_cny)
    : '尚无已打包视频';
}

function performanceRows(data) {
  const rows = [...(data.performance?.rows || [])];
  const numericValue = (row) => {
    if (state.performanceSort === 'plays') return number(row.play_count);
    if (state.performanceSort === 'cost') return number(row.accounted_cost_cny);
    if (state.performanceSort === 'latest') {
      const parsed = Date.parse(String(row.observed_at || ''));
      return Number.isFinite(parsed) ? parsed : 0;
    }
    const roi = Number(row.roi_multiple);
    return Number.isFinite(roi) && roi >= 0 ? roi : -1;
  };
  return rows.sort((left, right) => (
    Number(Boolean(left.duplicate_binding)) - Number(Boolean(right.duplicate_binding))
    || numericValue(right) - numericValue(left)
    || number(right.play_count) - number(left.play_count)
    || String(left.title || '').localeCompare(String(right.title || ''), 'zh-CN')
  ));
}

function performanceStatus(row) {
  if (row.duplicate_binding) return {className: 'duplicate', label: '重复绑定 · 未计入汇总'};
  if (!row.cost_complete) return {className: 'provisional', label: '成本暂估'};
  if (row.roi_multiple === null || row.roi_multiple === undefined) {
    return {className: 'unavailable', label: '待成本基准'};
  }
  if (row.target_achieved) return {className: 'achieved', label: '已达 10 倍'};
  return {className: 'tracking', label: '继续观察'};
}

function renderPerformance(data) {
  const performance = data.performance || {};
  const summary = performance.summary || {};
  const rows = performanceRows(data);
  const tracked = number(summary.tracked_video_count);
  const boundJobs = number(summary.bound_job_count);
  const duplicateBindings = number(summary.duplicate_binding_count);
  const packaged = number(summary.packaged_video_count);
  const untracked = number(summary.untracked_packaged_count);
  const hasCostBasis = number(summary.accounted_cost_cny) > 0;

  $('#performance-cohort-note').textContent = performance.period?.cohort_basis
    || '按首次绑定时间纳入，刷新不会改变视频所属时间范围。';
  $('#performance-tracked-videos').textContent = `${formatInteger(boundJobs)} / ${formatInteger(packaged)}`;
  $('#performance-tracking-detail').textContent = packaged
    ? [
      `${formatInteger(tracked)} 个唯一抖音作品`,
      `${formatInteger(untracked)} 条未绑定`,
      duplicateBindings ? `${formatInteger(duplicateBindings)} 条重复绑定` : '',
      `有效覆盖 ${formatPercent(summary.tracking_coverage_ratio)}`,
    ].filter(Boolean).join(' · ')
    : '当前范围内尚无已打包视频';
  $('#performance-total-plays').textContent = formatInteger(summary.total_play_count);
  $('#performance-snapshot-detail').textContent = [
    `${formatInteger(summary.snapshot_count)} 次手动快照`,
    summary.latest_observed_at ? `最近 ${formatDateTime(summary.latest_observed_at)}` : '',
  ].filter(Boolean).join(' · ');
  $('#performance-total-cost').textContent = formatMoney(summary.accounted_cost_cny);
  $('#performance-cost-detail').textContent = summary.cost_event_count
    ? `计价覆盖 ${formatPercent(summary.cost_coverage_ratio)} · ${formatInteger(summary.unpriced_event_count)} 笔待对账`
    : '暂无计费记录';
  $('#performance-playback-value').textContent = formatMoney(summary.playback_value_cny);
  $('#performance-roi').textContent = formatMultiple(summary.roi_multiple);
  $('#performance-roi-detail').textContent = `目标 ${formatMultiple(summary.target_roi_multiple)}${summary.cost_complete ? '' : ' · 当前暂估'}`;
  $('#performance-target-status').textContent = `${formatInteger(summary.target_achieved_count)} / ${formatInteger(tracked)}`;
  $('#performance-target-detail').textContent = tracked && !hasCostBasis
    ? '尚无可计算的已计视频成本'
    : summary.provisional_target_achieved_count
    ? `${formatInteger(summary.target_achieved_count)} 条确认达标 · ${formatInteger(summary.provisional_target_achieved_count)} 条暂估达标`
    : tracked
      ? `${formatPercent(summary.target_achievement_rate)} 的已回流视频确认达到目标`
    : '尚无回流视频';

  const currentViews = number(summary.total_play_count);
  const targetViews = number(summary.target_views);
  const targetRatio = targetViews > 0 ? Math.min(1, currentViews / targetViews) : 0;
  const targetPercent = Math.round(targetRatio * 100);
  const targetGap = $('#performance-target-gap');
  const targetGapDetail = $('#performance-target-gap-detail');
  if (!tracked) {
    targetGap.textContent = '—';
    targetGapDetail.textContent = '尚无已回流视频';
  } else if (!hasCostBasis) {
    targetGap.textContent = '—';
    targetGapDetail.textContent = '尚无可计算成本基准';
  } else if (summary.target_achieved) {
    targetGap.textContent = '已达标';
    targetGapDetail.textContent = `超出 ${formatInteger(Math.max(0, currentViews - targetViews))} 次播放`;
  } else if (summary.target_achieved_provisional) {
    targetGap.textContent = '暂时达标';
    targetGapDetail.textContent = '仍有成本待对账';
  } else {
    targetGap.textContent = `还差 ${formatInteger(summary.remaining_views)}`;
    targetGapDetail.textContent = `目标 ${formatInteger(targetViews)} 次播放${summary.cost_complete ? '' : ' · 暂估'}`;
  }
  $('#performance-target-current').textContent = formatInteger(currentViews);
  $('#performance-target-views').textContent = formatInteger(targetViews);
  $('#performance-target-bar').style.width = `${targetPercent}%`;
  const meter = $('#performance-target-meter');
  meter.setAttribute('aria-valuenow', String(targetPercent));
  meter.classList.toggle('achieved', !!summary.target_achieved);
  meter.classList.toggle('provisional', !!summary.target_achieved_provisional);
  const targetCaption = !tracked
    ? '尚无可计算目标的视频。'
    : !hasCostBasis
      ? '尚无已计视频成本，暂不能计算 10 倍 ROI 播放目标。'
    : summary.target_achieved
      ? `团队整体已经达到 10 倍 ROI 播放目标，超出 ${formatInteger(Math.max(0, currentViews - targetViews))} 次播放。`
      : summary.target_achieved_provisional
        ? `按当前已计成本暂时超过 10 倍目标 ${formatInteger(Math.max(0, currentViews - targetViews))} 次播放。`
      : `距离团队 10 倍 ROI 目标还差 ${formatInteger(summary.remaining_views)} 次播放。`;
  $('#performance-target-caption').textContent = summary.cost_complete
    ? targetCaption
    : `${targetCaption} ${formatInteger(summary.provisional_video_count)} 条视频含待对账成本，目标为暂估。`;

  $('#performance-row-count').textContent = summary.duplicate_binding_count
    ? `${rows.length} 条绑定 · ${formatInteger(summary.duplicate_binding_count)} 条重复`
    : `${rows.length} 条已回流`;
  const body = $('#performance-table-body');
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="5" class="empty">${
      packaged
        ? `当前范围有 ${formatInteger(packaged)} 条已打包视频，但尚未手填绑定抖音链接。`
        : '当前范围内还没有已打包并绑定抖音的视频。'
    }</td></tr>`;
  } else {
    body.innerHTML = rows.map((row) => {
      const status = performanceStatus(row);
      const refreshMeta = performanceRefreshMeta(data);
      const unitCost = Number(refreshMeta.estimated_cny_per_success);
      const inlineCost = Number.isFinite(unitCost) && unitCost > 0
        ? `（约 ${formatMoney(unitCost)}）`
        : '';
      const refreshAction = (
        row.can_refresh === true
        && !row.duplicate_binding
        && number(row.revision) >= 1
      )
        ? `<button class="performance-inline-refresh" type="button" data-refresh-performance-job="${escapeHtml(row.job_id)}" ${state.busy ? 'disabled' : ''}>更新此视频${inlineCost}</button>`
        : '';
      const rowTarget = number(row.target_views);
      const rowPlays = number(row.play_count);
      const rowHasCostBasis = number(row.accounted_cost_cny) > 0;
      const rowProgress = rowTarget > 0 ? Math.min(100, rowPlays / rowTarget * 100) : 0;
      const targetCopy = row.duplicate_binding
        ? '同一作品只按最早绑定任务计入团队汇总'
        : !rowHasCostBasis
        ? '暂无可计算成本基准'
        : row.target_achieved
        ? `超出 ${formatInteger(Math.max(0, rowPlays - rowTarget))} 次`
        : row.target_achieved_provisional
          ? `按已计成本暂超 ${formatInteger(Math.max(0, rowPlays - rowTarget))} 次`
        : `还差 ${formatInteger(row.remaining_views)} 次`;
      return `<tr>
        <td>
          <strong>${escapeHtml(row.title)}</strong>
          <small>${escapeHtml(row.creator)}${row.author_name ? ` · 抖音作者 ${escapeHtml(row.author_name)}` : ''}</small>
          <span class="performance-row-links"><a href="/qijia-video?job=${encodeURIComponent(row.job_id)}">查看任务</a><a href="${escapeHtml(row.video_url)}" target="_blank" rel="noopener noreferrer">打开抖音</a>${refreshAction}</span>
        </td>
        <td><strong>${formatInteger(row.play_count)}</strong><small>${escapeHtml(formatDateTime(row.observed_at))} · ${formatInteger(row.snapshot_count)} 次快照</small></td>
        <td class="performance-engagement-cell"><strong>点赞 ${formatOptionalInteger(row.like_count)} · 评论 ${formatOptionalInteger(row.comment_count)}</strong><small>分享 ${formatOptionalInteger(row.share_count)} · 收藏 ${formatOptionalInteger(row.collect_count)}</small></td>
        <td><strong>${escapeHtml(formatMoney(row.accounted_cost_cny))}</strong><small>播放价值 ${escapeHtml(formatMoney(row.playback_value_cny))}${row.unpriced_event_count ? ` · ${formatInteger(row.unpriced_event_count)} 笔待对账` : ''}</small></td>
        <td class="performance-roi-cell">
          <strong>${escapeHtml(formatMultiple(row.roi_multiple))}</strong>
          <small><span class="performance-status ${status.className}">${status.label}</span></small>
          <div class="performance-row-progress" aria-hidden="true"><span style="width:${rowProgress}%"></span></div>
          <small>${rowHasCostBasis ? `10 倍目标 ${formatInteger(row.target_views)} · ` : ''}${targetCopy}</small>
        </td>
      </tr>`;
    }).join('');
  }
  updatePerformanceRefreshControls(data);

  const basis = performance.basis || {};
  const scopeParts = [
    basis.cost_scope,
    basis.data_scope,
    summary.provisional_video_count
      ? `${formatInteger(summary.provisional_video_count)} 条视频存在待对账成本，整体 ROI 和目标均为暂估。`
      : '',
    summary.duplicate_binding_count
      ? `检测到 ${formatInteger(summary.duplicate_binding_count)} 条重复绑定，团队合计已按唯一抖音作品去重。`
      : '',
  ].filter(Boolean);
  $('#performance-scope-note').textContent = scopeParts.join('；');
  $('#performance-export-button').disabled = state.busy || !rows.length;
}

function renderTimeline(data) {
  const rows = data.timeline || [];
  const node = $('#cost-timeline');
  if (!rows.length) {
    node.innerHTML = '<p class="empty">当前范围内还没有收费调用。</p>';
    return;
  }
  const maxCny = Math.max(...rows.map((row) => number(row.accounted_cny)), 0);
  node.innerHTML = rows.map((row) => {
    const cny = number(row.accounted_cny);
    const cnyWidth = maxCny && cny ? Math.max(2, cny / maxCny * 100) : 0;
    return `<div class="cost-timeline-row">
      <strong>${escapeHtml(formatPeriod(row.period, data.period?.bucket))}</strong>
      <div class="cost-timeline-bars">
        <div><span class="cny" style="width:${cnyWidth}%"></span><small>${escapeHtml(formatMoney(row.accounted_cny))}</small></div>
      </div>
      <em>${formatInteger(row.request_count)} 次</em>
    </div>`;
  }).join('');
}

function renderBreakdown(selector, rows) {
  const node = $(selector);
  if (!rows?.length) {
    node.innerHTML = '<p class="empty">当前范围内暂无数据。</p>';
    return;
  }
  node.innerHTML = rows.map((row) => `<article class="cost-breakdown-row">
    <div><strong>${escapeHtml(row.label || row.key)}</strong><span>${formatInteger(row.request_count)} 次请求 · ${formatPercent(row.coverage_ratio)} 已计价</span></div>
    <div><strong>${escapeHtml(formatMoney(row.accounted_cny))}</strong><span>${escapeHtml(moneyBreakdown(row))}</span></div>
  </article>`).join('');
}

function renderContent(data) {
  const rows = data.content || [];
  $('#cost-content-count').textContent = `${rows.length} 项`;
  const body = $('#cost-content-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">当前范围内还没有内容记录。</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => `<tr>
    <td><strong>${escapeHtml(row.title)}</strong><small>${row.scope_type === 'video_job' ? '视频任务' : '选题研究'} · ${escapeHtml(formatDateTime(row.latest_at))}</small></td>
    <td>${escapeHtml(row.creator)}<small>${escapeHtml(STATUS_LABELS[row.status] || row.status)}</small></td>
    <td>${formatInteger(row.request_count)} 次<small>${formatInteger(row.total_tokens)} tokens</small></td>
    <td><strong>${escapeHtml(formatMoney(row.accounted_cny))}</strong><small>${escapeHtml(moneyBreakdown(row))}</small></td>
    <td><span class="cost-coverage-pill ${row.unpriced_event_count ? 'warning' : ''}">${row.event_count ? formatPercent(row.coverage_ratio) : '无调用'}</span><small>${formatInteger(row.unpriced_event_count)} 待对账</small></td>
  </tr>`).join('');
}

function eventAmount(row) {
  if (!row.priced) return '<span class="cost-unpriced">待对账</span>';
  return escapeHtml(formatMoney(row.accounted_cny));
}

function renderEvents(data) {
  const rows = data.events || [];
  const coverage = data.coverage || {};
  $('#cost-event-count').textContent = coverage.event_detail_limit_reached
    ? `显示最近 ${formatInteger(coverage.event_detail_limit)} 条`
    : `${rows.length} 条`;
  const body = $('#cost-event-body');
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">当前范围内还没有调用明细。</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => `<tr>
    <td>${escapeHtml(formatDateTime(row.occurred_at))}<small>${escapeHtml(row.stage_label)}</small></td>
    <td><strong>${escapeHtml(row.title)}</strong><small>${escapeHtml(row.creator)}</small></td>
    <td>${escapeHtml(row.provider_label)}<small>${escapeHtml(row.model_id || '未记录模型')}${row.request_id ? ` · 请求 ${escapeHtml(row.request_id)}` : ''}</small></td>
    <td>${formatInteger(row.request_count)} 次<small>${row.total_tokens ? `${formatInteger(row.total_tokens)} tokens` : `${number(row.quantity)} ${escapeHtml(row.unit)}`}</small></td>
    <td><strong>${eventAmount(row)}</strong><small title="${escapeHtml(row.note)}">${escapeHtml(VALUATION_LABELS[row.valuation] || row.valuation)}${row.note ? ` · ${escapeHtml(row.note)}` : ''}</small></td>
  </tr>`).join('');
}

function renderMethod(data) {
  const pricing = data.pricing || [];
  $('#cost-pricing').innerHTML = pricing.map((row) => `<article class="cost-pricing-row">
    <div><strong>${escapeHtml(row.provider)}</strong><span>${escapeHtml(row.valuation === 'reported' ? '供应商回传' : '规划估算')}</span></div>
    <strong>${escapeHtml(row.rate)}</strong>
    <a href="${escapeHtml(row.source)}" target="_blank" rel="noopener noreferrer">查看计价依据</a>
  </article>`).join('') || '<p class="empty">暂无计价配置。</p>';
  $('#cost-coverage-notes').innerHTML = (data.coverage?.notes || [])
    .map((note) => `<li>${escapeHtml(note)}</li>`).join('');
}

function renderNotice(data) {
  const coverage = data.coverage || {};
  const messages = [];
  if (coverage.has_unpriced_events) messages.push('存在待对账调用，当前金额不是最终完整成本。');
  if (coverage.job_limit_reached || coverage.topic_limit_reached) messages.push(`读取已达到每类 ${formatInteger(coverage.source_limit)} 项上限，全部历史可能未完整纳入。`);
  if (coverage.event_detail_limit_reached) messages.push('汇总包含全部已读取调用；审计表只展示最近明细。');
  const notice = $('#cost-notice');
  notice.hidden = !messages.length;
  notice.textContent = messages.join(' ');
  notice.classList.toggle('error', false);
}

function render(data) {
  state.data = data;
  $('#cost-period-title').textContent = data.period?.label || '成本与效果分析';
  renderSummary(data);
  renderPerformance(data);
  renderTimeline(data);
  renderBreakdown('#cost-by-provider', data.by_provider);
  renderBreakdown('#cost-by-stage', data.by_stage);
  renderBreakdown('#cost-by-creator', data.by_creator);
  renderContent(data);
  renderEvents(data);
  renderMethod(data);
  renderNotice(data);
  const status = $('#cost-status');
  status.hidden = true;
  $('#cost-export-button').disabled = !data.content?.length;
  $('#performance-export-button').disabled = !data.performance?.rows?.length;
}

async function loadCosts() {
  if (state.busy) return false;
  setBusy(true);
  const days = Number($('#cost-period-select').value || 30);
  const status = $('#cost-status');
  status.hidden = false;
  status.classList.remove('ready', 'warning');
  status.querySelector('span:last-child').textContent = '正在读取团队成本与抖音效果数据…';
  try {
    const response = await fetch(`${API}?days=${encodeURIComponent(days)}`, {
      credentials: 'same-origin', headers: {'Accept': 'application/json'},
    });
    let payload;
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok || payload.code !== 0) throw new Error(apiErrorMessage(payload));
    render(payload.data || {});
    return true;
  } catch (error) {
    status.classList.remove('ready');
    status.classList.add('warning');
    status.querySelector('span:last-child').textContent = `成本读取失败：${error.message}`;
    const notice = $('#cost-notice');
    notice.hidden = false;
    notice.classList.add('error');
    notice.textContent = '没有触发任何供应商调用，可以稍后安全刷新。';
    return false;
  } finally {
    setBusy(false);
  }
}

async function requestPerformanceRefresh(row) {
  const response = await fetch(
    `${WORKBENCH_API}/jobs/${encodeURIComponent(row.job_id)}/douyin-performance/actions/refresh`,
    {
      method: 'POST',
      credentials: 'same-origin',
      headers: {'Accept': 'application/json', 'Content-Type': 'application/json'},
      body: JSON.stringify({
        expected_revision: number(row.revision),
        confirm_cost: true,
      }),
    },
  );
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok || payload.code !== 0) throw new Error(apiErrorMessage(payload));
  return payload.data;
}

async function refreshPerformanceRow(requestedRow) {
  if (state.busy) return;
  const row = refreshablePerformanceRows().find(
    (candidate) => candidate.job_id === requestedRow?.job_id,
  );
  if (!row) {
    setPerformanceRefreshStatus('这条视频当前不可更新；请重新载入看板后再试。', 'error');
    return;
  }
  const meta = performanceRefreshMeta();
  if (meta.ready !== true) {
    setPerformanceRefreshStatus(
      `TikHub 暂不可用：${(meta.missing_configuration || []).join('、') || '配置不完整'}`,
      'error',
    );
    return;
  }

  let refreshError = null;
  state.performanceRefreshProgress = {jobId: row.job_id};
  setBusy(true);
  setPerformanceRefreshStatus(
    `正在通过 TikHub 更新：${row.title || row.job_id}。本次只读取 1 个作品，请勿重复点击。`,
    'loading',
  );
  try {
    await requestPerformanceRefresh(row);
  } catch (error) {
    refreshError = error;
  } finally {
    state.performanceRefreshProgress = null;
    setBusy(false);
  }

  const reloaded = await loadCosts();
  if (refreshError) {
    setPerformanceRefreshStatus(
      `更新失败：${refreshError?.message || '未知错误'}${reloaded ? '' : '；看板重新载入也失败，请稍后重试。'}`,
      'error',
    );
    return;
  }
  if (!reloaded) {
    setPerformanceRefreshStatus(
      'TikHub 已返回并保存数据，但看板重新载入失败；请点击“重新载入看板”。',
      'warning',
    );
    return;
  }
  const latest = (state.data?.performance?.rows || []).find(
    (candidate) => candidate.job_id === row.job_id,
  );
  setPerformanceRefreshStatus(
    `更新成功：播放 ${formatInteger(latest?.play_count)} · 点赞 ${formatOptionalInteger(latest?.like_count)} · 评论 ${formatOptionalInteger(latest?.comment_count)} · 分享 ${formatOptionalInteger(latest?.share_count)} · 收藏 ${formatOptionalInteger(latest?.collect_count)} · 数据时间 ${formatDateTime(latest?.observed_at)}。`,
    'success',
  );
}

function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\t\r\n ]*[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

function downloadCsv(header, rows, filename) {
  const output = [header, ...rows]
    .map((row) => row.map(csvCell).join(','))
    .join('\r\n');
  const blob = new Blob([`\ufeff${output}`], {type: 'text/csv;charset=utf-8'});
  const link = document.createElement('a');
  const objectUrl = URL.createObjectURL(blob);
  link.href = objectUrl;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(objectUrl);
}

function exportCsv() {
  const rows = state.data?.content || [];
  if (!rows.length) return;
  const header = [
    'time', 'type', 'id', 'title', 'creator', 'status', 'requests', 'tokens',
    'reported_cny', 'estimated_cny', 'accounted_cny', 'coverage_ratio', 'unpriced_events',
  ];
  downloadCsv(header, rows.map((row) => [
    row.latest_at, row.scope_type, row.scope_id, row.title, row.creator, row.status,
    row.request_count, row.total_tokens, row.reported_cny, row.estimated_cny,
    row.accounted_cny,
    row.coverage_ratio, row.unpriced_event_count,
  ]), `qijia-costs-${state.data?.period?.days || 'all'}-${new Date().toISOString().slice(0, 10)}.csv`);
}

function exportPerformanceCsv() {
  const rows = performanceRows(state.data || {});
  if (!rows.length) return;
  const header = [
    'job_id', 'content_title', 'creator', 'douyin_video_id', 'douyin_video_url',
    'douyin_author', 'bound_at', 'observed_at', 'snapshot_count', 'play_count',
    'like_count', 'comment_count', 'share_count', 'collect_count',
    'accounted_cost_cny', 'playback_value_cny', 'roi_multiple', 'target_views',
    'remaining_views', 'target_achieved', 'target_achieved_provisional',
    'duplicate_binding', 'duplicate_of_job_id', 'cost_complete',
    'unpriced_events',
  ];
  downloadCsv(header, rows.map((row) => [
    row.job_id, row.title, row.creator, row.video_id, row.video_url,
    row.author_name, row.bound_at, row.observed_at, row.snapshot_count,
    row.play_count, row.like_count, row.comment_count, row.share_count,
    row.collect_count, row.accounted_cost_cny, row.playback_value_cny,
    row.roi_multiple, row.target_views, row.remaining_views,
    row.target_achieved, row.target_achieved_provisional,
    row.duplicate_binding, row.duplicate_of_job_id,
    row.cost_complete, row.unpriced_event_count,
  ]), `qijia-douyin-performance-${state.data?.period?.days || 'all'}-${new Date().toISOString().slice(0, 10)}.csv`);
}

$('#cost-refresh-button').addEventListener('click', loadCosts);
$('#cost-period-select').addEventListener('change', loadCosts);
$('#cost-export-button').addEventListener('click', exportCsv);
$('#performance-refresh-button').addEventListener('click', () => {
  const rows = refreshablePerformanceRows();
  if (rows.length === 1) refreshPerformanceRow(rows[0]);
});
$('#performance-table-body').addEventListener('click', (event) => {
  const button = event.target.closest('[data-refresh-performance-job]');
  if (!button || state.busy) return;
  const row = (state.data?.performance?.rows || []).find(
    (candidate) => candidate.job_id === button.dataset.refreshPerformanceJob,
  );
  if (row) refreshPerformanceRow(row);
});
$('#performance-sort-select').addEventListener('change', (event) => {
  state.performanceSort = event.target.value || 'roi';
  if (state.data) renderPerformance(state.data);
});
$('#performance-export-button').addEventListener('click', exportPerformanceCsv);
loadCosts();
