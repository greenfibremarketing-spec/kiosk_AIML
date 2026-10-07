"""Main entrypoint for Greenie AI Kiosk server.

Runs the FastAPI backend on host and port configured in settings (default 0.0.0.0:5007).

Usage:
  python main.py
  uvicorn main:api --host 0.0.0.0 --port 5007 --reload
"""

import uvicorn
from app.config import settings
from app.server import api

if __name__ == "__main__":
    print(f"Starting Greenie AI Kiosk server on {settings.host}:{settings.port}...")
    uvicorn.run(
        "main:api",
        host=settings.host,
        port=settings.port,
        reload=False,
    )
