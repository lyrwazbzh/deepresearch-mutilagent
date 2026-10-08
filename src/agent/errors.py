from __future__ import annotations


class DomainError(RuntimeError):
    code = "domain_error"
    status_code = 400
    title = "Domain error"
    retryable = False

    def __init__(self, detail: str, *, errors: list[dict[str, str]] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors


class UnauthenticatedError(DomainError):
    code = "unauthenticated"
    status_code = 401
    title = "Unauthenticated"


class InvalidRefreshSessionError(UnauthenticatedError):
    code = "invalid_refresh_session"
    title = "Invalid refresh session"


class ForbiddenError(DomainError):
    code = "forbidden"
    status_code = 403
    title = "Forbidden"


class EventCursorExpiredError(DomainError):
    code = "event_cursor_expired"
    status_code = 409
    title = "Event cursor expired"


class ExportExpiredError(DomainError):
    code = "export_expired"
    status_code = 410
    title = "Export expired"


class RateLimitedError(DomainError):
    code = "rate_limited"
    status_code = 429
    title = "Rate limited"
    retryable = True


class RangeNotSatisfiableError(DomainError):
    code = "range_not_satisfiable"
    status_code = 416
    title = "Range not satisfiable"


class NotFoundError(DomainError):
    code = "not_found"
    status_code = 404
    title = "Not found"


class IdempotencyConflictError(DomainError):
    code = "idempotency_conflict"
    status_code = 409
    title = "Idempotency conflict"


class ConflictError(DomainError):
    code = "conflict"
    status_code = 409
    title = "Conflict"


class IdempotencyTargetDeletedError(DomainError):
    code = "idempotency_target_deleted"
    status_code = 410
    title = "Idempotency target deleted"


class IdempotencyInProgressError(DomainError):
    code = "idempotency_in_progress"
    status_code = 409
    title = "Idempotency operation in progress"
    retryable = True


class InvalidStateError(DomainError):
    code = "invalid_state"
    status_code = 409
    title = "Invalid state"


class StaleConflictError(DomainError):
    code = "stale_conflict"
    status_code = 409
    title = "Stale conflict"


class InvalidArgumentError(DomainError):
    code = "invalid_argument"
    status_code = 422
    title = "Invalid argument"


class LeaseLostError(DomainError):
    code = "lease_lost"
    status_code = 409
    title = "Lease lost"


class DependencyUnavailableError(DomainError):
    code = "dependency_unavailable"
    status_code = 503
    title = "Dependency unavailable"
    retryable = True


class PatchOwnershipError(DomainError):
    code = "patch_ownership_violation"
    status_code = 409
    title = "Patch ownership violation"


class QuotaExceededError(DomainError):
    code = "quota_exceeded"
    status_code = 413
    title = "Quota exceeded"


class PolicyDeniedError(DomainError):
    code = "policy_denied"
    status_code = 403
    title = "Policy denied"


class UploadTooLargeError(DomainError):
    code = "upload_too_large"
    status_code = 413
    title = "Upload too large"


class UnsupportedMediaError(DomainError):
    code = "unsupported_media"
    status_code = 415
    title = "Unsupported media"


class InvalidCursorError(DomainError):
    code = "invalid_cursor"
    status_code = 422
    title = "Invalid cursor"


class TaskControlSignal(BaseException):
    """Cooperative control-flow signal that must cross graph exception boundaries."""


class TaskPauseRequested(TaskControlSignal):
    pass


class TaskCancelRequested(TaskControlSignal):
    pass
