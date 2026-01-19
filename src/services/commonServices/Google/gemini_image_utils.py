from io import BytesIO
from PIL import Image
from google.genai import types
from src.services.utils.gcp_upload_service import uploadDoc


IMAGEN_MODELS = {
    "imagen-4.0-generate-001",
    "imagen-4.0-ultra-generate-001",
    "imagen-4.0-fast-generate-001",
    "imagen-4.0-generate-preview-06-06",
}


async def handle_imagen_generation(client, model, prompt, configuration):
    """Generate images using Imagen and return formatted response with GCP URLs."""
    # Generate images
    response = client.models.generate_images(
        model=model,
        prompt=prompt,
        config=types.GenerateImagesConfig(**configuration) if configuration else None
    )
    
    # Process and upload images
    gcp_urls = []
    for img in response.generated_images:
        image = Image.open(BytesIO(img.image.image_bytes))
        img_buffer = BytesIO()
        image.save(img_buffer, format="PNG")
        img_buffer.seek(0)
        
        gcp_url = await uploadDoc(
            file=img_buffer,
            folder="generated-images",
            real_time=True,
            content_type="image/png"
        )
        gcp_urls.append(gcp_url)
    
    # Return formatted response
    return {
        "success": True,
        "response": {
            "data": [{
                "urls": gcp_urls,
                "text_content": []
            }]
        }
    }


async def handle_gemini_generation(client, model, prompt, configuration):
    """Generate content using Gemini and return formatted response with image and text."""
    # Extract and build configuration
    aspect_ratio = configuration.pop('aspect_ratio', None)
    image_size = configuration.pop('image_size', None)
    
    config_params = {
        'response_modalities': ['TEXT', 'IMAGE'],
        'image_config': types.ImageConfig(aspect_ratio=aspect_ratio, image_size=image_size)
    }
    config_params.update(configuration)
    
    # Generate content
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**config_params)
    )
    
    # Process response
    text_content = []
    gcp_url = None
    
    for part in response.candidates[0].content.parts:
        if part.text:
            text_content.append(part.text)
        elif part.inline_data:
            image = Image.open(BytesIO(part.inline_data.data))
            img_buffer = BytesIO()
            image.save(img_buffer, format="PNG")
            img_buffer.seek(0)
            
            gcp_url = await uploadDoc(
                file=img_buffer,
                folder="generated-images",
                real_time=True,
                content_type="image/png"
            )
    
    # Return formatted response
    return {
        "success": True,
        "response": {
            "data": [{
                "url": gcp_url,
                "text_content": text_content
            }]
        }
    }

