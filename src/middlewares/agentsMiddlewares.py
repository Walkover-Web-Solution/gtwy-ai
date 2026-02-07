import traceback

import jwt
from fastapi import HTTPException, Request

from config import Config
from src.services.utils.time import Timer


async def agents_auth(request: Request):
    try:
        timer_obj = Timer()
        timer_obj.start()
        request.state.timer = timer_obj.getTime()
        # request.state.timer = timer
        check_token = False
        if request.headers.get("Authorization"):
            token = request.headers.get("Authorization")
            if not token:
                raise HTTPException(status_code=498, detail="invalid token")
            check_token = jwt.decode(token, Config.PUBLIC_CHATBOT_TOKEN, algorithms=["HS256"])
            if check_token:
                request.state.profile = check_token
                request.state.profile["limiter_key"] = check_token.get("userId")
                return

        raise HTTPException(status_code=404, detail="not valid user")
    except Exception as err:
        traceback.print_exc()
        print(f"middleware error => {err}")
        raise HTTPException(status_code=401, detail="unauthorized user") from err
