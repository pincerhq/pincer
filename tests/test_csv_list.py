"""Tests for the shared comma-separated-list helper."""

import pytest

from pincer.utils.csv_list import split_csv


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        (",, ,", []),
        ("a", ["a"]),
        (" a , b ", ["a", "b"]),
        ("a,", ["a"]),
        ("a,,b,", ["a", "b"]),
        ("hey pincer, @pincer", ["hey pincer", "@pincer"]),
    ],
)
def test_split_csv(raw, expected):
    assert split_csv(raw) == expected
