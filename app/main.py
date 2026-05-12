import json
from contextlib import asynccontextmanager
from datetime import date

from dotenv import load_dotenv
load_dotenv()  # .env を他の import より先に読み込む

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from .database import get_db, init_db
from .line_handler import (
    format_menu_message,
    handle_follow,
    handle_message,
    push_message,
    reply_message,
    verify_signature,
)
from .models import User, WorkoutPlan
from .scheduler import scheduler, start_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    yield
    scheduler.shutdown()


app = FastAPI(title="Workout LINE Bot", lifespan=lifespan)


# ── LINE Webhook ──────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(body)
    for event in payload.get("events", []):
        event_type = event.get("type")
        reply_token = event.get("replyToken", "")

        if event_type == "follow":
            reply_text = handle_follow(event, db)
            if reply_text:
                reply_message(reply_token, reply_text)

        elif event_type == "message" and event["message"]["type"] == "text":
            reply_text = handle_message(event, db)
            if reply_text:
                reply_message(reply_token, reply_text)

    return JSONResponse({"status": "ok"})


# ── Health ────────────────────────────────────────────────────────────────────

@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return {"status": "ok"}


# ── Analysis ──────────────────────────────────────────────────────────────────

@app.get("/users/{user_id}/analysis")
async def get_analysis(user_id: str, days: int = 60, db: Session = Depends(get_db)):
    from .analysis import compute_and_save_analysis
    return compute_and_save_analysis(db, user_id, days=days)


# ── Manual trigger (dev / ops) ────────────────────────────────────────────────

@app.post("/users/{user_id}/notify")
async def trigger_notify(user_id: str, db: Session = Depends(get_db)):
    """Manually generate and push today's menu. Useful for testing."""
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from .ai_agent import generate_daily_menu

    today = date.today()
    plan_data = generate_daily_menu(db, user_id, today)

    existing = db.query(WorkoutPlan).filter(
        WorkoutPlan.user_id == user_id, WorkoutPlan.date == today
    ).first()
    if not existing:
        db.add(WorkoutPlan(
            user_id=user_id,
            date=today,
            menu_json=json.dumps(plan_data.get("menu", []), ensure_ascii=False),
            ai_reason=plan_data.get("reason", ""),
        ))
        db.commit()

    message = format_menu_message(plan_data)
    push_message(user_id, message)
    return {"status": "sent", "message": message}


# ── User registration (for testing without LINE flow) ─────────────────────────

@app.post("/users")
async def register_user(body: dict, db: Session = Depends(get_db)):
    user_id = body.get("user_id")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    existing = db.query(User).filter(User.user_id == user_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="User already exists")

    db.add(User(
        user_id=user_id,
        name=body.get("name", "テストユーザー"),
        goal=body.get("goal", "体力維持"),
        level=body.get("level", "beginner"),
        equipment=body.get("equipment", "bodyweight"),
        notify_time=body.get("notify_time", "07:00"),
        onboarding_step=body.get("onboarding_step", "0"),
        status=body.get("status", "active"),
    ))
    db.commit()
    return {"status": "created", "user_id": user_id}
