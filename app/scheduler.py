import json
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .ai_agent import generate_daily_menu, generate_zero_activity_message
from .analysis import compute_and_save_analysis
from .database import SessionLocal
from .line_handler import format_menu_message, push_message
from .models import User, WorkoutLog, WorkoutPlan

scheduler = BackgroundScheduler(timezone="Asia/Tokyo")

# 継続率しきい値とリマインド間隔（日）
_REMINDER_HIGH_RATE_THRESHOLD = 60   # %以上 → 3日ごと
_REMINDER_HIGH_INTERVAL_DAYS = 3
_REMINDER_LOW_INTERVAL_DAYS = 5

# 連続トレーニング日数のしきい値
_REST_AFTER_CONSECUTIVE_DAYS = 3

_MSG_AUTO_REST = (
    "🌿 今日は休養日にしましょう！\n\n"
    "3日連続でよく頑張りました💪\n"
    "筋肉は休息中に成長します。今日はしっかり休みましょう！\n\n"
    "「筋トレ」と送ればメニューを作ることもできます。"
)


def _is_consecutive_training_days(db, user_id: str, today, min_days: int) -> bool:
    """直前 min_days 日間が連続して done/partial か判定する。"""
    check_date = today - timedelta(days=1)
    for _ in range(min_days):
        log = (
            db.query(WorkoutLog)
            .filter(
                WorkoutLog.user_id == user_id,
                WorkoutLog.date == check_date,
                WorkoutLog.status.in_(["done", "partial"]),
            )
            .first()
        )
        if not log:
            return False
        check_date -= timedelta(days=1)
    return True


def _send_daily_notifications() -> None:
    """Generate AI menus and push to users whose notify_time matches the current minute."""
    import datetime
    db = SessionLocal()
    try:
        now_jst = datetime.datetime.now(tz=datetime.timezone(datetime.timedelta(hours=9)))
        current_hhmm = now_jst.strftime("%H:%M")
        today = date.today()

        users = (
            db.query(User)
            .filter(
                User.status == "active",
                User.onboarding_step == "complete",
                User.notify_time == current_hhmm,
            )
            .all()
        )

        for user in users:
            try:
                # 当日すでにプランまたはログがある場合はスキップ
                if db.query(WorkoutPlan).filter(
                    WorkoutPlan.user_id == user.user_id,
                    WorkoutPlan.date == today,
                ).first():
                    continue
                if db.query(WorkoutLog).filter(
                    WorkoutLog.user_id == user.user_id,
                    WorkoutLog.date == today,
                ).first():
                    continue

                # 連続トレーニング判定 → 休養日通知
                if _is_consecutive_training_days(
                    db, user.user_id, today, _REST_AFTER_CONSECUTIVE_DAYS
                ):
                    db.add(WorkoutLog(
                        user_id=user.user_id,
                        date=today,
                        status="rest",
                        comment="連続トレーニングによる自動休養日",
                    ))
                    db.commit()
                    push_message(user.user_id, _MSG_AUTO_REST)
                    continue

                plan_data = generate_daily_menu(db, user.user_id, today)

                db.add(WorkoutPlan(
                    user_id=user.user_id,
                    date=today,
                    menu_json=json.dumps(plan_data.get("menu", []), ensure_ascii=False),
                    ai_reason=plan_data.get("reason", ""),
                ))
                db.commit()

                push_message(user.user_id, format_menu_message(plan_data))

            except Exception as e:
                print(f"[scheduler] daily notification failed for {user.user_id}: {e}")
                db.rollback()
    finally:
        db.close()


def _send_evening_reminder() -> None:
    """Remind users who haven't logged yet today."""
    db = SessionLocal()
    try:
        today = date.today()
        logged_ids = {
            log.user_id
            for log in db.query(WorkoutLog).filter(WorkoutLog.date == today).all()
        }
        for user in db.query(User).filter(
            User.status == "active", User.onboarding_step == "complete"
        ).all():
            if user.user_id not in logged_ids:
                push_message(
                    user.user_id,
                    "💪 今日の筋トレはどうでしたか？\n「完了」「一部」「なし」で教えてください！",
                )
    finally:
        db.close()


