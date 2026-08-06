const API = '/api/qijia-video/accounts';
const state = {administrator: null, members: [], busy: false};

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
}[character]));

function formatDateTime(value) {
  const parsed = Date.parse(String(value || ''));
  if (!Number.isFinite(parsed)) return '尚未登录';
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  }).format(new Date(parsed));
}

function notify(message, error = false) {
  const node = $('#account-notice');
  node.hidden = !message;
  node.textContent = message || '';
  node.classList.toggle('error', error);
}

function setBusy(busy) {
  state.busy = busy;
  document.querySelectorAll('button, input').forEach((node) => {
    node.disabled = busy;
  });
}

const validationFieldLabels = {
  username: '账号名',
  password: '初始密码',
  new_password: '新密码',
  expected_revision: '账号版本',
  is_active: '启用状态',
  can_use_workbench: '工作台权限',
};

function validationIssueMessage(issue) {
  if (!issue || typeof issue !== 'object') return '';
  const location = Array.isArray(issue.loc)
    ? issue.loc.filter((part) => !['body', 'query', 'path'].includes(String(part)))
    : [];
  const fieldName = String(location[location.length - 1] || '');
  const fieldLabel = validationFieldLabels[fieldName] || fieldName || '请求内容';
  const context = issue.ctx && typeof issue.ctx === 'object' ? issue.ctx : {};
  const type = String(issue.type || '');
  const knownReasons = {
    missing: '不能为空',
    extra_forbidden: '包含不支持的字段',
    bool_parsing: '必须是有效的开关值',
    int_parsing: '必须是有效的整数',
  };
  let reason = knownReasons[type] || '';
  if (type === 'string_too_short') reason = `至少需要 ${Number(context.min_length) || 1} 个字符`;
  if (type === 'string_too_long') reason = `不能超过 ${Number(context.max_length) || 0} 个字符`;
  if (type === 'greater_than_equal') reason = `不能小于 ${context.ge}`;
  if (!reason) {
    reason = String(issue.msg || issue.message || '格式不正确')
      .replace(/^Field required$/i, '不能为空')
      .replace(/^Input should be a valid boolean$/i, '必须是有效的开关值');
  }
  return `${fieldLabel}：${reason}`;
}

function apiErrorMessage(payload) {
  const detail = payload?.detail ?? payload?.message;
  if (Array.isArray(detail)) {
    const messages = detail.map(validationIssueMessage).filter(Boolean);
    return [...new Set(messages)].join('；') || '账号信息格式不正确';
  }
  if (detail && typeof detail === 'object') {
    return validationIssueMessage(detail) || '账号操作失败';
  }
  return String(detail || '账号操作失败');
}

async function api(method, path = '', body = undefined) {
  const response = await fetch(`${API}${path}`, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? {} : {'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload;
  try { payload = await response.json(); } catch { payload = {}; }
  if (!response.ok || payload.code !== 0) {
    throw new Error(apiErrorMessage(payload));
  }
  return payload.data;
}

function renderAccounts() {
  const administrator = state.administrator?.username || 'admin';
  const activeCount = state.members.filter(
    (member) => member.is_active && member.can_use_workbench,
  ).length;
  $('#account-status').textContent = `管理员 ${administrator} · ${activeCount} 个可用同事账号`;
  const node = $('#account-list');
  if (!state.members.length) {
    node.innerHTML = '<p class="empty">还没有同事账号。</p>';
    return;
  }
  node.innerHTML = state.members.map((member) => {
    const available = member.is_active && member.can_use_workbench;
    return `<article class="account-card" data-account-id="${Number(member.id)}">
      <header class="account-card-heading">
        <div>
          <h3>${escapeHtml(member.username)}</h3>
          <p>最近登录：${escapeHtml(formatDateTime(member.last_login_at))}</p>
        </div>
        <span class="state-pill ${available ? '' : 'inactive'}">${available ? '可以使用' : '不可使用'}</span>
      </header>
      <form class="account-permission-form" data-account-update>
        <label class="confirmation-check">
          <input name="can_use_workbench" type="checkbox" ${member.can_use_workbench ? 'checked' : ''}>
          <span>允许使用齐家内容工作台</span>
        </label>
        <label class="confirmation-check">
          <input name="is_active" type="checkbox" ${member.is_active ? 'checked' : ''}>
          <span>账号已启用</span>
        </label>
        <button class="button secondary" type="submit">保存权限</button>
      </form>
      <details class="account-password-reset">
        <summary>重置密码</summary>
        <form class="account-password-form" data-account-password>
          <label>新密码
            <input name="new_password" type="password" required minlength="12" maxlength="128" autocomplete="new-password" placeholder="至少 12 个字符">
          </label>
          <button class="button danger" type="submit">重置并退出旧会话</button>
        </form>
      </details>
    </article>`;
  }).join('');
}

async function loadAccounts() {
  const data = await api('GET');
  state.administrator = data.administrator;
  state.members = data.members || [];
  renderAccounts();
  if (state.busy) setBusy(true);
}

function memberFor(form) {
  const card = form.closest('[data-account-id]');
  return state.members.find((member) => String(member.id) === card?.dataset.accountId);
}

$('#account-create-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const request = {
    username: String(data.get('username') || '').trim(),
    password: String(data.get('password') || ''),
    can_use_workbench: data.get('can_use_workbench') === 'on',
    is_active: data.get('is_active') === 'on',
  };
  notify(''); setBusy(true);
  try {
    await api('POST', '', request);
    form.reset();
    form.elements.can_use_workbench.checked = true;
    form.elements.is_active.checked = true;
    await loadAccounts();
    notify('同事账号已创建。请通过安全渠道单独发送初始密码。');
  } catch (error) {
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

$('#account-list').addEventListener('submit', async (event) => {
  const form = event.target;
  if (!form.matches('[data-account-update], [data-account-password]')) return;
  event.preventDefault();
  const member = memberFor(form);
  if (!member || state.busy) return;
  const data = new FormData(form);
  notify(''); setBusy(true);
  try {
    if (form.matches('[data-account-update]')) {
      await api('PATCH', `/${encodeURIComponent(member.id)}`, {
        expected_revision: member.revision,
        can_use_workbench: data.get('can_use_workbench') === 'on',
        is_active: data.get('is_active') === 'on',
      });
      await loadAccounts();
      notify(`账号 ${member.username} 的权限已保存；如有变化，旧会话已失效。`);
    } else {
      const password = String(data.get('new_password') || '');
      await api('POST', `/${encodeURIComponent(member.id)}/actions/reset-password`, {
        expected_revision: member.revision,
        new_password: password,
      });
      form.reset();
      await loadAccounts();
      notify(`账号 ${member.username} 的密码已重置，旧会话已失效。`);
    }
  } catch (error) {
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

$('#account-refresh-button').addEventListener('click', async () => {
  notify(''); setBusy(true);
  try {
    await loadAccounts();
    notify('账号列表已刷新。');
  } catch (error) {
    notify(error.message, true);
  } finally {
    setBusy(false);
  }
});

setBusy(true);
loadAccounts()
  .catch((error) => notify(error.message, true))
  .finally(() => setBusy(false));
