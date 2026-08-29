"""
Call threads (Sprint 13) — related calls grouped by *matter*.

A thread is one matter ("Termin Dr. Müller nächste Woche"), not a contact
timeline: a contact may have many threads, and a thread may span numbers
(``primary_number`` is an attribute, never the identity). Retries,
explicit follow-ups and matched inbound callbacks attach to the same thread,
which carries two derived artefacts:

- ``rolling_summary`` — regenerated after every attached call, ≤ 1200 chars,
  the user's one coherent story instead of N scattered transcripts;
- ``open_commitments`` — who owes what by when, aggregated from the Sprint 3
  call outcomes.

Both are what the agent gets back as ``THREAD CONTEXT`` on the next outbound
call of the matter ("wie am Dienstag besprochen…"), which is the whole point.

Two hard boundaries this module enforces rather than trusts:

1. **Ambiguity never guesses.** ``find_open_by_number`` attaches an inbound
   call only on EXACTLY one open match; zero or two or more → no attach.
2. **Attaching an inbound call is grouping, not disclosure.** CallerID is
   spoofable, so thread knowledge reaches an inbound conversation only under
   ``PINCER_THREAD_INBOUND_CONTEXT=ack``, and then only as the neutral
   ``THREAD_INBOUND_ACK`` line — never the subject, summary, dates or
   commitments (§4.3). The Sprint 12 deflection rules override everything.

Retention: threads are derived facts and outlive the transcript purge (the
Sprint 3 T3.3 precedent). ``call_thread_members`` keeps every attached call
listed as a stub (sid, date, outcome code) after its ``voice_calls`` row and
transcript are gone.
"""

from __future__ import annotations

import contextlib
import json
import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pincer.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# ── Vocabulary (stable strings: persisted, logged, API-visible) ──────

STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"
STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_RESOLVED, STATUS_CLOSED)

ORIGIN_USER_TASK = "user_task"
ORIGIN_INBOUND = "inbound"
ORIGINS: tuple[str, ...] = (ORIGIN_USER_TASK, ORIGIN_INBOUND)

KIND_ORIGIN = "origin"
KIND_RETRY = "retry"
KIND_FOLLOWUP = "followup"
KIND_INBOUND_MATCHED = "inbound_matched"
KIND_MANUAL = "manual"
ATTACH_KINDS: tuple[str, ...] = (KIND_ORIGIN, KIND_RETRY, KIND_FOLLOWUP, KIND_INBOUND_MATCHED, KIND_MANUAL)
# Attaching any of these to a `resolved` thread reopens it (§5).
REOPENING_KINDS: frozenset[str] = frozenset({KIND_RETRY, KIND_FOLLOWUP, KIND_INBOUND_MATCHED, KIND_MANUAL})

COMMITMENT_OPEN = "open"
COMMITMENT_DONE = "done"
COMMITMENT_EXPIRED = "expired"
COMMITMENT_WHO: frozenset[str] = frozenset({"callee", "agent", "user"})

# §5: closed is final. Anything not listed here is refused.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_OPEN: frozenset({STATUS_OPEN, STATUS_RESOLVED, STATUS_CLOSED}),
    STATUS_RESOLVED: frozenset({STATUS_RESOLVED, STATUS_OPEN, STATUS_CLOSED}),
    STATUS_CLOSED: frozenset({STATUS_CLOSED}),
}

# §2/§6.1/§7: bounded by construction, not by hope.
MAX_SUBJECT = 120
MAX_SUMMARY = 1200
MAX_CONTEXT_BLOCK = 1600
MAX_COMMITMENTS = 20
MAX_CONTEXT_COMMITMENTS = 5
# How many recent threads the expired-commitment filter scans (see list_threads).
EXPIRED_SCAN_LIMIT = 500
# Terminal outcome codes that count as task success for the §5 auto-resolve.
TERMINAL_SUCCESS_OUTCOMES: frozenset[str] = frozenset({"completed"})


class ThreadError(RuntimeError):
    """A thread operation the caller asked for is not allowed (§4.2/§5).

    Carries a user-facing message: tool handlers turn it into an
    ``Error: …`` string, API routes into a 4xx detail.
    """


# ── Model ────────────────────────────────────────────────────────────


@dataclass
class Thread:
    """One matter. `open_commitments` is the parsed §6.2 array."""

    thread_id: str
    subject: str
    status: str = STATUS_OPEN
    origin: str = ORIGIN_USER_TASK
    primary_number: str = ""
    contact_name: str = ""
    language: str = ""
    rolling_summary: str = ""
    open_commitments: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    resolved_at: str | None = None
    closed_at: str | None = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> Thread:
        return cls(
            thread_id=str(row["thread_id"]),
            subject=str(row["subject"] or ""),
            status=str(row["status"] or STATUS_OPEN),
            origin=str(row["origin"] or ORIGIN_USER_TASK),
            primary_number=str(row["primary_number"] or ""),
            contact_name=str(row["contact_name"] or ""),
            language=str(row["language"] or ""),
            rolling_summary=str(row["rolling_summary"] or ""),
            open_commitments=parse_commitments(row["open_commitments"]),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            resolved_at=row["resolved_at"],
            closed_at=row["closed_at"],
        )

    @property
    def open_count(self) -> int:
        return sum(1 for c in self.open_commitments if c.get("status") == COMMITMENT_OPEN)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "subject": self.subject,
            "status": self.status,
            "origin": self.origin,
            "primary_number": self.primary_number,
            "contact_name": self.contact_name,
            "language": self.language,
            "rolling_summary": self.rolling_summary,
            "open_commitments": self.open_commitments,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "resolved_at": self.resolved_at,
            "closed_at": self.closed_at,
        }


