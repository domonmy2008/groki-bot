# SPDX-License-Identifier: MIT
import asyncio
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from stackchan_mcp.gbot_voice_bridge import GbotVoiceBridge


class Executor:
    def __init__(self):
        self.execute = AsyncMock(return_value={"ok": True})
        self.close = AsyncMock()
        self.observe_user_input = AsyncMock()

    def declarations(self):
        return [
            {
                "name": "self.tracking.start",
                "description": "Track",
                "parameters": {"type": "object"},
            },
            {
                "name": "ask_grokbot",
                "description": "Delegate",
                "parameters": {
                    "type": "object",
                    "properties": {"task": {"type": "string"}},
                    "required": ["task"],
                },
            },
        ]

    def instructions(self, **kwargs):
        assert kwargs == {"model_search": False}
        return "Shared local instructions"


def make(monkeypatch, enabled=True):
    monkeypatch.setenv("STACKCHAN_TOOL_BOT", "desktop" if enabled else "")
    monkeypatch.setenv("STACKCHAN_TOOL_BOT_ID", "agent")
    b = GbotVoiceBridge(
        on_audio=AsyncMock(),
        on_turn_complete=AsyncMock(),
        on_interrupt=AsyncMock(),
        on_session_dead=AsyncMock(),
        agent_id="agent",
        tool_executor=Executor(),
    )
    b._send = AsyncMock()
    b._native = SimpleNamespace(ready=True, close=AsyncMock())
    b.running = True
    b._configure_tools(
        {
            "tools": [
                {
                    "type": "function",
                    "name": "send_task",
                    "parameters": {
                        "type": "object",
                        "properties": {"request": {"type": "string"}},
                        "required": ["request"],
                    },
                }
            ]
        }
    )
    return b


@pytest.mark.asyncio
async def test_advertised_alias_dispatches_original_local_name(monkeypatch):
    b = make(monkeypatch)
    name = next(
        name
        for name, original in b._local_tools.items()
        if original == "self.tracking.start"
    )
    assert "." not in name
    await b._tool({"name": name, "call_id": "one", "arguments": "{}"})
    b._executor.execute.assert_awaited_once_with("self.tracking.start", {})
    await b.stop()


@pytest.mark.asyncio
async def test_native_tool_once_per_real_user_and_no_result_recursion(monkeypatch):
    b = make(monkeypatch)
    b._local = AsyncMock(return_value={"receipt": "accepted"})
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    event = {"name": "send_task", "call_id": "one", "arguments": '{"request":"lookup"}'}
    await b._tool(event)
    await b._tool(event)
    assert b._local.await_count == 1
    assert b._local.call_args.args[1]["name"] == "send_task"
    assert b._local.call_args.args[1]["input"] == {"request": "lookup"}
    await b._native_update(
        {
            "type": "async_update.add",
            "async_update": {"topic": "work_landed", "payload": []},
        },
        False,
    )
    await b._tool({**event, "call_id": "two"})
    assert b._local.await_count == 1
    b._executor.execute.assert_not_awaited()
    await b.stop()


@pytest.mark.asyncio
async def test_slow_tool_does_not_block_interrupt_and_stop_drops_late_results(
    monkeypatch,
):
    b = make(monkeypatch)
    entered = asyncio.Event()

    async def slow(*args):
        entered.set()
        await asyncio.Event().wait()

    b._executor.execute = slow
    name = next(iter(b._local_tools))
    await b._handle_event(
        {
            "type": "response.function_call_arguments.done",
            "name": name,
            "call_id": "slow",
            "arguments": "{}",
        }
    )
    await entered.wait()
    await asyncio.wait_for(
        b._handle_event({"type": "input_audio_buffer.speech_started"}), 0.2
    )
    await b.stop()
    count = b._send.await_count
    await b._native_update({"type": "async_update.add"}, False)
    assert b._send.await_count == count


