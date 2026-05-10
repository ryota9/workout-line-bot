import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base, UserDumbbellWeight

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./workout.db")
# Render の PostgreSQL URL は "postgres://" で始まるが SQLAlchemy 2.x は "postgresql://" 必須
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate(engine)


def _migrate(engine) -> None:
    """Add new columns to existing tables without dropping data."""
    new_columns = [
        ("users", "equipment",        "VARCHAR DEFAULT 'bodyweight'"),
        ("users", "onboarding_step",  "VARCHAR DEFAULT '0'"),
        ("users", "pending_action",        "VARCHAR"),
        ("users", "last_reminder_sent",    "DATE"),
    ]
    with engine.connect() as conn:
        for table, column, definition in new_columns:
            try:
                conn.execute(__import__("sqlalchemy").text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                ))
                conn.commit()
            except Exception as e:
                if "duplicate column" in str(e).lower() or "already exists" in str(e).lower():
                    pass  # 既存カラムは正常
                else:
                    print(f"[migrate] unexpected error on {table}.{column}: {e}")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
