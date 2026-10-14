import csv
import geopy.distance
import logging
import requests
import smtplib
import ssl
import threading
import time
import os
import re

from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from os.path import exists

import paths
import secret_store
import settings as config


logger = logging.getLogger('flightAlerter')

AIRCRAFT_DB_CACHE = {}
# The receiver's database is split into shard files; cache them so per-aircraft lookups stay cheap.
DB_SHARD_CACHE = {}
DB_SHARD_TTL_SECONDS = 24 * 3600
# After a failed shard fetch, don't retry it for a while so an unreachable database can't stall every cycle.
DB_SHARD_RETRY_SECONDS = 5 * 60
DB_SHARD_FAILURES = {}
SENT_ALERTS_FILENAME = os.path.join(config.DATA_DIR, 'sentAlerts.csv')
SENT_ALERT_COLUMNS = ['flight_num', 'flight_name', 'sent_time']
# Guards sentAlerts.csv, which both the check loop and the web UI touch.
SENT_ALERTS_LOCK = threading.Lock()
# Aircraft whose last position is older than this are left off the map and out of the counts.
POSITION_MAX_AGE_SECONDS = 60

# Whole words only: "RAF" must not match "AIRCRAFT", nor "MARINE" a marine services company.
MILITARY_OPERATOR_PATTERN = re.compile(r'\b(AIR FORCE|USAF|NAVY|MARINE CORPS|USMC|ARMY|COAST GUARD|RAF|LUFTWAFFE)\b')
MILITARY_CALLSIGN_PATTERN = re.compile(r'^(RCH|MC|PAT|AE|VV|NAVY|REACH|KING|SPAR|GOLD)\d+')


def is_military_aircraft(flight):
    db_flags = flight.get('dbFlags')
    if isinstance(db_flags, int) and (db_flags & 1) == 1:
        return True

    if flight.get('military') is True:
        return True

    for field in ('ownOp', 'op'):
        if MILITARY_OPERATOR_PATTERN.search(str(flight.get(field) or '').upper()):
            return True

    callsign = str(flight.get('flight', '')).strip().upper()
    return bool(MILITARY_CALLSIGN_PATTERN.match(callsign))


def get_aircraft_db_url(aircraft_data_url):
    if '/data/' in aircraft_data_url:
        return aircraft_data_url.split('/data/', 1)[0] + '/db'

    return aircraft_data_url.rsplit('/', 1)[0] + '/db'


def fetch_db_shard(db_url, block_key, timeout_seconds):
    """Returns the shard dict, {} if the shard doesn't exist, or None on a transient error."""
    cache_key = db_url + '/' + block_key
    cached = DB_SHARD_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < DB_SHARD_TTL_SECONDS:
        return cached[1]
    failed_at = DB_SHARD_FAILURES.get(cache_key)
    if failed_at and time.time() - failed_at < DB_SHARD_RETRY_SECONDS:
        return None

    try:
        response = requests.get(db_url + '/' + block_key + '.json', timeout=timeout_seconds)
        if response.status_code == 404:
            shard_data = {}
        else:
            response.raise_for_status()
            shard_data = response.json()
    except (requests.exceptions.RequestException, ValueError):
        DB_SHARD_FAILURES[cache_key] = time.time()
        return None

    DB_SHARD_FAILURES.pop(cache_key, None)
    DB_SHARD_CACHE[cache_key] = (time.time(), shard_data)
    return shard_data


def fetch_aircraft_db_record(db_url, aircraft_hex, timeout_seconds=5):
    if not aircraft_hex:
        return {}

    icao = str(aircraft_hex).strip().upper()
    if not icao:
        return {}

    if icao in AIRCRAFT_DB_CACHE:
        return AIRCRAFT_DB_CACHE[icao]

    for level in range(1, len(icao) + 1):
        block_key = icao[:level]
        data_key = icao[level:]

        shard_data = fetch_db_shard(db_url, block_key, timeout_seconds)
        if shard_data is None:
            return {}  # transient failure: don't cache, try again next time

        if data_key in shard_data:
            AIRCRAFT_DB_CACHE[icao] = shard_data[data_key]
            return shard_data[data_key]

        children = shard_data.get('children', [])
        if not data_key or (block_key + data_key[:1]) not in children:
            AIRCRAFT_DB_CACHE[icao] = {}
            return {}

    AIRCRAFT_DB_CACHE[icao] = {}
    return {}


