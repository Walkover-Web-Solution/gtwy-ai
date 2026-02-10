from fastapi import APIRouter, Depends, HTTPException, Request

from src.middlewares.agentsMiddlewares import agents_auth
from src.middlewares.interfaceMiddlewares import chat_bot_auth, reset_chatBot, send_data_middleware
from src.middlewares.ratelimitMiddleware import rate_limit

router = APIRouter()


async def auth_and_rate_limit(request: Request):
    await chat_bot_auth(request)
    await rate_limit(request, key_path="body.slugName", points=100)
    await rate_limit(request, key_path="body.threadId", points=20)


async def public_auth_and_rate_limit(request: Request):
    await agents_auth(request)
    await rate_limit(request, key_path="state.profile.limiter_key", points=10)


async def combined_auth(request: Request):
    try:
        # Try first authentication method
        await auth_and_rate_limit(request)
    except HTTPException as e1:
        try:
            # If first fails, try second authentication method
            await public_auth_and_rate_limit(request)
        except HTTPException:
            # If both fail, raise the first error
            raise e1 from None


@router.post("/{botId}/sendMessage", dependencies=[Depends(combined_auth)])
async def send_message(request: Request, botId: str):
    result = await send_data_middleware(request, botId)
    return result


@router.post("/{botId}/resetchat", dependencies=[Depends(auth_and_rate_limit)])
async def reset_chat(request: Request, botId: str):
    return await reset_chatBot(request, botId)
