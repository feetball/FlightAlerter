'use strict';

const errorBox = document.getElementById('auth-error');

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

async function post(path, body) {
  const response = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'FlightAlerter' },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

async function init() {
  const status = await fetch('/api/auth/status').then((r) => r.json());
  if (status.logged_in) { location.replace('/'); return; }
  if (status.version && status.version !== 'unknown') {
    const badge = document.getElementById('app-version');
    badge.textContent = 'v' + status.version;
    badge.hidden = false;
  }
  const form = document.getElementById(status.configured ? 'login-form' : 'setup-form');
  form.hidden = false;
  form.querySelector('input').focus();
}

document.getElementById('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  showError('');
  try {
    await post('/api/auth/login', { password: document.getElementById('login-password').value });
    location.replace('/');
  } catch (error) {
    showError(error.message);
    document.getElementById('login-password').select();
  }
});

document.getElementById('setup-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  showError('');
  const password = document.getElementById('setup-password').value;
  if (password !== document.getElementById('setup-confirm').value) {
    showError('The two passwords do not match.');
    return;
  }
  try {
    await post('/api/auth/setup', { code: document.getElementById('setup-code').value, password });
    location.replace('/');
  } catch (error) {
    showError(error.message);
  }
});

init().catch(() => showError('Could not reach the Flight Alerter service.'));
