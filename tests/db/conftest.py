"""tests/db exercises the real Alembic upgrade, not the template database."""

from pathlib import Path

import pytest

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.is_relative_to(_HERE):
            item.add_marker(pytest.mark.real_migrations)
