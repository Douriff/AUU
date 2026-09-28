"""Shared unittest setup.

``python -m unittest discover -s tests`` does not import this package (the
start directory is the top-level directory). ``app/__init__.py`` installs the
same isolation as soon as ``unittest`` is on ``sys.modules``, which is what
that command does. Importing ``tests`` as a package installs it here too.
"""
from app.data_paths import install_test_isolation

install_test_isolation()
