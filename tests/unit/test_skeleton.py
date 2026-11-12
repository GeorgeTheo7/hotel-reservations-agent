import importlib

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("module", ["anemos", "anemos.reservations"])
def test_package_imports(module: str) -> None:
    assert importlib.import_module(module).__doc__
