def extract_gemini_image_usage(model_response):

    usage = {}
    usage_metadata = model_response.get('usage', {})
    
    usage["total_images_generated"] = usage_metadata.get("total_images_generated", 0)

    # Extract prompt (input) token details from modality breakdown
    prompt_tokens_details = usage_metadata.get('prompt_tokens_details', [])
    for detail in prompt_tokens_details:
        modality = detail.get('modality', '')
        token_count = detail.get('token_count', 0)
        
        if modality == 'TEXT':
            usage['text_input_tokens'] = token_count
        elif modality == 'IMAGE':
            usage['image_input_tokens'] = token_count
    
    # Extract candidates (output) token details from modality breakdown
    candidates_tokens_details = usage_metadata.get('candidates_tokens_details', [])
    for detail in candidates_tokens_details:
        modality = detail.get('modality', '')
        token_count = detail.get('token_count', 0)
        
        if modality == 'TEXT':
            usage['text_output_tokens'] = token_count
        elif modality == 'IMAGE':
            usage['image_output_tokens'] = token_count
    
    return usage


def calculate_gemini_image_cost(image_usage, pricing, model_obj):

    cost = {
        "text_input_cost": 0,
        "text_output_cost": 0,
        "image_input_cost": 0,
        "image_output_cost": 0,
        "cached_text_input_cost": 0,
        "cached_image_input_cost": 0,
        "total_cost": 0
    }
    
    # Gemini has flat pricing: input_text, input_image, output_text, output_image
    
    if model_obj['model_name'].startswith("imagen"):
        # Calculate per Image cost
        cost["total_cost"] = image_usage['total_images_generated'] * pricing["output_image"]
        return cost

    # Calculate input text cost (per million tokens)
    if image_usage['text_input_tokens'] and pricing.get('input_text'):
        cost['text_input_cost'] = (image_usage['text_input_tokens'] / 1_000_000) * pricing['input_text']
    
    # Calculate input image cost (per million tokens)
    if image_usage['image_input_tokens'] and pricing.get('input_image'):
        cost['image_input_cost'] = (image_usage['image_input_tokens'] / 1_000_000) * pricing['input_image']
    
    # Calculate output text cost (per million tokens)
    if image_usage['text_output_tokens'] and pricing.get('output_text'):
        cost['text_output_cost'] = (image_usage['text_output_tokens'] / 1_000_000) * pricing['output_text']
    
    # Calculate output image cost (per million tokens)
    if image_usage['image_output_tokens'] and pricing.get('output_image'):
        cost['image_output_cost'] = (image_usage['image_output_tokens'] / 1_000_000) * pricing['output_image']
    
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
