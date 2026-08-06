const API = '/api/qijia-video/costs';
const state = {data: null, busy: false};

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
  if (detail && typeof detail === 'object') return detail.msg || '读取成本失败';
  return String(detail || '读取成本失败');
}

function setBusy(busy) {
  state.busy = busy;
  $('#cost-refresh-button').disabled = busy;
  $('#cost-period-select').disabled = busy;
  $('#cost-export-button').disabled = busy || !state.data?.content?.length;
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
  $('#cost-period-title').textContent = data.period?.label || '成本分析';
  renderSummary(data);
  renderTimeline(data);
  renderBreakdown('#cost-by-provider', data.by_provider);
  renderBreakdown('#cost-by-stage', data.by_stage);
  renderBreakdown('#cost-by-creator', data.by_creator);
  renderContent(data);
  renderEvents(data);
  renderMethod(data);
  renderNotice(data);
  const status = $('#cost-status');
  status.classList.add('ready');
  status.classList.remove('warning');
  status.querySelector('span:last-child').textContent = `成本计算完成 · ${data.period?.label || ''} · 更新于 ${formatDateTime(data.generated_at)}`;
  $('#cost-export-button').disabled = !data.content?.length;
}

async function loadCosts() {
  if (state.busy) return;
  setBusy(true);
  const days = Number($('#cost-period-select').value || 30);
  try {
    const response = await fetch(`${API}?days=${encodeURIComponent(days)}`, {
      credentials: 'same-origin', headers: {'Accept': 'application/json'},
    });
    let payload;
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok || payload.code !== 0) throw new Error(apiErrorMessage(payload));
    render(payload.data || {});
  } catch (error) {
    const status = $('#cost-status');
    status.classList.remove('ready');
    status.classList.add('warning');
    status.querySelector('span:last-child').textContent = `成本读取失败：${error.message}`;
    const notice = $('#cost-notice');
    notice.hidden = false;
    notice.classList.add('error');
    notice.textContent = '没有触发任何供应商调用，可以稍后安全刷新。';
  } finally {
    setBusy(false);
  }
}

function csvCell(value) {
  let text = String(value ?? '');
  if (/^[\t\r\n ]*[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replace(/"/g, '""')}"`;
}

function exportCsv() {
  const rows = state.data?.content || [];
  if (!rows.length) return;
  const header = [
    'time', 'type', 'id', 'title', 'creator', 'status', 'requests', 'tokens',
    'reported_cny', 'estimated_cny', 'accounted_cny', 'coverage_ratio', 'unpriced_events',
  ];
  const output = [header, ...rows.map((row) => [
    row.latest_at, row.scope_type, row.scope_id, row.title, row.creator, row.status,
    row.request_count, row.total_tokens, row.reported_cny, row.estimated_cny,
    row.accounted_cny,
    row.coverage_ratio, row.unpriced_event_count,
  ])].map((row) => row.map(csvCell).join(',')).join('\r\n');
  const blob = new Blob([`\ufeff${output}`], {type: 'text/csv;charset=utf-8'});
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `qijia-costs-${state.data?.period?.days || 'all'}-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(link.href);
}

$('#cost-refresh-button').addEventListener('click', loadCosts);
$('#cost-period-select').addEventListener('change', loadCosts);
$('#cost-export-button').addEventListener('click', exportCsv);
loadCosts();
