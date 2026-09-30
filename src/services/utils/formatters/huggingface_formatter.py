"""Response formatter for Hugging Face — image generation only.

Chat completions for huggingface flow through format_openai_compatible (see
ai_middleware_format.py's has_openai_choices_shape branch) since the HF Router
is OpenAI-Chat-Completions-shaped. Image generation is a different HTTP
surface entirely (raw image bytes from the classic Inference API, uploaded to
GCP by hf_image_model), so it needs its own formatter — shaped identically to
openai_formatter.py's _format_image since hf_image_model produces the same
url/image_url/permanent_url/revised_prompt item shape.
"""


def format_huggingface_image(response):
    image_urls = []
    for image_data in response.get("data", []):
        gcp_url = image_data.get("image_url") or image_data.get("permanent_url") or image_data.get("url")
        image_urls.append(
            {
                "revised_prompt": image_data.get("revised_prompt"),
                "image_url": gcp_url,
                "permanent_url": gcp_url,
            }
        )
    return {
        "data": {"image_urls": image_urls},
        "usage": response.get("usage", {}),
    }