@dataclass
class ThreadCall:
    """A call in a thread. `purged` marks a stub whose voice_calls row and
    transcript are gone (retention) but whose membership survives (§5)."""

    call_sid: str
    thread_id: str
    attach_kind: str = ""
    attached_at: str = ""
    started_at: str = ""
    ended_at: str | None = None
    direction: str = ""
    outcome_code: str = ""
    task_result: str = ""
    failure_code: str = ""
    purged: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "thread_id": self.thread_id,
            "thread_attach_kind": self.attach_kind,
            "attached_at": self.attached_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "direction": self.direction,
            "outcome": self.outcome_code,
            "task_result": self.task_result,
            "failure_code": self.failure_code,
            "purged": self.purged,
        }


@dataclass
class ThreadUpdate:
    """What `update_after_call` did — the post-call report renders from this."""

    thread: Thread
    call_index: int = 1
    call_total: int = 1
    commitments_changed: bool = False
    resolved_now: bool = False
    summary_failed: bool = False


def new_thread_id() -> str:
    return "thr_" + secrets.token_hex(6)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def parse_commitments(raw: Any) -> list[dict[str, Any]]:
    """Parse the stored §6.2 JSON array defensively — a malformed column must
    degrade to "no commitments", never blow up a call report."""
    if isinstance(raw, list):
        items = raw
    else:
        try:
            items = json.loads(str(raw or "[]"))
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(items, list):
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        who = str(item.get("who", "")).strip().lower()
        what = str(item.get("what", "")).strip()
        if who not in COMMITMENT_WHO or not what:
            continue
        status = str(item.get("status", COMMITMENT_OPEN)).strip().lower()
        out.append(
            {
                "who": who,
                "what": what[:300],
                "due": str(item["due"]) if item.get("due") else None,
                "status": status
                if status in (COMMITMENT_OPEN, COMMITMENT_DONE, COMMITMENT_EXPIRED)
                else COMMITMENT_OPEN,
                "source_call_sid": str(item.get("source_call_sid", "") or ""),
            }
        )
    return out[:MAX_COMMITMENTS]


