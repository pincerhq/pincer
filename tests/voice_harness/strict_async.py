"""`pytestmark` for webhook tests whose engine mocks must not swallow coroutines.

An un-awaited coroutine warns from its finalizer, so pytest reports the
RuntimeWarning as a PytestUnraisableExceptionWarning: `error::RuntimeWarning`
alone lets the test pass. Both must be errors.
"""

from __future__ import annotations

import pytest

FAIL_ON_UNAWAITED_COROUTINE = [
    pytest.mark.filterwarnings("error::RuntimeWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning"),
]