def _send_smart_reminders() -> None:
    """Send reminders based on completion rate: low rate every 5 days, high rate every 3 days."""
    db = SessionLocal()
    try:
        today = date.today()
        seven_days_ago = today - timedelta(days=7)

        users = (
            db.query(User)
            .filter(User.status == "active", User.onboarding_step == "complete")
            .all()
        )

        for user in users:
            try:
                logs = (
                    db.query(WorkoutLog)
                    .filter(
                        WorkoutLog.user_id == user.user_id,
                        WorkoutLog.date >= seven_days_ago,
                    )
                    .all()
                )
                if not logs:
                    continue

                done = sum(1 for l in logs if l.status == "done")
                partial = sum(1 for l in logs if l.status == "partial")
                rest_days = sum(1 for l in logs if l.status == "rest")
                training_total = len(logs) - rest_days
                rate = round((done + partial * 0.5) / training_total * 100) if training_total else 100

                interval = (
                    _REMINDER_HIGH_INTERVAL_DAYS
                    if rate >= _REMINDER_HIGH_RATE_THRESHOLD
                    else _REMINDER_LOW_INTERVAL_DAYS
                )

                last_sent = user.last_reminder_sent
                if last_sent and (today - last_sent).days < interval:
                    continue

                if rate >= _REMINDER_HIGH_RATE_THRESHOLD:
                    msg = (
                        f"🌟 継続率 {rate}% — 絶好調ですね！\n"
                        f"今日も短い時間でいいので体を動かしてみましょう💪\n"
                        f"「筋トレ」でメニューを確認できますよ！"
                    )
                else:
                    msg = (
                        f"👋 最近調子はどうですか？\n"
                        f"忙しい日でも1種目だけでも大丈夫です。\n"
                        f"「筋トレ」と送ると今日のメニューをお届けします💪"
                    )

                push_message(user.user_id, msg)
                user.last_reminder_sent = today
                db.commit()

            except Exception as e:
                print(f"[scheduler] smart reminder failed for {user.user_id}: {e}")
                db.rollback()
    finally:
        db.close()


def _send_weekly_report() -> None:
    """Push 7-day analysis report every Sunday morning."""
    db = SessionLocal()
    try:
        today = date.today()
        seven_days_ago = today - timedelta(days=7)

        for user in db.query(User).filter(
            User.status == "active", User.onboarding_step == "complete"
        ).all():
            try:
                logs = db.query(WorkoutLog).filter(
                    WorkoutLog.user_id == user.user_id,
                    WorkoutLog.date >= seven_days_ago,
                ).all()

                # 1週間実績ゼロ → 熊ストーリーで激励
                if not logs:
                    msg = generate_zero_activity_message(today)
                    push_message(user.user_id, msg)
                    continue

                s = compute_and_save_analysis(db, user.user_id, days=7)
                push_message(
                    user.user_id,
                    (
                        f"📊 今週の記録\n"
                        f"継続率: {s['completion_rate']}%\n"
                        f"完全実施: {s['done_count']}日 / "
                        f"一部: {s['partial_count']}日 / "
                        f"未実施: {s['skipped_count']}日 / "
                        f"休養: {s.get('rest_count', 0)}日\n\n"
                        f"{s['recommendation']}\n\n"
                        f"─────────────────\n"
                        f"📖 便利なコマンド\n"
                        f"「筋トレ」→ 今日のメニュー\n"
                        f"「ステータス確認」→ 継続率・記録\n"
                        f"「設定確認」→ プロフィール・重量設定\n"
                        f"「ヘルプ」→ 全コマンド一覧"
                    ),
                )
            except Exception as e:
                print(f"[scheduler] weekly report failed for {user.user_id}: {e}")
    finally:
        db.close()


def start_scheduler() -> None:
    # Per-user notify_time: check every minute and send to users whose time matches now
    scheduler.add_job(
        _send_daily_notifications,
        CronTrigger(minute="*", timezone="Asia/Tokyo"),
        id="daily_notification",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_evening_reminder,
        CronTrigger(hour=21, minute=0, timezone="Asia/Tokyo"),
        id="evening_reminder",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_weekly_report,
        CronTrigger(day_of_week="sun", hour=8, minute=0, timezone="Asia/Tokyo"),
        id="weekly_report",
        replace_existing=True,
    )
    scheduler.add_job(
        _send_smart_reminders,
        CronTrigger(hour=10, minute=0, timezone="Asia/Tokyo"),
        id="smart_reminder",
        replace_existing=True,
    )
    scheduler.start()
