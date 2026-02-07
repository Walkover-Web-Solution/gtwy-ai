import traceback

from google import genai
from google.genai import types


async def gemini_video_model(
    configuration, apikey, execution_time_logs, timer, file_data=None, prompt=None, youtube_url=None
):
    try:
        client = genai.Client(api_key=apikey)
        model = configuration.pop("model")
        youtube_url = configuration.pop("youtube_url", None)
        prompt = configuration.pop("prompt", None)
        timer.start()

        if youtube_url:
            video_part = types.Part(file_data=types.FileData(file_uri=youtube_url, mime_type="video/mp4"))
        elif file_data:
            if isinstance(file_data, dict):
                file_uri = file_data.get("uri") or file_data.get("file_uri") or file_data.get("name")
                mime_type = file_data.get("mime_type", "video/mp4")
            elif isinstance(file_data, str):
                file_uri, mime_type = file_data, "video/mp4"
            else:
                raise ValueError("Invalid file_data format — expected dict or str")

            video_part = types.Part(file_data=types.FileData(file_uri=file_uri, mime_type=mime_type))
        else:
            raise ValueError("Either youtube_url or file_data must be provided")

        # Add optional text prompt
        prompt_part = types.Part(text=prompt) if prompt else None

        # Construct clean contents (no extra fields)
        contents = types.Content(parts=[p for p in [video_part, prompt_part] if p])

        # Generate response
        response = client.models.generate_content(model=model, contents=[contents], config=None)

        execution_time_logs.append(
            {"step": "Gemini video content generation", "time_taken": timer.stop("Gemini video content generation")}
        )

        # Extract text content from response
        text_content = [part.text for part in response.candidates[0].content.parts if getattr(part, "text", None)]
        content_text = " ".join(text_content)

        return {
            "success": True,
            "response": {"data": [{"text_content": content_text, "file_reference": youtube_url or file_data}]},
        }

    except Exception as error:
        execution_time_logs.append(
            {"step": "Gemini video processing error", "time_taken": timer.stop("Gemini video processing error")}
        )
        print("gemini_video_model error=>", error)
        traceback.print_exc()
        return {"success": False, "error": str(error)}
