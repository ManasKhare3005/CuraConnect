import os
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session as DBSession
from dotenv import load_dotenv

from database.models import Base, User, Vital, Session

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/curaconnect")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


def init_db():
    Base.metadata.create_all(bind=engine)


def get_or_create_user(name: str, age: int | None = None) -> User:
    with SessionLocal() as db:
        user = db.query(User).filter(User.name.ilike(name)).first()
        if not user:
            user = User(name=name, age=age)
            db.add(user)
            db.commit()
            db.refresh(user)
        elif age and not user.age:
            user.age = age
            db.commit()
            db.refresh(user)
        return User(id=user.id, name=user.name, age=user.age, created_at=user.created_at)


def log_vital(user_id: int, vital_type: str, value: str, unit: str | None, notes: str | None) -> dict:
    with SessionLocal() as db:
        vital = Vital(
            user_id=user_id,
            vital_type=vital_type,
            value=value,
            unit=unit,
            notes=notes,
        )
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
            session.ended_at = datetime.utcnow()
            session.summary = summary
            db.commit()


def update_session_user(session_id: int, user_id: int):
    with SessionLocal() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.user_id = user_id
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

        if isinstance(age, int) and age > 0 and user.age != age:
            user.age = age
            changed = True

        if changed:
            db.commit()
