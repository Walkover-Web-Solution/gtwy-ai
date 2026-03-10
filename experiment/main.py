import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from executor import run_experiment

load_dotenv()

app = FastAPI(title="Experiment: AI Task Planner")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
async def serve_ui():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.post("/run")
async def run(request: Request):
    body = await request.json()
    goal = body.get("goal", "").strip()

    if not goal:
        raise HTTPException(status_code=400, detail="goal is required")

    api_key = body.get("api_key") or OPENAI_API_KEY
    if not api_key:
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY not configured")

    return EventSourceResponse(run_experiment(goal, api_key))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8888)
