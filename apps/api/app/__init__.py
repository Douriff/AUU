"""AUU Market Terminal API — paper/mock only."""
import sys

# `python -m unittest discover -s tests` imports app before any test body.
# The tests package itself is not imported by that command, so isolation
# has to start here, before load_dotenv() and before any store path is used.
if "unittest" in sys.modules:
    from app.data_paths import install_test_isolation

    install_test_isolation()
