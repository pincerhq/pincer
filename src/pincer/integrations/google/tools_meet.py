"""
Google Meet tools — 27 tools for full meeting lifecycle management.

Covers:
  - Meeting space management (8 tools): create, get, update, end conference,
    configure moderation, configure artifacts, add/remove members
  - Conference history (5 tools): list records, get record, list participants,
    get participant details, list participant sessions
  - Recording management (4 tools): list, get, download, check status
  - Transcript management (5 tools): list, get, list entries, get entry, summarize
  - Smart notes (2 tools): list, get
  - Event subscriptions (3 tools): subscribe, list, delete

Note: Members API (add/remove member) and moderation settings are Developer
Preview features and may not be available for all Google Workspace accounts.
Graceful fallback is implemented for both.

Requires Google Workspace Business Standard or higher. Personal Gmail accounts
do not have access to the Meet REST API.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import TYPE_CHECKING, Any

from pincer.integrations.google.models import fmt_list
from pincer.integrations.google.quota import with_backoff

if TYPE_CHECKING:
    from collections.abc import Callable

    from pincer.integrations.google.meet_events import MeetEventSubscriber
    from pincer.integrations.google.service_factory import GoogleServiceFactory
    from pincer.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_ACCESS_TYPE_DESCRIPTIONS = {
    "OPEN": "anyone with link",
    "TRUSTED": "same org only",
    "RESTRICTED": "invited members only",
}


def _fmt_ts(ts: str | None) -> str:
    """Normalise an ISO-8601 timestamp for human-readable display."""
    if not ts:
        return "unknown"
    return ts.replace("T", " ").replace("Z", " UTC").split("+")[0].strip()


# ── Space Management ──────────────────────────────────────────────────────────


async def google__create_meet_space(
    factory: GoogleServiceFactory,
    access_type: str = "OPEN",
    auto_recording: bool = False,
    auto_transcription: bool = False,
    auto_smart_notes: bool = False,
    entry_point_access: str = "ALL",
) -> str:
    """Create a new Google Meet meeting space and return the meeting URL."""
    svc = await factory.get("meet")

    config: dict[str, Any] = {
        "accessType": access_type,
        "entryPointAccess": entry_point_access,
    }

    artifact_config: dict[str, Any] = {}
    if auto_recording:
        artifact_config["recordingConfig"] = {"autoRecordingGeneration": "ENABLED"}
    if auto_transcription:
        artifact_config["transcriptionConfig"] = {"autoTranscriptionGeneration": "ENABLED"}
    if auto_smart_notes:
        artifact_config["smartNotesConfig"] = {"autoSmartNotesGeneration": "ENABLED"}
    if artifact_config:
        config["artifactConfig"] = artifact_config

    space = await with_backoff(lambda: svc.spaces().create(body={"config": config}).execute())

    features = []
    if auto_recording:
        features.append("auto-recording")
    if auto_transcription:
        features.append("auto-transcription")
    if auto_smart_notes:
        features.append("smart notes")
    feature_str = f" with {', '.join(features)}" if features else ""
    access_desc = _ACCESS_TYPE_DESCRIPTIONS.get(access_type, access_type)

    return (
        f"Meeting space created{feature_str}.\n\n"
        f"Meeting link: {space.get('meetingUri', '?')}\n"
        f"Meeting code: {space.get('meetingCode', '?')}\n"
        f"Space name:   {space.get('name', '?')}\n"
        f"Access:       {access_type} ({access_desc})\n\n"
        f"Share the meeting link with participants."
    )


async def google__get_meet_space(
    factory: GoogleServiceFactory,
    space_name: str = "",
    meeting_code: str = "",
) -> str:
    """Get details of a Google Meet space: meeting URL, config, active conference."""
    if not space_name and not meeting_code:
        return "Error: provide either space_name (e.g. spaces/xxx) or meeting_code (e.g. abc-defg-hij)."

    svc = await factory.get("meet")
    name = space_name if space_name else f"spaces/{meeting_code}"
    space = await with_backoff(lambda: svc.spaces().get(name=name).execute())

    active_conf = space.get("activeConference", {})
    active_str = (
        f"Yes — {active_conf.get('conferenceRecord', 'in progress')}" if active_conf else "No active conference"
    )

    cfg = space.get("config", {})
    artifact = cfg.get("artifactConfig", {})
    rec = artifact.get("recordingConfig", {}).get("autoRecordingGeneration", "DISABLED")
    trans = artifact.get("transcriptionConfig", {}).get("autoTranscriptionGeneration", "DISABLED")
    notes = artifact.get("smartNotesConfig", {}).get("autoSmartNotesGeneration", "DISABLED")

    return (
        f"Meeting link: {space.get('meetingUri', '?')}\n"
        f"Code:         {space.get('meetingCode', '?')}\n"
        f"Space:        {space.get('name', '?')}\n"
        f"Access:       {cfg.get('accessType', '?')}\n"
        f"Entry access: {cfg.get('entryPointAccess', '?')}\n"
        f"Active conf:  {active_str}\n"
        f"Auto-record:  {rec}\n"
        f"Auto-trans:   {trans}\n"
        f"Smart notes:  {notes}"
    )


async def google__update_meet_space(
    factory: GoogleServiceFactory,
    space_name: str,
    access_type: str = "",
    entry_point_access: str = "",
) -> str:
    """Update a Google Meet space's access type or entry point access."""
    svc = await factory.get("meet")

    config: dict[str, Any] = {}
    mask_parts: list[str] = []

    if access_type:
        config["accessType"] = access_type
        mask_parts.append("config.accessType")
    if entry_point_access:
        config["entryPointAccess"] = entry_point_access
        mask_parts.append("config.entryPointAccess")

    if not config:
        return "No fields to update. Provide access_type or entry_point_access."

    _body = {"config": config}
    _mask = ",".join(mask_parts)
    space = await with_backoff(lambda: svc.spaces().patch(name=space_name, body=_body, updateMask=_mask).execute())
    new_access = space.get("config", {}).get("accessType", "?")
    return f"Space {space_name} updated. Access: {new_access}"


