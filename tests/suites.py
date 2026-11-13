"""Which CI suite each test file belongs to.

CI runs `tests/` as four parallel jobs, one per suite (`pytest --suite=NAME`,
see the `--suite` option in conftest.py). A file belongs to the first suite
whose patterns match it; `core` is every file no pattern matches, so a new test
file always lands in exactly one suite.

Patterns are fnmatch patterns over the file's path relative to `tests/`,
without the `.py` suffix (`*` also matches `/`). Rebalance by measured time
(`pytest --durations=0`), not by file count.
"""

from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

SUITES: dict[str, list[str]] = {
    "voice": [
        "test_voice*",
        "voice_harness/*",
        "test_telephony*",
        "test_receptionist*",
        "test_api_voice",
        "test_in_call_tools",
        "test_call_tools",
        "test_tool_speech",
        "test_public_demo_call",
        "test_llm_router_voice_target",
        "test_ngrok_tunnel",
    ],
    "mcp-cli": [
        "test_mcp*",
        "mcp/*",
        "test_memory_mcp",
        "test_cli*",
        "test_daemon*",
        "test_pilot_tooling",
    ],
    "llm": [
        "test_llm",
        "test_provider_*",
        "test_anthropic*",
        "test_openai*",
        "test_*caching*",
        "test_thinking_blocks",
        "test_thinking_across_turns",
        "test_interrupted_tool_calls",
        "test_message_raw_output",
        "test_endpoint_capabilities",
        "test_endpoint_fallback",
        "test_reasoning*",
        "test_model_params",
        "test_structured_call_pins",
        "test_smoke_llm*",
        "test_compaction",
        "test_server_compaction",
        "test_hard_cut",
        "test_pricing*",
        "test_router",
        "test_image_router",
        "test_request_model",
        "test_cache_token_recording",
        "test_cost*",
        "test_budget_gate",
        "test_stable_system_prompt",
        "test_output_truncation",
        "test_agent",
        "test_image_gen_tool",
        "test_photos_stay*",
        "test_prepare_image",
        "test_tool_selection",
        "test_tool_index_multilingual",
    ],
}

CORE = "core"
ALL = "all"
NAMES = [ALL, CORE, *SUITES]


def suite_of(path: Path) -> str:
    """The suite a test file under `tests/` belongs to."""
    rel = path.resolve().relative_to(TESTS_DIR).with_suffix("").as_posix()
    for name, patterns in SUITES.items():
        if any(fnmatch(rel, pattern) for pattern in patterns):
            return name
    return CORE
