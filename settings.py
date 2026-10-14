import json
import os
import re
import threading

import pandas as pd


SETTINGS_FILENAME = './settings.json'
LEGACY_CONFIG_FILENAMES = ['./militaryFlights.csv', './monitoredFlits.csv', './monitoredFlights.csv']

EMAIL_PATTERN = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

_lock = threading.Lock()


def _env_float(name, default):
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def default_settings():
    smtp_username = os.environ.get('SMTP_USERNAME', '')
    return {
        'enabled': True,
        'send_emails': True,
        'alert_radius_miles': 25.0,
        'distribution_list': [smtp_username] if smtp_username else [],
        'subject': 'Military aircraft nearby',
        'home_lat': _env_float('HOME_LAT', 0.0),
        'home_lon': _env_float('HOME_LON', 0.0),
        'aircraft_data_url': os.environ.get('AIRCRAFT_DATA_URL', ''),
        'check_interval_seconds': 60,
        'max_alerts_per_aircraft': 2,
        'realert_minutes': 30,
        'max_seen_seconds': 10,
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


def _write(settings):
    temp_filename = SETTINGS_FILENAME + '.tmp'
    with open(temp_filename, 'w') as handle:
        json.dump(settings, handle, indent=2)
    os.replace(temp_filename, SETTINGS_FILENAME)


def load_settings():
    with _lock:
        settings = default_settings()
        if os.path.exists(SETTINGS_FILENAME):
            with open(SETTINGS_FILENAME) as handle:
                stored = json.load(handle)
            settings.update({key: value for key, value in stored.items() if key in settings})
        else:
            # First run: carry over the old CSV config so behaviour doesn't change.
            settings.update(_legacy_csv_settings())
            _write(settings)
        return settings


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


def _as_recipients(value):
    if isinstance(value, str):
        value = re.split(r'[|,;\s]+', value)
    if not isinstance(value, list):
        raise ValueError('must be a list of email addresses')
    recipients = [str(address).strip() for address in value if str(address).strip()]
    if not recipients:
        raise ValueError('needs at least one email address')
    invalid = [address for address in recipients if not EMAIL_PATTERN.match(address)]
    if invalid:
        raise ValueError('not a valid email address: ' + ', '.join(invalid))
    return recipients


def _as_subject(value):
    subject = str(value).strip()
    if not 1 <= len(subject) <= 200:
        raise ValueError('must be 1 to 200 characters')
    return subject


def _as_url(value):
    url = str(value).strip()
    if not re.match(r'^https?://[^\s/]+', url):
        raise ValueError('must start with http:// or https://')
    return url


VALIDATORS = {
    'enabled': _as_bool,
    'send_emails': _as_bool,
    'alert_radius_miles': _as_number(float, 0.1, 500),
    'distribution_list': _as_recipients,
    'subject': _as_subject,
    'home_lat': _as_number(float, -90, 90),
    'home_lon': _as_number(float, -180, 180),
    'aircraft_data_url': _as_url,
    'check_interval_seconds': _as_number(int, 15, 3600),
    'max_alerts_per_aircraft': _as_number(int, 1, 20),
    'realert_minutes': _as_number(int, 0, 1440),
    'max_seen_seconds': _as_number(int, 1, 300),
}


def update_settings(changes):
    """Validate and save a partial update. Returns (settings, errors); nothing is saved if any field fails."""
    errors = {}
    cleaned = {}
    for key, value in changes.items():
        if key not in VALIDATORS:
            errors[key] = 'unknown setting'
            continue
        try:
            cleaned[key] = VALIDATORS[key](value)
        except ValueError as exc:
            errors[key] = str(exc)

    settings = load_settings()
    if errors:
        return settings, errors

    with _lock:
        settings.update(cleaned)
        _write(settings)
    return settings, {}
