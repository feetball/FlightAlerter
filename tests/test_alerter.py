import os
import unittest
from unittest import mock

import flightAlerter as alerter
import settings as config


def entry(match, entry_id='a1b2c3d4', label='', subject='', radius=10, enabled=True, recipients=None):
    return {'id': entry_id, 'match': match, 'label': label, 'subject': subject, 'radius_miles': radius,
            'enabled': enabled, 'recipients': recipients or []}


def check_settings(**overrides):
    settings = config.default_settings()
    settings.update({'aircraft_data_url': 'http://receiver/data/aircraft.json', 'home_lat': 29.42, 'home_lon': -98.49,
                     'distribution_list': ['me@example.com'], 'watchlist': []})
    settings.update(overrides)
    return settings


class MilitaryClassificationTest(unittest.TestCase):
    def test_operator_words_match_whole_words_only(self):
        for operator in ('AIRCRAFT GUARANTY CORP TRUSTEE', 'WARBIRD AIRCRAFT LLC', 'MARINE SPILL RESPONSE CORP',
                         'BANK OF UTAH TRUSTEE'):
            self.assertFalse(alerter.is_military_aircraft({'ownOp': operator}), operator)
        for operator in ('UNITED STATES AIR FORCE', 'Royal Air Force', 'RAF', 'UNITED STATES MARINE CORPS', 'US NAVY',
                         'US ARMY', 'Luftwaffe'):
            self.assertTrue(alerter.is_military_aircraft({'ownOp': operator}), operator)

    def test_database_flag_and_callsign(self):
        self.assertTrue(alerter.is_military_aircraft({'dbFlags': 1}))
        self.assertFalse(alerter.is_military_aircraft({'dbFlags': 2}))
        self.assertTrue(alerter.is_military_aircraft({'flight': 'RCH471  '}))
        self.assertFalse(alerter.is_military_aircraft({'flight': 'SWA2713 '}))


class WatchlistMatchingTest(unittest.TestCase):
    def match(self, flight, watchlist):
        return alerter.match_watchlist(flight, alerter.build_watch_index(watchlist))

    def test_entry_with_several_aircraft_matches_each(self):
        jim = entry('N324FX, N268FX, AE1460')
        self.assertEqual(self.match({'r': 'N268FX'}, [jim])[1], {jim['id']: 'N268FX'})
        self.assertEqual(self.match({'flight': 'N324FX  '}, [jim])[1], {jim['id']: 'N324FX'})
        self.assertEqual(self.match({'hex': 'ae1460'}, [jim])[1], {jim['id']: 'AE1460'})
        self.assertEqual(self.match({'r': 'N999ZZ'}, [jim]), ([], {}))

    def test_dashes_are_ignored(self):
        self.assertEqual(self.match({'r': '07-3922'}, [entry('073922')])[1], {'a1b2c3d4': '073922'})

    def test_disabled_entries_never_match(self):
        self.assertEqual(self.match({'r': 'N1'}, [entry('N1', enabled=False)]), ([], {}))

    def test_aircraft_listed_twice_in_one_entry_is_named_by_the_first_listing(self):
        both = entry('SWA2713, N8712L')
        self.assertEqual(self.match({'flight': 'SWA2713', 'r': 'N8712L'}, [both])[1], {both['id']: 'SWA2713'})

    def test_matches_follow_watchlist_order(self):
        first, second = entry('N1', entry_id='11111111'), entry('N1', entry_id='22222222')
        entries, _ = self.match({'r': 'N1'}, [second, first])
        self.assertEqual([e['id'] for e in entries], ['22222222', '11111111'])

    def test_label_summarizes_long_lists(self):
        self.assertEqual(alerter.watch_label(entry('N1, N2, N3')), 'N1 +2 more')
        self.assertEqual(alerter.watch_label(entry('N1')), 'N1')
        self.assertEqual(alerter.watch_label(entry('N1, N2', label='Jim')), 'Jim')


class EmailTextTest(unittest.TestCase):
    def flights(self, watch, *idents):
        return [{'flight': ident, 'watch_idents': {watch['id']: ident}} for ident in idents]

    def test_placeholders_name_the_aircraft_in_range(self):
        jim = entry('N324FX, N268FX, N335FX', subject='Jim might be within <radius>mi in <aircraft>', radius=40)
        subject, heading = alerter.email_text(jim, self.flights(jim, 'N268FX'), check_settings())
        self.assertEqual(subject, 'Jim might be within 40mi in N268FX')
        self.assertEqual(heading, 'N268FX within range')

    def test_default_subject(self):
        plain = entry('N324FX, N268FX', radius=12.5)
        self.assertEqual(alerter.email_text(plain, self.flights(plain, 'N268FX'), check_settings())[0],
                         'N268FX is within 12.5 mi')
        named = entry('N1', label='Club <radius> Cessna', radius=15)
        # A nickname is used as written, even if it looks like a placeholder.
        self.assertEqual(alerter.email_text(named, self.flights(named, 'N1'), check_settings())[0],
                         'Club <radius> Cessna is within 15 mi')

    def test_military_subject_placeholders(self):
        subject, heading = alerter.email_text(None, [{'flight': 'RCH471 '}, {'r': '07-3922'}],
                                              check_settings(subject='<aircraft> within <radius> mi'))
        self.assertEqual(subject, 'RCH471, 07-3922 within 25 mi')
        self.assertEqual(heading, 'Military aircraft within range')


class SentAlertsTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(alerter.SENT_ALERTS_FILENAME):
            os.remove(alerter.SENT_ALERTS_FILENAME)

    def test_round_trip(self):
        rows = alerter.write_alert_log([{'aircraft_registration': 'ae07e3', 'flight': 'RCH471 '}], [])
        alerter.save_sent_alerts(rows)
        loaded = alerter.load_sent_alerts()
        self.assertEqual([(r['flight_num'], r['flight_name']) for r in loaded], [('ae07e3', 'RCH471')])
        self.assertAlmostEqual(loaded[0]['sent_time'], rows[0]['sent_time'])
        self.assertFalse(os.path.exists(alerter.SENT_ALERTS_FILENAME + '.tmp'))
        self.assertEqual(alerter.last_alert_time(), loaded[0]['sent_time'])

    def test_empty_or_damaged_file_does_not_break_checks(self):
        open(alerter.SENT_ALERTS_FILENAME, 'w').close()
        self.assertEqual(alerter.load_sent_alerts(), [])
        with open(alerter.SENT_ALERTS_FILENAME, 'w') as handle:
            handle.write('flight_num,flight_name,sent_time\nae07e3,RCH471,1700000000.5\nbroken,line\nae1460,DOSS14,nope\n')
        with self.assertLogs('flightAlerter', 'WARNING'):
            self.assertEqual([r['flight_num'] for r in alerter.load_sent_alerts()], ['ae07e3'])

    def test_limits(self):
        settings = check_settings(max_alerts_per_aircraft=2, realert_minutes=30)
        now = 1_700_000_000
        flight = {'hex': 'AE07E3'}
        recent = [{'flight_num': 'ae07e3', 'flight_name': 'X', 'sent_time': now - 10 * 60}]
        older = [{'flight_num': 'ae07e3', 'flight_name': 'X', 'sent_time': now - 40 * 60}]
        self.assertEqual(len(alerter.select_eligible_flights([flight], [], settings, now)), 1)
        self.assertEqual(alerter.select_eligible_flights([flight], recent, settings, now), [])
        self.assertEqual(len(alerter.select_eligible_flights([flight], older, settings, now)), 1)
        self.assertEqual(alerter.select_eligible_flights([flight], older + older, settings, now), [])


@mock.patch.object(alerter, 'fetch_aircraft_db_record', return_value={})
class RunCheckTest(unittest.TestCase):
    def setUp(self):
        if os.path.exists(alerter.SENT_ALERTS_FILENAME):
            os.remove(alerter.SENT_ALERTS_FILENAME)

    def run_check(self, aircraft, **settings):
        with mock.patch.object(alerter, 'fetch_aircraft_data', return_value={'aircraft': aircraft}):
            return alerter.run_check(check_settings(**settings))

    def test_one_failing_email_does_not_stop_the_others(self, _db):
        watchlist = [entry('N1', entry_id='11111111', radius=50), entry('N2', entry_id='22222222', radius=50)]
        aircraft = [{'hex': 'a1', 'r': 'N1', 'lat': 29.43, 'lon': -98.49, 'seen_pos': 1},
                    {'hex': 'a2', 'r': 'N2', 'lat': 29.43, 'lon': -98.49, 'seen_pos': 1}]
        sent = []

        def send(flights, subject, *args):
            if subject.startswith('N1'):
                raise OSError('mailbox unavailable')
            sent.append(subject)

        with mock.patch.object(alerter, 'send_email', side_effect=send), self.assertLogs('flightAlerter', 'ERROR'):
            result = self.run_check(aircraft, watchlist=watchlist)
        self.assertEqual(sent, ['N2 is within 50 mi'])
        self.assertTrue(result['ok'])
        self.assertIn('mailbox unavailable', result['error'])
        self.assertEqual([r['flight_num'] for r in alerter.load_sent_alerts()], ['watch:22222222:a2'])

    def test_old_positions_are_counted_but_not_alerted_on(self, _db):
        aircraft = [{'hex': 'ae07e3', 'dbFlags': 1, 'lat': 29.43, 'lon': -98.49, 'seen': 0.5, 'seen_pos': 30},
                    {'hex': 'ae0000', 'dbFlags': 1, 'lat': 29.43, 'lon': -98.49, 'seen': 0.5, 'seen_pos': 120}]
        with mock.patch.object(alerter, 'send_email') as send:
            result = self.run_check(aircraft)
        send.assert_not_called()
        self.assertEqual(result['aircraft_positioned'], 1)
        self.assertEqual(len(result['military_seen']), 1)
        self.assertEqual(result['military_in_range'], [])

    def test_unset_home_location_is_an_error(self, _db):
        result = self.run_check([], home_lat=0, home_lon=0)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'Home location is not set')


if __name__ == '__main__':
    unittest.main()
