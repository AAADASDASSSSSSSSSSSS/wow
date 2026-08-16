"""Session-wide guards that keep one test's environment out of the next one's.

Only the KiCad library paths are protected here, because those two variables
decide whether the symbol and footprint resolvers can see anything at all. When
they are wrong, nothing raises: every lookup returns ``None``, and the checks
that need real pin geometry, alternate functions or pad counts stop verifying
instead of reporting that they cannot. A leaked value is therefore invisible in
the test that caused it and shows up as an unrelated test failing later, only
when run as part of the full suite.
"""

from __future__ import annotations

import os

import pytest

# Variables whose value must not survive the test that set it.
_LIBRARY_PATH_VARS = ("KICAD_SYMBOL_DIR", "KICAD_FOOTPRINT_DIR")


@pytest.fixture(autouse=True)
def preserve_library_env():
    """Restore the KiCad library paths after every test.

    This deliberately does not take ``monkeypatch``. Because it does not, it is
    set up before the ``monkeypatch`` a test requests and torn down after that
    monkeypatch has finished undoing its own changes — so it is the last word on
    these two variables, whatever the test did to reach them.

    That ordering is the point. ``monkeypatch`` alone is not enough: it restores
    a variable to the value it held when monkeypatch was first asked to change
    it, so code that writes ``os.environ`` directly in between (which
    ``config.load_dotenv`` does by design) gets its write reinstated by undo
    rather than removed. This is a backstop, not a licence — a test that dirties
    the environment should still clean up after itself, so the reason it is
    dirty stays next to the code that made it so.
    """
    saved = {key: os.environ.get(key) for key in _LIBRARY_PATH_VARS}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