@pytest.mark.asyncio
async def test_function_result_waits_for_active_response_before_create(monkeypatch):
    b = make(monkeypatch)
    await b._handle_event({"type": "response.created", "response": {"id": "active"}})
    await b._tool(
        {"name": next(iter(b._local_tools)), "call_id": "tool", "arguments": "{}"}
    )
    assert not any(
        c.args[0]["type"] == "response.create" for c in b._send.call_args_list
    )
    await b._handle_event({"type": "response.done", "response": {"id": "active"}})
    assert any(c.args[0]["type"] == "response.create" for c in b._send.call_args_list)
    await b.stop()


@pytest.mark.asyncio
async def test_internal_speech_does_not_grant_desktop_permission(monkeypatch):
    b = make(monkeypatch)
    b._local = AsyncMock()
    await b.inject_user_text("read a task result")
    await b._tool(
        {"name": "send_task", "call_id": "one", "arguments": '{"request":"do more"}'}
    )
    b._local.assert_not_awaited()
    await b.stop()


def test_desktop_requires_explicit_matching_target(monkeypatch):
    b = make(monkeypatch, enabled=False)
    assert "ask_grokbot" not in b._local_tools
    assert "send_task" not in b._remote_tools


def test_alias_collision_fails_closed(monkeypatch):
    b = make(monkeypatch)
    b._executor.declarations = lambda: [
        {"name": "self.a", "parameters": {}},
        {"name": "self_a", "parameters": {}},
    ]
    with pytest.raises(ValueError, match="collision"):
        b._configure_tools({})


def test_diagnostics_disable_even_native_end_and_extra_tools(monkeypatch):
    b = make(monkeypatch)
    b._local_tools_enabled = False
    b._on_end_conversation = AsyncMock()
    monkeypatch.setenv("STACKCHAN_GBOT_VOICE_TOOLS", "1")
    assert (
        b._configure_tools(
            {"tools": [{"name": "end_the_call"}, {"name": "search_conversations"}]}
        )
        == []
    )


@pytest.mark.asyncio
async def test_native_update_waits_for_user_turn_and_never_creates_response(
    monkeypatch,
):
    b = make(monkeypatch)
    await b._handle_event({"type": "response.created", "response": {"id": "speech"}})
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    b._send.assert_not_awaited()
    await b._handle_event({"type": "response.done", "response": {"id": "speech"}})
    b._send.assert_awaited_once_with(update)
    await b.stop()


@pytest.mark.asyncio
async def test_native_task_survives_ordinary_interrupt(monkeypatch):
    b = make(monkeypatch)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def send(*args):
        entered.set()
        await finish.wait()
        return {"receipt": "accepted"}

    b._local = send
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._handle_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "send_task",
            "call_id": "task",
            "arguments": '{"request":"fixture"}',
        }
    )
    await entered.wait()
    await b.interrupt()
    assert any(not task.done() for task in b._tools)
    finish.set()
    await asyncio.gather(*b._tools)
    assert any(
        call.args[0]["type"] == "conversation.item.create"
        for call in b._send.call_args_list
    )
    await b.stop()


def test_executor_factory_gets_live_callback_options(monkeypatch):
    b = make(monkeypatch)
    b._executor = None
    observed = {}

    def factory(**options):
        observed.update(options)
        return Executor()

    b._executor_factory = factory
    b._configure_tools({})
    assert observed["announce_result"] == b._announce_result
    assert observed["cancel_idle"] == b._cancel_idle
    assert "wake_gate_state_getter" in observed


@pytest.mark.asyncio
async def test_idle_callback_uses_same_end_hook_and_keepalive_does_not_cancel(
    monkeypatch,
):
    b = make(monkeypatch)
    b._on_end_conversation = AsyncMock()
    b._conversation_idle_timeout_s = 0.01
    await b._finish_turn()
    await b.send_keepalive_audio(b"\0\0" * 160)
    await asyncio.sleep(0.03)
    b._on_end_conversation.assert_awaited_once()
    await b.stop()


@pytest.mark.asyncio
async def test_real_input_cancels_idle_and_is_forwarded_to_extensions(monkeypatch):
    b = make(monkeypatch)
    b._on_end_conversation = AsyncMock()
    b._conversation_idle_timeout_s = 0.01
    await b._finish_turn()
    await b.send_text("real request")
    await asyncio.sleep(0.03)
    b._on_end_conversation.assert_not_awaited()
    b._executor.observe_user_input.assert_awaited_once_with(
        "real request", finished=True
    )
    await b.stop()


