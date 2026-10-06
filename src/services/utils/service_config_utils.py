from src.configs.service_registry import has_anthropic_shape, has_gemini_shape, has_openai_choices_shape, has_openai_responses_shape


def tool_choice_function_name_formatter(service, configuration, toolchoice, found_choice):  # changes
    match service:
        # openai_chat services + gemini: all pass tool_choice through unchanged
        case s if has_openai_choices_shape(s) or has_gemini_shape(s):
            configuration["tool_choice"] = found_choice if found_choice is not None else toolchoice
            return configuration["tool_choice"]
        case s if has_openai_responses_shape(s):
            configuration["tool_choice"] = (
                {"type": "function", "name": toolchoice} if toolchoice is not None else found_choice
            )
            return configuration["tool_choice"]
        case s if has_anthropic_shape(s):
            if found_choice == "default":
                default_choice = found_choice
            else:
                default_choice = {"type": found_choice}
            user_choice = {"type": "tool", "name": toolchoice}
            configuration["tool_choice"] = default_choice if found_choice is not None else user_choice
            return configuration["tool_choice"]
    return configuration["tool_choice"]
