"""Every test run gets its own ~/.inaproc and never touches the real Keychain (no test may reach real settings)."""
import os
import tempfile

os.environ["INAPROC_HOME"] = tempfile.mkdtemp(prefix="inaproc-tests-")
os.environ["INAPROC_NO_KEYCHAIN"] = "1"
os.environ["INAPROC_AUTO_UPDATE"] = "0"
