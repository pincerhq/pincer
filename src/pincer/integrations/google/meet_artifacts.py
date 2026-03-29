"""
Google Meet artifact processing helpers.

Provides higher-level operations on meeting artifacts (transcripts,
recordings, smart notes) by combining the Meet REST API, Drive API,
and Docs API.

Usage::

    from pincer.integrations.google.meet_artifacts import MeetArtifactProcessor

    processor = MeetArtifactProcessor(auth, factory)

    # Compile full transcript text
    text = await processor.compile_full_transcript("conferenceRecords/xxx")

    # Download recording MP4 to disk
    path = await processor.download_recording("1BxiMVs0...", "/tmp/recording.mp4")

    # Read transcript Docs content
    content = await processor.get_transcript_doc_content("1BxiMVs0...")

    # Generate attendance report
    report = await processor.generate_attendance_report("conferenceRecords/xxx")
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pincer.integrations.google.auth import GoogleAuth
    from pincer.integrations.google.service_factory import GoogleServiceFactory

logger = logging.getLogger(__name__)


class MeetArtifactProcessor:
    """Helper for processing Google Meet artifacts post-meeting.

    Combines the Meet v2 REST API (transcript entries), Drive API
    (recording download), and Docs API (reading transcript documents).

    Args:
        auth: ``GoogleAuth`` instance for credential access.
        factory: ``GoogleServiceFactory`` for Drive and Docs service objects.
    """

    def __init__(
        self,
        auth: "GoogleAuth",
        factory: "GoogleServiceFactory",
    ) -> None:
        self._auth = auth
        self._factory = factory

    # ── Transcript ─────────────────────────────────────────────────────────────

    async def compile_full_transcript(self, conference_name: str) -> str:
        """Compile a full chronological transcript from structured transcript entries.

        Fetches all transcript entries across all transcripts for the conference,
        sorts them by start time, and formats as ``[HH:MM:SS UTC] Speaker: text``.

        Returns:
            Multi-line string transcript, or an error/empty message.
        """
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        creds = await asyncio.to_thread(self._auth.get_credentials)
        svc = await asyncio.to_thread(
            build, "meet", "v2", credentials=creds, cache_discovery=False
        )

        # List transcripts for the conference
        _conf = conference_name
        result = await asyncio.to_thread(
            lambda: svc.conferenceRecords().transcripts().list(parent=_conf).execute()
        )
        transcripts = result.get("transcripts", [])
        if not transcripts:
            return "No transcripts found."

        # Fetch all entries with pagination
        all_entries: list[dict[str, Any]] = []
        for transcript in transcripts:
            page_token: str | None = None
            while True:
                _t = transcript["name"]
                _pt = page_token

                def _fetch(t: str = _t, pt: str | None = _pt) -> dict[str, Any]:
                    kw: dict[str, Any] = {"parent": t, "pageSize": 250}
                    if pt:
                        kw["pageToken"] = pt
                    return (  # type: ignore[no-any-return]
                        svc.conferenceRecords()
                        .transcripts()
                        .entries()
                        .list(**kw)
                        .execute()
                    )

                page_result = await asyncio.to_thread(_fetch)
                all_entries.extend(page_result.get("transcriptEntries", []))
                page_token = page_result.get("nextPageToken")
                if not page_token:
                    break

        if not all_entries:
            return "No transcript entries found."

        all_entries.sort(key=lambda e: e.get("startTime") or "")

        lines: list[str] = []
        for entry in all_entries:
            ts_raw = entry.get("startTime", "")
            ts = ts_raw.replace("T", " ").replace("Z", " UTC").split("+")[0].strip() if ts_raw else ""
            speaker = entry.get("participantDisplayName") or "Unknown"
            text = entry.get("text", "")
            lines.append(f"[{ts}] {speaker}: {text}")

        return "\n".join(lines)

    # ── Recording ──────────────────────────────────────────────────────────────

    async def download_recording(self, drive_file_id: str, local_path: str) -> str:
        """Download a recording MP4 from Google Drive to *local_path*.

        Args:
            drive_file_id: The Drive file ID from ``recording.driveDestination.file``.
            local_path: Absolute or relative path to save the MP4.

        Returns:
            Confirmation with file path and size, or an error message.
        """
        import io

        from googleapiclient.http import MediaIoBaseDownload  # type: ignore[import-untyped]

        drive_svc = await self._factory.get("drive")
        buf = io.BytesIO()

        def _do_download() -> None:
            req = drive_svc.files().get_media(fileId=drive_file_id)
            downloader = MediaIoBaseDownload(buf, req)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        try:
            await asyncio.to_thread(_do_download)
            with open(local_path, "wb") as f:
                f.write(buf.getvalue())
            size_mb = len(buf.getvalue()) / 1_048_576
            return f"Downloaded to {local_path} ({size_mb:.1f} MB)"
        except Exception as exc:
            logger.error("Recording download failed for %s: %s", drive_file_id, exc)
            return f"Download failed: {exc}"

    # ── Docs ───────────────────────────────────────────────────────────────────

    async def get_transcript_doc_content(self, docs_document_id: str) -> str:
        """Read the plain-text content of a transcript Google Doc.

        This is an alternative to the structured transcript entries — the
        Docs version has different paragraph formatting and may be easier
        for human reading, but lacks per-entry timestamps.

        Args:
            docs_document_id: The Docs document ID from
                ``transcript.docsDestination.document``.

        Returns:
            Plain-text document content.
        """
        docs_svc = await self._factory.get("docs")
        _did = docs_document_id
        doc = await asyncio.to_thread(
            lambda: docs_svc.documents().get(documentId=_did).execute()
        )

        content = doc.get("body", {}).get("content", [])
        text_parts: list[str] = []
        for element in content:
            paragraph = element.get("paragraph", {})
            for elem in paragraph.get("elements", []):
                text_run = elem.get("textRun", {})
                chunk = text_run.get("content", "")
                if chunk:
                    text_parts.append(chunk)

        return "".join(text_parts)

    # ── Attendance ─────────────────────────────────────────────────────────────

    async def generate_attendance_report(self, conference_name: str) -> str:
        """Generate an attendance report for a conference.

        Lists all participants with join/leave times and total session count.

        Args:
            conference_name: Conference resource name (``conferenceRecords/xxx``).

        Returns:
            Formatted attendance report string.
        """
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        creds = await asyncio.to_thread(self._auth.get_credentials)
        svc = await asyncio.to_thread(
            build, "meet", "v2", credentials=creds, cache_discovery=False
        )

        # Get conference record
        _conf = conference_name
        record = await asyncio.to_thread(
            lambda: svc.conferenceRecords().get(name=_conf).execute()
        )

        # Fetch all participants (paginated)
        all_participants: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            _pt = page_token

            def _list_parts(pt: str | None = _pt) -> dict[str, Any]:
                kw: dict[str, Any] = {"parent": conference_name, "pageSize": 100}
                if pt:
                    kw["pageToken"] = pt
                return svc.conferenceRecords().participants().list(**kw).execute()  # type: ignore[no-any-return]

            result = await asyncio.to_thread(_list_parts)
            all_participants.extend(result.get("participants", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

        def _ts(raw: str | None) -> str:
            if not raw:
                return "?"
            return raw.replace("T", " ").replace("Z", " UTC").split("+")[0].strip()

        start = _ts(record.get("startTime"))
        end_raw = record.get("endTime")
        end = _ts(end_raw) if end_raw else "ongoing"

        lines: list[str] = [
            f"Attendance Report: {conference_name}",
            f"Meeting time:      {start} → {end}",
            f"Total participants: {len(all_participants)}",
            "",
        ]

        for p in all_participants:
            if p.get("signerUser"):
                identity = p["signerUser"].get("displayName", "Unknown")
            elif p.get("anonymousUser"):
                identity = p["anonymousUser"].get("displayName", "Anonymous")
            elif p.get("phoneUser"):
                identity = p["phoneUser"].get("displayName", "Phone user")
            else:
                identity = "Unknown"

            join = _ts(p.get("earliestStartTime"))
            leave = _ts(p.get("latestEndTime")) if p.get("latestEndTime") else "still in meeting"

            lines.append(f"  • {identity}")
            lines.append(f"    Joined: {join}  →  Left: {leave}")

        return "\n".join(lines)