@pytest.mark.asyncio
async def test_native_tool_accepts_schema_allowed_extra_fields_without_forwarding_them(
    monkeypatch,
):
    b = make(monkeypatch)
    b._local = AsyncMock(
        return_value={"kind": "served", "output": {"status": "accepted"}}
    )
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "one",
            "arguments": '{"request":"offline fixture","language":"zh"}',
        }
    )
    b._local.assert_awaited_once()
    assert b._local.call_args.args[1]["input"] == {"request": "offline fixture"}
    await b.stop()


@pytest.mark.asyncio
async def test_native_tool_obeys_explicit_additional_properties_false(monkeypatch):
    b = make(monkeypatch)
    b._tool_schemas["send_task"]["additionalProperties"] = False
    b._local = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "one",
            "arguments": '{"request":"offline fixture","language":"zh"}',
        }
    )
    b._local.assert_not_awaited()
    await b.stop()


@pytest.mark.asyncio
async def test_tool_diagnostics_do_not_log_arguments_ids_or_unknown_names(
    monkeypatch, caplog
):
    import logging

    b = make(monkeypatch)
    caplog.set_level(logging.INFO, logger="stackchan_mcp.gbot_voice_bridge")
    await b._handle_event(
        {
            "type": "response.function_call_arguments.done",
            "name": "UNTRUSTED_NAME_SENTINEL",
            "call_id": "PRIVATE_ID_SENTINEL",
            "arguments": '{"request":"PRIVATE_TASK_SENTINEL"}',
        }
    )
    assert b.metrics["tool_events"] == {"other": 1}
    assert b.metrics["tool_outcomes"] == {"other:undeclared": 1}
    assert "SENTINEL" not in caplog.text
    await b.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "native,expected",
    [
        ({"kind": "served", "output": {"relay": "accepted"}}, "accepted"),
        (
            {
                "kind": "served",
                "output": {"relay": "refused", "spoken": "Cannot accept this task."},
            },
            "refused",
        ),
        (
            {
                "kind": "served",
                "output": {
                    "relay": "unreachable",
                    "spoken": "Desktop is unreachable.",
                    "errorClass": "network",
                },
            },
            "unreachable",
        ),
        ({"kind": "invalid_input", "output": {"relay": "accepted"}}, "unreachable"),
        ({"kind": "unknown_tool"}, "unreachable"),
        ({"kind": "served", "output": {"ok": True}}, "unreachable"),
    ],
)
async def test_native_task_uses_client_relay_outcome_not_http_success(
    monkeypatch, native, expected
):
    import json

    b = make(monkeypatch)
    b._local = AsyncMock(return_value=native)
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "task",
            "arguments": '{"request":"offline fixture"}',
        }
    )
    output = next(
        c.args[0]["item"]["output"]
        for c in b._send.call_args_list
        if c.args[0]["type"] == "conversation.item.create"
    )
    parsed = json.loads(output)
    assert parsed["relay"] == expected
    assert "kind" not in parsed and "output" not in parsed
    if expected != "accepted":
        assert parsed.get("spoken")
    await b.stop()


@pytest.mark.asyncio
async def test_native_task_transport_failure_is_not_accepted(monkeypatch):
    import json

    b = make(monkeypatch)
    b._local = AsyncMock(side_effect=RuntimeError("PRIVATE_FAILURE_SENTINEL"))
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool(
        {
            "name": "send_task",
            "call_id": "task",
            "arguments": '{"request":"offline fixture"}',
        }
    )
    raw = next(
        c.args[0]["item"]["output"]
        for c in b._send.call_args_list
        if c.args[0]["type"] == "conversation.item.create"
    )
    assert json.loads(raw)["relay"] == "unreachable"
    assert "PRIVATE_FAILURE_SENTINEL" not in raw
    await b.stop()


def test_grok_advertises_native_desktop_tool_without_local_alias(monkeypatch):
    b = make(monkeypatch)
    assert "send_task" in b._remote_tools
    assert "ask_grokbot" not in b._local_tools


