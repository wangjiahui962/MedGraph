"""Project-specific errors with stable CLI exit semantics."""

from typing import Optional


class MedGraphError(Exception):
    """Base class for expected project errors."""


class ConfigurationError(MedGraphError):
    """The catalog, configuration, or command arguments are invalid."""


class InputRequiredError(MedGraphError):
    """A human must provide an authorized export or finish a login/CAPTCHA."""


class SourceError(MedGraphError):
    """A source could not be read or contacted."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class ParseError(MedGraphError):
    """A source export exists but does not match the supported format."""


class ResumeMismatchError(MedGraphError):
    """A checkpoint belongs to a different configuration or code contract."""


class RunLockedError(MedGraphError):
    """Another process is already mutating the same run."""


class PublicationConflictError(MedGraphError):
    """The current published generation changed before this run activated."""


class QualityGateError(MedGraphError):
    """The collected corpus does not meet configured acceptance gates."""