async def google__end_active_conference(
    factory: GoogleServiceFactory,
    space_name: str,
) -> str:
    """End the active conference in a meeting space — disconnects all participants."""
    svc = await factory.get("meet")
    _name = space_name
    await with_backoff(lambda: svc.spaces().endActiveConference(name=_name, body={}).execute())
    return f"Active conference in {space_name} has been ended. All participants disconnected."


async def google__configure_meet_moderation(
    factory: GoogleServiceFactory,
    space_name: str,
    chat_restriction: str = "",
    reaction_restriction: str = "",
    present_restriction: str = "",
    default_join_as_viewer: bool = False,
) -> str:
    """Configure moderation restrictions for a Google Meet space (Developer Preview).

    Restriction values: NO_RESTRICTION, HOST_AND_CO_HOSTS, ORGANIZER_ONLY
    """
    svc = await factory.get("meet")

    moderation: dict[str, Any] = {}
    mask_parts: list[str] = []

    if chat_restriction:
        moderation["chatRestriction"] = chat_restriction
        mask_parts.append("config.moderationRestrictions.chatRestriction")
    if reaction_restriction:
        moderation["reactionRestriction"] = reaction_restriction
        mask_parts.append("config.moderationRestrictions.reactionRestriction")
    if present_restriction:
        moderation["presentRestriction"] = present_restriction
        mask_parts.append("config.moderationRestrictions.presentRestriction")
    if default_join_as_viewer:
        moderation["defaultJoinAsViewerType"] = "DEFAULT_JOIN_AS_VIEWER"
        mask_parts.append("config.moderationRestrictions.defaultJoinAsViewerType")

    if not moderation:
        return "No moderation settings to update."

    _body = {"config": {"moderationRestrictions": moderation}}
    _mask = ",".join(mask_parts)
    _name = space_name

    try:
        await with_backoff(lambda: svc.spaces().patch(name=_name, body=_body, updateMask=_mask).execute())
        parts = [f"  {k}: {v}" for k, v in moderation.items()]
        return f"Moderation configured for {space_name}:\n" + "\n".join(parts)
    except Exception as exc:
        logger.warning("Moderation API unavailable (Developer Preview): %s", exc)
        return (
            "Moderation configuration is a Developer Preview feature and may not be "
            "enabled for your account. As a workaround, configure moderation manually "
            f"in Google Meet settings for the space.\nError: {exc}"
        )


async def google__configure_meet_artifacts(
    factory: GoogleServiceFactory,
    space_name: str,
    auto_recording: bool | None = None,
    auto_transcription: bool | None = None,
    auto_smart_notes: bool | None = None,
) -> str:
    """Pre-configure auto-recording, auto-transcription, and smart notes for a space."""
    svc = await factory.get("meet")

    artifact_config: dict[str, Any] = {}
    mask_parts: list[str] = []

    if auto_recording is not None:
        val = "ENABLED" if auto_recording else "DISABLED"
        artifact_config["recordingConfig"] = {"autoRecordingGeneration": val}
        mask_parts.append("config.artifactConfig.recordingConfig")
    if auto_transcription is not None:
        val = "ENABLED" if auto_transcription else "DISABLED"
        artifact_config["transcriptionConfig"] = {"autoTranscriptionGeneration": val}
        mask_parts.append("config.artifactConfig.transcriptionConfig")
    if auto_smart_notes is not None:
        val = "ENABLED" if auto_smart_notes else "DISABLED"
        artifact_config["smartNotesConfig"] = {"autoSmartNotesGeneration": val}
        mask_parts.append("config.artifactConfig.smartNotesConfig")

    if not artifact_config:
        return (
            "No artifact settings to update. "
            "Provide at least one of auto_recording, auto_transcription, auto_smart_notes."
        )

    _body = {"config": {"artifactConfig": artifact_config}}
    _mask = ",".join(mask_parts)
    _name = space_name

    await with_backoff(lambda: svc.spaces().patch(name=_name, body=_body, updateMask=_mask).execute())

    settings = []
    if auto_recording is not None:
        settings.append(f"Auto-recording: {'ENABLED' if auto_recording else 'DISABLED'}")
    if auto_transcription is not None:
        settings.append(f"Auto-transcription: {'ENABLED' if auto_transcription else 'DISABLED'}")
    if auto_smart_notes is not None:
        settings.append(f"Smart notes: {'ENABLED' if auto_smart_notes else 'DISABLED'}")

    return f"Artifact settings updated for {space_name}:\n" + "\n".join(f"  {s}" for s in settings)


async def google__add_meet_member(
    factory: GoogleServiceFactory,
    space_name: str,
    email: str,
    role: str = "MEMBER",
) -> str:
    """Add a member to a Google Meet space (Developer Preview).

    Roles: HOST, COHOST, MEMBER. Members can join without knocking.
    """
    svc = await factory.get("meet")
    _parent = space_name
    _body = {"role": role, "signerUser": {"user": f"users/{email}"}}

    try:
        member = await with_backoff(lambda: svc.spaces().members().create(parent=_parent, body=_body).execute())
        return (
            f"Member added to {space_name}.\n  Email: {email}\n  Role: {role}\n  Member ID: {member.get('name', '?')}"
        )
    except Exception as exc:
        logger.warning("Members API unavailable (Developer Preview): %s", exc)
        return (
            "The Members API is a Developer Preview feature and may not be available "
            "for your account. As a workaround, share the meeting link directly. "
            f"Use google__get_meet_space to retrieve the meeting URI.\nError: {exc}"
        )


