from fastapi import FastAPI

from knoventra.api.chat import router as chat_router
from knoventra.api.routes import router

app = FastAPI(title="Knoventra")
app.include_router(router)
app.include_router(chat_router)
