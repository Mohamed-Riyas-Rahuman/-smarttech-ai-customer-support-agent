/** Secure account picker for the existing HttpOnly cookie authentication flow. */
function renderLoginPage() {
  return `
    <div class="login-page">
      <div class="login-frame">
        <section class="login-story">
          <div class="login-brand"><span class="login-brand-mark"><svg viewBox="0 0 40 40" aria-hidden="true"><path d="M20 4 34 12v16L20 36 6 28V12L20 4Z" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="20" cy="20" r="4" fill="currentColor"/><circle cx="20" cy="9" r="2" fill="currentColor"/><circle cx="29.5" cy="25.5" r="2" fill="currentColor"/><circle cx="10.5" cy="25.5" r="2" fill="currentColor"/><path d="m20 16 0-5m3.5 11.8 4.3 2.2m-11.3-2.2-4.3 2.2" stroke="currentColor" stroke-width="1.5"/></svg></span><span>SMARTTECH</span></div>
          <div class="login-story-copy"><span class="eyebrow">AI-POWERED CUSTOMER SUPPORT PLATFORM</span><h1>Intelligent Support.<br><em>Simplified.</em></h1><p>Bring every customer conversation, support workflow, and intelligent action together in one clear workspace.</p></div>
          <div class="login-network" aria-hidden="true"><div class="network-ring ring-one"></div><div class="network-ring ring-two"></div><div class="network-core"></div><i class="node node-a"></i><i class="node node-b"></i><i class="node node-c"></i><i class="node node-d"></i></div>
          <div class="login-story-footer"><span>SMARTTECH</span><span>BUILT FOR BETTER SERVICE</span></div>
        </section>
        <section class="login-form-side"><div class="login-card">
          <div class="login-mobile-brand"><span class="login-brand-mark"><svg viewBox="0 0 40 40"><path d="M20 4 34 12v16L20 36 6 28V12L20 4Z" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="20" cy="20" r="4" fill="currentColor"/><circle cx="20" cy="9" r="2" fill="currentColor"/><circle cx="29.5" cy="25.5" r="2" fill="currentColor"/><circle cx="10.5" cy="25.5" r="2" fill="currentColor"/></svg></span>SMARTTECH</div>
          <div class="login-kicker">SECURE WORKSPACE ACCESS</div><h2 class="login-title">Welcome back.</h2><p class="login-sub">Sign in to your workspace.</p>
          <div id="login-error" style="display:none" class="login-error" role="alert"></div>
          <div id="login-accounts" class="login-account-list" aria-live="polite"><p class="text-muted">Loading active accounts...</p></div>
          <form class="login-form" id="login-form" style="display:none" onsubmit="event.preventDefault(); handleLogin();">
            <div class="login-selected-account"><span class="login-field-label">Signing in as</span><strong id="login-selected-email"></strong></div>
            <div class="login-field-group"><label class="login-field-label" for="login-password">Password</label><div class="login-password-row"><input id="login-password" type="password" class="login-password-input" placeholder="Enter your password" autocomplete="current-password"/><button type="button" id="toggle-login-password" class="toggle-password-btn" aria-label="Show password" aria-pressed="false" onclick="togglePasswordVisibility()">Show</button></div></div>
            <div class="login-action-row"><button type="submit" class="login-btn login-btn-full" id="login-btn"><span>Sign in</span></button><button type="button" class="login-back-btn" onclick="cancelLoginSelection()">Back to accounts</button></div>
          </form>
          <div class="login-security"><span class="security-dot"></span>Protected sign-in &middot; Authorized team members</div>
        </div></section>
      </div>
    </div>`;
}
let _loginMembers = [];
let _selectedLoginEmail = null;

function togglePasswordVisibility() {
  const input = document.getElementById('login-password');
  const button = document.getElementById('toggle-login-password');
  if (!input || !button) return;
  const showing = input.type === 'password';
  input.type = showing ? 'text' : 'password';
  button.setAttribute('aria-pressed', String(showing));
  button.setAttribute('aria-label', showing ? 'Hide password' : 'Show password');
}

