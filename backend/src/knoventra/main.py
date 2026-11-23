from fastapi import FastAPI

from knoventra.api.chat import router as chat_router
from knoventra.api.help import router as help_router
from knoventra.api.memory import router as memory_router
from knoventra.api.routes import router

app = FastAPI(title="Knoventra")
app.include_router(router)
app.include_router(chat_router)
app.include_router(help_router)
app.include_router(memory_router)
