"""Secrets that must never reach the browser or git: the SMTP password, the admin password hash
and the session signing key. Stored in secrets.json, readable only by the file's owner."""
import hashlib
import hmac
import json
import os
import secrets
import threading


SECRETS_FILENAME = os.path.join(os.environ.get('DATA_DIR', '.'), 'secrets.json')
PBKDF2_ITERATIONS = 600000

_lock = threading.Lock()


def _load():
    if not os.path.exists(SECRETS_FILENAME):
        return {}
    with open(SECRETS_FILENAME) as handle:
        return json.load(handle)


def _save(data):
    temp_filename = SECRETS_FILENAME + '.tmp'
    descriptor = os.open(temp_filename, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(data, handle, indent=2)
    os.chmod(temp_filename, 0o600)
    os.replace(temp_filename, SECRETS_FILENAME)


def get(key, default=None):
    with _lock:
        return _load().get(key, default)


def set_values(**values):
    with _lock:
        data = _load()
        data.update(values)
        _save(data)


def session_key():
    with _lock:
        data = _load()
        if not data.get('session_key'):
            data['session_key'] = secrets.token_hex(32)
            _save(data)
        return data['session_key']


def _hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), PBKDF2_ITERATIONS)
    return 'pbkdf2_sha256$' + str(PBKDF2_ITERATIONS) + '$' + salt + '$' + digest.hex()


def admin_configured():
    return bool(get('admin_password_hash'))


def verify_admin_password(password):
    encoded = get('admin_password_hash')
    if not encoded:
        return False
    try:
        _, iterations, salt, expected = encoded.split('$')
        digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected)


def set_admin_password(password):
    """Also bumps password_version, which signs out every existing session."""
    with _lock:
        data = _load()
        data['admin_password_hash'] = _hash_password(password)
        data['password_version'] = data.get('password_version', 0) + 1
        _save(data)
        return data['password_version']
