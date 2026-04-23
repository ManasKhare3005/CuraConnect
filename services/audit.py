"""
HIPAA-style audit logging for all data access events.

Every access to patient health information (PHI) is logged with:
- WHO accessed it (actor)
- WHAT was accessed (resource)
- WHEN it was accessed (timestamp)
- WHERE they accessed from (IP, if available)
- WHY / HOW (action type)

This log is append-only. Entries are never modified or deleted.
"""

from __future__ import annotations

import sys

from database import db as database


def sanitize_for_log(text: str) -> str:
    """Remove potential PII from text before logging."""
    import re

    sanitized = str(text or "")
    sanitized = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "[EMAIL]", sanitized)
    sanitized = re.sub(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b", "[PHONE]", sanitized)
    sanitized = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[SSN]", sanitized)
    sanitized = re.sub(r"\b\d+\s+[A-Z][a-z]+\s+(?:St|Ave|Rd|Dr|Blvd|Ln|Ct|Way|Pl)\b", "[ADDRESS]", sanitized)
    return sanitized


def log_audit_event(
    action: str,
    resource_type: str,
    resource_id: int | None = None,
    user_id: int | None = None,
    actor_id: int | None = None,
    actor_type: str = "user",
    ip_address: str | None = None,
    details: str | None = None,
):
    """Log an audit event. This should never raise - failures are logged to stderr."""
    try:
        safe_details = None
        if isinstance(details, str) and details.strip():
            safe_details = sanitize_for_log(details.strip())[:1000]

        database.create_audit_log(
            action=(action or "").strip()[:50],
            resource_type=(resource_type or "").strip()[:50],
            resource_id=resource_id,
            user_id=user_id,
            actor_id=actor_id,
            actor_type=(actor_type or "user").strip().lower()[:20] or "user",
            ip_address=(ip_address or "").strip()[:45] or None,
            details=safe_details,
        )
    except Exception as exc:
        print(
            f"[audit] failed to record action={action!r} resource_type={resource_type!r}: {exc}",
            file=sys.stderr,
        )


def audit_data_access(
    action: str,
    resource_type: str,
    resource_id: int | None,
    actor_id: int | None,
    ip: str | None = None,
):
    """Audit a user data access event."""
    log_audit_event(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=actor_id,
        actor_id=actor_id,
        actor_type="user",
        ip_address=ip,
    )


def audit_auth_event(action: str, user_id: int | None, ip: str | None = None, details: str | None = None):
    """Audit an authentication event."""
    log_audit_event(
        action=action,
        resource_type="auth",
        user_id=user_id,
        actor_id=user_id,
        actor_type="user",
        ip_address=ip,
        details=details,
    )


def audit_system_event(action: str, details: str | None = None):
    """Audit a system-level event."""
    log_audit_event(
        action=action,
        resource_type="system",
        actor_type="system",
        details=details,
    )
