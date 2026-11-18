const state = {
  token: null,
  user: null,
  currentPage: 'dashboard',
  data: { dashboardStats: null, dashboardLoadError: null, tickets: [], drafts: [], customers: [], knowledgeDocs: [], pendingDraftsCount: 0, escalatedCount: 0, snapshotHistory: [], selectedSnapshot: null, analytics: null, analyticsLoadedAt: 0 }
};
const API = window.location.origin;

async function api(method, path, body=
null, isForm=false) {
  const headers = {};
  if (!isForm) headers['Content-Type'] = 'application/json';
    const opts = { method, headers, credentials: 'same-origin' };
  if (body) opts.body = isForm ? body : JSON.stringify(body);
  try {
    const res = await fetch(API + path, opts);
    if (res.status === 401) {
      const err = await res.json().catch(() => ({ detail: 'Authentication required.' }));
      if (state.user) { state.token = null; state.user = null; renderApp(); loadLoginUsers(); }
      throw new Error(err.detail || 'Authentication required.');
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      const apiError = new Error(err.detail || res.statusText);
      apiError.status = res.status;
      throw apiError;
    }
    if (res.status === 204) return null;
    return await res.json();
  } catch(e) {
    if (e.message === 'Failed to fetch') throw new Error('Cannot connect to API. Ensure the backend is running on port 8000.');
    throw e;
  }
}

async function login(email, password) {
  const data = await api('POST', '/api/auth/login', { email, password });
  if (data) {
    state.token = true;
    state.user = data.user;
    renderApp();
    loadAllData();
    startAutoRefresh();
    showGmailConnectionNotice();
  }
  return data;
}

async function logout() {
  if (state.user) {
    try { await api('POST', '/api/auth/logout'); } catch (_) { /* Clear local UI even if the session already expired. */ }
  }
  if (typeof _autoRefreshInterval !== 'undefined' && _autoRefreshInterval) {
    clearInterval(_autoRefreshInterval);
    _autoRefreshInterval = null;
  }
  state.token = null; state.user = null;
  renderApp();
  loadLoginUsers();
}

function toast(msg, type='info') {
  const icons = { success:'âœ…', error:'âŒ', warning:'âš ï¸', info:'â„¹ï¸' };
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = '<span class="toast-icon">' + icons[type] + '</span><span class="toast-msg">' + msg + '</span>';
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function showGmailConnectionNotice() {
  const params = new URLSearchParams(window.location.search);
  const result = params.get('gmail_connection');
  if (!result) return;
  const notices = {
    connected: ['Gmail connected and profile verified.', 'success'],
    verification_failed: ['Google authorized the account, but required Gmail permissions or profile verification failed. Reauthorize Gmail.', 'warning'],
    failed: ['Gmail authorization failed. Check the configured redirect URI, OAuth client, and Google consent screen.', 'error'],
  };
  const notice = notices[result];
  if (notice) toast(notice[0], notice[1]);
  params.delete('gmail_connection');
  const query = params.toString();
  history.replaceState({}, '', window.location.pathname + (query ? '?' + query : '') + window.location.hash);
}

function openModal(title, bodyHTML, footerHTML='') {
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay'; overlay.id = 'active-modal';
  overlay.innerHTML = '<div class="modal"><div class="modal-header"><span class="modal-title">' + title + '</span><span class="modal-close" onclick="closeModal()">&#x2715;</span></div><div class="modal-body">' + bodyHTML + '</div>' + (footerHTML ? '<div class="modal-footer">' + footerHTML + '</div>' : '') + '</div>';
  overlay.addEventListener('click', e => { if (e.target === overlay) closeModal(); });
  document.body.appendChild(overlay);
}

function closeModal() {
  const m = document.getElementById('active-modal');
  if (m) m.remove();
}

function badge(val, type) {
  const cls = type || (val || '').toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g,'');
  return '<span class="badge badge-' + cls + '">' + (val || '&mdash;') + '</span>';
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  })[character]);
}

