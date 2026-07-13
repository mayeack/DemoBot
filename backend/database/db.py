from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool, QueuePool
from backend.models.db_models import Base
from backend.config import settings
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)


def create_db_engine():
    """
    Create database engine with appropriate settings based on database type.
    
    Supports:
    - SQLite: For local development (single-threaded, static pool)
    - PostgreSQL: For AWS production (connection pooling, optimized for concurrent access)
    """
    database_url = settings.database_url
    
    if database_url.startswith("sqlite"):
        # SQLite configuration for local development
        logger.info("Configuring SQLite database for local development")
        return create_engine(
            database_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=settings.debug
        )
    elif database_url.startswith("postgresql"):
        # PostgreSQL configuration for AWS production
        logger.info("Configuring PostgreSQL database for production")
        return create_engine(
            database_url,
            poolclass=QueuePool,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,  # Verify connections before use
            pool_recycle=3600,   # Recycle connections after 1 hour
            echo=settings.debug
        )
    else:
        # Default configuration for other databases
        logger.warning(f"Unknown database type, using default configuration: {database_url[:20]}...")
        return create_engine(
            database_url,
            echo=settings.debug
        )


# Create engine based on configuration
engine = create_db_engine()

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def reconcile_schema(target_engine=None):
    """Idempotently add any columns the ORM models declare but the physical tables
    lack (additive only — never drops or retypes). Returns the list of
    ``(table, column)`` pairs it added.

    ``Base.metadata.create_all`` creates missing *tables* but never ALTERs an
    existing one, and the SQLite ``medadvice.db`` file is gitignored and persists
    across code pulls. So when a model grows a column, the on-disk schema drifts:
    the next INSERT referencing the new column raises "no such column", which the
    governance writer swallows — silently dropping every governance record. We
    reconcile once at startup so drift is auto-healed here (logged loudly) instead
    of failing silently per write. Works on SQLite and PostgreSQL (both support
    ``ALTER TABLE ADD COLUMN``).
    """
    eng = target_engine or engine
    inspector = inspect(eng)
    existing_tables = set(inspector.get_table_names())
    added = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # brand-new table: create_all handles it
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            coltype = col.type.compile(dialect=eng.dialect)
            try:
                with eng.begin() as conn:
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {coltype}'))
                logger.warning("schema reconcile: added %s.%s (%s)", table.name, col.name, coltype)
                added.append((table.name, col.name))
            except Exception as e:
                logger.error("schema reconcile: could not add %s.%s: %s", table.name, col.name, e)
    return added


def init_db():
    """Initialize database tables"""
    try:
        Base.metadata.create_all(bind=engine)
        reconcile_schema()
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        raise

def get_db() -> Session:
    """Dependency for getting database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@contextmanager
def get_db_context():
    """Context manager for database sessions"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        db.close()
