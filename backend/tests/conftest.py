from collections.abc import Iterator

import pytest

from app.settings import get_settings


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Settings are cached per process; clear the cache so env changes in a test apply."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
