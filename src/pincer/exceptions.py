"""Pincer exception hierarchy."""


class PincerError(Exception):
    """Base exception for all Pincer errors."""


class ConfigError(PincerError):
    """Configuration validation or loading failed."""


class LLMError(PincerError):
    """Error communicating with an LLM provider.

    ``retryable``: the failure is transient (a rate limit, an overload, a server error), so the
    same request may succeed when asked again; a 4xx or a connection failure is not."""

    def __init__(self, message: str = "", *, retryable: bool = False) -> None:
        self.retryable = retryable
        super().__init__(message)


class DailyBudgetReachedError(PincerError):
    """An LLM request was refused before it was sent: today's recorded spend is at the daily budget."""


class BudgetCheckFailedError(PincerError):
    """An LLM request was refused before it was sent: the daily budget could not be read."""


class LLMRateLimitError(LLMError):
    """Rate limit hit — should retry with backoff."""

    def __init__(self, retry_after: float | None = None):
        self.retry_after = retry_after
        super().__init__(
            f"Rate limited. Retry after {retry_after}s" if retry_after else "Rate limited.", retryable=True
        )


class LLMOutputTruncatedError(LLMError):
    """The response stopped on max_tokens and cannot be used: a tool call may be cut off, or there is no text."""

    def __init__(self, output_tokens: int, max_tokens: int):
        self.output_tokens = output_tokens
        self.max_tokens = max_tokens
        super().__init__(f"LLM output cut off at max_tokens ({output_tokens} of {max_tokens} tokens)")


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
