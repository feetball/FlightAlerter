import os
import tempfile
import unittest
from unittest import mock

import paths
import settings as config


class WatchMatchValidationTest(unittest.TestCase):
    def test_normalizes_and_removes_duplicates(self):
        self.assertEqual(config._as_watch_match('n324fx, N268FX  n335fx;N324FX | N-324FX'), 'N324FX, N268FX, N335FX')

    def test_rejects_bad_input(self):
        for value in ('', '-', 'N1, ---', 'N1@', ['N1', None], 'N1 ' * 1000,
                      ', '.join('N%d' % i for i in range(51))):
            with self.assertRaises(ValueError, msg=repr(value)[:40]):
                config._as_watch_match(value)

    def test_entry_numbers_in_errors(self):
        with self.assertRaisesRegex(ValueError, 'entry #2'):
            config._as_watchlist([{'match': 'N1'}, {'match': 'BAD!'}])


class NumberValidationTest(unittest.TestCase):
    def test_rejects_non_finite_and_huge_values(self):
        for value in (float('inf'), float('nan'), 10 ** 400, '1e400', 'abc', None, True):
            with self.assertRaises(ValueError, msg=repr(value)[:40]):
                config._as_number(int, 15, 3600)(value)
            with self.assertRaises(ValueError, msg=repr(value)[:40]):
                config._as_number(float, 0.1, 500)(value)

    def test_whole_numbers(self):
        self.assertEqual(config._as_number(int, 15, 3600)(60), 60)
        self.assertEqual(config._as_number(int, 15, 3600)('60'), 60)
        with self.assertRaises(ValueError):
            config._as_number(int, 15, 3600)(60.5)


class LegacyImportTest(unittest.TestCase):
    def write(self, folder, name, text):
        filename = os.path.join(folder, name)
        with open(filename, 'w') as handle:
            handle.write(text)
        return filename

    def test_old_csv_files(self):
        with tempfile.TemporaryDirectory() as folder:
            military = self.write(folder, 'militaryFlights.csv',
                                  'distribution_list,military_distance,subject\na@example.com|b@example.com,25,Military nearby\n')
            watchlist = self.write(folder, 'monitoredFlights.csv',
                                   'aircraft_registration, distribution_list, max_distance, subject\n'
                                   'N251BP, a@example.com,3.5, Cessna is near\n\n053977, ,20,\nBAD!,,1,\n')
            with mock.patch.object(config, 'LEGACY_CONFIG_FILENAMES', [military]), \
                    mock.patch.object(config, 'LEGACY_WATCHLIST_FILENAME', watchlist):
                self.assertEqual(config._legacy_csv_settings(), {
                    'alert_radius_miles': 25.0, 'distribution_list': ['a@example.com', 'b@example.com'],
                    'subject': 'Military nearby'})
                entries = config._legacy_watchlist()
        self.assertEqual([(e['match'], e['recipients'], e['radius_miles'], e['subject']) for e in entries],
                         [('N251BP', ['a@example.com'], 3.5, 'Cessna is near'), ('053977', [], 20.0, '')])


class DataDirTest(unittest.TestCase):
    def test_moves_files_from_the_code_folder_into_data(self):
        with tempfile.TemporaryDirectory() as app_dir:
            data_dir = os.path.join(app_dir, 'data')
            for name in ('settings.json', 'secrets.json'):
                with open(os.path.join(app_dir, name), 'w') as handle:
                    handle.write('{}')
            environ = {key: value for key, value in os.environ.items() if key != 'DATA_DIR'}
            with mock.patch.dict(os.environ, environ, clear=True), \
                    mock.patch.object(paths, 'APP_DIR', app_dir), mock.patch.object(paths, 'DATA_DIR', data_dir):
                paths.prepare_data_dir()
            self.assertEqual(sorted(os.listdir(data_dir)), ['secrets.json', 'settings.json'])
            self.assertFalse(os.path.exists(os.path.join(app_dir, 'settings.json')))

    def test_leaves_an_explicit_data_dir_alone(self):
        with tempfile.TemporaryDirectory() as app_dir:
            with open(os.path.join(app_dir, 'settings.json'), 'w') as handle:
                handle.write('{}')
            with mock.patch.dict(os.environ, {'DATA_DIR': os.path.join(app_dir, 'elsewhere')}), \
                    mock.patch.object(paths, 'APP_DIR', app_dir), \
                    mock.patch.object(paths, 'DATA_DIR', os.path.join(app_dir, 'elsewhere')):
                paths.prepare_data_dir()
            self.assertTrue(os.path.exists(os.path.join(app_dir, 'settings.json')))


if __name__ == '__main__':
    unittest.main()
