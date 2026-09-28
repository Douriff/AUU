"""AUU Market Terminal API — paper/mock only."""

# discover -s tests does not import the tests package, so the runner check
# has to happen before load_dotenv() and before any store path is used.
# Importing unittest or unittest.mock from a normal API process does not match.
from app.data_paths import install_test_isolation, test_process_requested

if test_process_requested():
    install_test_isolation()
