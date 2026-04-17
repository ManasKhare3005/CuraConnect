import os
from datetime import date, datetime, timezone
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker, Session as DBSession
from dotenv import load_dotenv

from database.models import Base, ConversationMessage, User, Vital, Session

load_dotenv()

SQLITE_FALLBACK_URL = "sqlite:///./health_assistant.db"
DATABASE_URL = os.getenv("DATABASE_URL", SQLITE_FALLBACK_URL)


def _create_engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url)


engine = _create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


def _ensure_users_auth_columns():
    """Lightweight in-place migration for legacy user tables.

    Older project versions created `users` without auth fields.
    This adds missing nullable columns so auth routes can work without
    requiring manual DB resets.
    """
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("users")}
    statements: list[str] = []

    if "email" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN email VARCHAR(255)")
    if "password_hash" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)")
    if "dob" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN dob DATE")
    if "weight_kg" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN weight_kg FLOAT")
    if "height_cm" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN height_cm FLOAT")
    if "address" not in existing_columns:
        statements.append("ALTER TABLE users ADD COLUMN address VARCHAR(500)")

    if not statements:
        # Ensure the email index exists even if columns already existed.
        statements.append("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)")

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))

        # Index creation is safe to retry and helps lookup performance.
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"))


def _ensure_sessions_metadata_columns():
    """Add missing timezone metadata columns to legacy `sessions` tables."""
    inspector = inspect(engine)
    if "sessions" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("sessions")}
    statements: list[str] = []

    if "timezone_name" not in existing_columns:
        statements.append("ALTER TABLE sessions ADD COLUMN timezone_name VARCHAR(80)")
    if "utc_offset_minutes" not in existing_columns:
        statements.append("ALTER TABLE sessions ADD COLUMN utc_offset_minutes INTEGER")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def init_db():
    global engine
    global DATABASE_URL

    try:
        Base.metadata.create_all(bind=engine)
        _ensure_users_auth_columns()
        _ensure_sessions_metadata_columns()
    except OperationalError:
        if DATABASE_URL.startswith("sqlite"):
            raise
        DATABASE_URL = SQLITE_FALLBACK_URL
        engine = _create_engine(DATABASE_URL)
        SessionLocal.configure(bind=engine)
        Base.metadata.create_all(bind=engine)
        _ensure_users_auth_columns()
        _ensure_sessions_metadata_columns()


def get_or_create_user(name: str, age: int | None = None) -> User:
    with SessionLocal(expire_on_commit=False) as db:
        user = db.query(User).filter(User.name.ilike(name)).first()
        if not user:
            user = User(name=name, age=age)
            db.add(user)
            db.commit()
        elif age and not user.age:
            user.age = age
            db.commit()
        return user


def log_vital(
    user_id: int,
    vital_type: str,
    value: str,
    unit: str | None,
    notes: str | None,
    recorded_at: datetime | None = None,
) -> dict:
    with SessionLocal() as db:
        vital = Vital(
            user_id=user_id,
            vital_type=vital_type,
            value=value,
            unit=unit,
            notes=notes,
        )
        if recorded_at is not None:
            vital.recorded_at = recorded_at
        db.add(vital)
        db.commit()
        db.refresh(vital)
        return {
            "id": vital.id,
            "vital_type": vital.vital_type,
            "value": vital.value,
            "unit": vital.unit,
            "notes": vital.notes,
            "recorded_at": vital.recorded_at.isoformat(),
        }


def get_recent_vitals(user_id: int, limit: int = 10) -> list[dict]:
    with SessionLocal() as db:
        vitals = (
            db.query(Vital)
            .filter(Vital.user_id == user_id)
            .order_by(Vital.recorded_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "vital_type": v.vital_type,
                "value": v.value,
                "unit": v.unit,
                "notes": v.notes,
                "recorded_at": v.recorded_at.isoformat(),
            }
            for v in vitals
        ]


def create_session(user_id: int | None = None) -> int:
    with SessionLocal() as db:
        session = Session(user_id=user_id)
        db.add(session)
        db.commit()
        db.refresh(session)
        return session.id


def close_session(session_id: int, summary: str | None = None):
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.ended_at = datetime.now(timezone.utc)
            session.summary = summary
            db.commit()


def update_session_user(session_id: int, user_id: int):
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.user_id = user_id
            db.commit()


def update_session_timezone(
    session_id: int,
    timezone_name: str | None = None,
    utc_offset_minutes: int | None = None,
):
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if not session:
            return

        changed = False
        if isinstance(timezone_name, str) and timezone_name.strip():
            clean_tz = timezone_name.strip()[:80]
            if session.timezone_name != clean_tz:
                session.timezone_name = clean_tz
                changed = True

        if isinstance(utc_offset_minutes, int):
            if session.utc_offset_minutes != utc_offset_minutes:
                session.utc_offset_minutes = utc_offset_minutes
                changed = True

        if changed:
            db.commit()