async def google__remove_meet_member(
    factory: GoogleServiceFactory,
    member_name: str,
) -> str:
    """Remove a member from a Google Meet space (Developer Preview).

    member_name format: spaces/xxx/members/yyy
    """
    svc = await factory.get("meet")
    _name = member_name

    try:
        await with_backoff(lambda: svc.spaces().members().delete(name=_name).execute())
        return f"Member {member_name} removed from the meeting space."
    except Exception as exc:
        logger.warning("Members API unavailable (Developer Preview): %s", exc)
        return (
            f"The Members API is a Developer Preview feature and may not be available for your account.\nError: {exc}"
        )


# ── Conference History ─────────────────────────────────────────────────────────


async def google__list_conference_records(
    factory: GoogleServiceFactory,
    space_name: str = "",
    max_results: int = 10,
) -> str:
    """List past conference records, optionally filtered by meeting space."""
    svc = await factory.get("meet")

    _kwargs: dict[str, Any] = {"pageSize": max_results}
    if space_name:
        _kwargs["filter"] = f'space.name="{space_name}"'

    result = await with_backoff(lambda: svc.conferenceRecords().list(**_kwargs).execute())
    records = result.get("conferenceRecords", [])

    if not records:
        suffix = f" for space {space_name}" if space_name else ""
        return f"No conference records found{suffix}."

    lines = []
    for rec in records:
        start = _fmt_ts(rec.get("startTime"))
        end = _fmt_ts(rec.get("endTime")) if rec.get("endTime") else "ongoing"
        lines.append(
            f"  {rec.get('name', '?')}\n    Space: {rec.get('space', '?')}\n    Start: {start}\n    End:   {end}"
        )

    more = bool(result.get("nextPageToken"))
    return fmt_list(lines, header=f"Conference records ({len(lines)}):", more=more)


async def google__get_conference_record(
    factory: GoogleServiceFactory,
    conference_name: str,
) -> str:
    """Get details of a specific past conference: start/end times, space, expiry."""
    svc = await factory.get("meet")
    _name = conference_name
    rec = await with_backoff(lambda: svc.conferenceRecords().get(name=_name).execute())

    return (
        f"Conference: {rec.get('name', '?')}\n"
        f"Space:      {rec.get('space', '?')}\n"
        f"Start:      {_fmt_ts(rec.get('startTime'))}\n"
        f"End:        {_fmt_ts(rec.get('endTime')) if rec.get('endTime') else 'ongoing'}\n"
        f"Expires:    {_fmt_ts(rec.get('expireTime'))}"
    )


async def google__list_conference_participants(
    factory: GoogleServiceFactory,
    conference_name: str,
    max_results: int = 50,
) -> str:
    """List all participants who joined a conference with join/leave times."""
    svc = await factory.get("meet")
    _parent = conference_name
    result = await with_backoff(
        lambda: svc.conferenceRecords().participants().list(parent=_parent, pageSize=max_results).execute()
    )
    participants = result.get("participants", [])

    if not participants:
        return f"No participants found for {conference_name}."

    lines = []
    for p in participants:
        if p.get("signerUser"):
            identity = p["signerUser"].get("displayName", "Unknown signed-in user")
            kind = "signed in"
        elif p.get("anonymousUser"):
            identity = p["anonymousUser"].get("displayName", "Anonymous")
            kind = "anonymous"
        elif p.get("phoneUser"):
            identity = p["phoneUser"].get("displayName", "Phone user")
            kind = "phone"
        else:
            identity = "Unknown participant"
            kind = ""

        join = _fmt_ts(p.get("earliestStartTime"))
        leave = _fmt_ts(p.get("latestEndTime")) if p.get("latestEndTime") else "still in meeting"
        kind_str = f" ({kind})" if kind else ""

        lines.append(f"  {identity}{kind_str}\n    Joined: {join} → Left: {leave}\n    ID: {p.get('name', '?')}")

    more = bool(result.get("nextPageToken"))
    return fmt_list(lines, header=f"Participants ({len(lines)}):", more=more)


async def google__get_participant_details(
    factory: GoogleServiceFactory,
    participant_name: str,
) -> str:
    """Get detailed info for a conference participant including earliest/latest times."""
    svc = await factory.get("meet")
    _name = participant_name
    p = await with_backoff(lambda: svc.conferenceRecords().participants().get(name=_name).execute())

    if p.get("signerUser"):
        identity = p["signerUser"].get("displayName", "?")
        user = p["signerUser"].get("user", "?")
    elif p.get("anonymousUser"):
        identity = p["anonymousUser"].get("displayName", "Anonymous")
        user = "anonymous"
    elif p.get("phoneUser"):
        identity = p["phoneUser"].get("displayName", "Phone user")
        user = "phone"
    else:
        identity = "Unknown"
        user = "?"

    leave = _fmt_ts(p.get("latestEndTime")) if p.get("latestEndTime") else "still in meeting"

    return (
        f"Participant: {identity}\n"
        f"User:        {user}\n"
        f"First join:  {_fmt_ts(p.get('earliestStartTime'))}\n"
        f"Last leave:  {leave}\n"
        f"ID:          {p.get('name', '?')}"
    )


