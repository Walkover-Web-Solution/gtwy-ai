import base64
import traceback

from openai import AsyncOpenAI

from src.services.utils.gcp_upload_service import uploadDoc


async def OpenAIImageModel(configuration, apiKey, execution_time_logs, timer):
    try:
        openai_config = AsyncOpenAI(api_key=apiKey)
        timer.start()
        chat_completion = await openai_config.images.generate(**configuration)
        execution_time_logs.append(
            {"step": "OpenAI image Processing time", "time_taken": timer.stop("OpenAI image Processing time")}
        )
        response = chat_completion.to_dict()
        user_message = configuration.get("prompt", "") or ""

        # Process all images in the response data array
        for i, image_data in enumerate(response["data"]):
            # Check if response contains URL or base64 data
            if "url" in image_data:
                # URL format - upload in background only (return predictive GCP URL immediately)
                original_image_url = image_data["url"]

                gcp_url = await uploadDoc(
                    file=original_image_url, folder="generated-images", real_time=False, content_type="image/png"
                )

                response["data"][i]["original_url"] = original_image_url
                response["data"][i]["url"] = gcp_url
                response["data"][i]["image_url"] = gcp_url
                response["data"][i]["permanent_url"] = gcp_url
                response["data"][i]["revised_prompt"] = image_data.get("revised_prompt")
            elif "b64_json" in image_data:
                # Base64 format - upload first (synchronous), then return with user message as revised_prompt
                image_bytes = base64.b64decode(image_data["b64_json"])

                gcp_url = await uploadDoc(
                    file=image_bytes, folder="generated-images", real_time=True, content_type="image/png"
                )

                response["data"][i]["url"] = gcp_url
                response["data"][i]["image_url"] = gcp_url
                response["data"][i]["permanent_url"] = gcp_url
                response["data"][i]["revised_prompt"] = user_message
                # Optionally remove b64_json from response to avoid sending large payload
                if "b64_json" in response["data"][i]:
                    del response["data"][i]["b64_json"]
            else:
                raise ValueError(f"Image data contains neither 'url' nor 'b64_json' key. Available keys: {list(image_data.keys())}")
        
        response.setdefault("usage", {})["total_images_generated"] = len(response["data"])
        return {
            'success': True,
            'response': response
        }
    except Exception as error:
        execution_time_logs.append(
            {"step": "OpenAI image Processing time", "time_taken": timer.stop("OpenAI image Processing time")}
        )
        print("runmodel error=>", error)
        traceback.print_exc()
        return {"success": False, "error": str(error)}
