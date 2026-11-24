import uvicorn

from knoventra.config import settings
from knoventra.mcp_server.app import build_app

if __name__ == "__main__":
    base = f"http://{settings.mcp_host}:{settings.mcp_port}"
    app = build_app(base_url=base, host=settings.mcp_host, allowed_hosts=settings.mcp_allowed_hosts)
    uvicorn.run(app, host=settings.mcp_host, port=settings.mcp_port)
