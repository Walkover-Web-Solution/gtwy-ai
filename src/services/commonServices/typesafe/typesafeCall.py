"""Handler for the TypeSafe (Jev) service.

Maps a GTWY chat request onto Jev's ``{model, state, questions}`` contract:

- ``state``      <- the user message (parsed as JSON when it is valid JSON)
- ``questions``  <- ``configuration.questions`` from the request body, with
                    ``{{variables}}`` substituted inside every string leaf
- ``model``      <- the bridge/request model

Nothing else from the bridge configuration is forwarded: Jev rejects unknown
fields with a 422.
"""

import json

from src.configs.constant import service_name
from src.services.utils.ai_middleware_format import Response_formatter

from ..baseService.baseService import BaseService

# TypeSafe answers an unknown question type with a bare 400 "Invalid request.", so check it here.
QUESTION_TYPES = ("choice", "score", "noul")

QUESTIONS_REQUIRED_ERROR = (
    "configuration.questions is required for the typesafe service. Send a map of typed questions, e.g. "
    '{"is_urgent": {"type": "noul", "instructions": "The message conveys urgency"}}.'
)


class TypeSafe(BaseService):
    async def execute(self):
        service = service_name["typesafe"]

        questions = _parse_questions(self.customConfig.pop("questions", None))
        if self.variables:
            questions = _substitute_variables(questions, self.variables)

        if self.user in (None, ""):
            raise ValueError("A user message is required: it is sent to TypeSafe as the `state`.")

        payload = {
            "model": self.customConfig.get("model") or self.model,
            "state": _build_state(self.user),
            "questions": questions,
        }
        # Keep the exact provider payload on customConfig so history (AiConfig) reflects what was sent.
        self.customConfig = payload

        providerResponse = await self.chats(payload, self.apikey, service)
        modelResponse = providerResponse.get("modelResponse", {})
        if not providerResponse.get("success"):
            await self.handle_failure(providerResponse)
            raise ValueError(providerResponse.get("error"))

        response = await Response_formatter(modelResponse, service, {}, self.type, self.image_data)
        historyParams = self.prepare_history_params(response, modelResponse, {})

        return {
            "success": True,
            "modelResponse": modelResponse,
            "historyParams": historyParams,
            "response": response,
        }


def _parse_questions(raw):
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(f"configuration.questions must be a JSON object: {error}") from error
    if not isinstance(raw, dict) or not raw:
        raise ValueError(QUESTIONS_REQUIRED_ERROR)
    for key, question in raw.items():
        if not isinstance(question, dict) or question.get("type") not in QUESTION_TYPES:
            raise ValueError(
                f"configuration.questions['{key}'] must be an object whose 'type' is one of: choice, score, noul"
            )
    return raw


def _substitute_variables(value, variables):
    """Apply ``{{var}}`` replacement to every string leaf without touching JSON structure."""
    if isinstance(value, str):
        # Lazy import: helper.py imports this module to build the handler, so a
        # top-level import here would form a cycle.
        from src.services.utils.helper import Helper

        replaced, _missing = Helper.replace_variables_in_prompt(value, variables)
        return replaced
    if isinstance(value, dict):
        return {key: _substitute_variables(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute_variables(item, variables) for item in value]
    return value


def _build_state(user):
    if isinstance(user, str):
        stripped = user.strip()
        if stripped[:1] in ("{", "["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return user
    return user
