from src.configs.model_configuration import model_config_document
from src.services.utils.gemini_token_utils import extract_gemini_image_usage, calculate_gemini_image_cost
from src.services.utils.openai_token_utils import extract_openai_image_usage, calculate_openai_image_cost


class TokenCalculator:
    def __init__(self, service, model_output_config):
        self.service = service
        self.model_output_config = model_output_config
        self.service_tier = None
        self.total_usage = {
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "reasoning_tokens": 0,
            "audio_duration_seconds": 0,
            "audio_duration_minutes": 0,
        }
        # Image-specific token tracking
        self.image_usage = {
            "text_input_tokens": 0,
            "text_output_tokens": 0,
            "image_input_tokens": 0,
            "image_output_tokens": 0,
            "cached_text_input_tokens": 0,
            "cached_image_input_tokens": 0,
            "total_images_generated": 0
        }

    def calculate_usage(self, model_response):
        usage = {}
        match self.service:
            case "open_router" | "mistral" | "openai_completion" | "neev_cloud" | "moonshot":
                usage["inputTokens"] = (model_response.get("usage") or {}).get("prompt_tokens", 0)
                usage["outputTokens"] = (model_response.get("usage") or {}).get("completion_tokens", 0)
                usage["totalTokens"] = (model_response.get("usage") or {}).get("total_tokens", 0)
                # Handle optional token details with safe access
                usage["cachedTokens"] = (model_response["usage"].get("prompt_tokens_details") or {}).get(
                    "cached_tokens", 0
                )
                usage["reasoningTokens"] = (model_response["usage"].get("completion_tokens_details") or {}).get(
                    "reasoning_tokens", 0
                )

            case "groq":
                _usage = model_response.get("usage") or {}
                usage["inputTokens"] = _usage.get("prompt_tokens", 0)
                usage["outputTokens"] = _usage.get("completion_tokens", 0)
                usage["totalTokens"] = _usage.get("total_tokens", 0)
                usage["cachedTokens"] = 0
                usage["reasoningTokens"] = (_usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)

            case "deepseek":
                # DeepSeek token calculation (similar to OpenAI format)
                usage["inputTokens"] = (model_response.get("usage") or {}).get("prompt_tokens", 0)
                usage["outputTokens"] = (model_response.get("usage") or {}).get("completion_tokens", 0)
                usage["totalTokens"] = (model_response.get("usage") or {}).get("total_tokens", 0)
                # DeepSeek uses prompt_cache_hit_tokens for cached tokens
                usage["cachedTokens"] = (model_response.get("usage") or {}).get("prompt_cache_hit_tokens", 0)
                # Extract reasoning_tokens from completion_tokens_details
                usage["reasoningTokens"] = ((model_response.get("usage") or {}).get("completion_tokens_details") or {}).get(
                    "reasoning_tokens", 0
                )

            case "grok":
                # Support both dicts (HTTP response) and SDK objects
                usage_obj = model_response.get("usage") or {}

                def _get_usage_value(key, default=0):
                    if isinstance(usage_obj, dict):
                        return usage_obj.get(key, default)
                    return getattr(usage_obj, key, default)

                usage["inputTokens"] = _get_usage_value("prompt_tokens")
                usage["outputTokens"] = _get_usage_value("completion_tokens")
                usage["totalTokens"] = _get_usage_value("total_tokens")
                # Grok may return cached/reasoning tokens under different keys, prefer documented ones
                usage["cachedTokens"] = _get_usage_value("cached_prompt_text_tokens") or _get_usage_value(
                    "cached_tokens"
                )
                usage["reasoningTokens"] = _get_usage_value("reasoning_tokens")

            case "gemini":
                # Batch send camelCase, normal completion returns snake_case
                usage_metadata = model_response.get('usage_metadata') or model_response.get('usageMetadata') or {}
                usage["inputTokens"] = usage_metadata.get('prompt_token_count') or usage_metadata.get('promptTokenCount') or 0
                usage["outputTokens"] = usage_metadata.get('candidates_token_count') or usage_metadata.get('candidatesTokenCount') or 0
                usage["totalTokens"] = usage_metadata.get('total_token_count') or usage_metadata.get('totalTokenCount') or 0
                usage["cachedTokens"] = usage_metadata.get('cached_content_token_count') or usage_metadata.get('cachedContentTokenCount') or 0
                usage["reasoningTokens"] = usage_metadata.get('thoughts_token_count') or usage_metadata.get('thoughtsTokenCount') or 0

            case "openai":
                _usage = model_response.get("usage") or {}
                usage["inputTokens"] = _usage.get("input_tokens", 0)
                usage["outputTokens"] = _usage.get("output_tokens", 0)
                usage["totalTokens"] = _usage.get("total_tokens", 0)
                usage["cachedTokens"] = (_usage.get("input_tokens_details") or {}).get(
                    "cached_tokens", 0
                )
                usage["reasoningTokens"] = (_usage.get("output_tokens_details") or {}).get(
                    "reasoning_tokens", 0
                )
                if model_response.get("service_tier"):
                    self.service_tier = model_response["service_tier"]

            case "anthropic":
                _usage = model_response.get("usage") or {}
                usage["inputTokens"] = _usage.get("input_tokens", 0)
                usage["outputTokens"] = _usage.get("output_tokens", 0)
                usage["totalTokens"] = usage["inputTokens"] + usage["outputTokens"]
                usage["cachingReadTokens"] = _usage.get("cache_read_input_tokens", 0)
                usage["cachingCreationInputTokens"] = _usage.get("cache_creation_input_tokens", 0)

            case "deepgram":
                metadata = model_response.get("metadata", {}) or {}
                usage_payload = model_response.get("usage", {}) or {}
                audio_duration_seconds = float(metadata.get("duration") or usage_payload.get("audio_duration") or 0)

                usage["audioDurationSeconds"] = audio_duration_seconds

            case _:
                pass

        self._update_total_usage(usage)
        return usage

    def _update_total_usage(self, usage):
        self.total_usage["total_tokens"] += usage.get("totalTokens") or 0
        self.total_usage["input_tokens"] += usage.get("inputTokens") or 0
        self.total_usage["output_tokens"] += usage.get("outputTokens") or 0
        self.total_usage["cached_tokens"] += usage.get("cachedTokens") or 0
        self.total_usage["cache_read_input_tokens"] += usage.get("cachingReadTokens") or 0
        self.total_usage["cache_creation_input_tokens"] += usage.get("cachingCreationInputTokens") or 0
        self.total_usage["reasoning_tokens"] += usage.get("reasoningTokens") or 0
        self.total_usage["audio_duration_seconds"] += usage.get("audioDurationSeconds") or 0
        self.total_usage["audio_duration_minutes"] += (usage.get("audioDurationSeconds") or 0) / 60

    def calculate_image_usage(self, model_response):
        """
        Calculate usage for image generation models
        Handles both OpenAI and Gemini response structures
        
        Args:
            model_response: Response from image generation API
            
        Returns:
            Dictionary with image-specific usage metrics
        """
        usage = {}
        match self.service:
            case 'gemini':
                # Gemini format - use utility function
                usage = extract_gemini_image_usage(model_response)
            
            case 'openai':
                # OpenAI format - use utility function
                usage = extract_openai_image_usage(model_response)
            
            case _:
                # Default case - no image usage data available for this service
                pass
        
        self._update_image_usage(usage)
        return self.image_usage


    def calculate_total_cost(self, model, service):
        """
        Calculate total cost in dollars using accumulated total_usage

        Args:
            model: model name
            service: service name

        Returns:
            Dictionary with cost breakdown using total_usage
        """
        model_obj = model_config_document[service][model]

        # Regular chat model cost calculation
        pricing = model_obj['outputConfig']['usage'][0]['total_cost']

        # Priority processing charges 2x the standard cost
        priority_multiplier = 2 if self.service_tier == "priority" else 1

        cost = {
            "input_cost": 0,
            "output_cost": 0,
            "cached_cost": 0,
            "reasoning_cost": 0,
            "cache_read_cost": 0,
            "cache_creation_cost": 0,
            "audio_cost": 0,
            "total_cost": 0,
        }

        # Calculate costs per million tokens using total_usage
        if self.total_usage["input_tokens"] and pricing.get("input_cost"):
            cost["input_cost"] = (self.total_usage["input_tokens"] / 1_000_000) * pricing["input_cost"]

        if self.total_usage["output_tokens"] and pricing.get("output_cost"):
            cost["output_cost"] = (self.total_usage["output_tokens"] / 1_000_000) * pricing["output_cost"]

        if self.total_usage["cached_tokens"] and pricing.get("cached_cost"):
            cost["cached_cost"] = (self.total_usage["cached_tokens"] / 1_000_000) * pricing["cached_cost"]

        if self.total_usage["reasoning_tokens"] and pricing.get("output_tokens"):
            cost["reasoning_cost"] = (self.total_usage["reasoning_tokens"] / 1_000_000) * pricing["output_tokens"]

        if self.total_usage["cache_read_input_tokens"] and pricing.get("caching_read_cost"):
            cost["cache_read_cost"] = (self.total_usage["cache_read_input_tokens"] / 1_000_000) * pricing[
                "caching_read_cost"
            ]

        if self.total_usage["cache_creation_input_tokens"] and pricing.get("caching_write_cost"):
            cost["cache_creation_cost"] = (self.total_usage["cache_creation_input_tokens"] / 1_000_000) * pricing[
                "caching_write_cost"
            ]

        if self.total_usage["audio_duration_minutes"] and pricing.get("audio_cost_per_minute"):
            cost["audio_cost"] = self.total_usage["audio_duration_minutes"] * pricing["audio_cost_per_minute"]

        # Calculate total cost
        cost["total_cost"] = (
            cost["input_cost"]
            + cost["output_cost"]
            + cost["cached_cost"]
            + cost["reasoning_cost"]
            + cost["cache_read_cost"]
            + cost["cache_creation_cost"]
            + cost["audio_cost"]
        ) * priority_multiplier

        return cost
    
    def calculate_image_cost(self, model):
        """
        Calculate cost for image generation models
        
        Args:
            model: model name
            service: service name
            
        Returns:
            Dictionary with detailed cost breakdown for image models
        """
        model_obj = model_config_document[self.service][model]
        pricing = model_obj['outputConfig']['usage'][0]['total_cost']
        
        cost = {
            "text_input_cost": 0,
            "text_output_cost": 0,
            "image_input_cost": 0,
            "image_output_cost": 0,
            "cached_text_input_cost": 0,
            "cached_image_input_cost": 0,
            "total_cost": 0
        }
        
        # Check if this is Gemini service with flat pricing structure
        match self.service:
            case 'gemini':
                # Gemini cost calculation - use utility function
                return calculate_gemini_image_cost(self.image_usage, pricing, model_obj)
            
            case 'openai':
                # OpenAI cost calculation - use utility function
                return calculate_openai_image_cost(self.image_usage, pricing)
            
            case _:
                # Default case - no specific pricing logic for this service
                pass
        
        # Calculate total cost (token-based only)
        cost['total_cost'] = (
            cost['text_input_cost'] +
            cost['text_output_cost'] +
            cost['image_input_cost'] +
            cost['image_output_cost'] +
            cost['cached_text_input_cost'] +
            cost['cached_image_input_cost']
        )

        return cost

    def _update_image_usage(self, usage):
        self.image_usage["text_input_tokens"] += usage.get("text_input_tokens") or 0
        self.image_usage["text_output_tokens"] += usage.get("text_output_tokens") or  0
        self.image_usage["image_input_tokens"] += usage.get("image_input_tokens") or 0
        self.image_usage["image_output_tokens"] += usage.get("image_output_tokens") or  0
        self.image_usage["cached_text_input_tokens"] += usage.get("cached_text_input_tokens") or 0
        self.image_usage["cached_image_input_tokens"] += usage.get("cached_image_input_tokens") or 0
        self.image_usage["total_images_generated"] += usage.get("total_images_generated") or 0

    def get_total_usage(self):
        return self.total_usage
    
    def get_image_usage(self):
        return self.image_usage
