"""Tests for the call state machine."""

from __future__ import annotations

import math
import time

import pytest

from pincer.voice.state_machine import (
    PHASE_TIMEOUTS,
    UNTIMED_PHASES,
    VALID_TRANSITIONS,
    CallPhase,
    CallStateMachine,
)


class TestCallStateMachine:
    def test_initial_state(self):
        sm = CallStateMachine("CA123")
        assert sm.phase == CallPhase.RINGING
        assert sm.call_sid == "CA123"
        assert not sm.is_terminal

    def test_inbound_start(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        assert sm.phase == CallPhase.GREETING

    def test_outbound_start(self):
        sm = CallStateMachine("CA123", is_outbound=True)
        sm.start_call()
        assert sm.phase == CallPhase.OUTBOUND_GREETING

    def test_valid_transition(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        assert sm.transition(CallPhase.INTENT_CAPTURE, "user spoke")
        assert sm.phase == CallPhase.INTENT_CAPTURE

    def test_invalid_transition(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        result = sm.transition(CallPhase.EXECUTE, "skip to execute")
        assert not result
        assert sm.phase == CallPhase.GREETING

    def test_full_inbound_flow(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        assert sm.phase == CallPhase.GREETING

        sm.transition(CallPhase.INTENT_CAPTURE, "greeted")
        assert sm.phase == CallPhase.INTENT_CAPTURE

        sm.transition(CallPhase.VERIFY, "action needed")
        assert sm.phase == CallPhase.VERIFY

        sm.transition(CallPhase.EXECUTE, "user confirmed")
        assert sm.phase == CallPhase.EXECUTE

        sm.transition(CallPhase.CONFIRM, "tool done")
        assert sm.phase == CallPhase.CONFIRM

        sm.transition(CallPhase.ENDING, "nothing else")
        assert sm.phase == CallPhase.ENDING

        sm.transition(CallPhase.COMPLETED, "goodbye")
        assert sm.phase == CallPhase.COMPLETED
        assert sm.is_terminal

    def test_freeform_flow(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        sm.transition(CallPhase.INTENT_CAPTURE, "greeted")
        sm.transition(CallPhase.FREEFORM, "just chatting")
        assert sm.phase == CallPhase.FREEFORM

        sm.transition(CallPhase.ENDING, "done chatting")
        assert sm.phase == CallPhase.ENDING

    def test_error_recovery(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        sm.transition(CallPhase.INTENT_CAPTURE, "greeted")
        sm.transition(CallPhase.VERIFY, "action")
        sm.transition(CallPhase.EXECUTE, "confirmed")
        sm.transition(CallPhase.ERROR_RECOVERY, "tool failed")
        assert sm.phase == CallPhase.ERROR_RECOVERY

        sm.transition(CallPhase.INTENT_CAPTURE, "try again")
        assert sm.phase == CallPhase.INTENT_CAPTURE

    def test_pending_action(self):
        sm = CallStateMachine("CA123")
        sm.set_pending_action("calendar_create", {"title": "Meeting"}, "Create a meeting")
        assert sm.state.pending_action is not None
        assert sm.state.pending_action.tool_name == "calendar_create"

        action = sm.confirm_action()
        assert action is not None
        assert action.confirmed is True

    def test_reject_action(self):
        sm = CallStateMachine("CA123")
        sm.set_pending_action("email_send", {}, "Send email")
        action = sm.reject_action()
        assert action is not None
        assert action.confirmed is False

    def test_error_count(self):
        sm = CallStateMachine("CA123")
        assert not sm.record_error()
        assert not sm.record_error()
        assert sm.record_error()  # 3rd error = max exceeded

    def test_transition_log(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        sm.transition(CallPhase.INTENT_CAPTURE, "greeted user")
        assert len(sm.state.transitions) == 2
        assert sm.state.transitions[0].from_phase == CallPhase.RINGING
        assert sm.state.transitions[0].to_phase == CallPhase.GREETING

    def test_serialization(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        sm.transition(CallPhase.INTENT_CAPTURE, "test")
        data = sm.state.to_dict()
        assert data["call_sid"] == "CA123"
        assert data["phase"] == "intent_capture"
        assert len(data["transitions"]) == 2

    def test_completed_is_terminal(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        sm.transition(CallPhase.INTENT_CAPTURE)
        sm.transition(CallPhase.ENDING)
        sm.transition(CallPhase.COMPLETED)
        assert sm.is_terminal
        assert not sm.transition(CallPhase.GREETING)  # can't leave terminal

    def test_all_phases_have_valid_transitions(self):
        for phase in CallPhase:
            assert phase in VALID_TRANSITIONS

    def test_phase_instruction(self):
        sm = CallStateMachine("CA123")
        sm.start_call()
        instruction = sm.get_phase_instruction()
        assert len(instruction) > 0
        assert "greet" in instruction.lower() or "Greet" in instruction


class TestUntimedPhases:
    """A call bridged to a human via <Dial> is not ours to time (the 30 s
    transfer drop): Twilio ends it, the Dial timeLimit is the ceiling."""

    def test_untimed_phase_never_times_out(self):
        assert CallPhase.TRANSFERRING in UNTIMED_PHASES
        assert not UNTIMED_PHASES & PHASE_TIMEOUTS.keys()
        sm = CallStateMachine("CA123")
        sm._state.phase = CallPhase.TRANSFERRING
        sm._state.phase_entered_at = time.monotonic() - 100_000
        assert sm.check_timeout() is False
        assert sm.get_timeout_remaining() == math.inf

    def test_message_taking_can_go_back_to_a_late_applied_transfer(self):
        """A redirect given up as not applied can still go live: the call
        returns from TAKE_MESSAGE to the untimed TRANSFERRING phase."""
        assert CallPhase.TRANSFERRING in VALID_TRANSITIONS[CallPhase.TAKE_MESSAGE]
        sm = CallStateMachine("CA123")
        sm._state.phase = CallPhase.TAKE_MESSAGE
        sm._state.phase_entered_at = time.monotonic() - (PHASE_TIMEOUTS[CallPhase.TAKE_MESSAGE] + 1)
        assert sm.check_timeout() is True
        assert sm.transition(CallPhase.TRANSFERRING, "transfer_applied_late") is True
        sm._state.phase_entered_at = time.monotonic() - 100_000
        assert sm.check_timeout() is False

    @pytest.mark.parametrize(("phase", "timeout"), sorted(PHASE_TIMEOUTS.items()))
    def test_timed_phases_unchanged(self, phase, timeout):
        sm = CallStateMachine("CA123")
        sm._state.phase = phase
        sm._state.phase_entered_at = time.monotonic() - (timeout - 5)
        assert sm.check_timeout() is False
        assert 0 < sm.get_timeout_remaining() <= 5
        sm._state.phase_entered_at = time.monotonic() - (timeout + 1)
        assert sm.check_timeout() is True
        assert sm.get_timeout_remaining() == 0.0