def log_conversation_message(
    session_id: int,
    role: str,
    content: str,
    user_id: int | None = None,
    created_at: datetime | None = None,
) -> dict:
    with SessionLocal() as db:
        message = ConversationMessage(
            session_id=session_id,
            user_id=user_id,
            role=(role or "assistant").strip().lower()[:20],
            content=(content or "").strip(),
        )
        if created_at is not None:
            message.created_at = created_at

        db.add(message)
        db.commit()
        db.refresh(message)
        return {
            "id": message.id,
            "session_id": message.session_id,
            "user_id": message.user_id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat() if message.created_at else None,
        }


def attach_session_messages_to_user(session_id: int, user_id: int):
    with SessionLocal() as db:
        (
            db.query(ConversationMessage)
            .filter(ConversationMessage.session_id == session_id, ConversationMessage.user_id.is_(None))
            .update({"user_id": user_id}, synchronize_session=False)
        )
        db.commit()


def update_user_profile(user_id: int, name: str | None = None, age: int | None = None):
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        changed = False
        if isinstance(name, str) and name.strip() and user.name != name.strip():
            user.name = name.strip()
            changed = True

        if isinstance(age, int) and not isinstance(age, bool) and age > 0 and user.age != age:
            user.age = age
            changed = True

        if changed:
            db.commit()


def update_user_full(
    user_id: int,
    name: str | None = None,
    dob: date | None = None,
    update_dob: bool = False,
    weight_kg: float | None = None,
    update_weight: bool = False,
    height_cm: float | None = None,
    update_height: bool = False,
    address: str | None = None,
    update_address: bool = False,
) -> User:
    with SessionLocal(expire_on_commit=False) as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"User {user_id} not found")

        if isinstance(name, str) and name.strip():
            user.name = name.strip()

        if update_dob:
            user.dob = dob
            user.age = age_from_dob(dob) if dob else None

        if update_weight:
            user.weight_kg = weight_kg

        if update_height:
            user.height_cm = height_cm

        if update_address:
            user.address = address.strip() if address else None

        db.commit()
        return user


def get_vitals_history(user_id: int, limit: int = 50) -> list[dict]:
    with SessionLocal() as db:
        vitals = (
            db.query(Vital)
            .filter(Vital.user_id == user_id)
            .order_by(Vital.recorded_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": v.id,
                "vital_type": v.vital_type,
                "value": v.value,
                "unit": v.unit,
                "notes": v.notes,
                "recorded_at": v.recorded_at.isoformat(),
            }
            for v in vitals
        ]


def get_conversation_history(
    user_id: int,
    limit_sessions: int = 25,
    limit_messages_per_session: int = 0,
) -> list[dict]:
    with SessionLocal() as db:
        sessions = (
            db.query(Session)
            .filter(Session.user_id == user_id)
            .order_by(Session.started_at.desc())
            .limit(limit_sessions)
            .all()
        )
        history: list[dict] = []
        for session in sessions:
            messages_query = (
                db.query(ConversationMessage)
                .filter(ConversationMessage.session_id == session.id)
                .order_by(ConversationMessage.created_at.asc())
            )
            if limit_messages_per_session > 0:
                messages = messages_query.limit(limit_messages_per_session).all()
            else:
                messages = messages_query.all()

            history.append(
                {
                    "session_id": session.id,
                    "started_at": session.started_at.isoformat() if session.started_at else None,
                    "ended_at": session.ended_at.isoformat() if session.ended_at else None,
                    "timezone_name": session.timezone_name,
                    "utc_offset_minutes": session.utc_offset_minutes,
                    "message_count": len(messages),
                    "messages": [
                        {
                            "id": message.id,
                            "role": message.role,
                            "content": message.content,
                            "created_at": message.created_at.isoformat() if message.created_at else None,
                        }
                        for message in messages
                    ],
                }
            )
        return history


def delete_user(user_id: int):
    with SessionLocal() as db:
        session_ids = [row[0] for row in db.query(Session.id).filter(Session.user_id == user_id).all()]
        if session_ids:
            db.query(ConversationMessage).filter(ConversationMessage.session_id.in_(session_ids)).delete(
                synchronize_session=False
            )
        db.query(Vital).filter(Vital.user_id == user_id).delete()
        db.query(ConversationMessage).filter(ConversationMessage.user_id == user_id).delete(synchronize_session=False)
        db.query(Session).filter(Session.user_id == user_id).delete()
        db.query(User).filter(User.id == user_id).delete()
        db.commit()


def get_user_by_email(email: str) -> User | None:
    with SessionLocal(expire_on_commit=False) as db:
        return db.query(User).filter(User.email.ilike(email.strip())).first()


def get_user_by_id(user_id: int) -> User | None:
    with SessionLocal(expire_on_commit=False) as db:
        return db.query(User).filter(User.id == user_id).first()


def age_from_dob(dob: date) -> int:
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def create_registered_user(
    name: str, email: str, password_hash: str, dob: date | None = None
) -> User:
    age = age_from_dob(dob) if dob else None
    with SessionLocal(expire_on_commit=False) as db:
        user = User(
            name=name.strip(),
            email=email.strip().lower(),
            password_hash=password_hash,
            dob=dob,
            age=age,
        )
        db.add(user)
        db.commit()
        return user
