import traceback
from google import genai
from src.services.commonServices.Google.gemini_image_utils import (
    IMAGEN_MODELS,
    handle_imagen_generation,
    handle_gemini_generation
)


async def gemini_image_model(configuration, apikey, execution_time_logs, timer):
    """Handle image generation for both Gemini and Imagen models."""
    try:
        client = genai.Client(api_key=apikey)
        
        prompt = configuration.pop("prompt", "")
        model = configuration.pop("model")
        
        timer.start()
        
        # IMAGEN MODELS
        if model in IMAGEN_MODELS:
            result = await handle_imagen_generation(client, model, prompt, configuration)
            
            execution_time_logs.append({
                "step": "Imagen-gemini image Processing time",
                "time_taken": timer.stop("Imagen image Processing time")
            })
            
            return result
        
        # GEMINI MODELS
        else:
            result = await handle_gemini_generation(client, model, prompt, configuration)
            
            execution_time_logs.append({
                "step": "Gemini image Processing time",
                "time_taken": timer.stop("Gemini image Processing time")
            })
            
            return result
    
    except Exception as error:
        execution_time_logs.append({
            "step": "Image Processing time",
            "time_taken": timer.stop("Image Processing time")
        })
        traceback.print_exc()
        
        return {
            "success": False,
            "error": str(error)
        }
