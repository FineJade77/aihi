"""Canonical message grouping for context retention and summarization."""

from __future__ import annotations

from aihi.models import Message


def group_tool_exchanges(
    messages: tuple[Message, ...] | list[Message],
) -> tuple[tuple[Message, ...], ...]:
    """Return the smallest groups that never split a Tool Call from its Result.

    A coding run commonly has one user request followed by many
    assistant -> tool-result cycles. Treating that whole run as one semantic
    user turn makes it impossible to compact. These groups deliberately model
    execution boundaries instead: ordinary messages stand alone, while a
    message containing Tool Calls owns every following message needed to close
    those calls, including nested/parallel calls.
    """

    groups: list[tuple[Message, ...]] = []
    index = 0
    while index < len(messages):
        start = index
        pending = {call.id for call in messages[index].tool_calls}
        pending.difference_update(
            result.tool_call_id for result in messages[index].tool_results
        )
        index += 1
        while pending and index < len(messages):
            message = messages[index]
            pending.difference_update(result.tool_call_id for result in message.tool_results)
            pending.update(call.id for call in message.tool_calls)
            index += 1
        groups.append(tuple(messages[start:index]))
    return tuple(groups)


__all__ = ["group_tool_exchanges"]
