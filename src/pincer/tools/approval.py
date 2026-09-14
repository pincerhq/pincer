"""Tool approval flow.

Every write-capable tool declares ``require_approval=True`` at registration.
That flag records *intent* ("this tool changes something"), not policy — with
300+ native tools, gating every one of them behind an in-chat ✅/❌ makes the
agent unusable for ordinary work.

This module turns the declared flag into an *effective* decision.  The single
question point is :meth:`ApprovalPolicy.requires_approval`, called from
``ToolRegistry.requires_approval`` — so the agent loop, the dashboard API and
the MCP exporter all see the same answer.

Modes
-----
``all``       Honour the declared flag verbatim (pre-policy behaviour).
``critical``  Approve only tools whose effects are hard to undo or reach a
              third party.  Reversible writes run unattended.  (default)
``none``      Never prompt.  Only for trusted, non-interactive deployments.

Classification is **fail-closed**: a declared tool keeps its gate unless its
name appears in :data:`NON_CRITICAL_TOOLS`.  A newly added tool — or any
MCP-bridged tool whose ``approval_required`` pattern the operator configured
in ``pincer.toml`` — is therefore treated as critical until someone
deliberately classifies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ApprovalMode = Literal["all", "critical", "none"]

APPROVAL_MODES: frozenset[str] = frozenset({"all", "critical", "none"})

# Tools that declare require_approval=True but whose effects are reversible and
# confined to the user's own workspace: creating and editing documents, filing
# drafts, organising files, booking and amending calendar entries, workspace
# housekeeping.  Under mode "critical" these run without a prompt.
#
# Deliberately NOT listed (kept behind approval): anything that sends to a
# third party (mail, Slack posts, phone calls), anything that deletes or
# empties, anything that changes who can see what (sharing, membership,
# recording/moderation config), and anything that executes code.
NON_CRITICAL_TOOLS: frozenset[str] = frozenset(
    {
        # ── Calendar: create/amend/respond (delete stays critical) ──
        "calendar_create",
        "google__accept_event",
        "google__add_google_meet",
        "google__create_event",
        "google__decline_event",
        "google__move_event",
        "google__update_event",
        "outlook__accept_event",
        "outlook__create_event",
        "outlook__decline_event",
        "outlook__tentative_event",
        "outlook__update_event",
        # ── Contacts: create/amend (delete stays critical) ──
        "google__create_contact",
        "google__update_contact",
        "outlook__create_contact",
        "outlook__update_contact",
        # ── Mail: drafts and filing, never sending ──
        "email_move",
        "google__create_draft",
        "google__create_label",
        "google__untrash_message",
        "outlook__create_draft",
        "outlook__move_message",
        "outlook__update_draft",
        # ── Files: create/organise (delete and share stay critical) ──
        "google__copy_file",
        "google__create_folder",
        "google__move_file",
        "google__rename_file",
        "google__upload_file",
        "onedrive__copy_file",
        "onedrive__create_folder",
        "onedrive__move_file",
        "onedrive__rename_file",
        "onedrive__upload_file",
        # ── Docs ──
        "google__add_comment",
        "google__create_doc",
        "google__insert_table",
        "google__insert_text",
        "google__replace_text",
        "google__update_paragraph_style",
        # ── Sheets (clear_sheet_values stays critical: data loss) ──
        "google__add_sheet",
        "google__append_sheet_values",
        "google__create_spreadsheet",
        "google__format_cells",
        "google__update_sheet_values",
        # ── Slides ──
        "google__add_image_to_slide",
        "google__add_slide",
        "google__create_presentation",
        "google__update_slide_text",
        # ── Tasks / to-do (delete stays critical) ──
        "google__complete_task",
        "google__create_task",
        "google__create_task_list",
        "google__update_task",
        "ms_todo__complete_task",
        "ms_todo__create_task",
        "ms_todo__create_task_list",
        "ms_todo__update_task",
        # ── OneNote ──
        "onenote__create_page",
        # ── Meet: space setup only; membership, moderation, artifacts and
        #    ending a live conference stay critical ──
        "google__create_meet_space",
        "google__update_meet_space",
        # ── Slack: workspace housekeeping, nothing others read as a message ──
        "slack__add_bookmark",
        "slack__create_channel",
        "slack__create_reminder",
        "slack__create_user_group",
        "slack__join_channel",
        "slack__leave_channel",
        "slack__pin_message",
        "slack__remove_bookmark",
        "slack__rename_channel",
        "slack__set_channel_purpose",
        "slack__set_channel_topic",
        "slack__set_user_status",
        "slack__unarchive_channel",
        "slack__unpin_message",
        "slack__update_user_group",
        # ── The user's own agent config ──
        # Note: send_owner_message is deliberately NOT here — it delivers a
        # message to a person over a channel, and nothing that sends is ungated.
        "memory_note",
        "schedule_create",
        "schedule_remove",
        "schedule_toggle",
    }
)


def parse_tool_names(value: object) -> frozenset[str]:
    """Normalise a tool-name override into a set.

    Accepts a comma-separated string (the env form) or any iterable of names.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(name).strip() for name in value if str(name).strip())
    return frozenset()


def is_critical(name: str) -> bool:
    """Whether a write-capable tool is critical enough to need approval.

    Fail-closed: unknown names are critical.
    """
    return name not in NON_CRITICAL_TOOLS


@dataclass(frozen=True)
class ApprovalPolicy:
    """Resolves declared approval flags into effective ones."""

    mode: ApprovalMode = "critical"
    # Per-deployment overrides, applied before the mode. `never` wins over
    # `always` only in the sense that it is checked first — keep them disjoint.
    always: frozenset[str] = field(default_factory=frozenset)
    never: frozenset[str] = field(default_factory=frozenset)

    def requires_approval(self, name: str, *, declared: bool) -> bool:
        """Effective approval decision for ``name``.

        ``declared`` is the tool's registration-time flag.  A tool that never
        declared approval is never escalated here — a deployment that wants to
        gate one anyway lists it in ``always``.
        """
        if name in self.never:
            return False
        if name in self.always:
            return True
        if self.mode == "none":
            return False
        if not declared:
            return False
        if self.mode == "all":
            return True
        return is_critical(name)

    @classmethod
    def from_settings(cls, settings: object) -> ApprovalPolicy:
        """Build a policy from a ``Settings``-like object.

        Tolerates objects missing the fields (older configs, test doubles) by
        falling back to the defaults.
        """
        mode = getattr(settings, "approval_policy", "critical")
        if not isinstance(mode, str) or mode not in APPROVAL_MODES:
            mode = "critical"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            always=parse_tool_names(getattr(settings, "approval_always", None)),
            never=parse_tool_names(getattr(settings, "approval_never", None)),
        )