async def google__list_participant_sessions(
    factory: GoogleServiceFactory,
    participant_name: str,
) -> str:
    """List all sessions for a participant (each join/leave pair for participants who reconnected)."""
    svc = await factory.get("meet")
    _parent = participant_name
    result = await with_backoff(
        lambda: svc.conferenceRecords().participants().participantSessions().list(parent=_parent).execute()
    )
    sessions = result.get("participantSessions", [])

    if not sessions:
        return f"No sessions found for participant {participant_name}."

    lines = []
    for i, s in enumerate(sessions, 1):
        start = _fmt_ts(s.get("startTime"))
        end = _fmt_ts(s.get("endTime")) if s.get("endTime") else "ongoing"
        lines.append(f"  Session {i}: {start} → {end}  (ID: {s.get('name', '?')})")

    return fmt_list(lines, header=f"Sessions ({len(lines)}) for {participant_name}:")


# ── Recordings ────────────────────────────────────────────────────────────────


async def google__list_meet_recordings(
    factory: GoogleServiceFactory,
    conference_name: str,
) -> str:
    """List recordings for a conference.

    States: STARTED=in progress, ENDED=processing, FILE_GENERATED=ready.
    Recordings take ~1x meeting duration to process after the meeting ends.
    """
    svc = await factory.get("meet")
    _parent = conference_name
    result = await with_backoff(lambda: svc.conferenceRecords().recordings().list(parent=_parent).execute())
    recordings = result.get("recordings", [])

    if not recordings:
        return "No recordings found. Was recording enabled during the meeting?"

    lines = []
    for r in recordings:
        state = r.get("state", "?")
        start = _fmt_ts(r.get("startTime"))
        end = _fmt_ts(r.get("endTime")) if r.get("endTime") else "processing"

        drive = r.get("driveDestination", {})
        drive_str = ""
        if drive.get("file"):
            drive_str = f"\n    Drive file: {drive['file']}"
        if drive.get("exportUri"):
            drive_str += f"\n    Export URI: {drive['exportUri']}"

        lines.append(f"  {r.get('name', '?')}\n    State: {state}\n    Time:  {start} → {end}{drive_str}")

    return fmt_list(lines, header=f"Recordings ({len(lines)}):")


async def google__get_meet_recording(
    factory: GoogleServiceFactory,
    recording_name: str,
) -> str:
    """Get details of a specific recording including Drive file ID for download."""
    svc = await factory.get("meet")
    _name = recording_name
    r = await with_backoff(lambda: svc.conferenceRecords().recordings().get(name=_name).execute())

    drive = r.get("driveDestination", {})
    end = _fmt_ts(r.get("endTime")) if r.get("endTime") else "processing"

    return (
        f"Recording: {r.get('name', '?')}\n"
        f"State:      {r.get('state', '?')}\n"
        f"Start:      {_fmt_ts(r.get('startTime'))}\n"
        f"End:        {end}\n"
        f"Drive file: {drive.get('file', 'not yet available')}\n"
        f"Export URI: {drive.get('exportUri', 'not yet available')}"
    )


async def google__download_meet_recording(
    factory: GoogleServiceFactory,
    drive_file_id: str,
    local_path: str,
) -> str:
    """Download a meeting recording MP4 from Google Drive to a local file path."""
    import io

    from googleapiclient.http import MediaIoBaseDownload

    drive_svc = await factory.get("drive")
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
        return f"Recording downloaded to {local_path} ({size_mb:.1f} MB)"
    except Exception as exc:
        return f"Download failed: {exc}\nUse google__get_meet_recording to get the export URI and download manually."


async def google__check_recording_status(
    factory: GoogleServiceFactory,
    recording_name: str,
) -> str:
    """Poll the processing state of a meeting recording.

    Useful to call after a meeting ends; recordings take time to process.
    """
    svc = await factory.get("meet")
    _name = recording_name
    r = await with_backoff(lambda: svc.conferenceRecords().recordings().get(name=_name).execute())

    state = r.get("state", "?")
    drive_file = r.get("driveDestination", {}).get("file", "")

    tips: dict[str, str] = {
        "STARTED": "Recording is in progress.",
        "ENDED": "Meeting ended — recording is being processed. Check again in a few minutes.",
        "FILE_GENERATED": f"Recording is ready! Drive file ID: {drive_file or '?'}",
    }
    return f"Recording state: {state}\n{tips.get(state, 'Unknown state.')}"


# ── Transcripts ───────────────────────────────────────────────────────────────


async def google__list_meet_transcripts(
    factory: GoogleServiceFactory,
    conference_name: str,
) -> str:
    """List transcripts for a conference (requires transcription to have been enabled)."""
    svc = await factory.get("meet")
    _parent = conference_name
    result = await with_backoff(lambda: svc.conferenceRecords().transcripts().list(parent=_parent).execute())
    transcripts = result.get("transcripts", [])

    if not transcripts:
        return "No transcripts found. Was transcription enabled during the meeting?"

    lines = []
    for t in transcripts:
        state = t.get("state", "?")
        start = _fmt_ts(t.get("startTime"))
        end = _fmt_ts(t.get("endTime")) if t.get("endTime") else "processing"
        docs = t.get("docsDestination", {})
        docs_str = f"\n    Docs document: {docs.get('document', '?')}" if docs else ""

        lines.append(f"  {t.get('name', '?')}\n    State: {state}\n    Time:  {start} → {end}{docs_str}")

    return fmt_list(lines, header=f"Transcripts ({len(lines)}):")


async def google__get_meet_transcript(
    factory: GoogleServiceFactory,
    transcript_name: str,
) -> str:
    """Get details of a specific transcript including the Docs document ID."""
    svc = await factory.get("meet")
    _name = transcript_name
    t = await with_backoff(lambda: svc.conferenceRecords().transcripts().get(name=_name).execute())

    docs = t.get("docsDestination", {})
    end = _fmt_ts(t.get("endTime")) if t.get("endTime") else "processing"

    return (
        f"Transcript: {t.get('name', '?')}\n"
        f"State:      {t.get('state', '?')}\n"
        f"Start:      {_fmt_ts(t.get('startTime'))}\n"
        f"End:        {end}\n"
        f"Docs doc:   {docs.get('document', 'not yet available')}\n"
        f"Export URI: {docs.get('exportUri', 'not yet available')}"
    )


