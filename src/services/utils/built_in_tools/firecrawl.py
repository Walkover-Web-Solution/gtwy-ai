from config import Config
from src.services.utils.apiservice import fetch
from src.services.utils.logger import logger


async def call_firecrawl_scrape(args):
    url = (args or {}).get('url') if isinstance(args, dict) else None
    if not url:
        return {
            'response': {'error': 'url is required for web_crawling tool'},
            'metadata': {'type': 'function'},
            'status': 0
        }

    api_key = Config.FIRECRAWL_API_KEY
    if not api_key:
        return {
            'response': {'error': 'web_crawling tool is not configured'},
            'metadata': {'type': 'function'},
            'status': 0
        }

    payload = {'url': url}
    formats = args.get('formats') if isinstance(args, dict) else None
    if formats:
        if isinstance(formats, list):
            payload['formats'] = formats
        elif isinstance(formats, str):
            payload['formats'] = [formats]
        else:
            payload['formats'] = [str(formats)]

    request_headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    
    try:
        response, headers = await fetch(
            "https://api.firecrawl.dev/v2/scrape",
            'POST',
            request_headers,
            None,
            payload
        )
        data = response.get('data') if isinstance(response, dict) and 'data' in response else response
        return {
            'response': data,
            'metadata': {
                'type': 'function',
                'flowHitId': headers.get('flowHitId') if isinstance(headers, dict) else None
            },
            'status': 1
        }
    except Exception as exc:
        logger.error(f"Firecrawl scrape failed: {exc}")
        return {
            'response': {'error': str(exc)},
            'metadata': {'type': 'function'},
            'status': 0
        }
