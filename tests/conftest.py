"""
Shared fixtures: load the bin/ modules (some have hyphens in their names,
which prevents normal imports).
"""
import sys
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).parent.parent / "bin"
sys.path.insert(0, str(BIN))   # lets parse_pdf be importable by other modules


def _load(filename: str):
    """Load a Python file by path regardless of whether its name is a valid identifier."""
    path = BIN / filename
    module_name = filename.replace("-", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def ccb():
    return _load("calculate-cost-basis.py")


@pytest.fixture(scope="session")
def combine():
    return _load("combine.py")


@pytest.fixture(scope="session")
def parse_pdf_mod():
    import parse_pdf
    return parse_pdf


@pytest.fixture(scope="session")
def parse_releases():
    return _load("parse-stock-releases.py")
