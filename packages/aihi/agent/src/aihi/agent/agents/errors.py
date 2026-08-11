"""Errors raised by the governed subagent coordinator."""

from __future__ import annotations

from aihi.agent._core.errors import AgentRuntimeError


class AgentError(AgentRuntimeError):
    code = "agent_error"


class AgentValidationError(AgentError):
    code = "agent_validation_error"


class AgentPermissionDenied(AgentError):
    code = "agent_permission_denied"


class AgentBudgetExceeded(AgentError):
    code = "agent_budget_exceeded"


class AgentDepthExceeded(AgentError):
    code = "agent_depth_exceeded"


class AgentStateError(AgentError):
    code = "agent_state_error"


class MailboxError(AgentError):
    code = "mailbox_error"


class MailboxConflict(MailboxError):
    code = "mailbox_conflict"
