"""Where settings, secrets and alert history are kept: $DATA_DIR, or the data/ folder next to the code."""
import logging
import os


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('DATA_DIR') or os.path.join(APP_DIR, 'data')
# Older versions kept these in the code folder itself.
DATA_FILES = ('settings.json', 'secrets.json', 'sentAlerts.csv', 'militaryFlights.csv', 'monitoredFlights.csv')

logger = logging.getLogger('flightAlerter')


def app_version():
    """The release number from the VERSION file (the one place to change it when releasing)."""
    try:
        with open(os.path.join(APP_DIR, 'VERSION')) as handle:
            return handle.read().strip() or 'unknown'
    except OSError:
        return 'unknown'


def prepare_data_dir():
    """Creates the data folder and, when running from the code folder without DATA_DIR, moves files
    left there by older versions into it. (deploy.sh does the same for Docker installs.)"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.environ.get('DATA_DIR') or os.path.exists(os.path.join(DATA_DIR, 'settings.json')):
        return
    for name in DATA_FILES:
        old_path = os.path.join(APP_DIR, name)
        if os.path.isfile(old_path) and not os.path.exists(os.path.join(DATA_DIR, name)):
            os.replace(old_path, os.path.join(DATA_DIR, name))
            logger.info('Moved ' + name + ' into ' + DATA_DIR)
