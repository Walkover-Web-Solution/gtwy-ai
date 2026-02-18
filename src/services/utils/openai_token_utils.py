def extract_openai_image_usage(model_response):

    usage = {}
    usage_data = model_response.get('usage', {})
    
    usage["total_images_generated"] = usage_data.get("total_images_generated", 0)

    # Extract input token details
    input_tokens_details = usage_data.get('input_tokens_details', {})
    usage['text_input_tokens'] = input_tokens_details.get('text_tokens', 0)
    usage['image_input_tokens'] = input_tokens_details.get('image_tokens', 0)
    usage['cached_text_input_tokens'] = input_tokens_details.get('cached_tokens', 0)
    
    # Extract output token details
    # gpt-image-1.5 has detailed output_tokens_details, others don't
    output_tokens_details = usage_data.get('output_tokens_details', {})
    if output_tokens_details:
        # gpt-image-1.5: has separate text and image tokens in output
        usage['text_output_tokens'] = output_tokens_details.get('text_tokens', 0)
        usage['image_output_tokens'] = output_tokens_details.get('image_tokens', 0)
    else:
        # gpt-image-1 and gpt-image-1-mini: all output tokens are image tokens
        usage['text_output_tokens'] = 0
        usage['image_output_tokens'] = usage_data.get('output_tokens', 0)
    
    return usage


def calculate_openai_image_cost(image_usage, pricing):
    
    cost = {
        "text_input_cost": 0,
        "text_output_cost": 0,
        "image_input_cost": 0,
        "image_output_cost": 0,
        "cached_text_input_cost": 0,
        "cached_image_input_cost": 0,
        "total_cost": 0
    }
    
    # OpenAI has nested pricing structure: text_tokens.input, image_tokens.output, etc.
    text_token_pricing = pricing.get('text_tokens', {})
    image_token_pricing = pricing.get('image_tokens', {})
    
    # Calculate text token costs (per million tokens)
    if image_usage['text_input_tokens'] and text_token_pricing.get('input'):
        cost['text_input_cost'] = (image_usage['text_input_tokens'] / 1_000_000) * text_token_pricing['input']
    
    if image_usage['text_output_tokens'] and text_token_pricing.get('output'):
        cost['text_output_cost'] = (image_usage['text_output_tokens'] / 1_000_000) * text_token_pricing['output']
    
    if image_usage['cached_text_input_tokens'] and text_token_pricing.get('cached_input'):
        cost['cached_text_input_cost'] = (image_usage['cached_text_input_tokens'] / 1_000_000) * text_token_pricing['cached_input']
    
    # Calculate image token costs (per million tokens)
    if image_usage['image_input_tokens'] and image_token_pricing.get('input'):
        cost['image_input_cost'] = (image_usage['image_input_tokens'] / 1_000_000) * image_token_pricing['input']
    
    if image_usage['image_output_tokens'] and image_token_pricing.get('output'):
        cost['image_output_cost'] = (image_usage['image_output_tokens'] / 1_000_000) * image_token_pricing['output']
    
    if image_usage['cached_image_input_tokens'] and image_token_pricing.get('cached_input'):
        cost['cached_image_input_cost'] = (image_usage['cached_image_input_tokens'] / 1_000_000) * image_token_pricing['cached_input']
    
    # Calculate total cost
    cost['total_cost'] = (
        cost['text_input_cost'] +
        cost['text_output_cost'] +
        cost['image_input_cost'] +
        cost['image_output_cost'] +
        cost['cached_text_input_cost'] +
        cost['cached_image_input_cost']
    )
    
    return cost
