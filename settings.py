import json
import os
import re
import secrets
import threading

import pandas as pd

import secret_store


SETTINGS_FILENAME = './settings.json'
LEGACY_CONFIG_FILENAMES = ['./militaryFlights.csv', './monitoredFlits.csv', './monitoredFlights.csv']
LEGACY_WATCHLIST_FILENAME = './monitoredFlights.csv'

EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
HOSTNAME_PATTERN = re.compile(r'^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$')
IDENT_PATTERN = re.compile(r'^[A-Za-z0-9-]{1,20}$')
MAX_WATCHLIST_ENTRIES = 100

_lock = threading.Lock()


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def default_settings():
    smtp_username = os.environ.get('SMTP_USERNAME', '')
    return {
        'enabled': True,
        'send_emails': True,
        'alert_radius_miles': 25.0,
        'distribution_list': [smtp_username] if EMAIL_PATTERN.match(smtp_username) else [],
        'subject': 'Military aircraft nearby',
        'home_lat': _env_float('HOME_LAT', 0.0),
        'home_lon': _env_float('HOME_LON', 0.0),
        'aircraft_data_url': os.environ.get('AIRCRAFT_DATA_URL', ''),
        'check_interval_seconds': 60,
        'max_alerts_per_aircraft': 2,
        'realert_minutes': 30,
        'max_seen_seconds': 10,
        'watchlist': [],
        'smtp_hostname': os.environ.get('SMTP_HOSTNAME', 'smtp.zoho.com'),
        'smtp_port': _env_int('SMTP_PORT', 465),
        'smtp_security': 'starttls' if _env_int('SMTP_PORT', 465) == 587 else 'ssl',
        'smtp_username': smtp_username,
        'smtp_from_name': os.environ.get('SMTP_FROM_NAME', 'Flight Alerter'),
    }


def _legacy_csv_settings():
    for filename in LEGACY_CONFIG_FILENAMES:
        if not os.path.exists(filename):
            continue

        config = pd.read_csv(filename, skipinitialspace=True)
        if len(config) == 0:
            return {}

        row = config.iloc[0]
        legacy = {}
        for column in ('military_distance', 'max_distance'):
            if column in config.columns and pd.notna(row.get(column)):
                legacy['alert_radius_miles'] = float(row.get(column))
                break
        if 'distribution_list' in config.columns and pd.notna(row.get('distribution_list')):
            legacy['distribution_list'] = [a.strip() for a in str(row.get('distribution_list')).split('|') if a.strip()]
        if 'subject' in config.columns and pd.notna(row.get('subject')):
            legacy['subject'] = str(row.get('subject')).strip()
        return legacy

    return {}


def _legacy_watchlist():
    """Rows of the old monitoredFlights.csv: registration, distribution_list, max_distance, subject."""
    if not os.path.exists(LEGACY_WATCHLIST_FILENAME):
        return []

    rows = pd.read_csv(LEGACY_WATCHLIST_FILENAME, skipinitialspace=True, dtype=str)
    watchlist = []
    for _, row in rows.iterrows():
        values = [str(value).strip() if pd.notna(value) else '' for value in row.values]
        values += [''] * (4 - len(values))
        try:
            watchlist.append(_as_watch_entry({
                'match': values[0],
                'recipients': [a for a in values[1].split('|') if a.strip()],
                'radius_miles': float(values[2]) if values[2] else 25,
                'subject': values[3],
            }))
        except ValueError:
            continue
    return watchlist


def _write(settings):
    temp_filename = SETTINGS_FILENAME + '.tmp'
    with open(temp_filename, 'w') as handle:
        json.dump(settings, handle, indent=2)
    os.replace(temp_filename, SETTINGS_FILENAME)


def load_settings():
    with _lock:
        settings = default_settings()
        stored = {}
        if os.path.exists(SETTINGS_FILENAME):
            with open(SETTINGS_FILENAME) as handle:
                stored = json.load(handle)
        else:
            # First run: carry over the old CSV config so behaviour doesn't change.
            stored.update(_legacy_csv_settings())

        if 'watchlist' not in stored:
            stored['watchlist'] = _legacy_watchlist()

        settings.update({key: value for key, value in stored.items() if key in settings})

        # Persist seeded values (from .env or old CSVs) so .env can later be emptied.
        if set(settings) - set(stored):
            _write(settings)

    if secret_store.get('smtp_password') is None and os.environ.get('SMTP_PASSWORD'):
        secret_store.set_values(smtp_password=os.environ['SMTP_PASSWORD'])
    return settings


def public_settings():
    settings = load_settings()
    settings['smtp_password_set'] = bool(secret_store.get('smtp_password'))
    return settings


# ---------- validation ----------

def _no_control_chars(text, field_name='value'):
    if re.search(r'[\x00-\x1f\x7f]', text):
        raise ValueError(field_name + ' cannot contain line breaks or control characters')
    return text


def _as_bool(value):
    if isinstance(value, bool):
        return value
    raise ValueError('must be true or false')


def _as_number(cast, low, high):
    def convert(value):
        if isinstance(value, bool):
            raise ValueError('must be a number')
        try:
            number = cast(value)
        except (TypeError, ValueError):
            raise ValueError('must be a number')
        if cast is int and float(value) != number:
            raise ValueError('must be a whole number')
        if not low <= number <= high:
            raise ValueError('must be between ' + str(low) + ' and ' + str(high))
        return number
    return convert


