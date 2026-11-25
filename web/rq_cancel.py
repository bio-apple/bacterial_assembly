"""Compatibility shim — prefer web.cancel.request_cancel."""

from web.cancel import cancel_rq_job, request_cancel

__all__ = ["cancel_rq_job", "request_cancel"]