def test_native_end_replaces_duplicate_local_end_only_when_available(monkeypatch):
    b = make(monkeypatch)
    b._on_end_conversation = AsyncMock()
    original = b._executor.declarations()
    b._executor.declarations = lambda: [
        *original,
        {"name": "end_conversation", "parameters": {"type": "object"}},
    ]
    declarations = b._configure_tools(
        {"tools": [{"name": "end_the_call", "parameters": {"type": "object"}}]}
    )
    names = {item["name"] for item in declarations}
    assert "end_the_call" in names and "end_conversation" not in names
    declarations = b._configure_tools({"tools": []})
    assert "end_conversation" in {item["name"] for item in declarations}


@pytest.mark.parametrize("legacy_flag", ["0", "1"])
def test_known_native_search_is_enabled_without_opening_unknown_tools(
    monkeypatch, legacy_flag
):
    b = make(monkeypatch)
    monkeypatch.setenv("STACKCHAN_GBOT_VOICE_TOOLS", legacy_flag)
    b._configure_tools(
        {
            "tools": [
                {"name": "send_task", "parameters": {"type": "object"}},
                {"name": "search_conversations", "parameters": {"type": "object"}},
                {"name": "unknown_remote_action", "parameters": {"type": "object"}},
            ]
        }
    )
    assert set(b._remote_tools) == {"send_task", "search_conversations"}


def test_grok_drops_legacy_wrapper_routing_without_changing_local_instructions(
    monkeypatch,
):
    b = make(monkeypatch)
    source = "Use move_head for hardware.\nask_grokbot returns at once. Say sent immediately.\nUse ask_grokbot(task) for tasks."
    b._executor.instructions = lambda **kwargs: source
    instructions = b._provider_tool_instructions()
    assert "ask_grokbot" not in instructions and "returns at once" not in instructions
    assert "Use move_head for hardware." in instructions
    assert "relay=accepted" in instructions
    assert b._executor.instructions() == source


def test_grok_omits_entire_legacy_desktop_instruction_section(monkeypatch):
    b = make(monkeypatch)
    b._executor.instructions = lambda **kwargs: (
        "Hardware instructions.\n\n## Handing tasks to desktop\n\n"
        "Call ask_grokbot(task).\nWhen desktop answers you get a system notice.\n\n"
        "Other local tool instructions."
    )
    instructions = b._provider_tool_instructions()
    assert "system notice" not in instructions
    assert "Hardware instructions." in instructions
    assert "Other local tool instructions." in instructions


@pytest.mark.asyncio
async def test_native_update_without_model_response_does_not_block_later_completion(
    monkeypatch,
):
    b = make(monkeypatch)
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    await b._native_update(update, False)
    assert b._send.await_count == 2
    assert b._response_requested is False
    assert not b._pending_updates
    await b.stop()


@pytest.mark.asyncio
async def test_completion_queued_during_user_speech_flushes_at_speech_stop(monkeypatch):
    b = make(monkeypatch)
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    b._send.assert_not_awaited()
    await b._handle_event({"type": "input_audio_buffer.speech_stopped"})
    b._send.assert_awaited_once_with(update)
    assert not b._pending_updates
    await b.stop()


@pytest.mark.asyncio
async def test_native_notification_waits_for_already_requested_manual_response(
    monkeypatch,
):
    b = make(monkeypatch)
    b._response_requested = True
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    await b._native_update(update, False)
    b._send.assert_not_awaited()
    assert b._response_requested is True
    assert len(b._pending_updates) == 1
    await b._handle_event({"type": "response.created", "response": {"id": "manual"}})
    await b._handle_event({"type": "response.done", "response": {"id": "manual"}})
    b._send.assert_awaited_once_with(update)
    await b.stop()


