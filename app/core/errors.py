"""Domain exceptions."""


class BillingBotError(Exception):
    """Base class for application errors."""


class UnsupportedDocumentError(BillingBotError):
    """The uploaded file type cannot be processed."""


class DocumentTooLargeError(BillingBotError):
    """The uploaded file exceeds the configured size limit."""


class ExtractionError(BillingBotError):
    """AI extraction failed after all retries and fallbacks."""


class NotFoundError(BillingBotError):
    """A requested resource does not exist (or belongs to another tenant)."""


class RateLimitedError(BillingBotError):
    """The sender exceeded their request quota."""
