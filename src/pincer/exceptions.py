"""Pincer exception hierarchy."""


class PincerError(Exception):
    """Base exception for all Pincer errors."""


class ConfigError(PincerError):
    """Configuration validation or loading failed."""


class LLMError(PincerError):
    """Error communicating with an LLM provider."""


class LLMRateLimitError(LLMError):
    """Rate limit hit — should retry with backoff."""

    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after}s" if retry_after else "Rate limited.")


class BudgetExceededError(PincerError):
    """Daily cost budget exceeded."""

    def __init__(self, spent: float, limit: float):
        self.spent = spent
        self.limit = limit
        super().__init__(f"Budget exceeded: ${spent:.4f} / ${limit:.2f}")


class ToolError(PincerError):
    """A tool execution failed."""


class ToolNotFoundError(ToolError):
    """Requested tool does not exist in registry."""


class ChannelError(PincerError):
    """Error in a messaging channel."""


class ShellBlockedError(ToolError):
    """A shell command was blocked by safety rules."""


class ChannelNotConnectedError(ChannelError):
    """Raised when attempting to use a channel that isn't connected."""


class ScheduleError(PincerError):
    """Raised for scheduler-related errors."""


class SkillLoadError(ToolError):
    """A skill failed to load (manifest, import, or validation error)."""


class RateLimitExceeded(PincerError):
    """User or global rate limit exceeded."""

    def __init__(self, message: str, wait_seconds: float = 0.0):
        self.message = message
        self.wait_seconds = wait_seconds
        super().__init__(message)