def truncate(text: str, limit: int) -> str:
    """Cut at a word boundary and mark the cut — a summary that silently loses
    its tail reads as a complete (wrong) summary."""
    body = str(text or "").strip()
    if len(body) <= limit:
        return body
    cut = body[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip() + "…"


# ── The LLM merge pass (§6.1 + §6.2) ─────────────────────────────────

MERGE_SYSTEM_PROMPT = """\
You maintain the rolling summary of a THREAD of related phone calls about ONE matter.

You are given the summary so far, the thread's open commitments, and the structured \
outcome of the CALL THAT JUST HAPPENED. Produce the updated summary.

STRICT RULES:
1. Use ONLY the previous summary and the new call's outcome. Never invent, infer, or \
speculate about anything neither of them states.
2. Chronological and factual. Keep what still matters from the previous summary; add \
what the new call changed. Drop nothing that is still open.
3. Write in the thread language given below, at most 1200 characters total.
4. The LAST line MUST be a one-line current state, prefixed exactly with "Stand: " for \
German or "Status: " for any other language (e.g. "Stand: wartet auf Rückruf der Praxis bis Fr.").
5. "satisfied_commitments" lists the INDEX of every previously open commitment that the \
NEW call proves was fulfilled. Evidence in the new call is required — an unmentioned \
commitment stays open. When in doubt, leave it out.

Respond with ONLY a JSON object, no markdown fences:
{"summary": "...", "satisfied_commitments": [0, 2]}\
"""


def _fallback_state_line(task_result: str, language: str) -> str:
    result = str(task_result or "").strip()
    if language.startswith("de"):
        return f"Stand: {result}" if result else "Stand: Ergebnis des letzten Anrufs liegt nicht strukturiert vor."
    if language.startswith("uk"):
        return f"Статус: {result}" if result else "Статус: результат останнього дзвінка не структуровано."
    return f"Status: {result}" if result else "Status: the last call's outcome is not available in structured form."


async def merge_summary(
    llm: BaseLLMProvider | None,
    current_summary: str,
    outcome: Any,
    commitments: list[dict[str, Any]],
    language: str,
) -> tuple[str, list[int], bool]:
    """(new summary, indices the LLM says are satisfied, failed).

    Never loses the old summary: on ANY failure the previous text is kept and
    a one-line fallback state derived from ``task_result`` is appended (§6.1).
    """
    previous = str(current_summary or "").strip()
    task_result = str(getattr(outcome, "task_result", "") or "")

    def _fallback() -> tuple[str, list[int], bool]:
        # The state line is the point of the fallback, so it gets its space
        # first: truncating the pair as one string would cut the very line the
        # user needs off the end of a summary that is already at the cap.
        line = truncate(_fallback_state_line(task_result, language), MAX_SUMMARY)
        if not previous:
            return line, [], True
        head = truncate(previous, max(0, MAX_SUMMARY - len(line) - 1))
        return f"{head}\n{line}", [], True

    if llm is None or outcome is None:
        return _fallback()

    from pincer.llm.base import LLMMessage, MessageRole

    numbered = "\n".join(
        f"[{i}] {c['who']}: {c['what']}" + (f" (due {c['due']})" if c.get("due") else "")
        for i, c in enumerate(commitments)
        if c.get("status") == COMMITMENT_OPEN
    )
    outcome_json = outcome.to_json() if hasattr(outcome, "to_json") else json.dumps(outcome, ensure_ascii=False)
    user_content = (
        f"thread language: {language or 'en'}\n\n"
        f"SUMMARY SO FAR:\n{previous or '(this is the first call of the thread)'}\n\n"
        f"OPEN COMMITMENTS (index: who: what):\n{numbered or '(none)'}\n\n"
        f"OUTCOME OF THE CALL THAT JUST HAPPENED:\n{outcome_json[:4000]}"
    )
    try:
        response = await llm.complete(
            messages=[LLMMessage(role=MessageRole.USER, content=user_content)],
            system=MERGE_SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=700,
        )
        data = _parse_json_object(response.content or "")
        summary = str((data or {}).get("summary", "")).strip()
        if not summary:
            raise ValueError("empty summary")
    except Exception:
        logger.warning("Thread summary merge failed — keeping the previous summary", exc_info=True)
        return _fallback()

    satisfied: list[int] = []
    for value in (data or {}).get("satisfied_commitments") or []:
        with contextlib.suppress(TypeError, ValueError):
            satisfied.append(int(value))
    return truncate(summary, MAX_SUMMARY), satisfied, False


def _parse_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _evidence_text(outcome: Any) -> str:
    """Everything the new call actually established — the grounding corpus for
    "this commitment is done" (§6.2: evidence in the new call required)."""
    if outcome is None:
        return ""
    parts = [str(getattr(outcome, "task_result", "") or "")]
    parts.extend(str(f) for f in (getattr(outcome, "key_facts", None) or []))
    for commitment in getattr(outcome, "commitments", None) or []:
        if isinstance(commitment, dict):
            parts.append(str(commitment.get("what", "")))
    return "\n".join(p for p in parts if p)


def merge_commitments(
    existing: list[dict[str, Any]],
    outcome: Any,
    satisfied_indices: list[int],
    call_sid: str,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """(merged §6.2 array, changed?).

    Deterministic on purpose — the LLM only *proposes* which commitments the
    new call satisfied; whether that proposal is accepted is decided here by
    the same mechanical grounding check the Sprint 3 extractor uses, and the
    expiry sweep is pure arithmetic. `due` in the past and not done becomes
    `expired` and is only FLAGGED — acting on it is out of scope (§14).
    """
    from pincer.voice.outcome import _is_grounded, _numbers, _significant_words

    moment = now or datetime.now(UTC)
    evidence_text = _evidence_text(outcome)
    evidence_words = _significant_words(evidence_text)
    evidence_numbers = _numbers(evidence_text)
    merged = [dict(c) for c in existing]
    changed = False

    # Indices address the full list — `merge_summary` numbers them that way.
    for index in satisfied_indices:
        if not 0 <= index < len(merged):
            continue
        target = merged[index]
        if target.get("status") != COMMITMENT_OPEN:
            continue
        if not _is_grounded(str(target.get("what", "")), evidence_words, evidence_numbers):
            logger.info(
                "Thread commitment %r claimed done without evidence in the new call — kept open",
                str(target.get("what", ""))[:60],
            )
            continue
        target["status"] = COMMITMENT_DONE
        changed = True

    known = {str(c.get("what", "")).strip().lower() for c in merged}
    for item in getattr(outcome, "commitments", None) or []:
        if not isinstance(item, dict):
            continue
        what = str(item.get("what", "")).strip()
        who = str(item.get("who", "")).strip().lower()
        if not what or who not in COMMITMENT_WHO or what.lower() in known:
            continue
        merged.append(
            {
                "who": who,
                "what": what[:300],
                "due": str(item["when"]) if item.get("when") else None,
                "status": COMMITMENT_OPEN,
                "source_call_sid": call_sid,
            }
        )
        known.add(what.lower())
        changed = True

    for commitment in merged:
        if commitment.get("status") != COMMITMENT_OPEN or not commitment.get("due"):
            continue
        due = _parse_dt(str(commitment["due"]))
        if due is not None and due < moment:
            commitment["status"] = COMMITMENT_EXPIRED
            changed = True

    return merged[:MAX_COMMITMENTS], changed


def _parse_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ── Manager ──────────────────────────────────────────────────────────


class ThreadManager:
    """Owns the thread tables. One instance per process (see the singleton
    helpers below) so the lifecycle rules have exactly one enforcer."""

    def __init__(self, db_path: str, settings: Any = None, llm: BaseLLMProvider | None = None) -> None:
        self._db_path = str(db_path or "")
        self._settings = settings
        self._llm = llm
        self._memory: Any = None
        # The schema is ensured once per manager, not once per query: a thread
        # read is a handful of SELECTs, and re-running the full DDL script plus
        # every ALTER in front of each of them costs more than the query.
        self._schema_ready = False

    def set_llm(self, llm: BaseLLMProvider | None) -> None:
        self._llm = llm

    def set_memory(self, memory: Any) -> None:
        """Memory backend for the §8 thread notes (tag ``thread:{id}``)."""
        self._memory = memory

    @property
    def db_path(self) -> str:
        return self._db_path

    @contextlib.asynccontextmanager
    async def _db(self) -> Any:
        from pincer.voice.retention import ensure_voice_tables

        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            if not self._schema_ready:
                await ensure_voice_tables(db)
                self._schema_ready = True
            yield db

    # ── Create / read ─────────────────────────────────────

    async def create(
        self,
        subject: str,
        origin: str = ORIGIN_USER_TASK,
        primary_number: str = "",
        contact_name: str = "",
        language: str = "",
    ) -> Thread:
        """Open a new thread. Subject is required (a nameless matter is not a
        matter) and capped at §2's 120 chars."""
        title = truncate(str(subject or "").strip(), MAX_SUBJECT)
        if not title:
            raise ThreadError("A thread needs a subject.")
        if origin not in ORIGINS:
            raise ThreadError(f"Unknown thread origin: {origin!r} (expected one of {', '.join(ORIGINS)})")
        stamp = _now()
        thread = Thread(
            thread_id=new_thread_id(),
            subject=title,
            status=STATUS_OPEN,
            origin=origin,
            primary_number=str(primary_number or ""),
            contact_name=str(contact_name or "")[:200],
            language=str(language or "")[:8],
            created_at=stamp,
            updated_at=stamp,
        )
        async with self._db() as db:
            await db.execute(
                "INSERT INTO call_threads (thread_id, subject, status, origin, primary_number, contact_name, "
                "language, rolling_summary, open_commitments, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, '', '[]', ?, ?)",
                (
                    thread.thread_id,
                    thread.subject,
                    thread.status,
                    thread.origin,
                    thread.primary_number,
                    thread.contact_name,
                    thread.language,
                    thread.created_at,
                    thread.updated_at,
                ),
            )
            await db.commit()
        logger.info("Thread created %s: %r (origin=%s)", thread.thread_id, thread.subject, thread.origin)
        return thread

    async def get(self, thread_id: str) -> Thread | None:
        if not thread_id:
            return None
        async with self._db() as db:
            cursor = await db.execute("SELECT * FROM call_threads WHERE thread_id = ?", (thread_id,))
            row = await cursor.fetchone()
        return Thread.from_row(row) if row is not None else None

    async def require(self, thread_id: str) -> Thread:
        thread = await self.get(thread_id)
        if thread is None:
            raise ThreadError(f"Thread {thread_id} does not exist.")
        return thread

    async def list_threads(
        self,
        status: str | Sequence[str] = "",
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        has_expired_commitments: bool = False,
    ) -> list[Thread]:
        """Threads by most recent activity.

        `status` accepts one status or several ("open + resolved" is a single
        view in the dashboard, not two round trips); empty means every status.
        """
        statuses = [status] if isinstance(status, str) else list(status)
        statuses = [s for s in (str(v).strip().lower() for v in statuses) if s]

        sql = "SELECT * FROM call_threads"
        where: list[str] = []
        args: list[Any] = []
        if statuses:
            where.append(f"status IN ({', '.join('?' for _ in statuses)})")
            args += statuses
        if query:
            where.append("(subject LIKE ? OR contact_name LIKE ? OR primary_number LIKE ?)")
            args += [f"%{query}%"] * 3
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC"

        if not has_expired_commitments:
            sql += " LIMIT ? OFFSET ?"
            args += [max(1, limit), max(0, offset)]
            async with self._db() as db:
                rows = await db.execute_fetchall(sql, args)
            return [Thread.from_row(r) for r in rows]

        # Commitments live in a JSON column, so "has an expired one" is decided
        # in Python — which means paging has to happen after the filter, not in
        # SQL. Bounded by SCAN_LIMIT rather than unbounded, and a truncated
        # scan says so in the log instead of quietly under-reporting.
        sql += " LIMIT ?"
        args.append(EXPIRED_SCAN_LIMIT)
        async with self._db() as db:
            rows = await db.execute_fetchall(sql, args)
        if len(rows) >= EXPIRED_SCAN_LIMIT:
            logger.warning(
                "Expired-commitment filter scanned the %d most recent threads; older ones were not considered",
                EXPIRED_SCAN_LIMIT,
            )
        matching = [
            thread
            for thread in (Thread.from_row(r) for r in rows)
            if any(c.get("status") == COMMITMENT_EXPIRED for c in thread.open_commitments)
        ]
        start = max(0, offset)
        return matching[start : start + max(1, limit)]

    async def thread_for_call(self, call_sid: str) -> str:
        """The thread a call belongs to ('' when threadless)."""
        if not call_sid:
            return ""
        async with self._db() as db:
            cursor = await db.execute("SELECT thread_id FROM call_thread_members WHERE call_sid = ?", (call_sid,))
            row = await cursor.fetchone()
        return str(row["thread_id"]) if row is not None else ""

    async def calls(self, thread_id: str) -> list[ThreadCall]:
        """Ordered calls of a thread. A member row whose voice_calls row is
        gone is returned as a purged stub, not dropped (§5)."""
        async with self._db() as db:
            rows = await db.execute_fetchall(
                "SELECT m.call_sid, m.thread_id, m.attach_kind, m.attached_at, m.call_started_at, "
                "       m.direction, m.outcome_code, m.task_result, "
                "       c.started_at AS live_started_at, c.ended_at AS live_ended_at, "
                "       c.direction AS live_direction, c.failure_code AS live_failure_code, "
                "       c.call_sid AS live_sid "
                "FROM call_thread_members m LEFT JOIN voice_calls c ON c.call_sid = m.call_sid "
                "WHERE m.thread_id = ? "
                "ORDER BY COALESCE(NULLIF(m.call_started_at, ''), m.attached_at) ASC, m.rowid ASC",
                (thread_id,),
            )
        out: list[ThreadCall] = []
        for row in rows:
            live = row["live_sid"] is not None
            out.append(
                ThreadCall(
                    call_sid=str(row["call_sid"]),
                    thread_id=str(row["thread_id"]),
                    attach_kind=str(row["attach_kind"] or ""),
                    attached_at=str(row["attached_at"] or ""),
                    started_at=str(row["live_started_at"] or row["call_started_at"] or ""),
                    ended_at=row["live_ended_at"] if live else None,
                    direction=str((row["live_direction"] if live else row["direction"]) or ""),
                    outcome_code=str(row["outcome_code"] or ""),
                    task_result=str(row["task_result"] or ""),
                    failure_code=str(row["live_failure_code"] or "") if live else "",
                    purged=not live,
                )
            )
        return out

    # ── Attach (§4) ───────────────────────────────────────

    async def attach(self, call_sid: str, thread_id: str, kind: str = KIND_ORIGIN) -> None:
        """Put a call in a thread.

        Enforces the §2 single-thread rule (a call belongs to at most one
        thread; only ``manual`` reassigns), refuses closed threads (§4.2), and
        reopens a resolved thread for every kind that represents new activity.
        """
        if not call_sid:
            raise ThreadError("A call SID is required to attach a call to a thread.")
        if kind not in ATTACH_KINDS:
            raise ThreadError(f"Unknown attach kind: {kind!r} (expected one of {', '.join(ATTACH_KINDS)})")
        thread = await self.require(thread_id)
        if thread.status == STATUS_CLOSED:
            raise ThreadError(
                f"Thread {thread_id} is closed and cannot take further calls. "
                "Start a new thread and reference this one in its subject."
            )

        current = await self.thread_for_call(call_sid)
        if current and current != thread_id and kind != KIND_MANUAL:
            raise ThreadError(
                f"Call {call_sid} already belongs to thread {current}. "
                "Only a manual reassignment can move a call between threads."
            )

        stamp = _now()
        async with self._db() as db:
            started_at, direction = "", ""
            cursor = await db.execute("SELECT started_at, direction FROM voice_calls WHERE call_sid = ?", (call_sid,))
            row = await cursor.fetchone()
            if row is not None:
                started_at, direction = str(row["started_at"] or ""), str(row["direction"] or "")
            await db.execute(
                "INSERT INTO call_thread_members "
                "(call_sid, thread_id, attach_kind, attached_at, call_started_at, direction) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(call_sid) DO UPDATE SET thread_id = excluded.thread_id, "
                "attach_kind = excluded.attach_kind, attached_at = excluded.attached_at",
                (call_sid, thread_id, kind, stamp, started_at, direction),
            )
            await db.execute(
                "UPDATE voice_calls SET thread_id = ?, thread_attach_kind = ? WHERE call_sid = ?",
                (thread_id, kind, call_sid),
            )
            await db.execute("UPDATE call_threads SET updated_at = ? WHERE thread_id = ?", (stamp, thread_id))
            await db.commit()

        if thread.status == STATUS_RESOLVED and kind in REOPENING_KINDS:
            await self.reopen(thread_id, reason=f"new_call_{kind}")
        logger.info("Call %s attached to thread %s (%s)", call_sid, thread_id, kind)

    async def detach(self, call_sid: str) -> None:
        """Remove a call from whatever thread it is in (manual reassign path)."""
        async with self._db() as db:
            await db.execute("DELETE FROM call_thread_members WHERE call_sid = ?", (call_sid,))
            await db.execute(
                "UPDATE voice_calls SET thread_id = '', thread_attach_kind = '' WHERE call_sid = ?", (call_sid,)
            )
            await db.commit()

    # ── Lifecycle (§5) ────────────────────────────────────

    async def set_status(self, thread_id: str, status: str, reason: str = "") -> Thread:
        """The single gate for every status change; invalid transitions raise."""
        if status not in STATUSES:
            raise ThreadError(f"Unknown thread status: {status!r} (expected one of {', '.join(STATUSES)})")
        thread = await self.require(thread_id)
        if status not in VALID_TRANSITIONS[thread.status]:
            raise ThreadError(
                f"Thread {thread_id} cannot go from {thread.status} to {status}"
                + (" — closed is final." if thread.status == STATUS_CLOSED else ".")
            )
        if status == thread.status:
            return thread

        stamp = _now()
        sets = ["status = ?", "updated_at = ?"]
        args: list[Any] = [status, stamp]
        if status == STATUS_RESOLVED:
            sets.append("resolved_at = ?")
            args.append(stamp)
        elif status == STATUS_CLOSED:
            sets.append("closed_at = ?")
            args.append(stamp)
        elif status == STATUS_OPEN:
            sets.append("resolved_at = NULL")
        async with self._db() as db:
            await db.execute(
                f"UPDATE call_threads SET {', '.join(sets)} WHERE thread_id = ?",  # noqa: S608 - fixed fragments
                (*args, thread_id),
            )
            await db.commit()

        await self._audit_lifecycle(thread, status, reason)
        logger.info("Thread %s: %s -> %s (%s)", thread_id, thread.status, status, reason or "no reason given")
        return await self.require(thread_id)

    async def resolve(self, thread_id: str, reason: str = "") -> None:
        await self.set_status(thread_id, STATUS_RESOLVED, reason or "resolved")

    async def reopen(self, thread_id: str, reason: str = "") -> None:
        await self.set_status(thread_id, STATUS_OPEN, reason or "reopened")

    async def close(self, thread_id: str, reason: str = "") -> None:
        await self.set_status(thread_id, STATUS_CLOSED, reason or "closed")

    async def _audit_lifecycle(self, thread: Thread, status: str, reason: str) -> None:
        """Every transition is audit-logged (§5). Best effort by construction."""
        try:
            from pincer.security.audit import AuditAction, AuditEntry, get_audit_logger

            audit = await get_audit_logger()
            await audit.log(
                AuditEntry(
                    user_id="system",
                    action=AuditAction.VOICE_THREAD_LIFECYCLE,
                    input_summary=f"{thread.thread_id}: {thread.status} -> {status}",
                    output_summary=reason,
                    metadata={
                        "thread_id": thread.thread_id,
                        "from": thread.status,
                        "to": status,
                        "reason": reason,
                    },
                )
            )
        except Exception:  # pragma: no cover — auditing must not break a transition
            logger.debug("Thread lifecycle audit failed [%s]", thread.thread_id, exc_info=True)

    async def update_subject(self, thread_id: str, subject: str) -> Thread:
        title = truncate(str(subject or "").strip(), MAX_SUBJECT)
        if not title:
            raise ThreadError("A thread needs a subject.")
        await self.require(thread_id)
        async with self._db() as db:
            await db.execute(
                "UPDATE call_threads SET subject = ?, updated_at = ? WHERE thread_id = ?",
                (title, _now(), thread_id),
            )
            await db.commit()
        return await self.require(thread_id)

    async def merge(self, source_thread_id: str, target_thread_id: str) -> Thread:
        """§4.4: source's calls re-attach to target as ``manual``; source is
        closed with a ``merged_into`` note in the audit log."""
        if source_thread_id == target_thread_id:
            raise ThreadError("A thread cannot be merged into itself.")
        source = await self.require(source_thread_id)
        target = await self.require(target_thread_id)
        if target.status == STATUS_CLOSED:
            raise ThreadError(f"Thread {target_thread_id} is closed and cannot absorb another thread.")

        stamp = _now()
        async with self._db() as db:
            await db.execute(
                "UPDATE call_thread_members SET thread_id = ?, attach_kind = ?, attached_at = ? WHERE thread_id = ?",
                (target_thread_id, KIND_MANUAL, stamp, source_thread_id),
            )
            await db.execute(
                "UPDATE voice_calls SET thread_id = ?, thread_attach_kind = ? WHERE thread_id = ?",
                (target_thread_id, KIND_MANUAL, source_thread_id),
            )
            await db.execute("UPDATE call_threads SET updated_at = ? WHERE thread_id = ?", (stamp, target_thread_id))
            await db.commit()

        if source.status != STATUS_CLOSED:
            await self.close(source_thread_id, reason=f"merged_into:{target_thread_id}")
        if target.status == STATUS_RESOLVED:
            await self.reopen(target_thread_id, reason=f"merged_from:{source_thread_id}")
        logger.info("Thread %s merged into %s", source_thread_id, target_thread_id)
        return await self.require(target_thread_id)

    async def autoclose(self, days: int, now: datetime | None = None) -> list[str]:
        """§5 auto-close: open/resolved threads with no activity for `days`.
        `days <= 0` means never. Returns the thread ids that were closed."""
        if days <= 0:
            return []
        cutoff = ((now or datetime.now(UTC)) - timedelta(days=days)).isoformat()
        async with self._db() as db:
            rows = await db.execute_fetchall(
                "SELECT thread_id FROM call_threads WHERE status != ? AND updated_at < ?",
                (STATUS_CLOSED, cutoff),
            )
        closed: list[str] = []
        for row in rows:
            thread_id = str(row["thread_id"])
            try:
                await self.close(thread_id, reason=f"autoclose_after_{days}d")
                closed.append(thread_id)
            except ThreadError:  # pragma: no cover — raced with a manual close
                logger.debug("Auto-close skipped for %s", thread_id)
        if closed:
            logger.info("Auto-closed %d inactive thread(s) (>%dd)", len(closed), days)
        return closed

    # ── Inbound matching (§4.3) ───────────────────────────

    async def find_open_by_number(self, number: str, within_days: int = 7) -> Thread | None:
        """The single open thread for this number inside the window, or None.

        Zero matches and two-or-more matches both return None: ambiguity never
        guesses, and a wrong grouping is worse than no grouping.
        """
        cleaned = str(number or "").strip()
        if not cleaned or within_days <= 0:
            return None
        cutoff = (datetime.now(UTC) - timedelta(days=within_days)).isoformat()
        async with self._db() as db:
            rows = await db.execute_fetchall(
                "SELECT * FROM call_threads WHERE primary_number = ? AND status = ? AND updated_at >= ? "
                "ORDER BY updated_at DESC LIMIT 3",
                (cleaned, STATUS_OPEN, cutoff),
            )
        if len(rows) != 1:
            if len(rows) > 1:
                logger.info("Inbound thread match ambiguous (%d open threads) — no attach", len(rows))
            return None
        return Thread.from_row(rows[0])

    # ── The heart (§6) ────────────────────────────────────

    async def update_after_call(
        self,
        call_sid: str,
        outcome: Any = None,
        llm: BaseLLMProvider | None = None,
        language: str = "",
    ) -> ThreadUpdate | None:
        """Fold a finished call into its thread: rolling summary, commitments,
        the derived lifecycle step, and the §8 memory note.

        Runs from the post-call pipeline AFTER Sprint 3 outcome extraction.
        Returns None when the call is threadless. Never raises: a thread
        bookkeeping failure must not cost the user their call report.
        """
        thread_id = await self.thread_for_call(call_sid)
        if not thread_id:
            return None
        thread = await self.get(thread_id)
        if thread is None:  # pragma: no cover — member row without its thread
            logger.warning("Call %s references missing thread %s", call_sid, thread_id)
            return None

        lang = str(language or thread.language or getattr(outcome, "language", "") or "en")[:2]
        summary, satisfied, failed = await merge_summary(
            llm or self._llm, thread.rolling_summary, outcome, thread.open_commitments, lang
        )
        commitments, changed = merge_commitments(thread.open_commitments, outcome, satisfied, call_sid)

        outcome_code = str(getattr(outcome, "outcome", "") or "")
        task_result = str(getattr(outcome, "task_result", "") or "")
        stamp = _now()
        async with self._db() as db:
            await db.execute(
                "UPDATE call_threads SET rolling_summary = ?, open_commitments = ?, language = ?, updated_at = ? "
                "WHERE thread_id = ?",
                (summary, json.dumps(commitments, ensure_ascii=False), lang, stamp, thread_id),
            )
            await db.execute(
                "UPDATE call_thread_members SET outcome_code = ?, task_result = ? WHERE call_sid = ?",
                (outcome_code, truncate(task_result, 500), call_sid),
            )
            await db.commit()

        thread.rolling_summary = summary
        thread.open_commitments = commitments
        thread.language = lang
        thread.updated_at = stamp

        # §5: terminal success AND nothing left open resolves the matter.
        resolved_now = False
        open_left = sum(1 for c in commitments if c.get("status") == COMMITMENT_OPEN)
        if thread.status == STATUS_OPEN and outcome_code in TERMINAL_SUCCESS_OUTCOMES and open_left == 0:
            with contextlib.suppress(ThreadError):
                await self.resolve(thread_id, reason=f"task_result_terminal:{call_sid}")
                thread.status = STATUS_RESOLVED
                resolved_now = True

        await self._write_thread_note(thread)

        members = await self.calls(thread_id)
        index = next((i for i, c in enumerate(members, start=1) if c.call_sid == call_sid), len(members))
        return ThreadUpdate(
            thread=thread,
            call_index=index,
            call_total=len(members),
            commitments_changed=changed,
            resolved_now=resolved_now,
            summary_failed=failed,
        )

    async def _write_thread_note(self, thread: Thread) -> None:
        """§8: the thread's state lives in memory under ``thread:{id}`` so
        "Was ist der Stand bei Dr. Müller?" answers from any chat channel.
        The previous note for the thread is deleted first — one note per
        thread, no memory spam."""
        memory = self._memory
        if memory is None or not thread.rolling_summary:
            return
        tag = f"thread:{thread.thread_id}"
        user_id = str(getattr(self._settings, "default_user_id", "") or "") or "voice"
        try:
            previous = await memory.list_memories(user_id=None, limit=20, tags=[tag])
            for note in previous:
                with contextlib.suppress(Exception):
                    await memory.delete_memory(note.id)
            open_items = "; ".join(
                f"{c['who']}: {c['what']}" for c in thread.open_commitments if c.get("status") == COMMITMENT_OPEN
            )
            content = (
                f"[Thread {thread.subject}] {thread.rolling_summary}"
                + (f"\nOpen: {open_items}" if open_items else "")
                + f"\n(status: {thread.status}, thread_id: {thread.thread_id})"
            )
            await memory.store_memory(
                user_id=user_id,
                content=content,
                category="voice_call",
                extra_tags=["source:voice_call", "thread", tag],
            )
        except Exception:
            logger.exception("Thread memory note failed [%s]", thread.thread_id)

    # ── Context injection (§7) ────────────────────────────

    async def build_context(self, thread_id: str, direction: str, settings: Any = None) -> str:
        """The THREAD CONTEXT block for the live call prompt.

        Outbound: summary + open commitments + last call, with the "reference
        it naturally, don't recite it, their current statement wins" rules.
        Inbound: §4.3 only — nothing at all in ``off`` mode, and at most the
        neutral acknowledgment line in ``ack`` mode. The first call of a
        thread gets no block at all (no empty scaffolding).
        """
        conf = settings if settings is not None else self._settings
        if str(direction or "").lower().endswith("inbound"):
            return self._inbound_ack_block(conf, thread_id)

        thread = await self.get(thread_id)
        if thread is None or not thread.rolling_summary.strip():
            return ""

        from pincer.voice.prompts import get_prompt
        from pincer.voice.tool_speech import spoken_datetime

        language = thread.language or "en"
        previous = [c for c in await self.calls(thread_id) if c.started_at]
        last_call, last_outcome = "", ""
        if previous:
            latest = previous[-1]
            parsed = _parse_dt(latest.started_at)
            last_call = spoken_datetime(parsed, language, include_time=False) if parsed else latest.started_at[:10]
            last_outcome = latest.task_result or latest.outcome_code

        open_items = [c for c in thread.open_commitments if c.get("status") == COMMITMENT_OPEN]
        rendered: list[str] = []
        for commitment in open_items[:MAX_CONTEXT_COMMITMENTS]:
            due = _parse_dt(str(commitment.get("due") or "")) if commitment.get("due") else None
            spoken_due = f" ({spoken_datetime(due, language)})" if due else ""
            rendered.append(f"{commitment['who']}: {commitment['what']}{spoken_due}")

        template = str(get_prompt("THREAD_CONTEXT_BLOCK", language) or "")
        if not template:  # pragma: no cover — every language pack defines it
            return ""
        block = template.format(
            summary=thread.rolling_summary,
            commitments="; ".join(rendered) or "—",
            last_call=last_call or "—",
            last_outcome=truncate(last_outcome, 200) or "—",
        )
        return truncate(block, MAX_CONTEXT_BLOCK)

    @staticmethod
    def _inbound_ack_block(settings: Any, thread_id: str) -> str:
        """§4.3 security boundary: an inbound conversation learns AT MOST that
        there was prior contact. Never the subject, summary, dates, or
        commitments — CallerID is spoofable, so a matched thread is grouping
        metadata, not an identity claim."""
        mode = str(getattr(settings, "thread_inbound_context", "off") or "off").strip().lower()
        if mode != "ack" or not thread_id:
            return ""
        from pincer.voice.prompts import get_prompt

        language = str(getattr(settings, "voice_default_language", "") or "en")[:2]
        ack = str(get_prompt("THREAD_INBOUND_ACK", language) or "").strip()
        instruction = str(get_prompt("THREAD_INBOUND_ACK_RULE", language) or "").strip()
        if not ack:  # pragma: no cover — every language pack defines it
            return ""
        return truncate(f'{instruction}\n"{ack}"' if instruction else ack, MAX_CONTEXT_BLOCK)


# ── Scheduled auto-close (§5) ────────────────────────────────────────


async def run_thread_autoclose(settings: Any) -> list[str]:
    """Close threads that have gone quiet for PINCER_THREAD_AUTOCLOSE_DAYS."""
    days = int(getattr(settings, "thread_autoclose_days", 0) or 0)
    return await get_thread_manager(settings).autoclose(days)


def make_autoclose_handler(settings: Any) -> Any:
    """CronScheduler action handler for ``thread_autoclose``."""

    async def _handler(pincer_user_id: str, action: dict[str, Any], channel: str) -> str | None:
        try:
            await run_thread_autoclose(settings)
        except Exception:
            logger.exception("Thread auto-close failed")
        return None  # nothing to send to the user

    return _handler


# ── Chat-side discoverability (§8) ───────────────────────────────────


def make_thread_lookup(settings: Any) -> Any:
    """Build the ``thread_lookup`` handler.

    Chat context only. The tier table lists it as X for calls: a stranger on
    the phone must never be able to enumerate the user's open matters, and
    "invisible" beats "visible but refused" (Sprint 11 §5.3).
    """

    async def thread_lookup(contact_or_subject: str = "", status: str = "open") -> str:
        """Find call threads (grouped phone calls about one matter) by contact
        name, subject, or phone number. Use before offering a follow-up call so
        the right thread_id can be passed to make_phone_call.
        """
        query = str(contact_or_subject or "").strip()
        wanted = str(status or "").strip().lower()
        if wanted and wanted != "all" and wanted not in STATUSES:
            return f"Error: unknown status {status!r} (expected one of {', '.join(STATUSES)}, all)."
        try:
            found = await get_thread_manager(settings).list_threads(
                status="" if wanted == "all" else wanted, query=query, limit=10
            )
        except Exception as e:
            logger.exception("thread_lookup failed")
            return f"Error: {e}"
        return json.dumps(
            [
                {
                    "thread_id": t.thread_id,
                    "subject": t.subject,
                    "status": t.status,
                    "contact_name": t.contact_name,
                    "primary_number": t.primary_number,
                    "summary": t.rolling_summary,
                    "open_commitments": [c for c in t.open_commitments if c.get("status") == COMMITMENT_OPEN],
                    "updated_at": t.updated_at,
                }
                for t in found
            ],
            ensure_ascii=False,
        )

    return thread_lookup


def register_thread_tools(registry: Any, settings: Any) -> list[str]:
    """Register the chat-side thread tool. Returns the registered names."""
    registry.register(
        name="thread_lookup",
        description=(
            "Look up call threads — groups of related phone calls about one matter (a booking, a "
            "complaint, a callback) — by contact name, subject, or phone number. Returns each thread's "
            "id, subject, status, rolling summary and open commitments. Use it to answer 'what's the "
            "status with X?' and to obtain the thread_id before offering a follow-up call. If two or "
            "more open threads match the same contact, ASK the user which one — never guess."
        ),
        handler=make_thread_lookup(settings),
        parameters={
            "type": "object",
            "properties": {
                "contact_or_subject": {
                    "type": "string",
                    "description": "Contact name, subject words, or phone number (empty = most recent threads)",
                    "default": "",
                },
                "status": {
                    "type": "string",
                    "enum": ["open", "resolved", "closed", "all"],
                    "description": "Which threads to return (default 'open')",
                    "default": "open",
                },
            },
            "required": [],
        },
        require_approval=False,
    )
    return ["thread_lookup"]


# ── Report decoration (§10) ──────────────────────────────────────────

_REPORT_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "header": "🧵 {subject} — call {index} of {total}",
        "open": "Open: {items}",
        "resolved": "✅ Matter settled.",
        "link": "🔗 Thread: {link}",
    },
    "de": {
        "header": "🧵 {subject} — Anruf {index} von {total}",
        "open": "Offen: {items}",
        "resolved": "✅ Anliegen erledigt.",
        "link": "🔗 Thread: {link}",
    },
    "uk": {
        "header": "🧵 {subject} — дзвінок {index} з {total}",
        "open": "Відкрито: {items}",
        "resolved": "✅ Питання вирішено.",
        "link": "🔗 Тред: {link}",
    },
}


