"""Framework integration for the embedded RatsNestPro project."""

import sys
from pathlib import Path

_EMBEDDED_SRC = (
    # Preserve substituted drives and junctions on Windows. Resolving this deep
    # checkout can expand the embedded package path beyond the legacy MAX_PATH
    # limit even though the same files are importable through the short path.
    Path(__file__).absolute().parents[2] / "RatsNestPro-main" / "RatsNestPro-main" / "src"
)
if _EMBEDDED_SRC.is_dir() and str(_EMBEDDED_SRC) not in sys.path:
    sys.path.insert(0, str(_EMBEDDED_SRC))
