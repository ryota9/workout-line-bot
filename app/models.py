import uuid
from sqlalchemy import Column, String, Text, DateTime, Date, func
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Date as DateType


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id = Column(String, primary_key=True)       # LINE user ID
    name = Column(String)
    goal = Column(Text)
    level = Column(String, default="beginner")        # beginner / intermediate
    equipment = Column(String, default="bodyweight")  # bodyweight / dumbbell / both
    notify_time = Column(String, default="07:00")     # HH:MM (JST)
    status = Column(String, default="active")         # active / inactive
    onboarding_step = Column(String, default="0")     # 0=new 1=goal 2=equipment 3=level done=complete
    pending_action = Column(String, nullable=True)    # propose_exercise / change_notify_time / None
    last_reminder_sent = Column(DateType, nullable=True)


class WorkoutPlan(Base):
    __tablename__ = "workout_plans"

    plan_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, index=True)
    date = Column(Date, index=True)
    menu_json = Column(Text)    # JSON: [{exercise, sets, reps, note}, ...]
    ai_reason = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class WorkoutLog(Base):
    __tablename__ = "workout_logs"

    log_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, index=True)
    date = Column(Date, index=True)
    status = Column(String)     # done / partial / skipped
    comment = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class UserExercise(Base):
    __tablename__ = "user_exercises"

    exercise_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, index=True)
    exercise_name = Column(String)
    created_at = Column(DateTime, server_default=func.now())


class UserDumbbellWeight(Base):
    __tablename__ = "user_dumbbell_weights"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, index=True)
    body_part = Column(String)      # chest / shoulder / back / neck / abs / hamstrings / legs / biceps / triceps
    weight_kg = Column(String)      # e.g. "10", "7.5"
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AnalysisSummary(Base):
    __tablename__ = "analysis_summary"

    summary_id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, index=True)
    period = Column(String)             # "YYYY-MM-DD_YYYY-MM-DD"
    completion_rate = Column(String)    # float as string (e.g. "72.5")
    skipped_pattern = Column(Text)      # JSON: {"Monday": 3, "Friday": 2}
    recommendation = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