def thread_deep_link(settings: Any, thread_id: str) -> str:
    """Dashboard URL for a thread, or the bare id when no dashboard is configured."""
    base = str(getattr(settings, "dashboard_url", "") or "").strip().rstrip("/")
    return f"{base}/voice/threads/{thread_id}" if base.startswith("http") else thread_id


def decorate_report(report: str, update: ThreadUpdate | None, settings: Any = None, language: str = "") -> str:
    """§10: a threaded call's report gains its position in the matter, the
    commitments that changed, and — when the matter is done — that it is done.

    The header goes FIRST: the user's eye should land on which matter this is
    before it lands on the call.
    """
    if update is None:
        return report
    lang = str(language or update.thread.language or "en").strip().lower()[:2]
    strings = _REPORT_STRINGS.get(lang, _REPORT_STRINGS["en"])
    thread = update.thread

    lines = [
        strings["header"].format(
            subject=thread.subject, index=update.call_index, total=max(update.call_total, update.call_index)
        )
    ]
    if report:
        lines.append(report)
    if update.commitments_changed:
        open_items = [c for c in thread.open_commitments if c.get("status") == COMMITMENT_OPEN]
        if open_items:
            rendered = "; ".join(
                f"{c['who']}: {c['what']}" + (f" ({c['due']})" if c.get("due") else "")
                for c in open_items[:MAX_CONTEXT_COMMITMENTS]
            )
            lines.append(strings["open"].format(items=rendered))
    if update.resolved_now:
        lines.append(strings["resolved"])
    lines.append(strings["link"].format(link=thread_deep_link(settings, thread.thread_id)))
    return "\n".join(lines)