def enrich_flight_with_aircraft_db(flight, db_url):
    aircraft_hex = flight.get('hex')
    aircraft_db_record = fetch_aircraft_db_record(db_url, aircraft_hex)
    if not aircraft_db_record:
        return flight

    for key, value in aircraft_db_record.items():
        if flight.get(key) in (None, ''):
            flight[key] = value

    return flight


def get_aircraft_type(flight):
    direct_fields = [
        't', 'desc', 'aircraft_type', 'ac_type', 'model', 'man', 'icaoType'
    ]
    for field in direct_fields:
        value = flight.get(field)
        if value not in (None, ''):
            return str(value)

    if flight.get('dbFlags') not in (None, ''):
        return 'Unknown (dbFlags=' + str(flight.get('dbFlags')) + ')'

    if flight.get('category') not in (None, ''):
        return 'Unknown (category ' + str(flight.get('category')) + ')'

    return 'Unknown'


def display_name(flight):
    return str(flight.get('flight') or '').strip() or flight.get('r') or str(flight.get('hex') or 'unknown').upper()


def fetch_aircraft_data(url, timeout_seconds=10, max_attempts=3, retry_delay_seconds=5):
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.get(url, timeout=timeout_seconds)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            logger.warning('Aircraft data fetch failed (attempt ' + str(attempt) + '/' + str(max_attempts) + '): ' + str(exc))
            if attempt < max_attempts:
                time.sleep(retry_delay_seconds)
        except ValueError as exc:
            logger.error('Aircraft data response was not valid JSON: ' + str(exc))
            return None

    logger.error('Unable to fetch aircraft data after retries. Skipping this cycle.')
    return None


def smtp_config(settings):
    password = secret_store.get('smtp_password') or ''
    return {
        'hostname': settings['smtp_hostname'],
        'port': settings['smtp_port'],
        'security': settings['smtp_security'],
        'username': settings['smtp_username'],
        'password': password,
        'from_name': settings['smtp_from_name'],
        'configured': bool(settings['smtp_hostname'] and settings['smtp_username'] and password),
    }


def watch_idents(entry):
    """The tail numbers, callsigns and hex codes of a watchlist entry; one entry can list several."""
    return [part for part in config.split_idents(entry['match']) if config.normalize_ident(part)]


def build_watch_index(watchlist):
    """Maps each normalized identifier to the enabled entries listing it, so matching an aircraft is a
    lookup rather than a scan of the whole watchlist. Values are (entry order, entry, identifier as written,
    position in the entry)."""
    index = {}
    for order, entry in enumerate(watchlist):
        if not entry['enabled']:
            continue
        for position, ident in enumerate(watch_idents(entry)):
            index.setdefault(config.normalize_ident(ident), []).append((order, entry, ident, position))
    return index


def match_watchlist(flight, watch_index):
    """(entries, {entry id: identifier it matched}) for the watchlist entries this aircraft matches by
    callsign, registration (tail number) or hex code, in watchlist order."""
    best = {}
    for field in ('flight', 'r', 'hex'):
        for order, entry, ident, position in watch_index.get(config.normalize_ident(flight.get(field)), []):
            # If an entry lists this aircraft twice (say callsign and tail number), name the one listed first.
            if entry['id'] not in best or position < best[entry['id']][3]:
                best[entry['id']] = (order, entry, ident, position)
    matches = sorted(best.values(), key=lambda match: match[0])
    return [match[1] for match in matches], {match[1]['id']: match[2] for match in matches}


def annotate_aircraft(flight, home_coords, db_url, watch_index, db_lookup_radius=None):
    """Adds distance, database fields, military flag and matching watchlist entries.

    With db_lookup_radius, aircraft farther away are classified from the receiver's own fields only,
    skipping the database lookup (several HTTP requests for an aircraft not seen before)."""
    flight['distance'] = round(geopy.distance.distance(home_coords, (flight['lat'], flight['lon'])).mi, 1)
    if db_lookup_radius is None or flight['distance'] <= db_lookup_radius:
        enrich_flight_with_aircraft_db(flight, db_url)
    flight['military'] = is_military_aircraft(flight)
    flight['watch_entries'], flight['watch_idents'] = match_watchlist(flight, watch_index)
    return flight


