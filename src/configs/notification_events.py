"""Single catalogue of notification events.

Mirrored in the Node backend at src/configs/notificationEvents.js — keep the two in sync.
Only the routing fields live here; labels and descriptions are owned by Node (it serves
them to the UI). Every alert is queued; the Node hub shows it in-app and sends it to the
webhooks set up in the Alerts section, GTWY team alerting and email.

audience: "org" = customer-facing, "internal" = GTWY team only, "global" = every org.
"""

NOTIFICATION_SEVERITIES = ("info", "warning", "critical")
NOTIFICATION_AUDIENCES = ("org", "internal", "global")

NOTIFICATION_EVENTS = {
    "agent.error": {"audience": "org", "severity": "critical"},
    "agent.system_error": {"audience": "org", "severity": "critical"},
    "agent.variables_missing": {"audience": "org", "severity": "warning"},
    "agent.retry_started": {"audience": "org", "severity": "warning"},
    "agent.response_broadcast": {"audience": "org", "severity": "info"},
    "agent.thumbs_down": {"audience": "org", "severity": "info"},
    "agent.metrics_limit_reached": {"audience": "org", "severity": "warning"},
    "knowledge_base.alert": {"audience": "org", "severity": "warning"},
    "alert.webhook_failed": {"audience": "org", "severity": "warning"},
    "usage.threshold_reached": {"audience": "org", "severity": "warning"},
    "usage.limit_reached": {"audience": "org", "severity": "critical"},
    "usage.daily_spike": {"audience": "org", "severity": "warning"},
    "billing.credits_exhausted": {"audience": "org", "severity": "critical"},
    "apikey.status_changed": {"audience": "org", "severity": "warning"},
    "system.announcement": {"audience": "global", "severity": "info"},
    "ops.internal_error": {"audience": "internal", "severity": "critical"},
    "ops.queue_failure": {"audience": "internal", "severity": "critical"},
}

# Alert types configured in the Alerts section -> the event that carries them through the hub.
# agent.response_broadcast is webhook-only (never shown in the inbox).
LEGACY_ALERT_TYPE_TO_EVENT = {
    "Error": "agent.error",
    "Variable": "agent.variables_missing",
    "retry_mechanism": "agent.retry_started",
    "metrix_limit_reached": "agent.metrics_limit_reached",
    "broadcast_response": "agent.response_broadcast",
}
