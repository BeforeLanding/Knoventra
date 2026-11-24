from contextlib import asynccontextmanager

from fastapi import FastAPI

from knoventra.api.chat import router as chat_router
from knoventra.api.chat import shutdown_resources
from knoventra.api.help import router as help_router
from knoventra.api.memory import router as memory_router
from knoventra.api.routes import router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup stays lazy on purpose: the graph, encoder and model clients are built on first use,
    # so routes that need none of them work without an LLM configured.
    yield
    shutdown_resources()


app = FastAPI(title="Knoventra", lifespan=lifespan)
app.include_router(router)
app.include_router(chat_router)
app.include_router(help_router)
app.include_router(memory_router)