def summarize_aircraft(flight):
    watch_idents_by_entry = flight.get('watch_idents', {})
    return {
        'hex': flight.get('hex'),
        'flight': str(flight.get('flight') or '').strip(),
        'registration': flight.get('r'),
        'type': get_aircraft_type(flight),
        'lat': flight.get('lat'),
        'lon': flight.get('lon'),
        'altitude': flight.get('alt_baro'),
        'speed': flight.get('gs'),
        'track': flight.get('track'),
        'squawk': flight.get('squawk'),
        'distance': flight.get('distance'),
        'military': bool(flight.get('military')),
        'watched': [{'id': entry['id'], 'label': entry.get('label') or watch_idents_by_entry.get(entry['id']),
                     'ident': watch_idents_by_entry.get(entry['id']), 'radius_miles': entry['radius_miles']}
                    for entry in flight.get('watch_entries', [])],
        'seen': flight.get('seen'),
    }


def watch_label(entry):
    if entry.get('label'):
        return entry['label']
    idents = watch_idents(entry)
    if len(idents) < 2:
        return entry['match']
    return idents[0] + ' +' + str(len(idents) - 1) + ' more'


def fill_placeholders(template, aircraft_names, radius_miles):
    """A user-written subject with <aircraft> and <radius> filled in."""
    text = re.sub(r'<\s*aircraft\s*>', lambda _: ', '.join(aircraft_names), template, flags=re.IGNORECASE)
    return re.sub(r'<\s*radius\s*>', lambda _: '%g' % radius_miles, text, flags=re.IGNORECASE)


def email_text(entry, flights, settings):
    """(subject, first line) of the email about these aircraft. entry is None for the military email."""
    if entry is None:
        names = [display_name(flight) for flight in flights]
        return (fill_placeholders(settings['subject'], names, settings['alert_radius_miles']),
                'Military aircraft within range')

    # Name the aircraft actually in range, not every aircraft the entry lists.
    names = []
    for flight in flights:
        ident = flight.get('watch_idents', {}).get(entry['id']) or display_name(flight)
        if ident not in names:
            names.append(ident)
    who = entry.get('label') or ', '.join(names)
    if entry['subject']:
        subject = fill_placeholders(entry['subject'], names, entry['radius_miles'])
    else:
        subject = who + ' is within ' + '%g' % entry['radius_miles'] + ' mi'
    return subject, who + ' within range'


def load_sent_alerts():
    """Sent alert rows, oldest first. Unreadable lines are skipped so one bad line can't stop every check."""
    if not exists(SENT_ALERTS_FILENAME):
        return []
    rows = []
    with open(SENT_ALERTS_FILENAME, newline='') as handle:
        for row in csv.DictReader(handle):
            try:
                rows.append({'flight_num': row['flight_num'].strip().lower(),
                             'flight_name': (row.get('flight_name') or '').strip(),
                             'sent_time': float(row['sent_time'])})
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.warning('Skipping unreadable line in sentAlerts.csv: ' + repr(row))
    return rows


def save_sent_alerts(sent_alerts):
    # Write a new file and swap it in, so a crash mid-write can't leave the history empty or cut short.
    temp_filename = SENT_ALERTS_FILENAME + '.tmp'
    with open(temp_filename, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=SENT_ALERT_COLUMNS)
        writer.writeheader()
        writer.writerows(sent_alerts)
    os.replace(temp_filename, SENT_ALERTS_FILENAME)


def last_alert_time():
    with SENT_ALERTS_LOCK:
        return max((row['sent_time'] for row in load_sent_alerts()), default=None)


def alert_key(flight, prefix=''):
    return prefix + (str(flight.get('hex') or flight.get('flight') or 'unknown').strip().lower() or 'unknown')


