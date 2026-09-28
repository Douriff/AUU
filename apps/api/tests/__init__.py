"""Shared unittest setup.

``python -m unittest discover -s tests`` does not import this package.
``app/__init__.py`` installs isolation when the process entry is unittest
or pytest, or when ``AUU_TEST=1``. Importing this package does the same
under that check.
"""
from app.data_paths import install_test_isolation, test_process_requested

if test_process_requested():
    install_test_isolation()