async function loadLoginUsers() {
  const list = document.getElementById('login-accounts');
  const errEl = document.getElementById('login-error');
  if (!list) return;
  if (errEl) errEl.style.display = 'none';
  list.replaceChildren();
  const loading = document.createElement('p');
  loading.className = 'text-muted';
  loading.textContent = 'Loading active accounts…';
  list.appendChild(loading);
  try {
    const users = await api('GET', '/api/auth/login-users');
    _loginMembers = Array.isArray(users)
      ? users.map(user => String(user.email || '').trim().toLowerCase()).filter(Boolean)
      : [];
    list.replaceChildren();
    if (!_loginMembers.length) {
      const empty = document.createElement('p');
      empty.className = 'text-muted';
      empty.textContent = 'No active accounts are available. Contact your administrator.';
      list.appendChild(empty);
      return;
    }
    for (const email of _loginMembers) {
      const row = document.createElement('div');
      row.className = 'login-account-row';
      const address = document.createElement('span');
      address.className = 'login-account-email';
      address.textContent = email;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'login-btn login-account-button';
      button.textContent = 'Login';
      button.addEventListener('click', () => selectLoginAccount(email));
      row.append(address, button);
      list.appendChild(row);
    }
  } catch (_) {
    _loginMembers = [];
    list.replaceChildren();
    const error = document.createElement('p');
    error.className = 'text-muted';
    error.textContent = 'Could not load active accounts. Check the server connection and try again.';
    list.appendChild(error);
  }
}

function selectLoginAccount(email) {
  const normalized = String(email || '').trim().toLowerCase();
  if (!_loginMembers.includes(normalized)) return;
  _selectedLoginEmail = normalized;
  const form = document.getElementById('login-form');
  const list = document.getElementById('login-accounts');
  const selected = document.getElementById('login-selected-email');
  const password = document.getElementById('login-password');
  const toggle = document.getElementById('toggle-login-password');
  const error = document.getElementById('login-error');
  if (selected) selected.textContent = normalized;
  if (form) form.style.display = 'flex';
  if (list) list.style.display = 'none';
  if (password) { password.value = ''; password.type = 'password'; password.focus(); }
  if (toggle) { toggle.setAttribute('aria-pressed', 'false'); toggle.setAttribute('aria-label', 'Show password'); }
  if (error) error.style.display = 'none';
}

function cancelLoginSelection() {
  _selectedLoginEmail = null;
  const form = document.getElementById('login-form');
  const list = document.getElementById('login-accounts');
  const password = document.getElementById('login-password');
  const toggle = document.getElementById('toggle-login-password');
  const error = document.getElementById('login-error');
  if (password) { password.value = ''; password.type = 'password'; }
  if (toggle) { toggle.setAttribute('aria-pressed', 'false'); toggle.setAttribute('aria-label', 'Show password'); }
  if (form) form.style.display = 'none';
  if (list) list.style.display = '';
  if (error) error.style.display = 'none';
}

async function handleLogin() {
  const button = document.getElementById('login-btn');
  const passwordInput = document.getElementById('login-password');
  const error = document.getElementById('login-error');
  const email = _selectedLoginEmail;
  const password = passwordInput?.value || '';
  if (error) error.style.display = 'none';
  if (!email || !_loginMembers.includes(email)) {
    if (error) { error.textContent = 'Select an active account before signing in.'; error.style.display = 'block'; }
    return;
  }
  if (!password) {
    if (error) { error.textContent = 'Enter your account password.'; error.style.display = 'block'; }
    passwordInput?.focus();
    return;
  }
  if (button) { button.disabled = true; button.textContent = 'Signing in…'; }
  try {
    await login(email, password);
  } catch (err) {
    if (passwordInput) { passwordInput.value = ''; passwordInput.type = 'password'; }
    const toggle = document.getElementById('toggle-login-password');
    if (toggle) { toggle.setAttribute('aria-pressed', 'false'); toggle.setAttribute('aria-label', 'Show password'); }
    if (error) {
      error.textContent = err.status === 401 ? 'Incorrect email or password. Please try again.' : (err.message || 'Sign in failed. Please try again.');
      error.style.display = 'block';
    }
    if (button) { button.disabled = false; button.textContent = 'Sign In'; }
    passwordInput?.focus();
  }
}