function fmtDate(s) {
  if (!s) return '&mdash;';
  return new Date(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function fmtDateTime(s) {
  if (!s) return '&mdash;';
  const d = new Date(s);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' });
}

function timeAgo(s) {
  if (!s) return '';
  const diff = (Date.now() - new Date(s)) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

function isManager() { return state.user && (state.user.role === 'manager' || state.user.role === 'admin'); }
function isAdmin() { return state.user && state.user.role === 'admin'; }

function setPage(page, navPage=page) {
  state.currentPage = page;
  state.currentNavPage = navPage;
  document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === navPage));
  document.querySelectorAll('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + page));
  if (page === 'drafts') {
    const title = document.querySelector('#page-drafts .page-title');
    const subtitle = document.querySelector('#page-drafts .page-subtitle');
    if (title) title.textContent = navPage === 'approvals' ? 'Review & Approvals' : 'Response Drafts';
    if (subtitle) subtitle.textContent = navPage === 'approvals' ? 'Human review queue · Approve or reject before any message is sent.' : 'AI-assisted replies, ready for your team to review and refine.';
  }
  loadPageData(page);
}

async function loadAllData() {
  state.data.dashboardLoadError = null;
  let stats;
  // Dashboard statistics are the primary view. Render them as soon as they
  // arrive instead of making them wait for unrelated workspace requests.
  try {
    stats = await api('GET', '/api/dashboard/stats');
    if (!stats) throw new Error('Dashboard statistics were unavailable. Refresh and sign in again if your session expired.');
    state.data.dashboardStats = stats;
    state.data.pendingDraftsCount = stats.pending_responses_count ?? 0;
    state.data.escalatedCount = stats.escalated_tickets ?? 0;
    updateNavBadges();
    if (state.currentPage === 'dashboard') renderDashboard();
  } catch(e) {
    state.data.dashboardLoadError = e.message || 'Dashboard data could not be loaded.';
    console.error('Dashboard data load failed:', e);
    if (state.currentPage === 'dashboard') renderDashboard();
    return;
  }

  // Secondary views can fail independently without hiding already loaded KPIs.
  const results = await Promise.allSettled([
    api('GET', '/api/tickets?limit=100'),
    api('GET', '/api/drafts?limit=100'),
    api('GET', '/api/customers?limit=100'),
    api('GET', '/api/gmail/worker/status'),
    isManager() ? api('GET', '/api/dashboard/snapshots') : Promise.resolve({snapshots: []}),
  ]);
  const valueAt = (index, fallback) => results[index].status === 'fulfilled' ? results[index].value : fallback;
  state.data.tickets = valueAt(0, state.data.tickets) || [];
  state.data.drafts = valueAt(1, state.data.drafts) || [];
  state.data.customers = valueAt(2, state.data.customers) || [];
  state.data.workerStatus = valueAt(3, null);
  state.data.snapshotHistory = valueAt(4, { snapshots: [] })?.snapshots || [];
  state.data.pendingDraftsCount = stats.pending_responses_count ?? state.data.drafts.filter(d => d.status === 'pending_review').length;
  state.data.escalatedCount = stats.escalated_tickets ?? state.data.tickets.filter(t => t.escalation_status === 'escalated').length;
  updateNavBadges();
  renderCurrentPage();
}

async function loadPageData(page) {
  if (page === 'dashboard') {
    try {
      // Dashboard KPIs are the primary view. Do not hold them behind Gmail
      // status or snapshot requests, which can be slow when integrations lag.
      const stats = await api('GET', '/api/dashboard/stats');
      if (!stats) throw new Error('Dashboard statistics are unavailable.');
      state.data.dashboardLoadError = null;
      state.data.dashboardStats = stats;
      state.data.pendingDraftsCount = stats.pending_responses_count ?? 0;
      state.data.escalatedCount = stats.escalated_tickets ?? 0;
      updateNavBadges();
      renderDashboard();
    } catch(e) {
      state.data.dashboardLoadError = e.message || 'Dashboard data could not be loaded.';
      renderDashboard();
      return;
    }

    // Update secondary dashboard panels independently after the KPIs render.
  const secondary = await Promise.allSettled([
      api('GET', '/api/gmail/worker/status'),
      isManager() ? api('GET', '/api/dashboard/snapshots') : Promise.resolve({snapshots: []}),
      (!state.data.analytics || Date.now() - state.data.analyticsLoadedAt > 300000) ? api('GET', '/api/dashboard/analytics') : Promise.resolve(state.data.analytics),
    ]);
    if (secondary[0].status === 'fulfilled') state.data.workerStatus = secondary[0].value || null;
    if (secondary[1].status === 'fulfilled') state.data.snapshotHistory = secondary[1].value?.snapshots || [];
    if (secondary[2].status === 'fulfilled') { state.data.analytics = secondary[2].value || null; state.data.analyticsLoadedAt = Date.now(); }
    if (state.currentPage === 'dashboard') renderDashboard();
  } else if (page === 'tickets') {
    loadTickets();
  } else if (page === 'drafts') {
    try { state.data.drafts = await api('GET', '/api/drafts?limit=100') || []; state.data.pendingDraftsCount = state.data.drafts.filter(d => d.status==='pending_review').length; updateNavBadges(); renderDraftsList(); } catch(e) {}
  } else if (page === 'customers') {
    renderCustomersList();
  } else if (page === 'customer_import') {
    renderCustomerImport();
  } else if (page === 'knowledge') {
    loadKnowledgeDocs();
  } else if (page === 'analytics') {
    loadAnalytics();
  } else if (page === 'settings') {
    loadSettings();
  }
}

function updateNavBadges() {
  const p = document.getElementById('badge-drafts');
  if (p) { p.textContent = state.data.pendingDraftsCount || ''; p.style.display = state.data.pendingDraftsCount ? '' : 'none'; }
  const e = document.getElementById('badge-escalated');
  if (e) { e.textContent = state.data.escalatedCount || ''; e.style.display = state.data.escalatedCount ? '' : 'none'; }
}

function renderCurrentPage() {
  if (state.currentPage === 'dashboard') renderDashboard();
  else if (state.currentPage === 'tickets') renderTicketsList();
  else if (state.currentPage === 'drafts') renderDraftsList();
  else if (state.currentPage === 'customers') renderCustomersList();
  else if (state.currentPage === 'customer_import') renderCustomerImport();
}

async function syncGmail() {
  const btn = document.getElementById('sync-gmail-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Syncing...'; }
  toast('Syncing incoming emails from Gmail...', 'info');
  try {
    const res = await api('POST', '/api/gmail/sync');
    const synced = res.synced_emails_count !== undefined ? res.synced_emails_count : (res.synced_count || 0);
    const created = res.tickets_created_or_updated ? res.tickets_created_or_updated.length : (res.tickets_created || 0);
    toast('Synchronized ' + synced + ' email(s), processed ' + created + ' ticket(s)' + (res.status === 'partial_success' ? '; AI drafting needs retry' : ''), res.status === 'partial_success' ? 'warning' : 'success');
    await loadAllData();
  } catch(e) {
    toast(e.message, 'warning');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#128231; Sync Gmail'; }
  }
}

function renderDashboard() {
  const s = state.data.dashboardStats;
  const el = document.getElementById('dashboard-content');
  if (!el) return;
  if (!s) {
    el.innerHTML = state.data.dashboardLoadError
      ? '<div class="card"><p class="text-danger">Dashboard data unavailable: ' + state.data.dashboardLoadError.replace(/</g, '&lt;') + '</p><button class="btn btn-secondary" onclick="loadAllData()">Retry</button></div>'
      : '<div class="loading-overlay"><div class="spinner"></div> Loading dashboard data...</div>';
    return;
  }

  // 0. Background Worker Status Banner
  const ws = state.data.workerStatus;
  const isRunning = ws && ws.monitoring_active;
  const autoSend = ws ? ws.auto_send_enabled : false;
  const gmailAuth = ws ? ws.gmail_authorized : false;
  const lastSyncStr = ws && ws.last_sync ? timeAgo(ws.last_sync) : 'never';
  const nextPollStr = ws && ws.next_poll_in_seconds !== null && ws.next_poll_in_seconds !== undefined ? ws.next_poll_in_seconds + 's' : (isRunning ? 'shortly' : 'paused');
  const connectedEmail = ws && ws.connected_email ? ws.connected_email : 'not verified';
  const gmailStatusLabels = {not_connected:'Not connected',authorization_expired:'Authorization expired',refresh_token_revoked_or_expired:'Authorization expired',reauthorization_required:'Authorization expired',oauth_network_error:'Google OAuth HTTPS connection failed; check outbound network access',oauth_client_configuration_error:'OAuth client configuration error',oauth_redirect_uri_mismatch:'OAuth redirect URI mismatch',gmail_api_unavailable:'Gmail API unavailable',permission_missing:'Required Gmail permission missing',required_permissions_missing:'Required Gmail permission missing',profile_verification_failed:'Gmail profile verification failed',authorization_required:'Authorization required',token_missing:'Not connected'};
  const monitorLabel = !ws ? 'Gmail status unavailable' : (!gmailAuth ? (gmailStatusLabels[ws?.gmail_status] || ws?.gmail_status || 'Not connected') : (ws.last_error ? 'Sync failed' : (isRunning ? 'Connected and monitoring' : 'Connected; sync worker paused')));
  const emailSyncCount = ws?.last_poll_metrics?.ingested_count;

  const workerBanner = '<div class="card mb-16" style="padding:14px 18px;background:linear-gradient(135deg, rgba(30, 41, 59, 0.7), rgba(15, 23, 42, 0.8));border:1px solid rgba(255,255,255,0.08);border-radius:10px;">' +
    '<div class="flex items-center justify-between flex-wrap gap-12">' +
      '<div class="flex items-center gap-12 flex-wrap">' +
        '<div class="flex items-center gap-8">' +
          '<span class="status-dot ' + (isRunning ? 'ok' : 'warn') + '"></span>' +
          '<strong style="font-size:13px">' + monitorLabel + '</strong>' +
        '</div>' +
        '<span style="color:var(--text-muted);font-size:12px">&bull;</span>' +
        '<span style="font-size:12px;color:var(--text-secondary)">Inbox Poll: <strong>' + (gmailAuth ? 'every ' + (ws?.poll_interval_seconds || 60) + 's' : 'paused') + '</strong></span>' +
        '<span style="color:var(--text-muted);font-size:12px">&bull;</span>' +
        '<span style="font-size:12px;color:var(--text-secondary)">Last Sync: <strong>' + (ws ? (ws.last_sync ? lastSyncStr : 'never') : 'unavailable') + '</strong></span>' +
        '<span style="color:var(--text-muted);font-size:12px">&bull;</span>' +
        '<span style="font-size:12px;color:var(--text-secondary)">Next Poll: <strong>' + nextPollStr + '</strong></span>' +
        '<span style="color:var(--text-muted);font-size:12px">&bull;</span>' +
        '<span style="font-size:12px;color:var(--text-secondary)">Gmail: <strong style="color:var(--text-accent)">' + (gmailAuth ? connectedEmail : monitorLabel) + '</strong></span>' +
        (emailSyncCount !== undefined ? '<span style="font-size:12px;color:var(--text-secondary)">Last poll synchronized: <strong>' + emailSyncCount + '</strong></span>' : '') +
        (ws?.last_error ? '<span class="text-danger text-sm">' + ws.last_error.replace(/</g, '&lt;') + '</span>' : '') +
      '</div>' +
      '<div class="flex items-center gap-12">' +
        '<span class="badge" style="background:' + (autoSend ? 'rgba(16, 185, 129, 0.15);color:#34d399;border:1px solid rgba(16, 185, 129, 0.3)' : 'rgba(245, 158, 11, 0.15);color:#fbbf24;border:1px solid rgba(245, 158, 11, 0.3)') + ';font-size:11.5px;padding:4px 10px;border-radius:6px;">' +
          '&#128274; Auto-Send: ' + (autoSend ? 'ENABLED' : 'DISABLED (Human approval required)') +
        '</span>' +
        (isAdmin() ? '<button class="btn btn-secondary btn-sm" onclick="setPage(\'settings\')">&#9881; Configure</button>' : '') +
      '</div>' +
    '</div>' +
  '</div>';

  // 1. KPI Cards Grid - Consolidated Metrics
  const kpiGrid = '<div class="stats-grid mb-16" style="grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));">' +
    '<div class="stat-card" style="--stat-color: var(--primary)"><span class="stat-icon">&#128232;</span><div class="stat-label">Incoming Emails</div><div class="stat-value">' + (s.incoming_emails_count ?? '—') + '</div><div class="stat-sub">' + (s.processed_emails_count ?? '—') + ' processed &middot; ' + (s.new_tickets ?? '—') + ' last 24h</div></div>' +
    '<div class="stat-card" style="--stat-color: var(--success)"><span class="stat-icon">&#10022;</span><div class="stat-label">AI Processing</div><div class="stat-value">' + (s.ai_success_count || 0) + '</div><div class="stat-sub">' + (s.ai_failed_count || 0) + ' failures &middot; Grounded RAG</div></div>' +
    '<div class="stat-card" style="--stat-color: var(--accent)"><span class="stat-icon">&#9997;&#65039;</span><div class="stat-label">Pending Responses</div><div class="stat-value">' + (s.pending_responses_count || s.total_drafts_pending || 0) + '</div><div class="stat-sub">' + (s.tickets_awaiting_approval || 0) + ' tickets awaiting approval</div></div>' +
    '<div class="stat-card" style="--stat-color: var(--info)"><span class="stat-icon">&#128228;</span><div class="stat-label">Sent Responses</div><div class="stat-value">' + (s.sent_responses_count ?? '—') + '</div><div class="stat-sub">Gmail acceptance confirmed</div></div>' +
    '<div class="stat-card" style="--stat-color: var(--danger)"><span class="stat-icon">&#9888;&#65039;</span><div class="stat-label">Failed Sends</div><div class="stat-value">' + (s.failed_sending_count ?? '—') + '</div><div class="stat-sub">' + ((s.failed_sending_count || 0) === 0 ? 'No recorded terminal failures' : 'Requires employee review') + '</div></div>' +
    '<div class="stat-card" style="--stat-color: var(--warning)"><span class="stat-icon">&#128680;</span><div class="stat-label">Attention Needed</div><div class="stat-value">' + (s.tickets_requiring_attention_count || s.high_priority_tickets || 0) + '</div><div class="stat-sub">' + (s.high_priority_tickets || 0) + ' high / ' + (s.escalated_tickets || 0) + ' escalated</div></div>' +
  '</div>';

  const sendMetrics = '<div class="stats-grid mb-16" style="grid-template-columns:repeat(auto-fill,minmax(170px,1fr))"><div class="stat-card"><div class="stat-label">Automatically Sent</div><div class="stat-value">' + (s.auto_sent_count ?? 0) + '</div></div><div class="stat-card"><div class="stat-label">Blocked Auto-Sends</div><div class="stat-value">' + (s.blocked_auto_sends_count ?? 0) + '</div></div></div>';

  // 2. Middle Row: Tickets Needing Attention & Pending Draft Responses
  const countCards = '<div class="stats-grid mb-16" style="grid-template-columns:repeat(auto-fill,minmax(170px,1fr))">' +
    [['Customers',s.total_customers],['All Tickets',s.total_tickets],['Open Tickets',s.total_open_tickets],['Resolved Tickets',s.total_resolved_tickets],['Approved Drafts',s.approved_drafts_count],['Indexed KB Documents',s.knowledge_documents_count]].map(x => '<div class="stat-card"><div class="stat-label">' + x[0] + '</div><div class="stat-value">' + (x[1] ?? '—') + '</div></div>').join('') + '</div>';

  const attentionList = (s.tickets_requiring_attention || []).map(t =>
    '<tr>' +
      '<td><span class="monospace" style="color:var(--text-accent)">' + t.ticket_number + '</span></td>' +
      '<td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + (t.subject || '') + '</td>' +
      '<td>' + (t.customer_name || '&mdash;') + '</td>' +
      '<td>' + badge(t.priority) + '</td>' +
      '<td>' + badge(t.status) + '</td>' +
      '<td>' + (t.escalation_status === 'escalated' ? badge('Escalated', 'urgent') : (t.escalation_status === 'recommended' ? badge('Escalation recommended', 'warning') : '&mdash;')) + '</td>' +
      '<td><div class="flex gap-8"><button class="btn btn-secondary btn-sm" onclick="viewTicket(' + t.id + ')">View</button><button class="btn btn-primary btn-sm" onclick="runAgent(' + t.id + ',\'' + t.ticket_number + '\')">&#10022; AI</button></div></td>' +
    '</tr>'
  ).join('');

  const pendingDrafts = (s.pending_drafts_list || []).map(d =>
    '<div class="card mb-8" style="margin-bottom:10px;padding:14px;background:rgba(255,255,255,0.02)">' +
      '<div class="flex justify-between items-center mb-8">' +
        '<div><span class="monospace text-sm" style="color:var(--text-accent)">To: ' + d.recipient + '</span></div>' +
        '<span class="text-muted text-sm">' + timeAgo(d.created_at) + '</span>' +
      '</div>' +
      '<div style="font-size:12px;font-weight:600;margin-bottom:4px">' + (d.subject || 'Draft Response') + '</div>' +
      '<div style="font-size:11px;color:var(--text-secondary);margin-bottom:10px;line-height:1.5">' + (d.body_preview || '').replace(/</g,'&lt;') + '...</div>' +
      '<div class="flex gap-8 items-center flex-wrap">' +
        '<span class="text-sm">Confidence: <strong>' + (d.confidence_score == null ? 'Unavailable' : (d.confidence_score * 100).toFixed(0) + '%') + '</strong></span>' +
        '<span class="badge badge-warning">Pending review</span>' +
        (d.rejection_reason ? '<span class="text-warning text-sm">' + escapeHtml(d.rejection_reason) + '</span>' : '') +
        (d.citations ? '<span class="text-muted text-sm">Citations: ' + escapeHtml(d.citations) + '</span>' : '') +
        (isManager() ? '<button class="btn btn-success btn-sm" onclick="approveDraft(' + d.id + ')">&#10003; Approve</button>' : '') +
        (isManager() ? '<button class="btn btn-danger btn-sm" onclick="rejectDraftModal(' + d.id + ')">&#10005; Reject</button>' : '') +
        '<button class="btn btn-secondary btn-sm" onclick="viewDraftFull(' + d.id + ')">Full View</button>' +
      '</div>' +
    '</div>'
  ).join('');

  const attentionSection = '<div class="grid-2 gap-16 mb-16">' +
    '<div class="card">' +
      '<div class="card-header"><span class="card-title">&#128680; Tickets Requiring Attention</span><button class="btn btn-secondary btn-sm" onclick="setPage(\'tickets\')">View All Tickets &rarr;</button></div>' +
      '<div class="table-wrap"><table><thead><tr><th>Ticket #</th><th>Subject</th><th>Customer</th><th>Priority</th><th>Status</th><th>Escalation</th><th>Action</th></tr></thead><tbody>' +
      (attentionList || '<tr><td colspan="7" class="table-empty">All tickets normal &mdash; no urgent action needed</td></tr>') +
      '</tbody></table></div>' +
    '</div>' +
    '<div class="card">' +
      '<div class="card-header"><span class="card-title">&#9997;&#65039; Pending Responses for Review</span><button class="btn btn-secondary btn-sm" onclick="setPage(\'drafts\')">View All Drafts &rarr;</button></div>' +
      '<div>' + (pendingDrafts || '<p class="text-muted text-sm" style="padding:20px;text-align:center">No response drafts currently awaiting review</p>') + '</div>' +
    '</div>' +
  '</div>';

  // 3. Bottom Row: Recent Agent Activity & Errors
  const activityItems = (s.recent_activity || []).map(a =>
    '<div class="activity-item"><div class="activity-dot"></div><div class="activity-details"><div class="activity-action">' + (a.details || a.action || '').replace(/</g,'&lt;') + '</div><div class="activity-time">' + timeAgo(a.timestamp) + '</div></div></div>'
  ).join('');

  const errorItems = (s.recent_agent_errors || []).map(e =>
    '<div class="activity-item" style="border-left:2px solid var(--danger);padding-left:10px;margin-bottom:8px">' +
      '<div class="activity-details">' +
        '<div style="font-size:12px;font-weight:600;color:var(--danger)">Ticket #' + (e.ticket_id || 'System') + ' (' + (e.category || 'General') + ')</div>' +
        '<div style="font-size:11px;color:var(--text-secondary)">' + (e.error_message || 'AI pipeline diagnostic recorded').replace(/</g,'&lt;') + '</div>' +
        '<div class="activity-time">' + timeAgo(e.timestamp) + '</div>' +
      '</div>' +
    '</div>'
  ).join('');

  const logsSection = '<div class="grid-2 gap-16">' +
    '<div class="card">' +
      '<div class="card-header"><span class="card-title">&#128220; Recent Agent Activity</span></div>' +
      '<div>' + (activityItems || '<p class="text-muted text-sm">No recent activity recorded</p>') + '</div>' +
    '</div>' +
    '<div class="card">' +
      '<div class="card-header"><span class="card-title">&#128737;&#65039; Recent Agent Errors &amp; Diagnostics</span></div>' +
      '<div>' + (errorItems || '<div style="padding:24px;text-align:center;color:var(--success)"><div style="font-size:24px;margin-bottom:6px">&#9989;</div><div style="font-weight:600;font-size:13px">All AI Operations Nominal</div><div class="text-muted text-sm mt-8">Zero unhandled agent errors or failed executions detected</div></div>') + '</div>' +
    '</div>' +
  '</div>';

  const history = state.data.snapshotHistory || [];
  const selected = state.data.selectedSnapshot || history[0];
  const snapshotOptions = history.map((x,i) => '<option value="' + x.id + '" ' + (selected && selected.id === x.id ? 'selected' : '') + '>Snapshot ' + new Date(x.created_at).toLocaleString() + (i===0?' (Latest)':'') + '</option>').join('');
  const changeMap = selected?.comparison || {};
  const historyRows = selected ? ['incoming_emails','processed_emails','ai_successes','ai_failures','pending_responses','tickets_awaiting_approval','sent_responses','failed_sends'].map(k => '<tr><td>' + k.replaceAll('_',' ') + '</td><td>' + selected[k] + '</td><td>' + (changeMap[k] === undefined ? '—' : (changeMap[k] > 0 ? '+' : '') + changeMap[k]) + '</td></tr>').join('') : '<tr><td colspan="3" class="table-empty">No historical snapshots saved yet</td></tr>';
  const historySection = isManager() ? '<div class="card mt-16"><div class="card-header"><span class="card-title">Weekly Historical Reporting</span>' + (isAdmin() ? '<button class="btn btn-primary btn-sm" onclick="saveDashboardSnapshot()">Save Historical Snapshot</button>' : '') + '</div><div class="flex items-center gap-12 flex-wrap mb-12"><span class="text-sm text-muted">Live statistics above refresh from PostgreSQL. Snapshot selection only changes this historical report.</span><select onchange="selectDashboardSnapshot(this.value)" style="max-width:320px"><option value="">Select snapshot</option>' + snapshotOptions + '</select></div>' + (selected ? '<div class="text-sm text-muted mb-8">Period: ' + new Date(selected.period_start).toLocaleString() + ' – ' + new Date(selected.period_end).toLocaleString() + ' · Saved: ' + new Date(selected.created_at).toLocaleString() + ' · Previous: ' + (history[1] ? new Date(history[1].created_at).toLocaleString() : 'none') + '</div>' : '') + '<div class="table-wrap"><table><thead><tr><th>Metric</th><th>Selected snapshot</th><th>Change vs previous</th></tr></thead><tbody>' + historyRows + '</tbody></table></div></div>' : '';
  const dashboardError = state.data.dashboardLoadError ? '<div class="card mb-16"><p class="text-danger">Refresh failed: ' + state.data.dashboardLoadError.replace(/</g, '&lt;') + '</p><button class="btn btn-secondary btn-sm" onclick="loadAllData()">Retry</button></div>' : '';
  const emptyState = (!s.total_customers && !s.total_tickets && !s.incoming_emails_count)
    ? '<div class="card mb-16"><strong>No customer support activity yet.</strong><p class="text-muted">New customer emails and support requests will appear here after Gmail synchronizes actual messages.</p></div>' : '';
  const metric = (tone, icon, label, value, note) => '<article class="command-metric ' + tone + '"><div class="command-metric-top"><span class="command-metric-icon">' + icon + '</span><span class="command-metric-label">' + label + '</span></div><strong>' + (value ?? '—') + '</strong><small>' + note + '</small></article>';
  const metrics = '<div class="command-metrics">' +
    metric('tint-blue','&#9636;','Total tickets',s.total_tickets,'Across all recorded support') +
    metric('tint-violet','&#9711;','Open tickets',s.total_open_tickets,'Currently awaiting resolution') +
    metric('tint-amber','&#9998;','Pending drafts',s.pending_responses_count ?? s.total_drafts_pending,'Responses awaiting human review') +
    metric('tint-green','&#9673;','Customers',s.total_customers,'Customer profiles in your directory') +
    metric('tint-cyan','&#8599;','Processed emails',s.processed_emails_count,'Messages handled by the platform') +
    metric('tint-indigo','&#10022;','AI agent successes',s.ai_success_count,'Successful runs recorded') + '</div>';
  const analytics = state.data.analytics || {};
  const volume = (analytics.ticket_volume_by_day || []).slice(-7);
  const maxVolume = Math.max(1, ...volume.map(x => Number(x.tickets) || 0));
  const linePoints = volume.map((x, i) => {
    const px = volume.length < 2 ? 50 : 12 + i * (76 / (volume.length - 1));
    const py = 82 - ((Number(x.tickets) || 0) / maxVolume) * 66;
    return { x: px, y: py, date: x.date, value: Number(x.tickets) || 0 };
  });
  const polyline = linePoints.map(p => p.x + ',' + p.y).join(' ');
  const volumeChart = volume.length ? '<svg class="activity-chart" viewBox="0 0 100 100" role="img" aria-label="Ticket volume over recent days"><defs><linearGradient id="activityFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#5685F5" stop-opacity=".28"/><stop offset="1" stop-color="#5685F5" stop-opacity="0"/></linearGradient></defs><path d="M12 18H94M12 48H94M12 78H94" class="chart-gridline"/><polygon points="' + (linePoints.length ? linePoints[0].x + ',92 ' + polyline + ' ' + linePoints[linePoints.length-1].x + ',92' : '') + '" fill="url(#activityFill)"/><polyline points="' + polyline + '" class="chart-line"/>' + linePoints.map(p => '<circle cx="' + p.x + '" cy="' + p.y + '" r="1.7" class="chart-point"><title>' + escapeHtml(p.date) + ': ' + p.value + ' tickets</title></circle>').join('') + '</svg><div class="chart-xlabels">' + linePoints.map(p => '<span title="' + escapeHtml(p.date) + '">' + escapeHtml(String(p.date).slice(-5)) + '</span>').join('') + '</div>' : '<div class="chart-empty">Ticket trend data will appear when available.</div>';
  const statuses = [ ['Open', Number(s.total_open_tickets) || 0, 'blue'], ['Pending review', Number(s.tickets_awaiting_approval) || 0, 'amber'], ['Resolved', Number(s.total_resolved_tickets) || 0, 'green'] ];
  const statusTotal = statuses.reduce((n, x) => n + x[1], 0);
  const statusViz = statusTotal ? '<div class="status-stack">' + statuses.filter(x => x[1] > 0).map(x => '<span class="status-segment ' + x[2] + '" style="width:' + (x[1] / statusTotal * 100).toFixed(1) + '%" title="' + x[0] + ': ' + x[1] + '"></span>').join('') + '</div><div class="status-legend">' + statuses.map(x => '<div><i class="legend-dot ' + x[2] + '"></i><span>' + x[0] + '</span><strong>' + x[1] + '</strong></div>').join('') + '</div>' : '<div class="chart-empty">No ticket status counts available yet.</div>';
  const chartSection = '<section class="command-insights"><article class="insight-panel trend-panel"><div class="insight-heading"><div><span class="section-eyebrow">SUPPORT PULSE</span><h3>Ticket activity</h3></div><span class="insight-period">Recent days</span></div>' + volumeChart + '</article><article class="insight-panel status-panel"><div class="insight-heading"><div><span class="section-eyebrow">LIVE WORKLOAD</span><h3>Ticket status</h3></div><span class="insight-mark">&#9675;</span></div>' + statusViz + '</article><article class="insight-panel gmail-panel"><div class="gmail-panel-top"><span class="gmail-symbol">G</span><span class="gmail-live ' + (gmailAuth ? 'is-connected' : '') + '"><i></i>' + (gmailAuth ? 'Connected' : 'Needs attention') + '</span></div><span class="section-eyebrow">EMAIL INTEGRATION</span><h3>Gmail</h3><p>' + escapeHtml(gmailAuth ? connectedEmail : monitorLabel) + '</p><div class="gmail-meta"><span>Last sync</span><strong>' + (ws?.last_sync ? lastSyncStr : 'Not synced') + '</strong></div><div class="gmail-meta"><span>Monitoring</span><strong>' + (isRunning ? 'Active' : 'Paused') + '</strong></div></article></section>';
  const hero = '<section class="command-hero"><div class="hero-copy"><span class="hero-eyebrow"><i></i> SMARTTECH SUPPORT INTELLIGENCE</span><h2>Your Support<br><em>Command Center</em></h2><p>Intelligent customer support, powered by AI.</p></div><div class="hero-network" aria-hidden="true"><div class="hero-orbit orbit-a"></div><div class="hero-orbit orbit-b"></div><div class="hero-orbit orbit-c"></div><div class="hero-core"><span>ST</span></div><i class="hero-node node-1"></i><i class="hero-node node-2"></i><i class="hero-node node-3"></i><i class="hero-node node-4"></i></div><div class="hero-footer"><span><i class="status-dot ' + (isRunning ? 'ok' : 'warn') + '"></i>' + escapeHtml(monitorLabel) + '</span><span>Inbox polling ' + (gmailAuth ? 'every ' + (ws?.poll_interval_seconds || 60) + 's' : 'paused') + '</span><button class="hero-sync" id="sync-gmail-btn" onclick="syncGmail()">&#8635; Sync mail</button></div></section>';
  const activityTimeline = (s.recent_activity || []).slice(0,5).map(a => '<div class="timeline-entry"><i class="timeline-point"></i><div><strong>' + escapeHtml(a.details || a.action || 'Support activity') + '</strong><small>' + timeAgo(a.timestamp) + '</small></div></div>').join('') || '<p class="chart-empty">No recent agent activity recorded.</p>';
  const bottom = '<section class="command-bottom"><article class="insight-panel recent-activity-panel"><div class="insight-heading"><div><span class="section-eyebrow">AUTOMATION</span><h3>Recent agent activity</h3></div><span class="insight-period">Live events</span></div><div class="activity-timeline">' + activityTimeline + '</div></article><article class="insight-panel agent-panel"><div class="insight-heading"><div><span class="section-eyebrow">AI OPERATIONS</span><h3>Agent performance</h3></div><button class="text-link" onclick="setPage(\'analytics\')">View analytics &rarr;</button></div><div class="agent-stat"><span>Successful runs</span><strong>' + (s.ai_success_count ?? '—') + '</strong></div><div class="agent-stat"><span>Failed runs</span><strong class="agent-failures">' + (s.ai_failed_count ?? '—') + '</strong></div><div class="agent-stat"><span>Drafts awaiting review</span><strong>' + (s.pending_responses_count ?? '—') + '</strong></div><div class="agent-note">Human approval remains part of your response workflow.</div></article></section>';
  const recentTickets = (state.data.tickets || []).slice(0,5).map(t=>'<tr><td><span class="monospace">'+escapeHtml(t.ticket_number)+'</span></td><td><strong>'+escapeHtml(t.subject||'Support request')+'</strong></td><td>'+escapeHtml(t.customer_name||t.customer?.full_name||'&mdash;')+'</td><td>'+badge(t.status)+'</td><td>'+badge(t.priority)+'</td><td class="text-sm text-muted">'+timeAgo(t.created_at)+'</td><td><button class="btn btn-secondary btn-sm" onclick="viewTicket('+t.id+')">Open</button></td></tr>').join('');
  const recentTicketSection = '<section class="recent-ticket-section"><div class="insight-heading"><div><span class="section-eyebrow">CUSTOMER INBOX</span><h3>Recent tickets</h3></div><button class="text-link" onclick="setPage(\'tickets\')">All tickets &rarr;</button></div><div class="table-wrap"><table><thead><tr><th>Ticket</th><th>Subject</th><th>Customer</th><th>Status</th><th>Priority</th><th>Created</th><th></th></tr></thead><tbody>'+(recentTickets||'<tr><td colspan="7" class="table-empty">No tickets available.</td></tr>')+'</tbody></table></div></section>';
  el.innerHTML = dashboardError + hero + emptyState + metrics + chartSection + attentionSection + bottom + recentTicketSection + logsSection + historySection;
}

async function saveDashboardSnapshot() {
  try {
    await api('POST', '/api/dashboard/snapshots?force=true');
    toast('Historical snapshot saved', 'success');
    state.data.selectedSnapshot = null;
    await loadAllData();
  } catch(e) { toast(e.message, 'warning'); }
}

function selectDashboardSnapshot(id) {
  state.data.selectedSnapshot = (state.data.snapshotHistory || []).find(x => String(x.id) === String(id)) || null;
  renderDashboard();
}

async function connectGmail() {
  try {
    // Let the backend use GMAIL_REDIRECT_URI exactly as registered in Google
    // Cloud. Deriving this from window.location.origin breaks when the SPA is
    // opened on 127.0.0.1 but OAuth is registered against localhost (or vice versa).
    const flow = await api('GET', '/api/gmail/auth-url');
    window.location.assign(flow.authorization_url);
  } catch(e) { toast(e.message, 'error'); }
}


async function loadTickets(params={}) {
  const qs = new URLSearchParams({ limit: 100, ...params }).toString();
  try { state.data.tickets = await api('GET', '/api/tickets?' + qs) || []; renderTicketsList(); } catch(e) { toast(e.message, 'error'); }
}

function renderTicketsList() {
  const el = document.getElementById('tickets-content');
  if (!el) return;
  const tickets = state.data.tickets;
  const rows = tickets.map(t => '<tr><td><span class="monospace" style="color:var(--text-accent)">' + t.ticket_number + '</span></td><td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + (t.subject||'') + '</td><td>' + (t.customer_name || t.customer?.full_name || '&mdash;') + '</td><td>' + badge(t.status) + '</td><td>' + badge(t.priority) + '</td><td>' + badge(t.category) + '</td><td>' + (t.escalation_status === 'escalated' ? badge('Escalated','urgent') : (t.escalation_status === 'recommended' ? badge('Escalation recommended','warning') : '&mdash;')) + '</td><td class="text-muted text-sm">' + timeAgo(t.created_at) + '</td><td><div class="flex gap-8"><button class="btn btn-secondary btn-sm" onclick="viewTicket(' + t.id + ')">View</button><button class="btn btn-primary btn-sm" onclick="runAgent(' + t.id + ',\'' + t.ticket_number + '\')">&#10022; AI</button></div></td></tr>').join('');
  const counts = [['All tickets',tickets.length,'neutral'],['Open',tickets.filter(t=>t.status==='open').length,'blue'],['In progress',tickets.filter(t=>t.status==='in_progress').length,'violet'],['Pending review',tickets.filter(t=>t.status==='awaiting_approval').length,'amber'],['Resolved',tickets.filter(t=>t.status==='resolved'||t.status==='closed').length,'green']];
  const overview = '<div class="page-overview ticket-overview">' + counts.map(x=>'<div class="overview-item"><span>'+x[0]+'</span><strong class="text-'+x[2]+'">'+x[1]+'</strong></div>').join('') + '</div>';
  el.innerHTML = overview + '<div class="ticket-toolbar"><div class="ticket-search-wrap"><span>⌕</span><input id="ticket-search" type="text" placeholder="Search tickets or customers..." oninput="filterTickets(this.value)" /></div><select aria-label="Filter by status" onchange="loadTickets({status:this.value})"><option value="">All statuses</option><option value="open">Open</option><option value="in_progress">In progress</option><option value="awaiting_approval">Pending review</option><option value="resolved">Resolved</option><option value="closed">Closed</option></select><select aria-label="Filter by priority" onchange="loadTickets({priority:this.value})"><option value="">All priorities</option><option value="urgent">Urgent</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select><button class="btn btn-primary" onclick="showCreateTicketModal()">+ New Ticket</button></div><div class="table-wrap" id="tickets-table-wrap"><table id="tickets-table"><thead><tr><th>Ticket #</th><th>Subject</th><th>Customer</th><th>Status</th><th>Priority</th><th>Category</th><th>Escalation</th><th>Created</th><th>Actions</th></tr></thead><tbody>' + (rows||'<tr><td colspan="9" class="table-empty">No tickets found</td></tr>') + '</tbody></table></div>';
}

function filterTickets(q) {
  document.querySelectorAll('#tickets-table tbody tr').forEach(row => { row.style.display = row.textContent.toLowerCase().includes(q.toLowerCase()) ? '' : 'none'; });
}

async function viewTicket(ticketId) {
  try {
    const [ticket, drafts, executions] = await Promise.all([
      api('GET', '/api/tickets/' + ticketId),
      api('GET', '/api/drafts?ticket_id=' + ticketId),
      api('GET', '/api/agent/executions/' + ticketId),
    ]);
    const messages = ticket.messages || [];
    const msgHTML = messages.map(m => '<div class="message-item message-' + m.sender_type + '"><div class="message-sender">' + (m.sender_name||m.sender_type) + ' &middot; ' + (m.is_internal_note ? '&#128274; Internal' : '&#128231; Email') + '</div><div class="message-body">' + (m.message_body||'').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</div><div class="message-time">' + fmtDateTime(m.created_at) + '</div></div>').join('') || '<p class="text-muted text-sm">No messages</p>';
    const lastExec = executions && executions[0];
    const draftEmpty = lastExec?.status === 'failed'
      ? '<p class="text-warning text-sm">AI response generation failed. ' + escapeHtml(lastExec.error_message || 'See the execution trace for details.') + ' Use AI Draft to retry after correcting the reported issue.</p>'
      : '<p class="text-muted text-sm">No drafts yet. Use AI Draft to start generation.</p>';
    const draftHTML = (drafts||[]).map(d => '<div class="card mb-8" style="margin-bottom:10px"><div class="flex items-center justify-between mb-8">' + badge(d.status) + '<span class="text-sm text-muted">' + fmtDateTime(d.created_at) + '</span></div>' + (d.rejection_reason ? '<div style="font-size:11.5px;color:#fbbf24;background:rgba(245,158,11,0.1);padding:4px 8px;border-radius:4px;margin-bottom:6px;border-left:2px solid #f59e0b">&#9888;&#65039; ' + escapeHtml(d.rejection_reason) + '</div>' : '') + '<div style="font-size:12px;color:var(--text-secondary);margin-bottom:6px">To: ' + escapeHtml(d.recipient) + '</div><div style="font-size:12px;font-weight:600;margin-bottom:6px">' + escapeHtml(d.subject||'') + '</div><div style="font-size:12px;color:var(--text-secondary);white-space:pre-wrap;max-height:120px;overflow-y:auto">' + escapeHtml((d.body||'').substring(0,350)) + (d.body&&d.body.length>350?'...':'') + '</div><div class="flex gap-8" style="margin-top:8px;flex-wrap:wrap">' + (d.status==='pending_review'&&isManager()?'<button class="btn btn-success btn-sm" onclick="approveDraft(' + d.id + ')">&#10003; Approve</button>':'') + (d.status==='pending_review'&&isManager()?'<button class="btn btn-danger btn-sm" onclick="rejectDraftModal(' + d.id + ')">&#10005; Reject</button>':'') + (d.status==='approved'?'<button class="btn btn-primary btn-sm" onclick="sendDraft(' + d.id + ')">&#128228; Send</button>':'') + '<button class="btn btn-secondary btn-sm" onclick="viewDraftFull(' + d.id + ')">Full View</button></div></div>').join('') || draftEmpty;
    let executionDiagnostics = null;
    try { executionDiagnostics = JSON.parse(lastExec?.reasoning_summary || 'null'); } catch (_) { executionDiagnostics = null; }
    const stageRows = Object.entries(executionDiagnostics?.stages || {}).map(([name, stage]) => {
      const details = stage.details || {};
      const extras = [details.chunks_retrieved == null ? '' : details.chunks_retrieved + ' chunks', (details.documents || []).join(', '), details.model || ''].filter(Boolean).join(' · ');
      return '<div class="trace-step"><div class="trace-dot">&#8226;</div><div class="trace-content"><div class="trace-tool-name">' + escapeHtml(name.replace(/_/g,' ')) + '</div><div class="trace-meta">' + badge(stage.status) + '<span>' + (stage.duration_ms == null ? '—' : stage.duration_ms + 'ms') + '</span></div>' + (extras ? '<div class="text-muted text-sm">' + escapeHtml(extras) + '</div>' : '') + (stage.error ? '<div class="text-warning text-sm">' + escapeHtml(stage.error) + '</div>' : '') + '</div></div>';
    }).join('');
    const traceHTML = lastExec ? '<div class="flex gap-8 text-sm">Execution status: ' + badge(lastExec.status) + (lastExec.execution_time_ms == null ? '' : '<span>' + lastExec.execution_time_ms + 'ms</span>') + '</div>' + (lastExec.error_message ? '<div class="text-warning text-sm" role="alert" style="margin-top:8px">' + escapeHtml(lastExec.error_message) + '</div>' : '') + (executionDiagnostics ? '<div class="text-muted text-sm" style="margin-top:6px">Provider: ' + escapeHtml(executionDiagnostics.provider || '—') + ' · Model: ' + escapeHtml(executionDiagnostics.model || '—') + '</div>' : '') + '<div class="trace-timeline">' + stageRows + (lastExec.tool_executions||[]).map((t,i) => '<div class="trace-step"><div class="trace-dot">' + (i+1) + '</div><div class="trace-content"><div class="trace-tool-name">' + escapeHtml(t.tool_name.replace(/_/g,' ')) + '</div><div class="trace-meta">' + badge(t.status) + '<span>' + t.duration_ms + 'ms</span></div></div></div>').join('') + '</div><div class="separator"></div><div class="flex gap-8 text-sm">Confidence: <strong class="' + (lastExec.confidence_score == null ? 'text-muted' : 'text-success') + '">' + (lastExec.confidence_score == null ? 'Unavailable (generation failed before evaluation)' : (lastExec.confidence_score*100).toFixed(0)+'%') + '</strong></div>' : '<p class="text-muted text-sm">No AI executions yet</p>';
    const ticketBadges = [badge(ticket.status), badge(ticket.priority), badge(ticket.category), ticket.escalation_status==='escalated'?badge('Escalated','urgent'):(ticket.escalation_status==='recommended'?badge('Escalation recommended','warning'):''), ticket.has_pending_draft?badge('Draft Pending','in_progress'):''].filter(Boolean).join('');
    const body = '<div class="ticket-detail-grid"><div><div class="card mb-16"><div class="flex items-center gap-8 mb-16 flex-wrap">' + ticketBadges + '</div><div class="grid-2 gap-16 text-sm mb-16"><div><span class="text-muted">Customer:</span> <strong>' + (ticket.customer_name || ticket.customer?.full_name || '&mdash;') + '</strong></div><div><span class="text-muted">Created:</span> ' + fmtDateTime(ticket.created_at) + '</div></div><div class="separator"></div><div class="card-title mb-8">Message Thread</div><div class="messages-list">' + msgHTML + '</div></div><div class="card"><div class="card-header"><span class="card-title">Add Response</span></div><div class="form-group mb-8"><textarea id="reply-body-' + ticketId + '" placeholder="Type your response..." rows="4"></textarea></div><div class="flex gap-8 items-center flex-wrap"><label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer"><input type="checkbox" id="reply-internal-' + ticketId + '" /> Internal Note</label><button class="btn btn-primary btn-sm" onclick="sendReply(' + ticketId + ')">Send Reply</button><button class="btn btn-secondary btn-sm" onclick="runAgent(' + ticketId + ',\'' + ticket.ticket_number + '\')">&#10022; AI Draft</button></div></div></div><div><div class="card mb-16"><div class="card-header"><span class="card-title">Drafts</span></div>' + draftHTML + '</div><div class="card"><div class="card-header"><span class="card-title">AI Execution Trace</span></div>' + traceHTML + '</div></div></div>';
    openModal('&#127915; ' + ticket.ticket_number + ': ' + ticket.subject, body, '');
  } catch(e) { toast(e.message, 'error'); }
}

async function sendReply(ticketId) {
  const body = document.getElementById('reply-body-' + ticketId)?.value?.trim();
  const isInternal = document.getElementById('reply-internal-' + ticketId)?.checked;
  if (!body) return toast('Please enter a reply message', 'warning');
  try {
    await api('POST', '/api/tickets/' + ticketId + '/messages', { message_body: body, is_internal_note: isInternal });
    toast('Reply sent', 'success'); closeModal(); await viewTicket(ticketId);
  } catch(e) { toast(e.message, 'error'); }
}

async function runAgent(ticketId, ticketNum) {
    toast('Running AI Agent for ' + ticketNum + '...', 'info');
  try {
    const res = await api('POST', '/api/agent/run', { ticket_id: ticketId, force_regenerate: true });
    toast('&#9989; Draft generated! Confidence: ' + ((res.confidence_score||0)*100).toFixed(0) + '%', 'success');
    state.data.drafts = await api('GET', '/api/drafts?limit=100') || [];
    state.data.pendingDraftsCount = state.data.drafts.filter(d => d.status==='pending_review').length;
    updateNavBadges();
    await loadAllData();
    if (document.getElementById('active-modal')) { closeModal(); await viewTicket(ticketId); }
  } catch(e) { toast('AI run failed: ' + e.message, 'error'); }
}

async function showCreateTicketModal() {
  const customers = state.data.customers;
  const opts = customers.map(c => '<option value="' + c.id + '">' + c.full_name + ' (' + c.email + ')</option>').join('');
  const body = '<div class="flex-col flex gap-12"><div class="form-group"><label>Customer *</label><select id="new-ticket-customer">' + opts + '</select></div><div class="form-group"><label>Subject *</label><input id="new-ticket-subject" type="text" placeholder="Brief description of the issue" /></div><div class="grid-2 gap-16"><div class="form-group"><label>Priority</label><select id="new-ticket-priority"><option value="medium">Medium</option><option value="high">High</option><option value="urgent">Urgent</option><option value="low">Low</option></select></div><div class="form-group"><label>Category</label><select id="new-ticket-category"><option value="technical">Technical</option><option value="order">Order</option><option value="warranty">Warranty</option><option value="billing">Billing</option><option value="returns">Returns</option><option value="general">General</option></select></div></div><div class="form-group"><label>Initial Message</label><textarea id="new-ticket-msg" placeholder="Customer message..." rows="4"></textarea></div></div>';
  openModal('Create New Ticket', body, '<button class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-primary" onclick="createTicket()">Create Ticket</button>');
}

async function createTicket() {
  const customer_id = parseInt(document.getElementById('new-ticket-customer')?.value);
  const subject = document.getElementById('new-ticket-subject')?.value?.trim();
  const priority = document.getElementById('new-ticket-priority')?.value;
  const category = document.getElementById('new-ticket-category')?.value;
  const initial_message = document.getElementById('new-ticket-msg')?.value?.trim() || null;
  if (!subject || !customer_id) return toast('Customer and subject are required', 'warning');
  try {
    await api('POST', '/api/tickets', { customer_id, subject, priority, category, initial_message });
    toast('Ticket created', 'success'); closeModal(); await loadTickets(); await loadAllData();
  } catch(e) { toast(e.message, 'error'); }
}

function renderDraftsList() {
  const el = document.getElementById('drafts-content');
  if (!el) return;
  const allDrafts = state.data.drafts;
  const approvalView = state.currentNavPage === 'approvals';
  const drafts = approvalView ? allDrafts.filter(d => d.status === 'pending_review') : allDrafts;
  const rows = drafts.map(d => '<tr><td><span class="monospace text-muted">#' + d.id + '</span></td><td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + (d.subject||'&mdash;') + '</td><td style="font-size:12px">' + (d.recipient||'&mdash;') + '</td><td>' + badge(d.status) + '</td><td style="max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:var(--text-secondary)">' + (d.body||'').substring(0,80) + '...</td><td class="text-sm text-muted">' + timeAgo(d.created_at) + '</td><td><div class="flex gap-8">' + (d.status==='pending_review'&&isManager()?'<button class="btn btn-success btn-sm" onclick="approveDraft(' + d.id + ')">&#10003;</button>':'') + (d.status==='pending_review'&&isManager()?'<button class="btn btn-danger btn-sm" onclick="rejectDraftModal(' + d.id + ')">&#10005;</button>':'') + (d.status==='approved'?'<button class="btn btn-primary btn-sm" onclick="sendDraft(' + d.id + ')">&#128228; Send</button>':'') + '<button class="btn btn-secondary btn-sm" onclick="viewDraftFull(' + d.id + ')">View</button></div></td></tr>').join('');
  const pending = allDrafts.filter(d=>d.status==='pending_review').length, approved=allDrafts.filter(d=>d.status==='approved').length, rejected=allDrafts.filter(d=>d.status==='rejected').length;
  const overview = '<div class="page-overview draft-overview"><div class="overview-item"><span>All responses</span><strong>'+allDrafts.length+'</strong></div><div class="overview-item"><span>Awaiting review</span><strong class="text-amber">'+pending+'</strong></div><div class="overview-item"><span>Approved</span><strong class="text-green">'+approved+'</strong></div><div class="overview-item"><span>Rejected</span><strong class="text-red">'+rejected+'</strong></div></div>';
  el.innerHTML = overview + '<div class="draft-toolbar">'+(approvalView?'<div class="review-callout"><span>✳</span><div><strong>Review before sending</strong><small>Approval authorizes the response; sending remains a separate action.</small></div></div>':'<div class="draft-toolbar-label">Your response workspace</div>')+'<select aria-label="Filter drafts by status" onchange="filterDraftsByStatus(this.value)" style="max-width:190px"><option value="">'+(approvalView?'Pending review':'All statuses')+'</option><option value="pending_review">Pending review</option><option value="approved">Approved</option><option value="rejected">Rejected</option><option value="sent">Sent</option></select></div><div class="table-wrap draft-table-wrap"><table><thead><tr><th>ID</th><th>Subject</th><th>Recipient</th><th>Status</th><th>Preview</th><th>Created</th><th>Actions</th></tr></thead><tbody>' + (rows||(approvalView?'<tr><td colspan="7" class="table-empty">The review queue is clear.</td></tr>':'<tr><td colspan="7" class="table-empty">No drafts found</td></tr>')) + '</tbody></table></div>';
}

async function filterDraftsByStatus(status) {
  const qs = status ? '?status=' + status + '&limit=100' : '?limit=100';
  state.data.drafts = await api('GET', '/api/drafts' + qs) || [];
  renderDraftsList();
}

async function viewDraftFull(draftId) {
  try {
    const d = await api('GET', '/api/drafts/' + draftId);
    let citations = [];
    try { citations = JSON.parse(d.citations || '[]'); } catch {}
    citations = Array.isArray(citations) ? citations.filter(c => c && typeof c === 'object' && (c.document || c.document_title) && (c.citation || c.section || c.page)) : [];
    const citeHTML = citations.length ? '<div class="separator"></div><div class="card-title mb-8">&#128218; Sources (' + citations.length + ')</div><div class="citations-list">' + citations.map(c => { const score = Number.isFinite(c.similarity) ? c.similarity : c.relevance; return '<div class="citation-item"><div class="flex justify-between items-center mb-8"><span class="citation-doc">' + escapeHtml(c.document || c.document_title) + '</span><span class="citation-score">' + (Number.isFinite(score) ? 'Relevance: ' + (score*100).toFixed(0)+'%' : '') + '</span></div><div style="font-size:11px;color:var(--text-muted)">' + escapeHtml(c.citation || [c.section, c.page ? 'Page ' + c.page : ''].filter(Boolean).join(' · ')) + '</div></div>'; }).join('') + '</div>' : '';
    const reasonHTML = d.rejection_reason ? '<div class="card mb-16" style="padding:10px 14px;background:rgba(245, 158, 11, 0.1);border-left:3px solid var(--warning);border-radius:6px;font-size:12.5px;color:#fbbf24">&#9888;&#65039; <strong>Auto-Send Status:</strong> ' + d.rejection_reason + '</div>' : '';
    const body = '<div><div class="flex items-center gap-8 mb-16 flex-wrap">' + badge(d.status) + (d.approved_body_hash?'<span class="text-sm" style="color:var(--success)">&#128272; Cryptographically signed</span>':'') + '</div>' + reasonHTML + '<div class="grid-2 gap-16 mb-16 text-sm"><div><span class="text-muted">To:</span> <strong>' + d.recipient + '</strong></div><div><span class="text-muted">Created:</span> ' + fmtDateTime(d.created_at) + '</div></div><div class="form-group mb-16"><label>Subject</label><input id="edit-draft-subject-' + d.id + '" type="text" value="' + (d.subject||'').replace(/"/g,'&quot;') + '" /></div><div class="form-group mb-16"><label>Email Body</label><textarea id="edit-draft-body-' + d.id + '" style="min-height:200px;font-size:13px;line-height:1.7" rows="12">' + (d.body||'') + '</textarea></div>' + citeHTML + '</div>';

    const footer = '<button class="btn btn-secondary" onclick="closeModal()">Close</button>' + (d.status==='pending_review'?'<button class="btn btn-secondary" onclick="saveDraftEdit(' + d.id + ')">&#128190; Save Edits</button>':'') + (d.status==='pending_review'&&isManager()?'<button class="btn btn-success" onclick="approveDraft(' + d.id + ')">&#10003; Approve</button>':'') + (d.status==='pending_review'&&isManager()?'<button class="btn btn-danger" onclick="rejectDraftModal(' + d.id + ')">&#10005; Reject</button>':'') + (d.status==='approved'?'<button class="btn btn-primary" onclick="sendDraft(' + d.id + ')">&#128228; Send Email</button>':'');
    openModal('Draft #' + d.id + ': ' + (d.subject||'Untitled'), body, footer);
  } catch(e) { toast(e.message, 'error'); }
}

async function saveDraftEdit(draftId) {
  const subject = document.getElementById('edit-draft-subject-' + draftId)?.value;
  const body = document.getElementById('edit-draft-body-' + draftId)?.value;
  try {
    await api('PATCH', '/api/drafts/' + draftId, { subject, body });
    toast('Draft saved. Re-approval required.', 'warning'); closeModal();
    state.data.drafts = await api('GET', '/api/drafts?limit=100') || []; renderDraftsList();
  } catch(e) { toast(e.message, 'error'); }
}

async function approveDraft(draftId) {
  try {
    await api('POST', '/api/drafts/' + draftId + '/approve');
    toast('Draft approved &#10003;', 'success'); closeModal();
    state.data.drafts = await api('GET', '/api/drafts?limit=100') || [];
    state.data.pendingDraftsCount = state.data.drafts.filter(d => d.status==='pending_review').length;
    updateNavBadges(); renderDraftsList(); await loadAllData();
  } catch(e) { toast(e.message, 'error'); }
}

async function sendDraft(draftId) {
  try {
    const res = await api('POST', '/api/drafts/' + draftId + '/send');
    toast('Email sent! Mode: ' + res.mode, 'success'); closeModal();
    state.data.drafts = await api('GET', '/api/drafts?limit=100') || []; renderDraftsList(); await loadAllData();
  } catch(e) { toast(e.message, 'error'); }
}

function rejectDraftModal(draftId) {
  openModal('Reject Draft', '<div class="form-group"><label>Rejection Reason * (min 20 characters)</label><textarea id="reject-reason" placeholder="Explain clearly why this draft is rejected..." rows="4"></textarea></div>', '<button class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-danger" onclick="rejectDraft(' + draftId + ')">Reject Draft</button>');
}

async function rejectDraft(draftId) {
  const reason = document.getElementById('reject-reason')?.value?.trim();
  if (!reason || reason.length < 20) return toast('Rejection reason must be at least 20 characters', 'warning');
  try {
    await api('POST', '/api/drafts/' + draftId + '/reject', { reason });
    toast('Draft rejected', 'warning'); closeModal();
    state.data.drafts = await api('GET', '/api/drafts?limit=100') || [];
    state.data.pendingDraftsCount = state.data.drafts.filter(d => d.status==='pending_review').length;
    updateNavBadges(); renderDraftsList(); await loadAllData();
  } catch(e) { toast(e.message, 'error'); }
}

let customerSubTab = 'customers';
let cachedOrders = [];
let cachedDevices = [];

async function refreshCustomers() {
  toast('Refreshing records...', 'info');
  try {
    const [custs, ords, devs] = await Promise.all([
      api('GET', '/api/customers?limit=100'),
      api('GET', '/api/orders?limit=100'),
      api('GET', '/api/devices?limit=100'),
    ]);
    state.data.customers = custs || [];
    cachedOrders = ords || [];
    cachedDevices = devs || [];
    renderCustomersList();
    toast('Records refreshed', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function setCustomerSubTab(tab) {
  customerSubTab = tab;
  if (tab === 'orders' && cachedOrders.length === 0) {
    try { cachedOrders = await api('GET', '/api/orders?limit=100') || []; } catch {}
  } else if (tab === 'devices' && cachedDevices.length === 0) {
    try { cachedDevices = await api('GET', '/api/devices?limit=100') || []; } catch {}
  }
  renderCustomersList();
}

function renderCustomersList() {
  const el = document.getElementById('customers-content');
  if (!el) return;
  const customers = state.data.customers || [];
  const customerOverview = '<section class="crm-overview"><div class="crm-overview-copy"><span class="section-eyebrow">CUSTOMER RELATIONSHIPS</span><h3>Know every customer.</h3><p>A clear view of the people, orders, and devices your team supports.</p></div><div class="crm-overview-stats"><div><strong>'+customers.length+'</strong><span>Profiles</span></div><div><strong>'+((cachedOrders||[]).length)+'</strong><span>Orders loaded</span></div><div><strong>'+((cachedDevices||[]).length)+'</strong><span>Devices loaded</span></div></div></section>';

  const tabButtons = customerOverview + '<div class="crm-tabs">' +
    '<button class="btn ' + (customerSubTab === 'customers' ? 'btn-primary' : 'btn-secondary') + ' btn-sm" onclick="setCustomerSubTab(\'customers\')">&#128101; Customers (' + customers.length + ')</button>' +
    '<button class="btn ' + (customerSubTab === 'orders' ? 'btn-primary' : 'btn-secondary') + ' btn-sm" onclick="setCustomerSubTab(\'orders\')">&#128230; Orders &amp; Shipments</button>' +
    '<button class="btn ' + (customerSubTab === 'devices' ? 'btn-primary' : 'btn-secondary') + ' btn-sm" onclick="setCustomerSubTab(\'devices\')">&#128241; Devices &amp; Warranties</button>' +
  '</div>';

  if (customerSubTab === 'customers') {
    const rows = customers.map(c => '<tr><td><strong>' + c.full_name + '</strong></td><td style="font-size:12px">' + c.email + '</td><td style="font-size:12px">' + (c.phone||'&mdash;') + '</td><td style="font-size:12px">' + (c.company||'&mdash;') + '</td><td class="text-sm text-muted">' + fmtDate(c.created_at) + '</td><td><button class="btn btn-secondary btn-sm" onclick="viewCustomer(' + c.id + ')">View Details</button></td></tr>').join('');
    el.innerHTML = tabButtons + '<div class="flex gap-8 mb-16"><input type="text" placeholder="Search customers..." style="max-width:300px" oninput="filterTableRows(this, \'#customers-table\')" /></div><div class="table-wrap"><table id="customers-table"><thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>Company</th><th>Since</th><th>Actions</th></tr></thead><tbody>' + (rows||'<tr><td colspan="6" class="table-empty">No customers found</td></tr>') + '</tbody></table></div>';
  } else if (customerSubTab === 'orders') {
    const ordRows = (cachedOrders || []).map(o =>
      '<tr>' +
        '<td><strong class="monospace" style="color:var(--text-accent)">' + o.order_number + '</strong></td>' +
        '<td>' + (o.customer_name || 'Customer #' + o.customer_id) + '</td>' +
        '<td>' + badge(o.status) + '</td>' +
        '<td class="monospace text-sm">' + (o.tracking_number || '&mdash;') + '</td>' +
        '<td><strong>$' + (o.total_amount ? Number(o.total_amount).toFixed(2) : '0.00') + '</strong></td>' +
        '<td class="text-sm text-muted">' + fmtDate(o.order_date) + '</td>' +
      '</tr>'
    ).join('');
    el.innerHTML = tabButtons + '<div class="flex gap-8 mb-16"><input type="text" placeholder="Search orders..." style="max-width:300px" oninput="filterTableRows(this, \'#orders-table\')" /></div><div class="table-wrap"><table id="orders-table"><thead><tr><th>Order #</th><th>Customer</th><th>Status</th><th>Tracking #</th><th>Total</th><th>Date</th></tr></thead><tbody>' + (ordRows||'<tr><td colspan="6" class="table-empty">No orders found</td></tr>') + '</tbody></table></div>';
  } else if (customerSubTab === 'devices') {
    const devRows = (cachedDevices || []).map(d =>
      '<tr>' +
        '<td><strong class="monospace" style="color:var(--text-accent)">' + (d.serial_number_masked || d.serial_number || '&mdash;') + '</strong></td>' +
        '<td><strong>' + (d.product?.name || d.product_name || 'Product #' + d.product_id) + '</strong></td>' +
        '<td>' + (d.nickname || '&mdash;') + '</td>' +
        '<td>' + badge(d.warranty_status || 'unknown') + '</td>' +
        '<td class="text-sm text-muted">' + (d.purchase_date ? fmtDate(d.purchase_date) : '&mdash;') + '</td>' +
      '</tr>'
    ).join('');
    el.innerHTML = tabButtons + '<div class="flex gap-8 mb-16"><input type="text" placeholder="Search devices..." style="max-width:300px" oninput="filterTableRows(this, \'#devices-table\')" /></div><div class="table-wrap"><table id="devices-table"><thead><tr><th>Serial #</th><th>Product</th><th>Nickname</th><th>Warranty</th><th>Purchase Date</th></tr></thead><tbody>' + (devRows||'<tr><td colspan="5" class="table-empty">No registered devices found</td></tr>') + '</tbody></table></div>';
  }
}

function filterTableRows(input, tableId) {
  const q = input.value.toLowerCase();
  document.querySelectorAll(tableId + ' tbody tr').forEach(row => { row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none'; });
}

async function viewCustomer(customerId) {
  try {
    const [cust, tickets, devices, orders] = await Promise.all([
      api('GET', '/api/customers/' + customerId),
      api('GET', '/api/tickets?customer_id=' + customerId + '&limit=20'),
      api('GET', '/api/devices?customer_id=' + customerId + '&limit=20'),
      api('GET', '/api/orders?customer_id=' + customerId + '&limit=10'),
    ]);
    const deviceHTML = (devices||[]).map(d => '<div class="card mb-8" style="margin-bottom:8px;padding:12px"><div class="flex justify-between items-center"><div><div style="font-size:13px;font-weight:600">' + (d.product?.name||d.product_name||'Device') + '</div><div class="text-sm text-muted monospace">' + (d.serial_number_masked||d.serial_number||'&mdash;') + '</div></div><div class="text-sm text-muted">' + badge(d.warranty_status||'unknown') + '<div style="font-size:11px;margin-top:4px">' + (d.purchase_date?fmtDate(d.purchase_date):'') + '</div></div></div></div>').join('') || '<p class="text-muted text-sm">No devices registered</p>';
    const orderHTML = (orders||[]).map(o =>
      '<div class="card mb-8" style="margin-bottom:8px;padding:12px">' +
        '<div class="flex justify-between items-center mb-4">' +
          '<div><strong class="monospace" style="color:var(--text-accent)">' + o.order_number + '</strong>' +
          '<span class="text-sm text-muted" style="margin-left:8px">' + fmtDate(o.order_date) + '</span></div>' +
          '<div>' + badge(o.status) + '</div>' +
        '</div>' +
        '<div class="flex justify-between items-center text-sm">' +
          '<div><span class="text-muted">Total:</span> <strong>$' + (o.total_amount ? Number(o.total_amount).toFixed(2) : '0.00') + '</strong></div>' +
          '<div><span class="text-muted">Tracking:</span> <span class="monospace text-sm">' + (o.tracking_number || '&mdash;') + '</span></div>' +
        '</div>' +
      '</div>'
    ).join('') || '<p class="text-muted text-sm">No orders on record</p>';
    const ticketHTML = (tickets||[]).map(t => '<div class="flex justify-between items-center" style="padding:8px 0;border-bottom:1px solid var(--border)"><div><span class="monospace text-muted text-sm">' + t.ticket_number + '</span><span style="font-size:13px;margin-left:8px">' + t.subject + '</span></div><div class="flex gap-8 items-center">' + badge(t.status) + badge(t.priority) + '</div></div>').join('') || '<p class="text-muted text-sm">No tickets</p>';
    const body = '<div class="grid-2 gap-16 mb-16">' +
      '<div class="card"><div class="card-title mb-8">Contact Info</div><div class="flex-col flex gap-8 text-sm"><div><span class="text-muted">Email:</span> <strong>' + cust.email + '</strong></div><div><span class="text-muted">Phone:</span> ' + (cust.phone||'&mdash;') + '</div><div><span class="text-muted">Company:</span> ' + (cust.company||'&mdash;') + '</div><div><span class="text-muted">Address:</span> ' + (cust.address||'&mdash;') + '</div><div><span class="text-muted">Customer since:</span> ' + fmtDate(cust.created_at) + '</div></div></div>' +
      '<div class="card"><div class="card-title mb-8">Registered Devices (' + (devices||[]).length + ')</div>' + deviceHTML + '</div>' +
    '</div>' +
    '<div class="grid-2 gap-16">' +
      '<div class="card"><div class="card-title mb-8">Order History (' + (orders||[]).length + ')</div>' + orderHTML + '</div>' +
      '<div class="card"><div class="card-title mb-8">Support Tickets (' + (tickets||[]).length + ')</div>' + ticketHTML + '</div>' +
    '</div>';
    openModal('&#128100; ' + cust.full_name, body, '');
  } catch(e) { toast(e.message, 'error'); }
}

// ==========================================
// Customer Data Import (CSV / Excel)
// ==========================================
let currentImportFile = null;
let currentImportPreview = null;
let lastImportResult = null;
let importPreviewFilter = 'all';

async function downloadTemplate(fmt) {
  try {
    toast('Downloading ' + fmt.toUpperCase() + ' template...', 'info');
    const res = await fetch(API + '/api/customers/import/template?format=' + fmt, {
      credentials: 'same-origin'
    });
    if (!res.ok) throw new Error('Download failed: ' + res.statusText);
    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'customer_import_template.' + fmt;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
    toast('Downloaded customer_import_template.' + fmt, 'success');
  } catch(e) {
    toast(e.message, 'error');
  }
}

function handleImportFileSelect(e) {
  const files = e.target.files || e.dataTransfer?.files;
  if (!files || !files.length) return;
  const file = files[0];
  const name = file.name.toLowerCase();
  if (!name.endsWith('.csv') && !name.endsWith('.xlsx')) {
    toast('Unsupported format. Please upload a .csv or .xlsx file.', 'warning');
    return;
  }
  currentImportFile = file;
  previewImportFile(file);
}

async function previewImportFile(file) {
  const el = document.getElementById('customer-import-content');
  if (el) el.innerHTML = '<div class="loading-overlay"><div class="spinner"></div> Analyzing file & validating schema...</div>';
  const formData = new FormData();
  formData.append('file', file);
  try {
    const data = await api('POST', '/api/customers/import/preview', formData, true);
    currentImportPreview = data;
    lastImportResult = null;
    importPreviewFilter = 'all';
    renderCustomerImport();
    toast('Preview generated: ' + data.valid_count + ' valid, ' + data.duplicate_count + ' duplicate(s), ' + data.invalid_count + ' invalid.', 'info');
  } catch(e) {
    toast('Preview failed: ' + e.message, 'error');
    currentImportPreview = null;
    renderCustomerImport();
  }
}

async function confirmCustomerImport() {
  if (!currentImportFile) {
    toast('No file staged for import.', 'warning');
    return;
  }
  const btn = document.getElementById('confirm-import-btn');
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Committing to PostgreSQL...'; }
  toast('Importing records with database transaction...', 'info');

  // Send the original file again — the backend re-validates and imports only valid rows
  const formData = new FormData();
  formData.append('file', currentImportFile);
  try {
    const res = await api('POST', '/api/customers/import/confirm', formData, true);
    lastImportResult = res;
    currentImportPreview = null;
    currentImportFile = null;
    toast('Import successful! Stored records in PostgreSQL database.', 'success');
    renderCustomerImport();
    loadAllData();
  } catch(e) {
    toast('Import failed: ' + e.message, 'error');
    if (btn) { btn.disabled = false; btn.innerHTML = '&#10003; Confirm &amp; Import Valid Records'; }
  }
}

function resetImportUI() {
  currentImportFile = null;
  currentImportPreview = null;
  lastImportResult = null;
  importPreviewFilter = 'all';
  renderCustomerImport();
}

function setImportFilter(f) {
  importPreviewFilter = f;
  renderCustomerImport();
}

function renderCustomerImport() {
  const el = document.getElementById('customer-import-content');
  if (!el) return;

  // 1. Success Banner if just imported
  let resultBanner = '';
  if (lastImportResult) {
    resultBanner = '<div class="card mb-16" style="border: 1px solid var(--success); background: rgba(34,197,94,0.06);">' +
      '<div class="flex items-center justify-between mb-12">' +
        '<div class="flex items-center gap-10">' +
          '<span style="font-size:24px">&#9989;</span>' +
          '<div>' +
            '<div style="font-size:16px;font-weight:700;color:var(--success)">Customer Database Import Successful</div>' +
            '<div class="text-sm text-secondary">' + (lastImportResult.message || 'Records committed to PostgreSQL.') + '</div>' +
          '</div>' +
        '</div>' +
        '<div class="flex gap-8">' +
          '<button class="btn btn-secondary btn-sm" onclick="setPage(\'customers\')">View In Customers Page &rarr;</button>' +
          '<button class="btn btn-primary btn-sm" onclick="resetImportUI()">Import Another File</button>' +
        '</div>' +
      '</div>' +
      '<div class="stats-grid" style="grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); margin-bottom: 0;">' +
        '<div class="stat-card" style="--stat-color: var(--primary); padding:14px;"><div class="stat-label">Customers</div><div class="stat-value" style="font-size:24px;">' + (lastImportResult.imported_customers || 0) + '</div><div class="stat-sub">Created / Linked</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--info); padding:14px;"><div class="stat-label">Orders</div><div class="stat-value" style="font-size:24px;">' + (lastImportResult.imported_orders || 0) + '</div><div class="stat-sub">Recorded</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--accent); padding:14px;"><div class="stat-label">Devices</div><div class="stat-value" style="font-size:24px;">' + (lastImportResult.imported_devices || 0) + '</div><div class="stat-sub">Registered</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--success); padding:14px;"><div class="stat-label">Warranties</div><div class="stat-value" style="font-size:24px;">' + (lastImportResult.imported_warranties || 0) + '</div><div class="stat-sub">Policies Linked</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--warning); padding:14px;"><div class="stat-label">Duplicates</div><div class="stat-value" style="font-size:24px;">' + (lastImportResult.skipped_duplicates || 0) + '</div><div class="stat-sub">Safely Skipped</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--danger); padding:14px;"><div class="stat-label">Invalid</div><div class="stat-value" style="font-size:24px;">' + (lastImportResult.invalid_records || 0) + '</div><div class="stat-sub">Rejected</div></div>' +
      '</div>' +
    '</div>';
  }

  // 2. Upload / Dropzone Card (shown when no preview active or alongside results)
  let uploadCard = '';
  if (!currentImportPreview) {
    uploadCard = '<div class="grid-2 gap-16 mb-16">' +
      '<div class="card">' +
        '<div class="card-header"><span class="card-title">&#128193; Upload Customer Spreadsheet</span></div>' +
        '<div class="import-dropzone" id="import-dropzone" onclick="document.getElementById(\'import-file-input\').click()">' +
          '<div class="import-dropzone-icon">&#128229;</div>' +
          '<div style="font-weight:600;font-size:15px;margin-bottom:6px">Drag &amp; Drop your CSV or Excel file here</div>' +
          '<div class="text-sm text-secondary mb-12">Supports standard .csv and .xlsx formats with customer, order, device &amp; warranty fields</div>' +
          '<button type="button" class="btn btn-primary btn-sm" onclick="event.stopPropagation(); document.getElementById(\'import-file-input\').click()">Browse Local File</button>' +
          '<input type="file" id="import-file-input" accept=".csv, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/vnd.ms-excel" style="display:none" onchange="handleImportFileSelect(event)" />' +
        '</div>' +
      '</div>' +
      '<div class="card">' +
        '<div class="card-header"><span class="card-title">&#128161; Supported Columns &amp; Stable Identifiers</span></div>' +
        '<div class="text-sm text-secondary" style="line-height:1.6">' +
          '<p class="mb-8">The import engine automatically detects and maps columns using flexible headers. Imported entities are linked via reliable relational keys:</p>' +
          '<ul style="padding-left:18px; margin-bottom:12px; display:flex; flex-direction:column; gap:4px;">' +
            '<li><strong>Customer Profile:</strong> <code class="monospace text-sm" style="color:var(--text-accent)">full_name</code>, <code class="monospace text-sm" style="color:var(--text-accent)">email</code> (unique key), <code class="monospace text-sm">phone</code>, <code class="monospace text-sm">company</code></li>' +
            '<li><strong>Orders &amp; Shipments:</strong> <code class="monospace text-sm" style="color:var(--text-accent)">order_number</code> (unique key), <code class="monospace text-sm">order_date</code>, <code class="monospace text-sm">status</code>, <code class="monospace text-sm">total_amount</code>, <code class="monospace text-sm">tracking_number</code></li>' +
            '<li><strong>Devices &amp; Hardware:</strong> <code class="monospace text-sm" style="color:var(--text-accent)">serial_number</code> (unique key), <code class="monospace text-sm">product_sku</code> / <code class="monospace text-sm">product_name</code>, <code class="monospace text-sm">nickname</code></li>' +
            '<li><strong>Warranties:</strong> <code class="monospace text-sm" style="color:var(--text-accent)">warranty_policy_number</code>, <code class="monospace text-sm">warranty_end_date</code>, <code class="monospace text-sm">warranty_status</code></li>' +
          '</ul>' +
          '<div class="flex gap-8 mt-16">' +
            '<button class="btn btn-secondary btn-sm" onclick="downloadTemplate(\'csv\')">&#128196; Download CSV Template</button>' +
            '<button class="btn btn-secondary btn-sm" onclick="downloadTemplate(\'xlsx\')">&#128202; Download Excel (.xlsx)</button>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  // 3. Preview Section (shown if preview loaded)
  let previewSection = '';
  if (currentImportPreview) {
    const p = currentImportPreview;
    // API returns field 'preview', not 'rows'
    let allRows = p.preview || [];
    let filteredRows = allRows;
    if (importPreviewFilter === 'valid') filteredRows = allRows.filter(r => r.status === 'valid');
    else if (importPreviewFilter === 'duplicate') filteredRows = allRows.filter(r => r.status === 'duplicate');
    else if (importPreviewFilter === 'invalid') filteredRows = allRows.filter(r => r.status === 'invalid');

    const tableRows = filteredRows.map(r => {
      let badgeClass = 'badge-valid';
      let statusLabel = 'Valid';
      if (r.status === 'duplicate') { badgeClass = 'badge-duplicate'; statusLabel = 'Duplicate (Skip)'; }
      else if (r.status === 'invalid') { badgeClass = 'badge-invalid'; statusLabel = 'Invalid (Reject)'; }

      // API returns 'issues' (errors + dup_reasons combined), not 'errors'
      const issues = r.issues || [];
      const details = issues.length
        ? issues.join('; ')
        : (r.status === 'duplicate' ? 'Existing record in database or file (will be skipped)' : 'Ready for import');

      return '<tr>' +
        '<td><span class="monospace text-muted">' + r.row_number + '</span></td>' +
        '<td><span class="badge ' + badgeClass + '">' + statusLabel + '</span></td>' +
        '<td><strong>' + (r.customer_name || '&mdash;') + '</strong></td>' +
        '<td class="text-sm">' + (r.customer_email || '&mdash;') + '</td>' +
        '<td><span class="monospace text-sm" style="color:var(--text-accent)">' + (r.order_number || '&mdash;') + '</span></td>' +
        '<td><span class="monospace text-sm">' + (r.device_serial || '&mdash;') + '</span></td>' +
        '<td><span class="text-sm monospace text-muted">' + (r.product_sku || '&mdash;') + '</span></td>' +
        '<td><span class="monospace text-sm">' + (r.warranty_policy || '&mdash;') + '</span></td>' +
        '<td class="text-sm" style="max-width:280px;line-height:1.4;' + (r.status === 'invalid' ? 'color:var(--danger)' : (r.status === 'duplicate' ? 'color:var(--warning)' : 'color:var(--text-secondary)')) + '">' + details + '</td>' +
      '</tr>';
    }).join('');

    previewSection = '<div class="card mb-16">' +
      '<div class="flex justify-between items-center mb-16 flex-wrap gap-12">' +
        '<div>' +
          '<div style="font-size:16px;font-weight:700">Spreadsheet Preview &amp; Pre-Import Validation</div>' +
          '<div class="text-sm text-secondary">File: <strong style="color:var(--text-primary)">' + (p.filename || currentImportFile?.name || 'Uploaded File') + '</strong> &middot; ' + p.total_rows + ' total rows parsed</div>' +
        '</div>' +
        '<div class="flex gap-8 items-center">' +
          '<button class="btn btn-secondary" onclick="resetImportUI()">&#10005; Cancel / Change File</button>' +
          '<button class="btn btn-primary" id="confirm-import-btn" ' + (p.valid_count === 0 ? 'disabled style="opacity:0.5;cursor:not-allowed"' : '') + ' onclick="confirmCustomerImport()">&#10003; Confirm &amp; Import ' + p.valid_count + ' Valid Records</button>' +
        '</div>' +
      '</div>' +
      '<div class="stats-grid mb-16" style="grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));">' +
        '<div class="stat-card" style="--stat-color: var(--primary)"><div class="stat-label">Total Rows</div><div class="stat-value">' + p.total_rows + '</div><div class="stat-sub">In spreadsheet</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--success)"><div class="stat-label">Valid Records</div><div class="stat-value text-success">' + p.valid_count + '</div><div class="stat-sub">Will be imported</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--warning)"><div class="stat-label">Duplicates</div><div class="stat-value text-warning">' + p.duplicate_count + '</div><div class="stat-sub">Will be skipped</div></div>' +
        '<div class="stat-card" style="--stat-color: var(--danger)"><div class="stat-label">Invalid Rows</div><div class="stat-value text-danger">' + p.invalid_count + '</div><div class="stat-sub">Will be rejected</div></div>' +
      '</div>' +
      (p.duplicate_count > 0 ? '<div class="card mb-16" style="padding:12px 16px; background: rgba(245,158,11,0.08); border: 1px solid rgba(245,158,11,0.25);"><div class="flex gap-8 items-center"><span style="font-size:18px">&#9888;&#65039;</span><div class="text-sm text-secondary"><strong style="color:var(--warning)">Duplicate Protection Active:</strong> ' + p.duplicate_count + ' row(s) already exist in PostgreSQL or file. They will be skipped without overwriting existing data.</div></div></div>' : '') +
      '<div class="flex justify-between items-center mb-12 flex-wrap gap-8">' +
        '<div class="flex gap-8">' +
          '<button class="btn ' + (importPreviewFilter === 'all' ? 'btn-primary' : 'btn-secondary') + ' btn-sm" onclick="setImportFilter(\'all\')">All Rows (' + p.total_rows + ')</button>' +
          '<button class="btn ' + (importPreviewFilter === 'valid' ? 'btn-primary' : 'btn-secondary') + ' btn-sm" onclick="setImportFilter(\'valid\')">Valid Only (' + p.valid_count + ')</button>' +
          '<button class="btn ' + (importPreviewFilter === 'duplicate' ? 'btn-primary' : 'btn-secondary') + ' btn-sm" onclick="setImportFilter(\'duplicate\')">Duplicates (' + p.duplicate_count + ')</button>' +
          '<button class="btn ' + (importPreviewFilter === 'invalid' ? 'btn-primary' : 'btn-secondary') + ' btn-sm" onclick="setImportFilter(\'invalid\')">Invalid (' + p.invalid_count + ')</button>' +
        '</div>' +
        '<input type="text" placeholder="Filter preview..." style="max-width:240px" oninput="filterTableRows(this, \'#import-preview-table\')" />' +
      '</div>' +
      '<div class="table-wrap" style="max-height:480px; overflow-y:auto;">' +
        '<table id="import-preview-table">' +
          '<thead><tr><th>Row</th><th>Status</th><th>Customer</th><th>Email</th><th>Order #</th><th>Serial #</th><th>SKU</th><th>Warranty #</th><th>Validation Details</th></tr></thead>' +
          '<tbody>' + (tableRows || '<tr><td colspan="9" class="table-empty">No rows match the selected filter</td></tr>') + '</tbody>' +
        '</table>' +
      '</div>' +
    '</div>';
  }

  el.innerHTML = resultBanner + uploadCard + previewSection;

  // Setup drag-and-drop listeners if dropzone present
  const dropzone = document.getElementById('import-dropzone');
  if (dropzone) {
    dropzone.addEventListener('dragover', e => { e.preventDefault(); dropzone.classList.add('dragover'); });
    dropzone.addEventListener('dragleave', e => { e.preventDefault(); dropzone.classList.remove('dragover'); });
    dropzone.addEventListener('drop', e => {
      e.preventDefault();
      dropzone.classList.remove('dragover');
      handleImportFileSelect(e);
    });
  }
}

async function loadKnowledgeDocs() {
  const el = document.getElementById('knowledge-content');
  if (!el) return;
  el.innerHTML = '<div class="loading-overlay"><div class="spinner"></div> Loading knowledge base...</div>';
  try {
    state.data.knowledgeDocs = await api('GET', '/api/knowledge/documents?limit=50') || [];
    renderKnowledge();
  } catch(e) { el.innerHTML = '<p class="text-danger">' + e.message + '</p>'; }
}

function renderKnowledge() {
  const el = document.getElementById('knowledge-content');
  if (!el) return;
  const docs = state.data.knowledgeDocs;
  const rows = docs.map(d => '<tr><td><strong>' + d.title + '</strong></td><td class="monospace text-sm">' + (d.filename||'&mdash;') + '</td><td>' + badge(d.file_type||'file') + ' ' + badge(d.document_type||'doc') + '</td><td>' + (d.is_indexed?'<span class="text-success">&#10003; Indexed</span>':'<span class="text-warning">&#9888; Not indexed</span>') + '</td><td class="text-sm text-muted">' + (d.chunk_count||0) + ' chunks</td><td class="text-sm text-muted">' + (d.file_size_bytes?(d.file_size_bytes/1024).toFixed(1)+' KB':'&mdash;') + '</td><td><div class="flex gap-8">' + (!d.is_indexed&&isManager()?'<button class="btn btn-secondary btn-sm" onclick="reindexDoc(' + d.id + ')">Re-index</button>':'') + '<button class="btn btn-secondary btn-sm" onclick="viewDocChunks(' + d.id + ',\'' + d.title.replace(/'/g,"\\'") + '\')">Chunks</button></div></td></tr>').join('');
  const types = [...new Set(docs.map(d=>d.document_type).filter(Boolean))];
  const docOptions = types.map(type=>'<option value="'+escapeHtml(type)+'">'+escapeHtml(type)+'</option>').join('');
  const indexedCount = docs.filter(d=>d.is_indexed).length;
  const docOverview = '<section class="doc-overview"><div><span class="section-eyebrow">KNOWLEDGE LIBRARY</span><strong>'+docs.length+'<small> documents</small></strong><p>Searchable guidance for grounded customer responses.</p></div><div class="doc-overview-count"><span>Indexed</span><strong>'+indexedCount+' / '+docs.length+'</strong></div><div class="doc-overview-count"><span>Awaiting index</span><strong>'+(docs.length-indexedCount)+'</strong></div></section>';
  el.innerHTML = docOverview + '<div class="doc-toolbar"><div class="ticket-search-wrap"><span>?</span><input id="knowledge-search" type="text" placeholder="Search documents..." oninput="filterKnowledgeDocs()" /></div><select id="knowledge-type-filter" aria-label="Filter by document type" onchange="filterKnowledgeDocs()"><option value="">All document types</option>'+docOptions+'</select>' + (isManager()?'<button class="btn btn-primary" onclick="showUploadModal()">Upload document</button><button class="btn btn-secondary" onclick="seedSampleDocs()">Seed sample docs</button>':'') + '<button class="btn btn-secondary" onclick="showSearchModal()">Semantic search</button></div><div class="table-wrap"><table id="knowledge-table"><thead><tr><th>Title</th><th>Filename</th><th>Type</th><th>Index status</th><th>Chunks</th><th>Size</th><th>Actions</th></tr></thead><tbody>' + (rows||'<tr><td colspan="7" class="table-empty">No documents. Upload a document to build your knowledge library.</td></tr>') + '</tbody></table></div>';
}

function filterKnowledgeDocs() {
  const query = (document.getElementById('knowledge-search')?.value || '').toLowerCase();
  const type = document.getElementById('knowledge-type-filter')?.value || '';
  document.querySelectorAll('#knowledge-table tbody tr').forEach((row, index) => {
    const doc = state.data.knowledgeDocs[index];
    row.style.display = (!doc || (!type || doc.document_type === type) && row.textContent.toLowerCase().includes(query)) ? '' : 'none';
  });
}

async function seedSampleDocs() {
  toast('Seeding sample documents...', 'info');
  try { const r = await api('POST', '/api/knowledge/seed-sample-docs'); toast('Seeded ' + r.ingested_count + ' documents', 'success'); await loadKnowledgeDocs(); }
  catch(e) { toast(e.message, 'error'); }
}

async function reindexDoc(docId) {
  toast('Re-indexing...', 'info');
  try { await api('POST', '/api/knowledge/reindex/' + docId); toast('Re-indexed', 'success'); await loadKnowledgeDocs(); }
  catch(e) { toast(e.message, 'error'); }
}

async function viewDocChunks(docId, title) {
  try {
    const chunks = await api('GET', '/api/knowledge/documents/' + docId + '/chunks');
    const html = (chunks||[]).map((c,i) => '<div class="search-result"><div class="search-result-title"><span>Chunk ' + (i+1) + ' &mdash; ' + (c.section_header||'N/A') + '</span><span class="text-sm ' + (c.has_embedding?'text-success':'text-warning') + '">' + (c.has_embedding?'&#10003; Embedded':'&#9888; No embedding') + '</span></div><div class="search-result-content">' + (c.content||'') + '</div></div>').join('') || '<p class="text-muted">No chunks</p>';
    openModal('&#128196; Chunks: ' + title, '<div style="max-height:60vh;overflow-y:auto">' + html + '</div>', '');
  } catch(e) { toast(e.message, 'error'); }
}

function showSearchModal() {
  openModal('&#128269; Semantic Knowledge Search', '<div class="form-group mb-16"><label>Search Query</label><input id="kb-search-query" type="text" placeholder="e.g., firmware update error, bluetooth pairing..." /></div><button class="btn btn-primary mb-16" onclick="runKbSearch()">Search</button><div id="kb-search-results"></div>', '');
}

async function runKbSearch() {
  const query = document.getElementById('kb-search-query')?.value?.trim();
  if (!query) return toast('Enter a search query', 'warning');
  const el = document.getElementById('kb-search-results');
  if (!el) return;
  el.innerHTML = '<div class="loading-overlay"><div class="spinner"></div> Searching...</div>';
  try {
    const results = await api('POST', '/api/knowledge/search', { query, top_k: 5, min_similarity: 0.1 });
    el.innerHTML = (results||[]).map(r => '<div class="search-result"><div class="search-result-title"><span>' + (r.document_title||r.citation||'Result') + '</span><span class="text-success">' + (r.similarity?(r.similarity*100).toFixed(0)+'%':'') + '</span></div><div class="search-result-content">' + (r.content||'') + '</div></div>').join('') || '<p class="text-muted">No results found</p>';
  } catch(e) { el.innerHTML = '<p class="text-danger">' + e.message + '</p>'; }
}

function showUploadModal() {
  openModal('&#128193; Upload Knowledge Document', '<div class="flex-col flex gap-12"><div class="form-group"><label>Document Title *</label><input id="upload-title" type="text" placeholder="e.g., SoundPulse Pro User Manual" /></div><div class="form-group"><label>File (PDF, TXT, MD) *</label><input id="upload-file" type="file" accept=".txt,.md,.pdf" /></div><div class="grid-2 gap-16"><div class="form-group"><label>Document Type</label><select id="upload-type"><option value="manual">Manual</option><option value="policy">Policy</option><option value="faq">FAQ</option><option value="guide">Guide</option></select></div><div class="form-group"><label>Auto-index</label><select id="upload-autoindex"><option value="true">Yes (recommended)</option><option value="false">No</option></select></div></div></div>', '<button class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-primary" onclick="uploadDocument()">Upload & Index</button>');
}

async function uploadDocument() {
  const title = document.getElementById('upload-title')?.value?.trim();
  const fileInput = document.getElementById('upload-file');
  const docType = document.getElementById('upload-type')?.value;
  const autoIndex = document.getElementById('upload-autoindex')?.value === 'true';
  if (!title || !fileInput?.files?.length) return toast('Title and file are required', 'warning');
  const formData = new FormData();
  formData.append('file', fileInput.files[0]);
  formData.append('title', title);
  formData.append('document_type', docType);
  formData.append('auto_index', autoIndex);
  try { await api('POST', '/api/knowledge/upload', formData, true); toast('Document uploaded and indexed', 'success'); closeModal(); await loadKnowledgeDocs(); }
  catch(e) { toast(e.message, 'error'); }
}

async function loadAnalytics() {
  const el = document.getElementById('analytics-content');
  if (!el) return;
  el.innerHTML = '<div class="loading-overlay"><div class="spinner"></div> Loading analytics...</div>';
  try { const data = await api('GET', '/api/dashboard/analytics'); renderAnalytics(data); }
  catch(e) { el.innerHTML = '<p class="text-danger">' + e.message + '</p>'; }
}

function renderAnalytics(data) {
  const el = document.getElementById('analytics-content');
  if (!el || !data) return;
  const ai = data.ai_metrics || {};
  const draft = data.draft_approval_metrics || {};
  const maxCat = Math.max(...(data.category_distribution||[]).map(x => x.count), 1);
  const catBars = (data.category_distribution||[]).map(c => '<div class="chart-bar-row"><span class="chart-bar-label">' + (c.category||'?') + '</span><div class="chart-bar-track"><div class="chart-bar-fill" style="width:' + (c.count/maxCat*100).toFixed(0) + '%"></div></div><span class="chart-bar-value">' + c.count + '</span></div>').join('');
  const volSlice = (data.ticket_volume_by_day||[]).slice(-14);
  const maxVol = Math.max(...volSlice.map(x=>x.tickets), 1);
  const volPoints = volSlice.map((d,i)=>({x:24+(volSlice.length<2?0:i*512/(volSlice.length-1)),y:176-(Number(d.tickets||0)/maxVol*142),date:d.date,count:Number(d.tickets||0)}));
  const volLine = volPoints.map(p=>p.x+','+p.y).join(' ');
  const volBars = volPoints.length ? '<div class="analytics-line-chart"><svg viewBox="0 0 560 210" role="img" aria-label="Ticket activity trend"><defs><linearGradient id="analyticsFill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#5685F5" stop-opacity=".3"/><stop offset="1" stop-color="#5685F5" stop-opacity="0"/></linearGradient></defs><path d="M24 34H536M24 105H536M24 176H536" class="chart-gridline"/><polygon points="'+volPoints[0].x+',180 '+volLine+' '+volPoints[volPoints.length-1].x+',180" fill="url(#analyticsFill)"/><polyline points="'+volLine+'" class="chart-line"/>'+volPoints.map(p=>'<circle cx="'+p.x+'" cy="'+p.y+'" r="3.5" class="chart-point"><title>'+escapeHtml(p.date)+': '+p.count+' tickets</title></circle>').join('')+'</svg><div class="analytics-xlabels">'+volPoints.map(p=>'<span>'+escapeHtml(String(p.date).slice(-5))+'</span>').join('')+'</div></div>' : '<p class="chart-empty">No ticket volume history available.</p>';
  const agentRows = (data.agent_workload||[]).map(a => '<tr><td><strong>' + a.name + '</strong></td><td>' + badge(a.role) + '</td><td><span style="color:var(--warning);font-weight:600">' + a.active_tickets + '</span></td><td><span style="color:var(--success);font-weight:600">' + a.resolved_tickets + '</span></td></tr>').join('');
  const rate = ai.success_rate == null ? 'Unavailable' : ai.success_rate + '%';
  const confidence = ai.avg_confidence == null ? 'Unavailable' : (ai.avg_confidence * 100).toFixed(0) + '%';
  const approvalRate = draft.approval_rate == null ? 'Unavailable' : draft.approval_rate + '%';
  const duration = ai.avg_duration_ms == null ? 'Unavailable' : (ai.avg_duration_ms / 1000).toFixed(1) + 's';
  el.innerHTML = '<div class="stats-grid mb-16"><div class="stat-card" style="--stat-color:#5685F5"><span class="stat-icon">&#10022;</span><div class="stat-label">AI Executions</div><div class="stat-value">' + (ai.total_executions ?? '&mdash;') + '</div><div class="stat-sub">' + rate + ' success rate</div></div><div class="stat-card" style="--stat-color:#16A36A"><span class="stat-icon">&#9673;</span><div class="stat-label">Avg Confidence</div><div class="stat-value">' + confidence + '</div><div class="stat-sub">AI grounding score</div></div><div class="stat-card" style="--stat-color:#8174E8"><span class="stat-icon">&#10003;</span><div class="stat-label">Draft Approval Rate</div><div class="stat-value">' + approvalRate + '</div><div class="stat-sub">' + (draft.approved ?? '&mdash;') + ' approved</div></div><div class="stat-card" style="--stat-color:#D99A25"><span class="stat-icon">&#9716;</span><div class="stat-label">Avg AI Duration</div><div class="stat-value">' + duration + '</div><div class="stat-sub">Per ticket analysis</div></div></div><div class="analytics-layout"><div class="insight-panel analytics-trend"><div class="insight-heading"><div><span class="section-eyebrow">VOLUME</span><h3>Ticket activity</h3></div><span class="insight-period">Last 14 days</span></div>' + volBars + '</div><div class="insight-panel analytics-category"><div class="insight-heading"><div><span class="section-eyebrow">INCOMING SUPPORT</span><h3>Category mix</h3></div></div>' + (catBars||'<p class="chart-empty">No category data available.</p>') + '</div><div class="insight-panel analytics-workload"><div class="insight-heading"><div><span class="section-eyebrow">TEAM CAPACITY</span><h3>Agent workload</h3></div></div><div class="table-wrap"><table><thead><tr><th>Agent</th><th>Role</th><th>Active</th><th>Resolved</th></tr></thead><tbody>' + (agentRows||'<tr><td colspan="4" class="table-empty">No workload data available.</td></tr>') + '</tbody></table></div></div></div>';
}

async function toggleAutoSend(enable) {
  if (enable) {
    if (!confirm('Enable Automatic Response Sending?\n\nEligible, safe routine inquiries (FAQs, product setup, troubleshooting, specifications, verified order/warranty requests) will be automatically sent directly to customers via Gmail.\n\nHigh-risk cases (refunds, shipping disputes, safety/legal issues, unverified identities) will still require human review.')) {
      return;
    }
  }
  try {
    await api('PATCH', '/api/settings/auto_send_enabled', { value: enable ? 'true' : 'false' });
    toast('Automatic sending ' + (enable ? 'ENABLED' : 'DISABLED'), 'success');
    await loadSettings();
    await loadAllData();
  } catch (e) {
    toast(e.message, 'error');
    await loadSettings();
  }
}

async function loadSettings() {
  const el = document.getElementById('settings-content');
  if (!el) return;
  el.innerHTML = '<div class="loading-overlay"><div class="spinner"></div> Loading...</div>';
  try {
    const [settingsData, users] = await Promise.all([
      api('GET', '/api/settings/status'),
      isAdmin() ? api('GET', '/api/auth/users') : Promise.resolve([]),
    ]);
    const deps = settingsData?.dependencies || {};
    const integrations = [
      {name:'PostgreSQL Database',key:'database',icon:'&#128452;'},
      {name:'pgvector Extension',key:'pgvector',icon:'&#129518;'},
      {name:'Groq LLM',key:'groq',icon:'&#10022;'},
      {name:'Gmail OAuth Authorization',key:'gmail_oauth',icon:'&#128231;'},
      {name:'Verified Gmail Account',key:'gmail_account',icon:'&#128100;'},
      {name:'Gmail API Availability',key:'gmail_api',icon:'&#9889;'},
      {name:'Gmail Send Permission',key:'gmail_send_permission',icon:'&#128274;'},
      {name:'RAG Retrieval System',key:'rag',icon:'&#128218;'},
      {name:'AI Agent Execution Pipeline',key:'agent',icon:'&#9889;'},
      {name:'Email Sending Configuration',key:'email_sending',icon:'&#128231;'},
      {name:'Auto-Send Configuration',key:'auto_send',icon:'&#128276;'},
    ];
    const statusHTML = integrations.map(i => {
      const status = deps[i.key] || settingsData?.[i.key + '_status'] || 'unknown';
      let statusDisplay = status;
      if (i.key === 'groq' && status === 'available') statusDisplay = 'Connected: ' + (settingsData?.groq_model || 'configured model');
      if (i.key === 'gmail_oauth' && status === 'authorized') statusDisplay = 'Authorized';
      if (i.key === 'gmail_account' && status === 'verified') statusDisplay = settingsData?.gmail_connected_email || 'Verified';
      if ((i.key === 'gmail_api' || i.key === 'gmail_send_permission') && status === 'available') statusDisplay = 'Available';
      if (i.key === 'auto_send' && status === 'disabled') statusDisplay = 'Disabled';
      const isNominal = status.startsWith('ok') || status.startsWith('v') || status === 'configured' || status === 'connected' || status === 'available' || status === 'authorized' || status === 'verified' || status === 'granted' || status === 'disabled' || status.startsWith('disabled (safe') || status.startsWith('operational');
      const dotClass = isNominal ? 'ok' : 'warn';
      const colorStyle = isNominal ? 'var(--success)' : 'var(--warning)';
      return '<div class="flex items-center justify-between" style="padding:10px 0;border-bottom:1px solid var(--border)"><div class="flex items-center gap-12"><span style="font-size:18px">' + i.icon + '</span><span style="font-size:13.5px;font-weight:500">' + i.name + '</span></div><div class="flex items-center gap-8"><span class="status-dot ' + dotClass + '"></span><span style="font-size:12px;font-weight:500;color:' + colorStyle + '">' + escapeHtml(statusDisplay) + '</span></div></div>';
    }).join('');
    const healthDetails = [
      settingsData?.groq_status !== 'available' && settingsData?.groq_error_message
        ? '<p class="text-warning text-sm">Groq: ' + escapeHtml(settingsData.groq_error_message) + '</p>' : '',
      settingsData?.gmail_authorization_message
        ? '<p class="text-warning text-sm">Gmail: ' + escapeHtml(settingsData.gmail_authorization_message) + '</p>' : '',
    ].join('');
    const userRows = (users||[]).map(u => '<tr><td><strong>' + escapeHtml(u.full_name) + '</strong></td><td style="font-size:12px">' + escapeHtml(u.email) + '</td><td>' + badge(u.role) + '</td><td>' + (u.is_active?'<span class="text-success">Active</span>':'<span class="text-danger">Inactive</span>') + '</td><td><button class="btn btn-secondary btn-sm" onclick="toggleUserActive(' + u.id + ',' + !u.is_active + ')">' + (u.is_active?'Deactivate':'Activate') + '</button></td></tr>').join('');
    const autoSendVal = settingsData?.auto_send_enabled === true;
    const gmailReady = settingsData?.gmail_oauth_authorized === true
      && settingsData?.gmail_account_verified === true
      && settingsData?.gmail_api_available === true
      && settingsData?.gmail_send_permission === true;
    const autoSendActive = autoSendVal && gmailReady;
    const toggleBtn = !isAdmin() ? '<span class="text-muted text-sm">Administrator access required.</span>'
      : autoSendVal
        ? '<button class="btn btn-warning btn-sm" onclick="toggleAutoSend(false)">&#128274; Disable Auto-Send</button>'
        : gmailReady
          ? '<button class="btn btn-primary btn-sm" onclick="toggleAutoSend(true)">&#128275; Enable Auto-Send</button>'
          : '<button class="btn btn-secondary btn-sm" disabled title="Reconnect Gmail and verify its profile and send permission first.">&#128274; Enable Auto-Send</button>';
    const autoSendExplanation = autoSendVal
      ? (gmailReady
        ? 'Enabled for eligible routine responses. High-risk, ungrounded, low-confidence, escalated, and otherwise flagged responses still require human review.'
        : 'Enabled in configuration but currently blocked because Gmail authorization, account verification, API availability, or send permission is unavailable.')
      : gmailReady
        ? 'Disabled. Gmail is verified; an administrator may explicitly enable dispatch for eligible routine responses.'
        : 'Disabled. Reconnect Gmail and verify the account, Gmail API, and send permission before enabling Auto-Send.';
    const autoSendConfig = settingsData?.dependencies ? ('Effective setting: ' + (settingsData.dependencies.auto_send_effective_source || 'unknown') + ' (' + (settingsData.dependencies.auto_send || 'unknown') + '). Environment: ' + (settingsData.dependencies.auto_send_environment || 'unknown') + '; database: ' + (settingsData.dependencies.auto_send_database || 'unset') + (settingsData.dependencies.auto_send_configuration_conflict === 'yes' ? ' — settings differ; database value is currently effective.' : '')) : '';
    const gmailNeedsReconnect = !gmailReady;
    const gmailActions = isAdmin() ? '<div class="card mt-16"><div class="card-header"><span class="card-title">Gmail Authorization</span></div><p class="text-sm">OAuth: <strong>' + (settingsData?.gmail_status || 'unknown') + '</strong> · Account: <strong>' + (settingsData?.gmail_connected_email || 'not verified') + '</strong></p><p class="text-sm">Gmail API: <strong>' + (settingsData?.gmail_api_available === true ? 'available' : settingsData?.gmail_api_available === false ? 'unavailable' : 'not checked') + '</strong> · Send permission: <strong>' + (settingsData?.gmail_send_permission ? 'granted' : 'missing') + '</strong></p><p class="text-muted text-sm">Required permissions: ' + ((settingsData?.gmail_required_scopes || []).map(s => s.split('/').pop()).join(', ') || 'not available') + '</p><p class="text-muted text-sm">Last verified authorization: ' + (settingsData?.gmail_last_authorized_at ? new Date(settingsData.gmail_last_authorized_at).toLocaleString() : 'not recorded') + '</p>' + (settingsData?.gmail_authorization_message ? '<p class="text-warning text-sm">' + escapeHtml(settingsData.gmail_authorization_message) + '</p>' : (settingsData?.gmail_authorization_error ? '<p class="text-warning text-sm">' + escapeHtml(settingsData.gmail_authorization_error) + '</p>' : '')) + '<p class="text-muted text-sm">Google redirect URI: ' + (settingsData?.gmail_redirect_uri || 'not configured') + '</p>' + '<button class="btn btn-primary btn-sm" onclick="connectGmail()">' + (gmailNeedsReconnect && settingsData?.gmail_status !== 'not_connected' ? 'Reconnect Gmail' : (settingsData?.gmail_connected_email ? 'Reauthorize Gmail' : 'Connect Gmail')) + '</button></div>' : '';

    el.innerHTML = '<div class="grid-2 gap-16"><div class="card"><div class="card-header"><span class="card-title">Integration &amp; System Health</span></div>' + statusHTML + healthDetails + '<div class="mt-16 flex items-center justify-between text-sm flex-wrap gap-8" style="padding-top:12px;border-top:1px solid var(--border)"><div class="flex-col"><div class="flex gap-8 items-center"><span class="text-muted">Auto-Send Email Mode:</span><span class="' + (autoSendActive ? 'text-success' : 'text-warning') + '" style="font-weight:600">' + (autoSendActive ? 'Enabled (guarded dispatch)' : (autoSendVal ? 'Configured, currently blocked' : 'Disabled')) + '</span></div><p class="text-muted text-sm">' + autoSendExplanation + '</p><p class="text-muted text-sm">' + escapeHtml(autoSendConfig) + '</p><p class="text-muted text-sm">' + (settingsData?.human_approval_policy || 'Human review remains required for flagged responses.') + '</p><label>Confidence threshold <input id="confidence-threshold" type="number" min="0" max="1" step="0.01" value="' + settingsData.confidence_threshold + '" style="width:90px" /></label> ' + (isAdmin()?'<button class="btn btn-secondary btn-sm" onclick="saveConfidenceThreshold()">Save threshold</button>':'') + '</div>' + toggleBtn + '</div>' + gmailActions + '</div>' + (isAdmin()?'<div class="card"><div class="card-header"><span class="card-title">User Management</span><button class="btn btn-primary btn-sm" onclick="showCreateUserModal()">+ Add User</button></div><div class="table-wrap"><table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Actions</th></tr></thead><tbody>' + (userRows||'<tr><td colspan="5" class="table-empty">No users</td></tr>') + '</tbody></table></div></div>':'<div class="card"><p class="text-muted">Admin access required for user management.</p></div>') + '</div>';
  } catch(e) { el.innerHTML = '<div class="card"><p class="text-danger">Failed to load: ' + e.message + '</p></div>'; }
}

function showCreateUserModal() {
  openModal('Create New User', '<div class="flex-col flex gap-12"><div class="form-group"><label>Full Name *</label><input id="nu-name" type="text" placeholder="Jane Smith" /></div><div class="form-group"><label>Email *</label><input id="nu-email" type="email" placeholder="jane@smarttech.local" /></div><div class="form-group"><label>Password *</label><input id="nu-pass" type="password" placeholder="Min 8 characters" /></div><div class="form-group"><label>Role</label><select id="nu-role"><option value="agent">Agent</option><option value="manager">Manager</option><option value="admin">Administrator</option></select></div></div>', '<button class="btn btn-secondary" onclick="closeModal()">Cancel</button><button class="btn btn-primary" onclick="createUser()">Create User</button>');
}

async function createUser() {
  const full_name = document.getElementById('nu-name')?.value?.trim();
  const email = document.getElementById('nu-email')?.value?.trim();
  const password = document.getElementById('nu-pass')?.value;
  const role = document.getElementById('nu-role')?.value;
  if (!full_name || !email || !password) return toast('All fields required', 'warning');
  try { await api('POST', '/api/auth/users', { full_name, email, password, role }); toast('User created', 'success'); closeModal(); loadSettings(); }
  catch(e) { toast(e.message, 'error'); }
}

async function toggleUserActive(id, is_active) {
  try { await api('PATCH', '/api/auth/users/' + id, { is_active }); toast(is_active ? 'User activated' : 'User deactivated', 'success'); await loadSettings(); }
  catch(e) { toast(e.message, 'error'); }
}

async function saveConfidenceThreshold() {
  const value = document.getElementById('confidence-threshold')?.value;
  try { await api('PATCH', '/api/settings/ai_confidence_threshold', { value }); toast('Confidence threshold updated', 'success'); await loadSettings(); }
  catch(e) { toast(e.message, 'error'); }
}

function renderApp() {
  const root = document.getElementById('app');
  if (!state.user) {
    if (!document.getElementById('login-accounts')) {
      root.innerHTML = renderLoginPage();
    }
    loadLoginUsers();
    return;
  }
  root.innerHTML = renderShell();
  setPage(state.currentPage);
}

function brandMark() { return '<svg viewBox="0 0 40 40" aria-hidden="true"><path d="M20 4 34 12v16L20 36 6 28V12L20 4Z" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="20" cy="20" r="4" fill="currentColor"/><circle cx="20" cy="9" r="2" fill="currentColor"/><circle cx="29.5" cy="25.5" r="2" fill="currentColor"/><circle cx="10.5" cy="25.5" r="2" fill="currentColor"/><path d="m20 16 0-5m3.5 11.8 4.3 2.2m-11.3-2.2-4.3 2.2" stroke="currentColor" stroke-width="1.5"/></svg>'; }
function navIcon(name) {
  const paths={dashboard:'<rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="5" rx="1"/><rect x="13" y="10" width="8" height="11" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/>',tickets:'<path d="M4 7V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v2a3 3 0 0 0 0 6v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-2a3 3 0 0 0 0-6Z"/><path d="M13 5v2m0 4v2m0 4v1"/>',drafts:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8m-8 4h6"/>',approvals:'<path d="M9 11 12 14 20 6"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9"/>',customers:'<path d="M16 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="10" cy="7" r="4"/><path d="M20 8v6m3-3h-6"/>',import:'<path d="M12 16V4m-5 5 5-5 5 5"/><path d="M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',knowledge:'<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2Z"/>',analytics:'<path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-5 5"/>',settings:'<circle cx="12" cy="12" r="3"/><path d="m19.4 15 .1.1 1.4 1.1-1.4 2.4-1.7-.7a8 8 0 0 1-1.7 1l-.3 1.8h-2.8l-.3-1.8a8 8 0 0 1-1.7-1l-1.7.7-1.4-2.4L7.3 15a8 8 0 0 1 0-2l-1.4-1.1 1.4-2.4 1.7.7a8 8 0 0 1 1.7-1l.3-1.8h2.8l.3 1.8a8 8 0 0 1 1.7 1l1.7-.7 1.4 2.4-1.4 1.1a8 8 0 0 1-.1 2Z"/>',logout:'<path d="M10 17l5-5-5-5m5 5H3"/><path d="M12 3h6a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-6"/>'};
 return '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+(paths[name]||paths.dashboard)+'</svg>'; }
function renderShell() {
 const user=state.user, initials=(user?.full_name||'U').split(' ').map(n=>n[0]).join('').substring(0,2).toUpperCase();
 const item=(page,label,icon,badge='')=>'<div class="nav-item" data-page="'+page+'" onclick="setPage(\''+page+'\')"><span class="nav-icon">'+navIcon(icon)+'</span><span>'+label+'</span>'+badge+'</div>';
 return '<div class="topbar"><div class="topbar-logo"><span class="logo-icon-svg">'+brandMark()+'</span><span class="brand-wordmark">SMARTTECH</span></div><div class="topbar-divider"></div><span class="topbar-context">AI-Powered Customer Support Platform</span><div class="topbar-spacer"></div><div class="topbar-user"><div class="avatar">'+initials+'</div><div><div class="user-name">'+(user?.full_name||'&mdash;')+'</div></div><span class="user-role">'+(user?.role||'&mdash;')+'</span></div></div><div class="app-shell"><nav class="sidebar"><div class="sidebar-brand"><span class="sidebar-mark">'+brandMark()+'</span><span><strong>SMARTTECH</strong><small>SUPPORT PLATFORM</small></span></div><div class="nav-section-label">Workspace</div>'+item('dashboard','Dashboard','dashboard')+'<div class="nav-section-label">Support</div>'+item('tickets','Tickets','tickets','<span class="nav-badge info" id="badge-escalated" style="display:none">0</span>')+item('drafts','Drafts','drafts','<span class="nav-badge" id="badge-drafts" style="display:none">0</span>')+'<div class="nav-item" data-page="approvals" onclick="setPage(\'drafts\',\'approvals\')"><span class="nav-icon">'+navIcon('approvals')+'</span><span>Approvals</span></div>'+'<div class="nav-section-label">Data</div>'+item('customers','Customers','customers')+item('customer_import','Customer Data Import','import')+item('knowledge','Knowledge Base','knowledge')+'<div class="nav-section-label">Reports</div>'+item('analytics','Analytics','analytics')+'<div class="nav-section-label">Admin</div>'+item('settings','Settings','settings')+'<div class="sidebar-bottom"><div class="nav-item" onclick="logout()"><span class="nav-icon">'+navIcon('logout')+'</span><span>Sign Out</span></div><div class="sidebar-footnote">Intelligent support operations</div></div></nav><main class="main-content">'+renderPages()+'</main></div>';
}
function renderPages() {
  return '<div class="page active" id="page-dashboard"><div class="page-header"><div><div class="page-title">Dashboard</div><div class="page-subtitle">Your intelligent customer support operations at a glance.</div></div><div class="flex gap-8 items-center"><button class="btn btn-secondary" onclick="loadAllData()">&#8635; Refresh</button></div></div><div id="dashboard-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div><div class="page" id="page-tickets"><div class="page-header"><div><div class="page-title">Support Tickets</div><div class="page-subtitle">Manage and respond to customer inquiries</div></div><button class="btn btn-secondary" onclick="loadTickets()">&#8635; Refresh</button></div><div id="tickets-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div><div class="page" id="page-drafts"><div class="page-header"><div><div class="page-title">Drafts &amp; Approvals</div><div class="page-subtitle">Human-in-the-loop review center</div></div><button class="btn btn-secondary" onclick="loadPageData(\'drafts\')">&#8635; Refresh</button></div><div id="drafts-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div><div class="page" id="page-customers"><div class="page-header"><div><div class="page-title">Customers</div><div class="page-subtitle">Customer profiles, devices &amp; order history</div></div><button class="btn btn-secondary" onclick="refreshCustomers()">&#8635; Refresh</button></div><div id="customers-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div><div class="page" id="page-customer_import"><div class="page-header"><div><div class="page-title">Customer Data Import</div><div class="page-subtitle">Batch import customers, orders, devices, and warranties via CSV or Excel (.xlsx)</div></div><div class="flex gap-8"><button class="btn btn-secondary btn-sm" onclick="downloadTemplate(\'csv\')">&#128196; Download CSV Template</button><button class="btn btn-secondary btn-sm" onclick="downloadTemplate(\'xlsx\')">&#128202; Download Excel Template</button></div></div><div id="customer-import-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div><div class="page" id="page-knowledge"><div class="page-header"><div><div class="page-title">Knowledge Base</div><div class="page-subtitle">RAG document management &amp; semantic search</div></div><button class="btn btn-secondary" onclick="loadKnowledgeDocs()">&#8635; Refresh</button></div><div id="knowledge-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div><div class="page" id="page-analytics"><div class="page-header"><div><div class="page-title">Analytics</div><div class="page-subtitle">AI performance &amp; team productivity metrics</div></div><button class="btn btn-secondary" onclick="loadAnalytics()">&#8635; Refresh</button></div><div id="analytics-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div><div class="page" id="page-settings"><div class="page-header"><div><div class="page-title">Settings &amp; System</div><div class="page-subtitle">Integration status and user management</div></div><button class="btn btn-secondary" onclick="loadSettings()">&#8635; Refresh</button></div><div id="settings-content"><div class="loading-overlay"><div class="spinner"></div> Loading...</div></div></div>';
}

let _autoRefreshInterval = null;
function startAutoRefresh() {
  if (_autoRefreshInterval) clearInterval(_autoRefreshInterval);
  _autoRefreshInterval = setInterval(() => {
    if (state.token && state.currentPage === 'dashboard') {
      loadAllData();
    }
  }, 10000);
}


// Always begin at the sign-in screen. The HttpOnly cookie remains protected
// from JavaScript and is replaced after the user explicitly signs in.
renderApp();
