import contextlib
import io
import os
import sys

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "inaproc", "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


def run_main(argv):
    import inaproc
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = inaproc.main(argv)
    return code, out.getvalue(), err.getvalue()
