from datetime import datetime
from typing import List, Optional
from sqlmodel import Field, SQLModel, Relationship, create_engine, Index


class User(SQLModel, table=True):
    zulip_id: int = Field(unique=True, index=True, default=None, primary_key=True)
    username: str
    is_muted: bool = Field(default=False)
    mute_until: Optional[datetime] = Field(default=None)
    agreed_new_rules: bool = Field(default=False)
    warnings: List["WarningRecord"] = Relationship(back_populates="user")


class WarningRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    type: str  # rule ID (e.g. "1.1")
    reason: str
    timestamp: datetime = Field(default_factory=datetime.now)
    user_id: int = Field(foreign_key="user.zulip_id")
    user: User = Relationship(back_populates="warnings")
    actor_id: Optional[int] = Field(default=None)  # who issued the warn
    actor_name: Optional[str] = Field(default=None)


class ModLog(SQLModel, table=True):
    """Action audit log."""
    __table_args__ = (
        Index("idx_modlog_target", "target_id", "timestamp"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    timestamp: datetime = Field(default_factory=datetime.now, index=True)
    actor_id: int
    actor_name: str
    action: str  # mute / unmute / warn / unwarn / purge
    target_id: int
    target_name: str
    details: str


class AutoRule(SQLModel, table=True):
    """Auto-moderation rule created by admins."""
    __table_args__ = (
        Index("idx_autorule_enabled", "enabled"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    name: Optional[str] = Field(default=None, max_length=100)
    # Target: "user" or "role"
    target_type: str = Field(max_length=20)
    target_value: str = Field(max_length=200)  # user_id str, or "moderator" for role>=300
    # Scope: stream_id (NULL=any), topics (JSON array or NULL=all)
    scope_stream_id: Optional[int] = Field(default=None)
    scope_topics: Optional[str] = Field(default=None)  # JSON array of topic names, NULL=all
    pattern: str = Field(max_length=500)  # regex
    action: str = Field(max_length=20)  # "allow", "delete", "warn", "mutewarn"
    created_by: int
    created_at: str = Field(max_length=30)
    enabled: int = Field(default=1)  # 1=active, 0=disabled


class PendingConfirmation(SQLModel, table=True):
    """Pending moderator confirmation request."""
    __table_args__ = (
        Index("idx_pending_target", "target_id", "action_type"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    action_type: str  # "mute", "unmute", "unwarn"
    actor_id: int  # who requested
    actor_name: str
    target_id: int
    target_name: str
    duration: Optional[str] = None  # e.g. "3h", "forever" — for mute
    duration_seconds: Optional[int] = None  # raw seconds for mute
    rule_id: Optional[str] = None  # for unwarn
    original_actor_id: Optional[int] = None  # who originally muted/warned
    original_actor_name: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.now)
    expires_at: datetime = Field(default_factory=lambda: datetime.now())


class NewRulesState(SQLModel, table=True):
    """Single-row table holding the global new-rules lockdown flag.

    Persisted so the lockdown survives bot restarts (in-memory state was lost
    on every restart, leaving `/new-rules-need-allow` silently inactive).
    """
    id: int = Field(default=1, primary_key=True)  # always 1 — single-row
    active: bool = Field(default=False)
    activated_at: Optional[datetime] = Field(default=None)
    activated_by: Optional[int] = Field(default=None)
    activated_by_name: Optional[str] = Field(default=None)


# Single engine (shared with app.py through manager)
sqlite_url = "sqlite:///database.db"
engine = create_engine(sqlite_url, echo=True)


def create_db_and_tables():
    """Create all tables (safe: won't overwrite existing data)."""
    print("Creating tables...")
    SQLModel.metadata.create_all(engine)
    print("Tables ready.")


def migrate_db():
    """Add new columns to existing tables if they don't exist."""
    import sqlite3
    conn = sqlite3.connect("database.db")
    cur = conn.cursor()

    # Check/add WarningRecord actor columns
    cur.execute("PRAGMA table_info(warningrecord)")
    cols = {r[1] for r in cur.fetchall()}
    if "actor_id" not in cols:
        cur.execute("ALTER TABLE warningrecord ADD COLUMN actor_id INTEGER")
        print("Migrated: warningrecord.actor_id")
    if "actor_name" not in cols:
        cur.execute("ALTER TABLE warningrecord ADD COLUMN actor_name TEXT")
        print("Migrated: warningrecord.actor_name")

    # Create AutoRule table if not exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS autorule (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            target_type TEXT NOT NULL,
            target_value TEXT NOT NULL,
            scope_stream_id INTEGER,
            scope_topics TEXT,
            pattern TEXT NOT NULL,
            action TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            enabled INTEGER DEFAULT 1
        )
    """)
    # Create index for enabled rules
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_autorule_enabled
        ON autorule(enabled)
    """)

    # Migration: add agreed_new_rules column to user table if missing
    cur.execute("PRAGMA table_info(user)")
    user_cols = {r[1] for r in cur.fetchall()}
    if "agreed_new_rules" not in user_cols:
        cur.execute(
            "ALTER TABLE user ADD COLUMN agreed_new_rules INTEGER DEFAULT 0"
        )
        print("Migrated: user.agreed_new_rules")

    # Migration: create newrulesstate table if missing (single-row, id=1)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS newrulesstate (
            id INTEGER PRIMARY KEY,
            active INTEGER DEFAULT 0,
            activated_at TEXT,
            activated_by INTEGER,
            activated_by_name TEXT
        )
    """)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    create_db_and_tables()
    migrate_db()
