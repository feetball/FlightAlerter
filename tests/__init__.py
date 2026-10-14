# The modules under test pick their data folder at import time; point it somewhere disposable first.
import os
import tempfile

os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='flightalerter-tests-')
