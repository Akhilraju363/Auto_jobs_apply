import os
import tempfile

# Tests must never write application diagnostics into the real output/ folder.
os.environ["APPLICATION_DEBUG_DIR"] = os.path.join(tempfile.gettempdir(), "auto_job_apply_test_debug")
