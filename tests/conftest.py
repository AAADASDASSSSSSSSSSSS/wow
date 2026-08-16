import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# This snapshot of the embedded RatsNestPro project omits its `ratsnestpro.parts`
# module, and agents/__init__.py eagerly imports the agent registry whose
# tools.py does a module-level `from ratsnestpro.parts import PartSelector`. That
# breaks importing the `agents`/`service` packages in every test. The routing
# tests here never exercise grounded part search, so — only when the real module
# is genuinely absent — register a minimal PartSelector shim reporting an
# unavailable catalog. Restore the real ratsnestpro.parts for parts-mode behavior.
_EMBEDDED_SRC = (
    Path(__file__).resolve().parents[1] / "src" / "RatsNestPro-main" / "RatsNestPro-main" / "src"
)
if _EMBEDDED_SRC.is_dir() and str(_EMBEDDED_SRC) not in sys.path:
    sys.path.insert(0, str(_EMBEDDED_SRC))
if (
    not (_EMBEDDED_SRC / "ratsnestpro" / "parts").is_dir()
    and not (_EMBEDDED_SRC / "ratsnestpro" / "parts.py").is_file()
):
    _parts_stub = types.ModuleType("ratsnestpro.parts")

    class _PartSelectorStub:
        def available(self) -> bool:
            return False

        def search(self, query: str, limit: int = 10) -> list:
            return []

    _parts_stub.PartSelector = _PartSelectorStub
    sys.modules.setdefault("ratsnestpro.parts", _parts_stub)


# Same shape of problem, one layer up. `.env` in this checkout enables EricAI, and
# `agents/agents.py` builds a model at IMPORT time, so a machine without the
# Ericsson-internal `ericai` package cannot even collect the agent tests — the
# failure is a RuntimeError during import, not a test result. The clients are
# thin subclasses of the openai ones (see core/llm.py), so standing in real
# openai clients with a dummy key makes the module importable while every test
# still monkeypatches `get_model` before any request is made. Guarded on the real
# package being genuinely absent, so a properly provisioned machine is unaffected.
try:  # pragma: no cover - environment probe
    import ericai  # noqa: F401
except ImportError:  # pragma: no cover - exercised only where ericai is absent
    try:
        import openai as _openai

        _ericai_stub = types.ModuleType("ericai")

        class _EricAIStub(_openai.OpenAI):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("api_key", "ericai-stub")
                super().__init__(*args, **kwargs)

        class _AsyncEricAIStub(_openai.AsyncOpenAI):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("api_key", "ericai-stub")
                super().__init__(*args, **kwargs)

        _ericai_stub.EricAI = _EricAIStub
        _ericai_stub.AsyncEricAI = _AsyncEricAIStub
        sys.modules.setdefault("ericai", _ericai_stub)
    except ImportError:
        pass


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker", action="store_true", default=False, help="run docker integration tests"
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: mark test as requiring docker containers")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-docker"):
        skip_docker = pytest.mark.skip(reason="need --run-docker option to run")
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(skip_docker)


@pytest.fixture
def mock_env():
    """Fixture to ensure environment is clean for each test."""
    with patch.dict(os.environ, {}, clear=True):
        yield
