"""Column types that keep today's storage formats on both dialects.

The migrations predate the models, so these types adapt to the DDL rather than
the other way round: a model column must produce exactly the column type the
migration created, on SQLite and on Postgres.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import REAL, BigInteger, DateTime, Dialect, Integer, Text
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, UUID
from sqlalchemy.types import TypeDecorator, TypeEngine


class IsoText(TypeDecorator[str]):
    """An ISO-8601 timestamp that is a `str` in Python on every dialect.

    Mirrors the migrations' `{NOW_COL}` token: `TEXT` on SQLite, `TIMESTAMP` on
    Postgres. Callers keep passing and reading ISO strings. On SQLite the text
    is stored verbatim. Postgres' `TIMESTAMP` has no zone, so the value is
    stored as UTC and read back with `+00:00`, which is the form the app writes
    (`datetime.now(UTC).isoformat()`). A string without an offset is taken to
    already be UTC, as SQLite's `CURRENT_TIMESTAMP` is.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: str | None, dialect: Dialect) -> Any:
        if value is None or dialect.name != "postgresql":
            return value
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC).isoformat()
        return value  # type: ignore[no-any-return]


class JSONText(TypeDecorator[Any]):
    """A JSON document stored as `TEXT`, as the migrations declare it.

    Not SQLAlchemy's `JSON`: on Postgres that would expect a `json` column, and
    these columns are `TEXT` there too. An empty string, which some columns
    default to, reads back as `None`.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return json.dumps(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:
        if not value:
            return None
        return json.loads(value)


class Uuid7(TypeDecorator[str]):
    """A row id Pincer minted: `uuid` on Postgres, `TEXT` on SQLite.

    Postgres is the primary target and gets the native 16-byte type, with its
    own equality and index. SQLite has no uuid type and stores the canonical
    36-character string — the documented degradation, for lightweight and test
    setups. Both compare in the same order, because the canonical form is
    fixed-width lowercase hex: sorting it as text is sorting the uuid by its
    bytes, which for a v7 is sorting by time.

    `str` in Python on both dialects, for the same reason `IsoText` keeps
    `str`: these ids are already strings everywhere they go — memory tags,
    path parameters, tool arguments, log lines, the dashboard's JSON. A
    `uuid.UUID` here would buy nothing on the wire (Postgres gets its native
    column either way) and cost a conversion at every one of those boundaries.

    The empty string is the app's "no id" sentinel on a few nullable columns;
    it binds as NULL, which is what the column actually means.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(UUID())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: str | None, dialect: Dialect) -> Any:
        if value is None or value == "":
            return None
        # Parse on both dialects, not just Postgres: a lenient SQLite branch
        # would let a malformed id through locally and fail only in CI.
        parsed = uuid.UUID(str(value))
        return parsed if dialect.name == "postgresql" else str(parsed)

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return str(value)


class BigInt(TypeDecorator[int]):
    """A 64-bit integer: `INTEGER` on SQLite, `BIGINT` on Postgres.

    Postgres' `INTEGER` is 4 bytes, which `time.monotonic_ns()` outruns 2.15
    seconds after boot. SQLite's `INTEGER` is already 64-bit and its DDL says
    `INTEGER`, so the type must not render as `BIGINT` there or every one of
    those columns would read as drift. Migration 0016 widened the Postgres
    columns.
    """

    impl = Integer
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(BigInteger())
        return dialect.type_descriptor(Integer())


class Real(TypeDecorator[float]):
    """An 8-byte float: `REAL` on SQLite, `DOUBLE PRECISION` on Postgres.

    Postgres' `REAL` is 4 bytes. At today's epoch (~1.7e9 s) that resolves to
    128 seconds, which put 23:59:59 into the next day, and money loses cents
    after about $100k. SQLite's `REAL` is already 8 bytes. Migration 0013
    widened the Postgres columns.
    """

    impl = REAL
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DOUBLE_PRECISION())
        return dialect.type_descriptor(REAL())
