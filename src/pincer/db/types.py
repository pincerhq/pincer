"""Column types that keep today's storage formats on both dialects.

The migrations predate the models, so these types adapt to the DDL rather than
the other way round: a model column must produce exactly the column type the
migration created, on SQLite and on Postgres.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import JSON, REAL, BigInteger, DateTime, Dialect, Integer, Text
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, JSONB, TIMESTAMP, UUID
from sqlalchemy.types import TypeDecorator, TypeEngine


class IsoText(TypeDecorator[str]):
    """An ISO-8601 timestamp that is a `str` in Python on every dialect.

    Mirrors the migrations' `{NOW_COL}` token: `TEXT` on SQLite, `TIMESTAMP` on
    Postgres. Callers keep passing and reading ISO strings. On SQLite the text
    is stored verbatim. Postgres' `TIMESTAMP` has no zone, so the value is
    stored as UTC and read back with `+00:00`, which is the form the app writes
    (`datetime.now(UTC).isoformat()`). A string without an offset is taken to
    already be UTC, as SQLite's `CURRENT_TIMESTAMP` is.

    An empty (or blank) string is the app's "no timestamp" sentinel; it binds
    as NULL on both dialects, as `Uuid7` does for its own sentinel, so the
    same write reads back the same everywhere.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(DateTime())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: str | None, dialect: Dialect) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if dialect.name != "postgresql":
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


def to_millis(value: datetime) -> datetime:
    """`value` in UTC, cut to whole milliseconds: the precision `UtcDateTime` stores."""
    if value.tzinfo is None:
        raise ValueError(f"a naive datetime has no zone to store: {value!r}")
    return value.astimezone(UTC).replace(microsecond=value.microsecond // 1000 * 1000)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS = timedelta(milliseconds=1)


def epoch_ms(value: datetime) -> int:
    """Epoch milliseconds of an aware datetime (cut, not rounded), as a UUIDv7 carries them."""
    return (to_millis(value) - _EPOCH) // _ONE_MS


def from_epoch_ms(ms: int) -> datetime:
    """The aware UTC datetime of epoch milliseconds `ms`."""
    return _EPOCH + ms * _ONE_MS


def from_epoch_seconds(seconds: float) -> datetime:
    """The aware UTC datetime of float epoch seconds, cut to the millisecond (how a `Real` timestamp
    becomes a `UtcDateTime` one, in the migration and anywhere else).

    Rounded to the microsecond before the cut, so float noise (`100.123` is `100.12299999…`) does not
    take a millisecond off; migration 0035 does the same in SQL on Postgres."""
    return from_epoch_ms(round(seconds * 1_000_000) // 1000)


class UtcDateTime(TypeDecorator[datetime]):
    """An instant to the millisecond: `TIMESTAMP(3) WITH TIME ZONE` on Postgres, fixed-width text on SQLite.

    Python only ever sees an aware UTC `datetime`, and a naive one is refused on bind: a zone guessed
    here would be wrong somewhere. Every value is cut to whole milliseconds before it is stored, on both
    dialects, so what is read back is exactly what the other dialect would return, and equal to the value
    as written (cut). SQLite stores `YYYY-MM-DDTHH:MM:SS.mmm+00:00`, always 29 characters in UTC, so
    ordering and comparing the text is ordering and comparing the instants.
    """

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(TIMESTAMP(timezone=True, precision=3))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"UtcDateTime takes a datetime, not {type(value).__name__}")
        cut = to_millis(value)
        return cut if dialect.name == "postgresql" else utc_text(cut)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return to_millis(parsed)


def utc_text(value: datetime) -> str:
    """How `UtcDateTime` stores an instant on SQLite: `YYYY-MM-DDTHH:MM:SS.mmm+00:00`."""
    return to_millis(value).isoformat(timespec="milliseconds")


#: A JSON document that comes back exactly as written: `json` on Postgres, which keeps the text (key
#: order included), JSON text on SQLite. For what is sent to an LLM again (a message's provider output
#: and tool calls): `jsonb` would sort the keys, so the request rebuilt after a reload would differ from
#: the one sent before it, and a prompt cache keyed on that prefix would miss.
JsonDocument = JSON(none_as_null=True)

#: A JSON document stored for querying, not replay: `jsonb` on Postgres, JSON text on SQLite.
JsonbDocument = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


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