async def google__list_transcript_entries(
    factory: GoogleServiceFactory,
    transcript_name: str,
    max_results: int = 100,
) -> str:
    """List transcript entries — each is a spoken segment with speaker name, text, and timestamps."""
    svc = await factory.get("meet")
    _parent = transcript_name
    result = await with_backoff(
        lambda: svc.conferenceRecords().transcripts().entries().list(parent=_parent, pageSize=max_results).execute()
    )
    entries = result.get("transcriptEntries", [])

    if not entries:
        return "No transcript entries found."

    lines = []
    for e in entries:
        ts = _fmt_ts(e.get("startTime"))
        speaker = e.get("participantDisplayName") or "Unknown"
        lines.append(f"  [{ts}] {speaker}: {e.get('text', '')}")

    more = bool(result.get("nextPageToken"))
    return fmt_list(lines, header=f"Transcript entries ({len(lines)}):", more=more)


async def google__get_transcript_entry(
    factory: GoogleServiceFactory,
    entry_name: str,
) -> str:
    """Get a single transcript entry: speaker, text, language, timestamps."""
    svc = await factory.get("meet")
    _name = entry_name
    e = await with_backoff(lambda: svc.conferenceRecords().transcripts().entries().get(name=_name).execute())

    return (
        f"Entry:    {e.get('name', '?')}\n"
        f"Speaker:  {e.get('participantDisplayName', 'Unknown')}\n"
        f"Text:     {e.get('text', '?')}\n"
        f"Language: {e.get('languageCode', '?')}\n"
        f"Start:    {_fmt_ts(e.get('startTime'))}\n"
        f"End:      {_fmt_ts(e.get('endTime'))}"
    )


async def google__summarize_meet_transcript(
    factory: GoogleServiceFactory,
    conference_name: str,
) -> str:
    """Fetch all transcript entries for a conference and return a compiled transcript for AI summarization.

    Returns the full speaker-attributed, timestamped transcript followed by
    summarization instructions. The agent's LLM will produce the summary,
    key decisions, and action items in the same turn.
    """
    svc = await factory.get("meet")

    # 1. List all transcripts for this conference
    _conf = conference_name
    transcripts_result = await with_backoff(lambda: svc.conferenceRecords().transcripts().list(parent=_conf).execute())
    transcripts = transcripts_result.get("transcripts", [])
    if not transcripts:
        return "No transcripts found. Was transcription enabled during the meeting?"

    # 2. Build a participant display-name map
    _conf2 = conference_name
    parts_result = await with_backoff(
        lambda: svc.conferenceRecords().participants().list(parent=_conf2, pageSize=100).execute()
    )
    participant_names: dict[str, str] = {}
    for p in parts_result.get("participants", []):
        pname = p.get("name", "")
        if p.get("signerUser"):
            display = p["signerUser"].get("displayName", "Unknown")
        elif p.get("anonymousUser"):
            display = p["anonymousUser"].get("displayName", "Anonymous")
        elif p.get("phoneUser"):
            display = p["phoneUser"].get("displayName", "Phone user")
        else:
            display = "Unknown"
        participant_names[pname] = display

    # 3. Fetch all entries across all transcripts (paginated)
    all_entries: list[dict[str, Any]] = []
    for transcript in transcripts:
        page_token: str | None = None
        while True:
            _t_name = transcript["name"]
            _pt = page_token

            def _list_entries(t: str = _t_name, pt: str | None = _pt) -> dict[str, Any]:
                kw: dict[str, Any] = {"parent": t, "pageSize": 250}
                if pt:
                    kw["pageToken"] = pt
                return svc.conferenceRecords().transcripts().entries().list(**kw).execute()  # type: ignore[no-any-return]

            result = await with_backoff(_list_entries)
            all_entries.extend(result.get("transcriptEntries", []))
            page_token = result.get("nextPageToken")
            if not page_token:
                break

    if not all_entries:
        return "No transcript entries found. The transcript may still be processing."

    # 4. Sort by start time and compile
    all_entries.sort(key=lambda e: e.get("startTime") or "")

    speakers_seen: set[str] = set()
    lines: list[str] = []
    for entry in all_entries:
        participant_ref = entry.get("participant", "")
        display_name = entry.get("participantDisplayName") or participant_names.get(participant_ref) or "Unknown"
        speakers_seen.add(display_name)
        ts = _fmt_ts(entry.get("startTime"))
        lines.append(f"[{ts}] {display_name}: {entry.get('text', '')}")

    full_transcript = "\n".join(lines)

    return (
        f"Meeting Transcript ({len(all_entries)} entries, {len(speakers_seen)} speakers)\n\n"
        f"{'─' * 60}\n\n"
        f"{full_transcript}\n\n"
        f"{'─' * 60}\n\n"
        "Please summarize this transcript. Include:\n"
        "1. A concise summary (3–5 sentences)\n"
        "2. Key decisions made\n"
        "3. Action items with owners and deadlines\n"
        "4. Full participant list"
    )


# ── Smart Notes ───────────────────────────────────────────────────────────────


