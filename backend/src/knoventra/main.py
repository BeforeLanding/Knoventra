from fastapi import FastAPI

from knoventra.api.routes import router

app = FastAPI(title="Knoventra")
app.include_router(router)
