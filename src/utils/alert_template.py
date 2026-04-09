from src.configs.constant import alert_types


def create_missing_vars(details, context=None):
    payload = {"alert": "Variables Missing", "alert_type": alert_types["variable"], "variables": details}
    if context:
        payload.update(_build_context_fields(context))
    return payload


def metrix_limit_reached(details, context=None):
    payload = {"alert": "Metrics Limit Reached", "alert_type": alert_types["metrix_limit_reached"], "limit_size": details}
    if context:
        payload.update(_build_context_fields(context))
    return payload


def create_error_payload(details, context=None):
    payload = {
        "alert": "Unexpected Error",
        "alert_type": alert_types["error"],
        "error_message": details.get("error") or details.get("error_message") or str(details),
    }
    if context:
        payload.update(_build_context_fields(context))
    return payload


def create_retry_mechanism_payload(details, context=None):
    payload = {"alert": "Retry Mechanism Started", "alert_type": alert_types["retry_mechanism"], "error_message": details}
    if context:
        payload.update(_build_context_fields(context))
    return payload


def create_broadcast_response_payload(details, context=None):
    payload = {"alert": "Broadcast Response", "alert_type": alert_types["broadcast_response"], "response": details}
    if context:
        payload.update(_build_context_fields(context))
    return payload


def _build_context_fields(context):
    fields = {}
    if context.get("api_name"):
        fields["api_name"] = context["api_name"]
    if context.get("source"):
        fields["source"] = context["source"]
    if context.get("service"):
        fields["service"] = context["service"]
    return fields


def create_response_format(url, headers):
    return {"type": "webhook", "cred": {"url": url, "headers": headers}}