@pytest.mark.asyncio
async def test_native_delivery_and_response_logs_are_content_free(monkeypatch, caplog):
    import logging

    b = make(monkeypatch)
    caplog.set_level(logging.INFO, logger="stackchan_mcp.gbot_voice_bridge")
    update = {
        "type": "async_update.add",
        "event_id": "PRIVATE_EVENT_SENTINEL",
        "async_update": {
            "topic": "work_landed",
            "payload": ["PRIVATE_RESULT_SENTINEL"],
        },
    }
    await b._native_update(update, False)
    await b._handle_event(
        {
            "type": "response.created",
            "response": {
                "id": "PRIVATE_RESPONSE_SENTINEL",
                "metadata": {"trigger": "async_update"},
            },
        }
    )
    await b._handle_event(
        {
            "type": "response.audio.delta",
            "response_id": "PRIVATE_RESPONSE_SENTINEL",
            "delta": "AAA=",
        }
    )
    assert b.metrics["native_updates"]["work_landed:sent"] == 1
    assert b.metrics["response_triggers"]["async_update"] == 1
    assert "state=first_audio trigger=async_update" in caplog.text
    assert "SENTINEL" not in caplog.text
    await b.stop()


@pytest.mark.asyncio
async def test_existing_manual_request_precedes_native_without_starvation(monkeypatch):
    b = make(monkeypatch)
    b._pending_response = True
    update = {
        "type": "async_update.add",
        "async_update": {"topic": "work_landed", "payload": []},
    }
    b._pending_updates.append(update)
    await b._flush_pending()
    b._send.assert_awaited_once_with({"type": "response.create"})
    assert b._response_requested is True and not b._pending_response
    assert b._pending_updates == [update]
    await b._handle_event({"type": "response.created", "response": {"id": "manual"}})
    await b._handle_event({"type": "response.done", "response": {"id": "manual"}})
    assert [call.args[0]["type"] for call in b._send.call_args_list] == [
        "response.create",
        "async_update.add",
    ]
    assert not b._pending_updates and not b._response_requested
    await b.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [("send_task", '{"request":"fixture"}'), ("search_conversations", "{}")],
)
async def test_native_not_ready_rejects_desktop_tools_without_post(
    monkeypatch, name, args
):
    from types import SimpleNamespace

    b = make(monkeypatch)
    b._remote_tools["search_conversations"] = {"parameters": {"type": "object"}}
    b._native = SimpleNamespace(ready=False, close=AsyncMock())
    b._local = AsyncMock()
    await b._handle_event({"type": "input_audio_buffer.speech_started"})
    await b._tool({"name": name, "call_id": "offline", "arguments": args})
    b._local.assert_not_awaited()
    assert b.metrics["tool_outcomes"][name + ":native_not_ready"] == 1
    await b.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [("send_task", '{"request":"fixture"}'), ("search_conversations", "{}")],
)
async def test_unavailable_desktop_has_readable_chinese_without_diagnostic_code(
    monkeypatch, name, args
):
    import json

    b = make(monkeypatch)
    b._remote_tools["search_conversations"] = {"parameters": {"type": "object"}}
    b._native.ready = False
    await b._tool({"name": name, "call_id": "offline", "arguments": args})
    raw = next(
        call.args[0]["item"]["output"]
        for call in b._send.call_args_list
        if call.args[0]["type"] == "conversation.item.create"
    )
    output = json.loads(raw)
    assert output["spoken"] == "桌面助手暂时未连上，请稍后再试。"
    assert "failureCode" not in output and "native_not_ready" not in raw
    assert b.metrics["tool_outcomes"][name + ":native_not_ready"] == 1
    assert "spoken" in b._provider_tool_instructions()
    await b.stop()

@pytest.mark.asyncio
async def test_end_the_call_acks_tool_without_requesting_spoken_response(monkeypatch):
    """Hangup must not trigger a follow-up spoken turn (e.g. Line dropped)."""
    b = make(monkeypatch)
    b._on_end_conversation = AsyncMock()
    b._configure_tools(
        {"tools": [{"name": "end_the_call", "parameters": {"type": "object"}}]}
    )
    await b._tool({"name": "end_the_call", "call_id": "hangup", "arguments": "{}"})
    b._on_end_conversation.assert_awaited_once()
    types = [call.args[0].get("type") for call in b._send.call_args_list]
    assert "conversation.item.create" in types
    assert "response.create" not in types
    assert b._drop_until_response is True
    await b.stop()

