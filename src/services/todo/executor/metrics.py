def init_metrics() -> dict:
    """Return a fresh aggregate container for primary-agent task telemetry."""
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "expected_cost": 0.0,
        "latency_total": 0.0,
        "per_task_latency": [],
        "tools_call_data": [],
        "reasoning_parts": [],
        "service": None,
        "model": None,
        "finish_reason": "stop",
        "success": True,
        "firstAttemptError": None,
        "AiConfig": None,
        "fallback_model": None,
        "llm_urls": [],
        "annotations": [],
        "last_error": None,
    }


def merge_task_metrics(
    metrics: dict,
    task_id: str,
    done_event: dict | None,
    collected_tool_calls: list,
    reasoning_buf: list,
    task_success: bool,
    error,
    *,
    agent_config=None,
    elapsed_seconds=None,
    fallback_model=None,
    service=None,
    model=None,
) -> None:
    """Fold a single primary-agent task's telemetry into the shared aggregate."""
    if metrics is None:
        return

    usage = (done_event or {}).get("usage") or {}
    response = (done_event or {}).get("response") or {}
    data = response.get("data") or {}

    input_t = usage.get("input_tokens") or 0
    output_t = usage.get("output_tokens") or 0
    metrics["input_tokens"] += input_t
    metrics["output_tokens"] += output_t
    metrics["total_tokens"] += usage.get("total_tokens") or (input_t + output_t)
    try:
        metrics["expected_cost"] += float(usage.get("cost") or usage.get("expected_cost") or 0)
    except (TypeError, ValueError):
        pass

    # Prefer provider-reported latency; fall back to wall-clock elapsed.
    provider_latency = data.get("latency")
    if isinstance(provider_latency, dict):
        over_all = provider_latency.get("over_all_time") or 0
    elif isinstance(provider_latency, (int, float)):
        over_all = provider_latency
    else:
        over_all = 0
    try:
        over_all = float(over_all or 0)
    except (TypeError, ValueError):
        over_all = 0.0
    if over_all <= 0 and elapsed_seconds is not None:
        try:
            over_all = float(elapsed_seconds)
        except (TypeError, ValueError):
            over_all = 0.0
    metrics["latency_total"] += over_all
    metrics["per_task_latency"].append({"task_id": task_id, "time": round(over_all, 4)})

    effective_model = data.get("model") or model
    if effective_model and not metrics["model"]:
        metrics["model"] = effective_model
    effective_service = response.get("service") or service
    if effective_service and not metrics["service"]:
        metrics["service"] = effective_service

    if response.get("firstAttemptError"):
        metrics["firstAttemptError"] = response["firstAttemptError"]
    if agent_config and not metrics["AiConfig"]:
        metrics["AiConfig"] = agent_config
    if fallback_model and not metrics["fallback_model"]:
        metrics["fallback_model"] = fallback_model
    if response.get("llm_urls"):
        metrics["llm_urls"].extend(response["llm_urls"])
    if response.get("annotations"):
        metrics["annotations"].extend(response["annotations"])
    if reasoning_buf:
        metrics["reasoning_parts"].append("".join(reasoning_buf))
    if collected_tool_calls:
        metrics["tools_call_data"].extend(collected_tool_calls)
    if not task_success:
        metrics["success"] = False
        metrics["finish_reason"] = "error"
        if error:
            metrics["last_error"] = str(error)


def finalize_metrics(metrics: dict | None) -> dict | None:
    """Shape the aggregate into the fields needed by the history payload."""
    if not metrics:
        return None
    return {
        "input_tokens": metrics["input_tokens"],
        "output_tokens": metrics["output_tokens"],
        "total_tokens": metrics["total_tokens"],
        "expected_cost": metrics["expected_cost"],
        "latency": {
            "over_all_time": metrics["latency_total"],
            "per_task": metrics["per_task_latency"],
        },
        "tools_call_data": metrics["tools_call_data"],
        "reasoning": "\n".join(p for p in metrics["reasoning_parts"] if p),
        "service": metrics["service"],
        "model": metrics["model"],
        "finish_reason": metrics["finish_reason"],
        "success": metrics["success"],
        "firstAttemptError": metrics["firstAttemptError"],
        "AiConfig": metrics["AiConfig"],
        "fallback_model": metrics["fallback_model"] or {},
        "llm_urls": metrics["llm_urls"],
        "annotations": metrics["annotations"],
        "last_error": metrics["last_error"],
    }