async def google__list_smart_notes(
    factory: GoogleServiceFactory,
    conference_name: str,
) -> str:
    """List smart notes (AI-generated meeting summaries by Google Meet) for a conference.

    Requires auto_smart_notes=True to have been set on the space.
    """
    svc = await factory.get("meet")
    _parent = conference_name

    try:
        result = await with_backoff(lambda: svc.conferenceRecords().transcripts().list(parent=_parent).execute())
        transcripts = result.get("transcripts", [])

        # Smart notes have a smartNotesDestination field or state SMART_NOTES_SAVED
        smart_notes = [t for t in transcripts if t.get("smartNotesDestination") or "SMART_NOTES" in t.get("state", "")]

        if not smart_notes:
            if not transcripts:
                return "No smart notes found. Enable auto_smart_notes=True when creating the space."
            lines = [f"  {t.get('name', '?')} (state: {t.get('state', '?')})" for t in transcripts]
            return (
                "No smart notes found (requires auto_smart_notes=True on the space).\n"
                f"Found {len(transcripts)} transcript(s):\n" + "\n".join(lines)
            )

        lines = []
        for n in smart_notes:
            docs: Any = n.get("smartNotesDestination") or n.get("docsDestination", {})
            doc_id = docs.get("document", "?") if isinstance(docs, dict) else str(docs)
            lines.append(f"  {n.get('name', '?')}\n    State: {n.get('state', '?')}\n    Docs document: {doc_id}")
        return fmt_list(lines, header=f"Smart notes ({len(lines)}):")
    except Exception as exc:
        return (
            "Smart notes not available. This feature requires auto_smart_notes=True "
            f"on the meeting space.\nError: {exc}"
        )


async def google__get_smart_notes(
    factory: GoogleServiceFactory,
    transcript_name: str,
) -> str:
    """Get the smart notes document for a transcript. Returns the Docs document ID."""
    svc = await factory.get("meet")
    _name = transcript_name

    try:
        t = await with_backoff(lambda: svc.conferenceRecords().transcripts().get(name=_name).execute())
        docs: Any = t.get("smartNotesDestination") or t.get("docsDestination", {})
        if not docs:
            return "No smart notes document found for this transcript."

        doc_id = docs.get("document", "?") if isinstance(docs, dict) else str(docs)
        export_uri = docs.get("exportUri", "?") if isinstance(docs, dict) else "?"

        return (
            f"Smart notes for {transcript_name}:\n"
            f"  Docs document ID: {doc_id}\n"
            f"  Export URI: {export_uri}\n\n"
            f"Use google__read_document with document_id='{doc_id}' to read the content."
        )
    except Exception as exc:
        return f"Could not retrieve smart notes: {exc}"


# ── Event Subscriptions ───────────────────────────────────────────────────────


async def google__subscribe_meet_events(
    factory: GoogleServiceFactory,
    subscriber: MeetEventSubscriber,
    space_name: str = "",
    subscribe_all: bool = False,
) -> str:
    """Subscribe to Google Meet events for a specific space or all user spaces.

    Events: conference started/ended, participant joined/left,
    recording ready, transcript ready.

    Requires a Google Cloud Pub/Sub topic in pincer.toml for push delivery.
    Falls back to polling every 60 seconds if Pub/Sub is not configured.
    """
    if not space_name and not subscribe_all:
        return "Provide either space_name (e.g. spaces/xxx) or set subscribe_all=True."
    if subscribe_all:
        return await subscriber.subscribe_to_user()
    return await subscriber.subscribe_to_space(space_name)


async def google__list_meet_subscriptions(
    factory: GoogleServiceFactory,
    subscriber: MeetEventSubscriber,
) -> str:
    """List active Google Meet event subscriptions."""
    subs = subscriber.active_subscriptions
    if not subs:
        return "No active Meet event subscriptions."
    lines = [f"  Space: {space}\n    Subscription: {sub_id}" for space, sub_id in subs.items()]
    return fmt_list(lines, header=f"Active subscriptions ({len(lines)}):")


async def google__delete_meet_subscription(
    factory: GoogleServiceFactory,
    subscriber: MeetEventSubscriber,
    space_name: str,
) -> str:
    """Delete a Google Meet event subscription for a space."""
    return await subscriber.unsubscribe(space_name)


# ── Registry ──────────────────────────────────────────────────────────────────