def select_eligible_flights(flights, sent_alerts, settings, current_timestamp, key_prefix=''):
    eligible_flights = []
    for flight_info in flights:
        aircraft_registration = alert_key(flight_info, key_prefix)
        past_alerts = [row['sent_time'] for row in sent_alerts if row['flight_num'] == aircraft_registration]

        if len(past_alerts) >= settings['max_alerts_per_aircraft']:
            continue

        # Repeat notifications must wait realert_minutes after the previous one.
        if past_alerts and (current_timestamp - max(past_alerts)) / 60 <= settings['realert_minutes']:
            continue

        eligible_flights.append(dict(flight_info, aircraft_registration=aircraft_registration))
    return eligible_flights


def run_check(settings):
    """One polling cycle. Returns a summary dict; raises only on unexpected errors."""
    result = {
        'started_at': time.time(),
        'ok': False,
        'error': None,
        'aircraft_total': 0,
        'aircraft_positioned': 0,
        'military_seen': [],
        'military_in_range': [],
        'watch_in_range': [],
        'notified': [],
        'emails_sent': 0,
        'email_sent': False,
    }

    if not settings['aircraft_data_url']:
        result['error'] = 'Receiver URL is not set'
        return result
    if settings['home_lat'] == 0 and settings['home_lon'] == 0:
        result['error'] = 'Home location is not set'
        return result

    json_dump = fetch_aircraft_data(settings['aircraft_data_url'])
    if json_dump is None:
        result['error'] = 'Could not reach the receiver'
        return result

    home_coords = (settings['home_lat'], settings['home_lon'])
    aircraft_db_url = get_aircraft_db_url(settings['aircraft_data_url'])
    watch_index = build_watch_index(settings['watchlist'])
    aircraft = json_dump.get('aircraft', [])
    result['aircraft_total'] = len(aircraft)

    # Aircraft outside every alert radius can't trigger an email, so they don't need a database lookup.
    lookup_radius = max([settings['alert_radius_miles']]
                        + [entry['radius_miles'] for entry in settings['watchlist'] if entry['enabled']])

    # (entry, [flights]) for each watchlist entry with a matching aircraft inside its own radius
    watch_hits = {}
    for flight in aircraft:
        if 'lat' not in flight or 'lon' not in flight:
            continue
        position_age = flight.get('seen_pos', flight.get('seen', 999))
        if position_age > POSITION_MAX_AGE_SECONDS:
            continue
        result['aircraft_positioned'] += 1

        annotate_aircraft(flight, home_coords, aircraft_db_url, watch_index, lookup_radius)
        if flight['military']:
            result['military_seen'].append(flight)

        # Only alert on a recent position: an aircraft moves miles in a minute.
        if position_age >= settings['max_seen_seconds']:
            continue
        in_watch_range = False
        for entry in flight['watch_entries']:
            if flight['distance'] <= entry['radius_miles']:
                watch_hits.setdefault(entry['id'], (entry, []))[1].append(flight)
                in_watch_range = True
        if in_watch_range:
            result['watch_in_range'].append(flight)
        if flight['military'] and flight['distance'] <= settings['alert_radius_miles']:
            result['military_in_range'].append(flight)

    # Each batch is one email: all military aircraft together (no entry), then one per watchlist entry.
    batches = [(None, settings['distribution_list'], '', result['military_in_range'])]
    for entry, flights in watch_hits.values():
        batches.append((entry, entry['recipients'] or settings['distribution_list'], 'watch:' + entry['id'] + ':', flights))

    email_errors = []
    with SENT_ALERTS_LOCK:
        sent_alerts = load_sent_alerts()
        for entry, recipients, key_prefix, flights in batches:
            eligible_flights = select_eligible_flights(flights, sent_alerts, settings, time.time(), key_prefix)
            if not eligible_flights:
                continue
            subject, heading = email_text(entry, eligible_flights, settings)
            if not settings['send_emails']:
                # Not logged as sent, so they alert once emails are switched back on.
                logger.info('Emails are off; would have sent "' + subject + '" about ' + str(len(eligible_flights)) + ' aircraft.')
                result['notified'].extend(eligible_flights)
                continue
            try:
                send_email(eligible_flights, subject, recipients, heading, settings)
            except Exception as exc:
                # Carry on with the other emails. Nothing is logged as sent, so this one is retried next check.
                message = str(exc) or repr(exc)
                logger.error('Could not send "' + subject + '": ' + message)
                email_errors.append(message)
                continue
            result['notified'].extend(eligible_flights)
            sent_alerts = write_alert_log(eligible_flights, sent_alerts)
            save_sent_alerts(sent_alerts)
            result['emails_sent'] += 1

    result['email_sent'] = result['emails_sent'] > 0
    if email_errors:
        result['error'] = ('Could not send ' + str(len(email_errors)) + ' email' + ('s' if len(email_errors) > 1 else '')
                           + ': ' + email_errors[0])
    if not result['notified'] and not email_errors:
        logger.info('Checked ' + str(result['aircraft_positioned']) + ' aircraft; '
                    + str(len(result['military_in_range'])) + ' military and '
                    + str(len(result['watch_in_range'])) + ' watched in range, none due a notification.')
    result['ok'] = True
    return result