def _as_recipients(value, allow_empty=False):
    if isinstance(value, str):
        value = re.split(r'[|,;\s]+', value)
    if not isinstance(value, list):
        raise ValueError('must be a list of email addresses')
    recipients = [str(address).strip() for address in value if str(address).strip()]
    if not recipients and not allow_empty:
        raise ValueError('needs at least one email address')
    if len(recipients) > 50:
        raise ValueError('can have at most 50 addresses')
    invalid = [address for address in recipients if not EMAIL_PATTERN.match(address) or len(address) > 254]
    if invalid:
        raise ValueError('not a valid email address: ' + ', '.join(invalid))
    return recipients


def _as_text(min_length, max_length):
    def convert(value):
        text = _no_control_chars(str(value if value is not None else '').strip())
        if not min_length <= len(text) <= max_length:
            if min_length:
                raise ValueError('must be ' + str(min_length) + ' to ' + str(max_length) + ' characters')
            raise ValueError('must be at most ' + str(max_length) + ' characters')
        return text
    return convert


def _as_url(value):
    url = _no_control_chars(str(value).strip())
    if not re.match(r'^https?://[^\s/]+', url) or len(url) > 500:
        raise ValueError('must start with http:// or https://')
    return url


def _as_hostname(value):
    hostname = str(value).strip()
    if not HOSTNAME_PATTERN.match(hostname):
        raise ValueError('must be a host name such as smtp.example.com')
    return hostname


def _as_security(value):
    if value not in ('ssl', 'starttls'):
        raise ValueError('must be ssl or starttls')
    return value


def _as_watch_entry(value):
    if not isinstance(value, dict):
        raise ValueError('each entry must be an object')
    match = str(value.get('match', '')).strip().upper()
    if not IDENT_PATTERN.match(match):
        raise ValueError('"' + match[:20] + '" is not a tail number, callsign or hex code (letters, digits and dashes only)')
    entry_id = str(value.get('id') or '')
    if not re.match(r'^[a-f0-9]{8}$', entry_id):
        entry_id = secrets.token_hex(4)
    return {
        'id': entry_id,
        'match': match,
        'label': _as_text(0, 60)(value.get('label', '')),
        'radius_miles': _as_number(float, 0.1, 500)(value.get('radius_miles', 25)),
        'recipients': _as_recipients(value.get('recipients', []), allow_empty=True),
        'subject': _as_text(0, 200)(value.get('subject', '')),
        'enabled': _as_bool(value.get('enabled', True)),
    }


def _as_watchlist(value):
    if not isinstance(value, list):
        raise ValueError('must be a list')
    if len(value) > MAX_WATCHLIST_ENTRIES:
        raise ValueError('can have at most ' + str(MAX_WATCHLIST_ENTRIES) + ' aircraft')
    entries = []
    for index, item in enumerate(value, start=1):
        try:
            entries.append(_as_watch_entry(item))
        except ValueError as exc:
            raise ValueError('aircraft #' + str(index) + ': ' + str(exc))
    return entries


def _as_smtp_password(value):
    password = str(value)
    if not 1 <= len(password) <= 256:
        raise ValueError('must be 1 to 256 characters')
    return _no_control_chars(password, 'password')


VALIDATORS = {
    'enabled': _as_bool,
    'send_emails': _as_bool,
    'alert_radius_miles': _as_number(float, 0.1, 500),
    'distribution_list': _as_recipients,
    'subject': _as_text(1, 200),
    'home_lat': _as_number(float, -90, 90),
    'home_lon': _as_number(float, -180, 180),
    'aircraft_data_url': _as_url,
    'check_interval_seconds': _as_number(int, 15, 3600),
    'max_alerts_per_aircraft': _as_number(int, 1, 20),
    'realert_minutes': _as_number(int, 0, 1440),
    'max_seen_seconds': _as_number(int, 1, 300),
    'watchlist': _as_watchlist,
    'smtp_hostname': _as_hostname,
    'smtp_port': _as_number(int, 1, 65535),
    'smtp_security': _as_security,
    'smtp_username': _as_text(0, 254),
    'smtp_from_name': _as_text(1, 100),
    'smtp_password': _as_smtp_password,
}


def update_settings(changes):
    """Validate and save a partial update. Returns (settings, errors); nothing is saved if any field fails.

    smtp_password is write-only: it goes to the secret store and is never returned."""
    errors = {}
    cleaned = {}
    for key, value in changes.items():
        if key not in VALIDATORS:
            errors[key] = 'unknown setting'
            continue
        if key == 'smtp_password' and value in (None, ''):
            continue  # blank means "keep the current password"
        try:
            cleaned[key] = VALIDATORS[key](value)
        except ValueError as exc:
            errors[key] = str(exc)

    if errors:
        return public_settings(), errors

    password = cleaned.pop('smtp_password', None)
    settings = load_settings()
    with _lock:
        settings.update(cleaned)
        _write(settings)
    if password is not None:
        secret_store.set_values(smtp_password=password)
    return public_settings(), {}
