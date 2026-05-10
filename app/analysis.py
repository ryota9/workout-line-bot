import json
from collections import Counter
from datetime import date, timedelta

from sqlalchemy.orm import Session

from .models import AnalysisSummary, WorkoutLog


def compute_analysis(db: Session, user_id: str, days: int = 60) -> dict:
    """Compute workout analytics for the last N days."""
    today = date.today()
    start_date = today - timedelta(days=days)

    logs = (
        db.query(WorkoutLog)
        .filter(
            WorkoutLog.user_id == user_id,
            WorkoutLog.date >= start_date,
            WorkoutLog.date <= today,
        )
        .all()
    )

    if not logs:
        return {
            "completion_rate": 0,
            "done_count": 0,
            "partial_count": 0,
            "skipped_count": 0,
            "skipped_days": {},
            "recommendation": "まだデータが不足しています。まず3日間続けてみましょう！",
        }

    done = sum(1 for l in logs if l.status == "done")
    partial = sum(1 for l in logs if l.status == "partial")
    skipped = sum(1 for l in logs if l.status == "skipped")
    rest = sum(1 for l in logs if l.status == "rest")
    total = len(logs)

    # rest は計算対象外（分母から除く）
    training_total = total - rest
    completion_rate = round((done + partial * 0.5) / training_total * 100, 1) if training_total else 100.0

    # 曜日別スキップ傾向（日本語）
    day_ja = {
        "Monday": "月曜", "Tuesday": "火曜", "Wednesday": "水曜",
        "Thursday": "木曜", "Friday": "金曜", "Saturday": "土曜", "Sunday": "日曜",
    }
    skipped_days = Counter(
        day_ja.get(l.date.strftime("%A"), l.date.strftime("%A"))
        for l in logs if l.status == "skipped"
    )

    recommendation = _build_recommendation(completion_rate, skipped_days, done, partial, skipped)

    return {
        "completion_rate": completion_rate,
        "done_count": done,
        "partial_count": partial,
        "skipped_count": skipped,
        "rest_count": rest,
        "skipped_days": dict(skipped_days),
        "recommendation": recommendation,
    }


def compute_and_save_analysis(db: Session, user_id: str, days: int = 60) -> dict:
    """Compute and persist analysis summary."""
    today = date.today()
    start_date = today - timedelta(days=days)
    result = compute_analysis(db, user_id, days)
    period = f"{start_date}_{today}"

    existing = (
        db.query(AnalysisSummary)
        .filter(AnalysisSummary.user_id == user_id, AnalysisSummary.period == period)
        .first()
    )
    if existing:
        existing.completion_rate = str(result["completion_rate"])
        existing.skipped_pattern = json.dumps(result["skipped_days"], ensure_ascii=False)
        existing.recommendation = result["recommendation"]
    else:
        db.add(AnalysisSummary(
            user_id=user_id,
            period=period,
            completion_rate=str(result["completion_rate"]),
            skipped_pattern=json.dumps(result["skipped_days"], ensure_ascii=False),
            recommendation=result["recommendation"],
        ))

    db.commit()
    return result


def _build_recommendation(
    completion_rate: float,
    skipped_days: Counter,
    done: int,
    partial: int,
    skipped: int,
) -> str:
    if completion_rate >= 80:
        return "素晴らしい継続率です！この調子で習慣化を完成させましょう🔥"
    elif completion_rate >= 60:
        return "いい調子です。あと少しで習慣化の域に入ります💪"
    else:
        hint = ""
        if skipped_days:
            worst_day = skipped_days.most_common(1)[0][0]
            hint = f"{worst_day}は特にスキップが多めです。その日だけメニューを軽くするのも手です。"
        return f"まず週3〜4日完遂を目標にしましょう。{hint}"
