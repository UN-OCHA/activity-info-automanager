"""Application tests use disposable preferences, never the user's saved state."""

import os
import tempfile

_preferences_directory = tempfile.TemporaryDirectory(prefix='activity-manager-tests-')
os.environ['LOCALAPPDATA'] = _preferences_directory.name
