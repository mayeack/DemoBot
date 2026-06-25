from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse
import logging
from pathlib import Path

from backend.config import settings
from backend.database.db import init_db
from backend.logging.log_handlers import setup_logging
from backend.middleware.request_logging import RequestLoggingMiddleware
from backend.middleware.access_key import AccessKeyMiddleware
from backend.routers import chat, admin, auth, settings as settings_routes, incident
from backend.telemetry import otel

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)

# Initialize code-based OpenTelemetry GenAI instrumentation (no-op unless
# settings.otel_enabled). Must run before the agentic graph is first invoked.
otel.init_telemetry(settings)

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

# Access-key gate (Basic Auth) — added before request logging so logging stays
# outermost and still records rejected (401) requests. No-op unless ACCESS_KEY is set.
app.add_middleware(AccessKeyMiddleware)

# Request logging middleware
app.add_middleware(RequestLoggingMiddleware)

# Include routers
app.include_router(chat.router)
app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(settings_routes.router)
app.include_router(incident.router)

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

    # Load persisted app settings: apply the configured log directory and start
    # the Splunk HEC forwarders (no-op until destinations are added).
    try:
        from backend import settings_store
        from backend.logging.governance_logger import governance_logger
        from backend.hec.runtime import hec_runtime
        app_cfg = settings_store.load()
        governance_logger.set_logs_directory(app_cfg.get("logs_directory", "logs"))
        hec_runtime.configure(settings_store.all_configs())
        from backend.model_emitter import model_emitter
        _em = settings_store.get_emit_model()
        model_emitter.configure(_em["enabled"], _em["model_name"], _em["random"])
        # Apply any persisted LLM provider override (Settings UI) over the .env default.
        settings_store.apply_ai_provider_from_store()
        await hec_runtime.start()
        logger.info("Settings loaded; HEC forwarders started")
    except Exception as e:
        logger.error(f"Failed to initialize settings/HEC: {e}")

    logger.info(f"Application started on {settings.host}:{settings.port}")

@app.on_event("shutdown")
async def shutdown_event():
    try:
        from backend.hec.runtime import hec_runtime
        await hec_runtime.stop()
    except Exception:
        logger.debug("HEC shutdown error", exc_info=True)
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
async def root(request: Request):
    # Browsers get the styled app; API clients get the JSON index.
    if "text/html" in request.headers.get("accept", ""):
        return RedirectResponse(url="/app")
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

        @app.get("/settings-ui", response_class=HTMLResponse)
        async def serve_settings():
            settings_file = frontend_dir / "settings.html"
            if settings_file.exists():
                return settings_file.read_text()
            return "<h1>Settings UI not found</h1>"
except Exception as e:
    logger.warning(f"Could not mount frontend: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        # Don't reload on data/log churn (the app writes these as it serves),
        # otherwise the dev reloader restarts after almost every request.
        reload_excludes=["*.db", "*.db-journal", "*.db-wal", "*.log", "*.json"],
        log_level=settings.log_level.lower()
    )
