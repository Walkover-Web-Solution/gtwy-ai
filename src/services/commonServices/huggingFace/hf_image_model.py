import io
import traceback
from huggingface_hub import AsyncInferenceClient
from src.services.utils.gcp_upload_service import uploadDoc
DEFAULT_IMAGE_PROVIDER = "fal-ai"


async def hf_image_model(configuration, apiKey, execution_time_logs, timer):
    try:
        model = configuration.get("model")
        prompt = configuration.get("prompt", "") or ""
        provider = configuration.get("provider") or DEFAULT_IMAGE_PROVIDER
        timer.start()

        client = AsyncInferenceClient(provider=provider, api_key=apiKey)
        # Returns a PIL.Image object, per huggingface_hub's InferenceClient.text_to_image contract.
        pil_image = await client.text_to_image(prompt, model=model)

        execution_time_logs.append(
            {
                "step": "Hugging Face image Processing time",
                "time_taken": timer.stop("Hugging Face image Processing time"),
            }
        )

        buffer = io.BytesIO()
        pil_image.save(buffer, format="PNG")
        image_bytes = buffer.getvalue()

        gcp_url = await uploadDoc(file=image_bytes, folder="generated-images", real_time=True, content_type="image/png")
        response = {
            "data": [
                {
                    "url": gcp_url,
                    "image_url": gcp_url,
                    "permanent_url": gcp_url,
                    "revised_prompt": prompt,
                }
            ],
            "model": model,
            "usage": {"total_images_generated": 1},
        }
        return {"success": True, "response": response}
    except Exception as error:
        execution_time_logs.append(
            {
                "step": "Hugging Face image Processing time",
                "time_taken": timer.stop("Hugging Face image Processing time"),
            }
        )
        traceback.print_exc()
        return {"success": False, "error": str(error)}