# ── Singleton (wired at startup, like the voice engine) ──────────────

_manager: ThreadManager | None = None


def set_thread_manager(manager: ThreadManager | None) -> None:
    global _manager  # noqa: PLW0603
    if manager is not None and _manager is not None and _manager is not manager:
        # Hotfix-3 lesson: two instances means two truths about one thread.
        logger.warning("ThreadManager replaced — there must be exactly one instance per process")
    _manager = manager


def get_thread_manager(settings: Any = None) -> ThreadManager:
    """The process-wide manager, lazily built from settings when startup did
    not wire one (API-only processes, tests). The LLM-backed summary merge is
    only available on the instance `init_thread_manager` created."""
    global _manager  # noqa: PLW0603
    if _manager is None:
        conf = settings
        if conf is None:
            from pincer.config import get_settings_relaxed

            conf = get_settings_relaxed()
        _manager = ThreadManager(str(getattr(conf, "db_path", "") or ""), settings=conf)
        logger.debug("ThreadManager created lazily (no startup instance was installed)")
    return _manager


def init_thread_manager(settings: Any, llm: BaseLLMProvider | None = None, memory: Any = None) -> ThreadManager:
    """Install the one instance for this process (called from `_run_agent`)."""
    manager = ThreadManager(str(getattr(settings, "db_path", "") or ""), settings=settings, llm=llm)
    manager.set_memory(memory)
    set_thread_manager(manager)
    return manager


def _reset_for_tests() -> None:
    global _manager  # noqa: PLW0603
    _manager = None


__all__ = [
    "ATTACH_KINDS",
    "KIND_FOLLOWUP",
    "KIND_INBOUND_MATCHED",
    "KIND_MANUAL",
    "KIND_ORIGIN",
    "KIND_RETRY",
    "MAX_CONTEXT_BLOCK",
    "MAX_SUMMARY",
    "ORIGIN_INBOUND",
    "ORIGIN_USER_TASK",
    "STATUS_CLOSED",
    "STATUS_OPEN",
    "STATUS_RESOLVED",
    "Thread",
    "ThreadCall",
    "ThreadError",
    "ThreadManager",
    "ThreadUpdate",
    "decorate_report",
    "get_thread_manager",
    "init_thread_manager",
    "make_thread_lookup",
    "merge_commitments",
    "merge_summary",
    "register_thread_tools",
    "set_thread_manager",
]
