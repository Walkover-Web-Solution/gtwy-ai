import traceback

from src.services.utils.apiservice import fetch
from src.services.utils.gcp_upload_service import uploadDoc


async def AiMlImageModel(configuration, apiKey, execution_time_logs, timer, images):
    try:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {apiKey}"}

        # Start timer for image generation
        timer.start()
        if images:
            configuration["images"] = images
        # Call AI.ml image generation API
        generation_data, _ = await fetch(
            url="https://api.ai.ml/v1/generation/images", method="POST", headers=headers, json_body=configuration
        )

        execution_time_logs.append(
            {"step": "AI.ml image generation time", "time_taken": timer.stop("AI.ml image generation time")}
        )

        # Check if generation was successful
        if generation_data.get("status") != "success":
            raise ValueError(f"AI.ml image generation failed: {generation_data}")

        # Get the image URL from response
        image_url = generation_data["data"]["image"]["url"]
        image_size = generation_data["data"]["image"]["size"]

        if "orgs" in image_url:
            image_path = image_url[image_url.index("orgs") :]
        else:
            # Fallback: just use the URL as-is if 'orgs' is not found
            image_path = image_url

        # Start timer for fetching actual image
        timer.start()

        # Call AI.ml media API to get the actual image URL
        media_data, _ = await fetch(
            url=f"https://api.ai.ml/v1/media/images/{image_path}", method="GET", headers=headers
        )

        execution_time_logs.append(
            {"step": "AI.ml fetch image URL time", "time_taken": timer.stop("AI.ml fetch image URL time")}
        )

        # Check if media fetch was successful
        if media_data.get("status") != "success":
            raise ValueError(f"AI.ml media fetch failed: {media_data}")

        # Get the actual image URL (S3 signed URL)
        actual_image_url = media_data["url"]

        # Upload to GCP bucket (background upload)
        gcp_url = await uploadDoc(
            file=actual_image_url, folder="generated-images", real_time=False, content_type="image/png"
        )

        # Format response to match OpenAI structure
        response = {
            "created": generation_data["data"].get("createdAt"),
            "data": [
                {
                    "original_url": actual_image_url,
                    "url": gcp_url,  # Primary URL (GCP)
                    "size": image_size,
                }
            ],
        }

        # Add usage information if available
        if "usage" in generation_data["data"]:
            response["usage"] = generation_data["data"]["usage"]

        # Add billed amount if available
        if "billed_amount_usd" in generation_data["data"]:
            response["billed_amount_usd"] = generation_data["data"]["billed_amount_usd"]

        return {"success": True, "response": response}
    except Exception as error:
        execution_time_logs.append(
            {"step": "AI.ml image Processing time", "time_taken": timer.stop("AI.ml image Processing time")}
        )
        print("AI.ml image model error=>", error)
        traceback.print_exc()
        return {"success": False, "error": str(error)}
