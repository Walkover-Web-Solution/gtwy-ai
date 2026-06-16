import asyncio
import signal
from contextlib import asynccontextmanager
import src.services.grafana
import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import Config
import globals as _globals
from globals import logger


def _handle_sigterm(*_):
    logger.info("SIGTERM received — marking server as not ready, stopping batch cron")
    _globals.is_ready = False


signal.signal(signal.SIGTERM, _handle_sigterm)
from models.Timescale.connections import init_async_dbservice
from src.configs.model_configuration import background_listen_for_changes, init_model_configuration
from src.configs.service_registry import background_listen_for_service_changes, init_service_registry
from src.routes.chatBot_routes import router as chatbot_router
from src.routes.image_process_routes import router as image_process_routes
from src.routes.rag_routes import router as rag_routes
from src.routes.v2.modelRouter import router as v2_router
from src.services.commonServices.queueService.queueLogService import sub_queue_obj
from src.services.commonServices.queueService.queueService import queue_obj
from src.services.utils.auto_router_utils import run_supported_services_refresh_loop
from src.services.utils.batch_script import repeat_function

async def consume_messages_in_executor():
    await queue_obj.consume_messages()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events."""
    logger.info("Starting up...")
    await init_model_configuration()
    await init_service_registry()
    # Run the consumer in the background without blocking the main event loop
    await queue_obj.connect()
    await queue_obj.create_queue_if_not_exists()
    await sub_queue_obj.connect()
    await sub_queue_obj.create_queue_if_not_exists()

    consume_task = None
    batch_task = None
    if Config.CONSUMER_STATUS.lower() == "true":
        consume_task = asyncio.create_task(consume_messages_in_executor())
        batch_task = asyncio.create_task(repeat_function())

    asyncio.create_task(init_async_dbservice()) if Config.ENVIRONMENT == "LOCAL" else await init_async_dbservice()

    logger.info("Starting MongoDB change stream listener as a background task.")
    change_stream_task = asyncio.create_task(background_listen_for_changes())
    service_registry_task = asyncio.create_task(background_listen_for_service_changes())
    supported_services_refresh_task = asyncio.create_task(run_supported_services_refresh_loop())

    yield  # Startup logic is complete

    # Shutdown logic
    logger.info("Shutting down...")

    logger.info("Shutting down MongoDB change stream listener.")
    change_stream_task.cancel()
    service_registry_task.cancel()
    supported_services_refresh_task.cancel()

    if consume_task:
        consume_task.cancel()
    if batch_task:
        batch_task.cancel()

    await queue_obj.disconnect()
    await sub_queue_obj.disconnect()

    try:
        if consume_task:
            await consume_task
    except asyncio.CancelledError:
        logger.error("Consumer task was cancelled during shutdown.")

    try:
        if batch_task:
            await batch_task
    except asyncio.CancelledError:
        logger.info("Batch script task was cancelled during shutdown.")

    try:
        await change_stream_task
    except asyncio.CancelledError:
        logger.info("MongoDB change stream listener task successfully cancelled.")

    try:
        await service_registry_task
    except asyncio.CancelledError:
        logger.info("Service registry change stream listener task successfully cancelled.")

    try:
        await supported_services_refresh_task
    except asyncio.CancelledError:
        logger.info("Supported services refresh task successfully cancelled.")


# Initialize the FastAPI app
app = FastAPI(debug=True, lifespan=lifespan)

# CORS middleware
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"], max_age=86400
)


# Healthcheck route
@app.get("/healthcheck")
async def healthcheck():
    return JSONResponse(
        status_code=200,
        content={
            "status": "OK running good... v1.3",
        },
    )


# Readiness probe — returns 503 after SIGTERM so LB drains this pod
@app.get("/ready")
async def ready():
    if _globals.is_ready:
        return JSONResponse(status_code=200, content={"status": "ready"})
    return JSONResponse(status_code=503, content={"status": "shutting down"})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": "Validation error", "errors": jsonable_encoder(exc.errors())},
    )


# Include routers
app.include_router(v2_router, prefix="/api/v2/model")
app.include_router(chatbot_router, prefix="/chatbot")
app.include_router(image_process_routes, prefix="/image/processing")
app.include_router(image_process_routes, prefix="/files")
app.include_router(rag_routes, prefix="/rag")

if __name__ == "__main__":
    PORT = int(Config.PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
