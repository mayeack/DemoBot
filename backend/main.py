from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
import logging
from pathlib import Path

from backend.config import settings
from backend.database.db import init_db
from backend.logging.log_handlers import setup_logging
from backend.middleware.request_logging import RequestLoggingMiddleware
from backend.routers import chat, admin

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Medical guidance application with comprehensive AI governance logging",
    debug=settings.debug
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request logging middleware
app.add_middleware(RequestLoggingMiddleware)

# Include routers
app.include_router(chat.router)
app.include_router(admin.router)

# Initialize database on startup
@app.on_event("startup")
async def startup_event():
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Environment: {settings.environment}")

    try:
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

    logger.info(f"Application started on {settings.host}:{settings.port}")

@app.on_event("shutdown")
async def shutdown_event():
    logger.info(f"Shutting down {settings.app_name}")

# Health check endpoint
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment
    }

# Root endpoint
@app.get("/")
async def root():
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "message": "Medical Advice API is running",
        "endpoints": {
            "chat": "/api/chat",
            "admin": "/admin",
            "health": "/health"
        }
    }

# Serve frontend (if running in production mode)
try:
    frontend_dir = Path(__file__).parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

        @app.get("/app", response_class=HTMLResponse)
        async def serve_app():
            index_file = frontend_dir / "index.html"
            if index_file.exists():
                return index_file.read_text()
            return "<h1>Frontend not found</h1>"

        @app.get("/admin-ui", response_class=HTMLResponse)
        async def serve_admin():
            admin_file = frontend_dir / "admin.html"
            if admin_file.exists():
                return admin_file.read_text()
            return "<h1>Admin UI not found</h1>"

        @app.get("/governance-ui", response_class=HTMLResponse)
        async def serve_governance():
            gov_file = frontend_dir / "governance.html"
            if gov_file.exists():
                return gov_file.read_text()
            return "<h1>Governance UI not found</h1>"
except Exception as e:
    logger.warning(f"Could not mount frontend: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower()
    )