def send_message(subject, message_text, recipients, smtp):
    if not smtp['configured']:
        raise RuntimeError('The email server is not set up. Add the SMTP server, username and password in Settings.')

    msg = MIMEText(message_text, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = formataddr((str(Header(smtp['from_name'], 'utf-8')), smtp['username']))
    msg['To'] = ', '.join(recipients)

    # create_default_context() verifies the server certificate; smtplib's own default does not.
    context = ssl.create_default_context()
    if smtp['security'] == 'starttls':
        server = smtplib.SMTP(smtp['hostname'], smtp['port'], timeout=30)
    else:
        server = smtplib.SMTP_SSL(smtp['hostname'], smtp['port'], timeout=30, context=context)
    # The with block always closes the connection, and a failing QUIT can't hide the real error.
    with server:
        if smtp['security'] == 'starttls':
            server.starttls(context=context)
        server.login(smtp['username'], smtp['password'])
        server.sendmail(smtp['username'], recipients, msg.as_string())


def send_email(flights, subject, recipients, heading, settings):
    flights_sorted = sorted(flights, key=lambda item: item.get('distance', 9999))
    message_lines = [
        heading + ': ' + str(len(flights_sorted)),
        ''
    ]

    for index, flight_info in enumerate(flights_sorted, start=1):
        message_lines.append('Aircraft #' + str(index))
        message_lines.append('Distance: ' + str(flight_info.get('distance', 'unknown')) + ' miles')
        message_lines.append('Type: ' + get_aircraft_type(flight_info))
        message_lines.append('Flight: ' + str(flight_info.get('flight', 'unknown')))
        message_lines.append('Registration: ' + str(flight_info.get('r', 'unknown')))
        message_lines.append('Hex: ' + str(flight_info.get('hex', 'unknown')))
        message_lines.append('All Stats:')
        for key in sorted(flight_info.keys()):
            if key in ('watch_entries', 'watch_idents', 'aircraft_registration'):
                continue
            message_lines.append('  ' + str(key) + ': ' + str(flight_info.get(key)))
        message_lines.append('')

    full_subject = subject + ' (' + str(len(flights_sorted)) + ')'
    send_message(full_subject, '\r\n'.join(message_lines), recipients, smtp_config(settings))
    logger.info('Email "' + full_subject + '" sent to ' + str(len(recipients)) + ' recipient(s)')


def send_test_email(settings):
    send_message(settings['subject'] + ' (test)',
                 'This is a test message from Flight Alerter. If you can read this, email alerts are working.',
                 settings['distribution_list'], smtp_config(settings))
    logger.info('Test email sent to ' + ', '.join(settings['distribution_list']))


def write_alert_log(flights, sent_alerts):
    timestamp = time.time()
    return sent_alerts + [{
        'flight_num': flight_info.get('aircraft_registration', 'unknown'),
        'flight_name': str(flight_info.get('flight') or '').strip() or flight_info.get('r') or 'unknown',
        'sent_time': timestamp,
    } for flight_info in flights]


def main():
    """Run a single check from the command line. The web app (app.py) runs this on a loop."""
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    paths.prepare_data_dir()
    result = run_check(config.load_settings())
    if result['error']:
        logger.error(result['error'])


if __name__ == "__main__":
    main()