def register_meet_tools(
    registry: ToolRegistry,
    factory: GoogleServiceFactory,
    subscriber: MeetEventSubscriber | None = None,
) -> int:
    """Register all 27 Google Meet tools in *registry*. Returns count."""
    from pincer.integrations.google.meet_events import MeetEventSubscriber as _Sub

    if subscriber is None:
        subscriber = _Sub(auth=factory.auth)

    def _h(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, **kwargs)

        return wrapper

    def _hs(fn: Callable[..., Any]) -> Callable[..., Any]:
        """Handler with subscriber injected alongside factory."""
        _sub = subscriber

        @functools.wraps(fn)
        async def wrapper(**kwargs):  # type: ignore[no-untyped-def]
            return await fn(factory, _sub, **kwargs)

        return wrapper

    # ── Space Management (8) ──────────────────────────────────────────────────
    registry.register(
        name="google__create_meet_space",
        description=(
            "Create a new Google Meet meeting space and return the meeting URL. "
            "access_type: OPEN=anyone with link, TRUSTED=same org only, RESTRICTED=invited only. "
            "Enable auto_recording, auto_transcription, auto_smart_notes for automatic artifacts."
        ),
        handler=_h(google__create_meet_space),
        parameters={
            "type": "object",
            "properties": {
                "access_type": {
                    "type": "string",
                    "enum": ["OPEN", "TRUSTED", "RESTRICTED"],
                    "default": "OPEN",
                },
                "auto_recording": {"type": "boolean", "default": False},
                "auto_transcription": {"type": "boolean", "default": False},
                "auto_smart_notes": {"type": "boolean", "default": False},
                "entry_point_access": {
                    "type": "string",
                    "enum": ["ALL", "CREATOR_APP_ONLY"],
                    "default": "ALL",
                },
            },
            "required": [],
        },
        require_approval=True,
    )
    registry.register(
        name="google__get_meet_space",
        description=(
            "Get details of a Google Meet space: meeting URL, code, config, active conference. "
            "Provide space_name (spaces/xxx) or meeting_code (abc-defg-hij)."
        ),
        handler=_h(google__get_meet_space),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {
                    "type": "string",
                    "description": "Space resource name (spaces/xxx)",
                    "default": "",
                },
                "meeting_code": {
                    "type": "string",
                    "description": "Meeting code (abc-defg-hij)",
                    "default": "",
                },
            },
            "required": [],
        },
    )
    registry.register(
        name="google__update_meet_space",
        description="Update a Google Meet space's access type or entry point access.",
        handler=_h(google__update_meet_space),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {"type": "string", "description": "Space resource name (spaces/xxx)"},
                "access_type": {
                    "type": "string",
                    "enum": ["OPEN", "TRUSTED", "RESTRICTED"],
                    "default": "",
                },
                "entry_point_access": {
                    "type": "string",
                    "enum": ["ALL", "CREATOR_APP_ONLY"],
                    "default": "",
                },
            },
            "required": ["space_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__end_active_conference",
        description=(
            "End the active conference in a meeting space — disconnects ALL participants immediately. "
            "Use with caution: only the meeting organiser can do this."
        ),
        handler=_h(google__end_active_conference),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {"type": "string", "description": "Space resource name (spaces/xxx)"},
            },
            "required": ["space_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__configure_meet_moderation",
        description=(
            "Configure who can chat, present, and react in a Google Meet space "
            "(Developer Preview — may not be available for all accounts). "
            "Restriction values: NO_RESTRICTION, HOST_AND_CO_HOSTS, ORGANIZER_ONLY"
        ),
        handler=_h(google__configure_meet_moderation),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {"type": "string"},
                "chat_restriction": {"type": "string", "default": ""},
                "reaction_restriction": {"type": "string", "default": ""},
                "present_restriction": {"type": "string", "default": ""},
                "default_join_as_viewer": {"type": "boolean", "default": False},
            },
            "required": ["space_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__configure_meet_artifacts",
        description=(
            "Pre-configure auto-recording, auto-transcription, and smart notes for a meeting space. "
            "More reliable than enabling during the meeting."
        ),
        handler=_h(google__configure_meet_artifacts),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {"type": "string"},
                "auto_recording": {"type": "boolean"},
                "auto_transcription": {"type": "boolean"},
                "auto_smart_notes": {"type": "boolean"},
            },
            "required": ["space_name"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__add_meet_member",
        description=(
            "Add a member to a Google Meet space with a role (Developer Preview). "
            "Roles: HOST, COHOST, MEMBER. Members can join without knocking."
        ),
        handler=_h(google__add_meet_member),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {"type": "string"},
                "email": {"type": "string", "description": "Member's email address"},
                "role": {
                    "type": "string",
                    "enum": ["HOST", "COHOST", "MEMBER"],
                    "default": "MEMBER",
                },
            },
            "required": ["space_name", "email"],
        },
        require_approval=True,
    )
    registry.register(
        name="google__remove_meet_member",
        description="Remove a member from a Google Meet space (Developer Preview).",
        handler=_h(google__remove_meet_member),
        parameters={
            "type": "object",
            "properties": {
                "member_name": {
                    "type": "string",
                    "description": "Member resource name (spaces/xxx/members/yyy)",
                },
            },
            "required": ["member_name"],
        },
        require_approval=True,
    )

    # ── Conference History (5) ─────────────────────────────────────────────────
    registry.register(
        name="google__list_conference_records",
        description=(
            "List past conference records. Optionally filter by space resource name. "
            "Returns conference IDs needed for retrieving participants, recordings, and transcripts."
        ),
        handler=_h(google__list_conference_records),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {
                    "type": "string",
                    "description": "Filter by space resource name",
                    "default": "",
                },
                "max_results": {"type": "integer", "default": 10},
            },
            "required": [],
        },
    )
    registry.register(
        name="google__get_conference_record",
        description="Get details of a specific past conference: start/end times, space, expiry.",
        handler=_h(google__get_conference_record),
        parameters={
            "type": "object",
            "properties": {
                "conference_name": {
                    "type": "string",
                    "description": "Conference resource name (conferenceRecords/xxx)",
                },
            },
            "required": ["conference_name"],
        },
    )
    registry.register(
        name="google__list_conference_participants",
        description="List all participants who joined a conference with join/leave times and identity.",
        handler=_h(google__list_conference_participants),
        parameters={
            "type": "object",
            "properties": {
                "conference_name": {
                    "type": "string",
                    "description": "Conference resource name (conferenceRecords/xxx)",
                },
                "max_results": {"type": "integer", "default": 50},
            },
            "required": ["conference_name"],
        },
    )
    registry.register(
        name="google__get_participant_details",
        description="Get detailed info for a conference participant including earliest/latest times.",
        handler=_h(google__get_participant_details),
        parameters={
            "type": "object",
            "properties": {
                "participant_name": {
                    "type": "string",
                    "description": "Participant resource name (conferenceRecords/xxx/participants/yyy)",
                },
            },
            "required": ["participant_name"],
        },
    )
    registry.register(
        name="google__list_participant_sessions",
        description="List all sessions for a participant (each join/leave pair, for participants who reconnected).",
        handler=_h(google__list_participant_sessions),
        parameters={
            "type": "object",
            "properties": {
                "participant_name": {
                    "type": "string",
                    "description": "Participant resource name",
                },
            },
            "required": ["participant_name"],
        },
    )

    # ── Recordings (4) ────────────────────────────────────────────────────────
    registry.register(
        name="google__list_meet_recordings",
        description=(
            "List recordings for a conference. "
            "States: STARTED=in progress, ENDED=processing, FILE_GENERATED=ready to download."
        ),
        handler=_h(google__list_meet_recordings),
        parameters={
            "type": "object",
            "properties": {
                "conference_name": {"type": "string"},
            },
            "required": ["conference_name"],
        },
    )
    registry.register(
        name="google__get_meet_recording",
        description="Get details of a specific recording including the Drive file ID for download.",
        handler=_h(google__get_meet_recording),
        parameters={
            "type": "object",
            "properties": {
                "recording_name": {"type": "string"},
            },
            "required": ["recording_name"],
        },
    )
    registry.register(
        name="google__download_meet_recording",
        description="Download a meeting recording MP4 from Google Drive to a local file path.",
        handler=_h(google__download_meet_recording),
        parameters={
            "type": "object",
            "properties": {
                "drive_file_id": {
                    "type": "string",
                    "description": "Drive file ID from recording metadata (driveDestination.file)",
                },
                "local_path": {
                    "type": "string",
                    "description": "Local path to save MP4 (e.g. /tmp/recording.mp4)",
                },
            },
            "required": ["drive_file_id", "local_path"],
        },
    )
    registry.register(
        name="google__check_recording_status",
        description=(
            "Poll the processing state of a meeting recording. "
            "Recordings take ~1x meeting duration to process. "
            "Call this after a meeting ends to know when the file is ready."
        ),
        handler=_h(google__check_recording_status),
        parameters={
            "type": "object",
            "properties": {
                "recording_name": {"type": "string"},
            },
            "required": ["recording_name"],
        },
    )

    # ── Transcripts (5) ───────────────────────────────────────────────────────
    registry.register(
        name="google__list_meet_transcripts",
        description="List transcripts for a conference (requires transcription to have been enabled).",
        handler=_h(google__list_meet_transcripts),
        parameters={
            "type": "object",
            "properties": {
                "conference_name": {"type": "string"},
            },
            "required": ["conference_name"],
        },
    )
    registry.register(
        name="google__get_meet_transcript",
        description="Get details of a specific transcript including the Google Docs document ID.",
        handler=_h(google__get_meet_transcript),
        parameters={
            "type": "object",
            "properties": {
                "transcript_name": {"type": "string"},
            },
            "required": ["transcript_name"],
        },
    )
    registry.register(
        name="google__list_transcript_entries",
        description=(
            "List transcript entries for a transcript — each entry is a spoken segment "
            "with speaker name, text, and timestamps."
        ),
        handler=_h(google__list_transcript_entries),
        parameters={
            "type": "object",
            "properties": {
                "transcript_name": {"type": "string"},
                "max_results": {"type": "integer", "default": 100},
            },
            "required": ["transcript_name"],
        },
    )
    registry.register(
        name="google__get_transcript_entry",
        description="Get a single transcript entry by resource name.",
        handler=_h(google__get_transcript_entry),
        parameters={
            "type": "object",
            "properties": {
                "entry_name": {"type": "string"},
            },
            "required": ["entry_name"],
        },
    )
    registry.register(
        name="google__summarize_meet_transcript",
        description=(
            "Fetch all transcript entries for a conference and compile a full speaker-attributed transcript. "
            "The agent's LLM summarizes it, extracts key decisions, and lists action items. "
            "This is the primary post-meeting analysis tool."
        ),
        handler=_h(google__summarize_meet_transcript),
        parameters={
            "type": "object",
            "properties": {
                "conference_name": {"type": "string"},
            },
            "required": ["conference_name"],
        },
    )

    # ── Smart Notes (2) ───────────────────────────────────────────────────────
    registry.register(
        name="google__list_smart_notes",
        description=(
            "List AI-generated smart notes for a conference (requires auto_smart_notes=True on the meeting space)."
        ),
        handler=_h(google__list_smart_notes),
        parameters={
            "type": "object",
            "properties": {
                "conference_name": {"type": "string"},
            },
            "required": ["conference_name"],
        },
    )
    registry.register(
        name="google__get_smart_notes",
        description="Get the smart notes document for a transcript. Returns the Docs document ID.",
        handler=_h(google__get_smart_notes),
        parameters={
            "type": "object",
            "properties": {
                "transcript_name": {"type": "string"},
            },
            "required": ["transcript_name"],
        },
    )

    # ── Event Subscriptions (3) ───────────────────────────────────────────────
    registry.register(
        name="google__subscribe_meet_events",
        description=(
            "Subscribe to Google Meet events for a space: conference started/ended, "
            "participant joined/left, recording ready, transcript ready. "
            "Requires Google Cloud Pub/Sub topic in pincer.toml for push delivery; "
            "falls back to polling every 60s if Pub/Sub is not configured."
        ),
        handler=_hs(google__subscribe_meet_events),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {
                    "type": "string",
                    "description": "Space resource name to subscribe to",
                    "default": "",
                },
                "subscribe_all": {
                    "type": "boolean",
                    "description": "Subscribe to all user spaces (requires Pub/Sub)",
                    "default": False,
                },
            },
            "required": [],
        },
        require_approval=True,
    )
    registry.register(
        name="google__list_meet_subscriptions",
        description="List active Google Meet event subscriptions.",
        handler=_hs(google__list_meet_subscriptions),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    registry.register(
        name="google__delete_meet_subscription",
        description="Delete a Google Meet event subscription for a space.",
        handler=_hs(google__delete_meet_subscription),
        parameters={
            "type": "object",
            "properties": {
                "space_name": {
                    "type": "string",
                    "description": "Space resource name to unsubscribe from",
                },
            },
            "required": ["space_name"],
        },
        require_approval=True,
    )

    return 27
