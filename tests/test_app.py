import threading
import time
import unittest
from unittest import mock

import app as webapp
import flightAlerter as alerter
import secret_store


class StopLoop(Exception):
    pass


class SchedulerTest(unittest.TestCase):
    def test_an_error_does_not_stop_the_scheduler(self):
        with mock.patch.object(webapp.config, 'load_settings', side_effect=ValueError('bad settings.json')), \
                mock.patch.object(webapp.wake_scheduler, 'wait', side_effect=StopLoop) as wait, \
                self.assertLogs('flightAlerter', 'ERROR'):
            with self.assertRaises(StopLoop):
                webapp.scheduler_loop()
        wait.assert_called_once_with(60)
        self.assertIsNotNone(webapp.state['next_run_at'])


class VersionTest(unittest.TestCase):
    def test_version_comes_from_the_version_file_and_is_in_status(self):
        self.assertRegex(webapp.VERSION, r'^\d+\.\d+\.\d+')
        with open(webapp.os.path.join(webapp.paths.APP_DIR, 'VERSION')) as handle:
            self.assertEqual(webapp.VERSION, handle.read().strip())
        webapp.app.secret_key = 'test'
        version = secret_store.set_admin_password('correct horse')
        client = webapp.app.test_client()
        with client.session_transaction() as session:
            session.update({'user': 'admin', 'password_version': version})
        self.assertEqual(client.get('/api/status').get_json()['version'], webapp.VERSION)


class LoginThrottleTest(unittest.TestCase):
    def setUp(self):
        webapp.login_failures.clear()

    def test_attempts_are_counted_before_the_password_is_checked(self):
        with webapp.app.test_request_context(environ_base={'REMOTE_ADDR': '10.0.0.9'}):
            # Five attempts in flight at once, none finished yet: a sixth must already be refused.
            self.assertEqual([webapp.begin_login_attempt() for _ in range(5)], [0] * 5)
            self.assertGreater(webapp.begin_login_attempt(), 0)
            webapp.clear_login_failures()
            self.assertEqual(webapp.begin_login_attempt(), 0)

    def test_wrong_passwords_lock_the_address_out(self):
        secret_store.set_admin_password('correct horse')
        webapp.app.secret_key = 'test'
        client = webapp.app.test_client()
        headers = {'X-Requested-With': 'FlightAlerter'}
        with self.assertLogs('flightAlerter', 'WARNING'):
            codes = [client.post('/api/auth/login', json={'password': 'wrong'}, headers=headers).status_code
                     for _ in range(6)]
        self.assertEqual(codes, [403] * 5 + [429])


class AlertsApiTest(unittest.TestCase):
    def setUp(self):
        webapp.app.secret_key = 'test'
        version = secret_store.set_admin_password('correct horse')
        self.client = webapp.app.test_client()
        with self.client.session_transaction() as session:
            session.update({'user': 'admin', 'password_version': version})
        alerter.save_sent_alerts([
            {'flight_num': 'ae07e3', 'flight_name': 'RCH471', 'sent_time': 100.0},
            {'flight_num': 'ae07e3', 'flight_name': 'RCH471', 'sent_time': 200.0},
            {'flight_num': 'watch:11111111:a1', 'flight_name': 'N1', 'sent_time': 150.0},
        ])

    def test_list_and_reset(self):
        data = self.client.get('/api/alerts').get_json()
        self.assertEqual(data['total'], 3)
        self.assertEqual([(a['key'], a['count'], a['kind']) for a in data['alerts']],
                         [('ae07e3', 2, 'military'), ('watch:11111111:a1', 1, 'watch')])
        response = self.client.delete('/api/alerts/AE07E3', headers={'X-Requested-With': 'FlightAlerter'})
        self.assertEqual(response.get_json(), {'removed': 2})
        self.assertEqual([r['flight_num'] for r in alerter.load_sent_alerts()], ['watch:11111111:a1'])


class LiveAircraftTest(unittest.TestCase):
    def test_parallel_requests_build_the_payload_once(self):
        webapp.live_cache.update({'fetched_at': 0, 'key': None, 'response': None})
        calls = []

        def slow_build(settings):
            calls.append(1)
            time.sleep(0.3)
            return {'aircraft': []}, 200

        responses = []
        with mock.patch.object(webapp, 'build_live_response', side_effect=slow_build), \
                mock.patch.object(webapp, 'is_logged_in', return_value=True):
            def request():
                responses.append(webapp.app.test_client().get('/api/aircraft').status_code)
            threads = [threading.Thread(target=request) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(responses, [200] * 5)


if __name__ == '__main__':
    unittest.main()
