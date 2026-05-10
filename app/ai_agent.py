import json
import os
import re
from datetime import date, timedelta

from google import genai
from sqlalchemy.orm import Session

from .models import User, UserDumbbellWeight, WorkoutLog, WorkoutPlan

_BODY_PART_JA = {
    "chest": "胸",
    "shoulder": "肩",
    "back": "背中",
    "neck": "首",
    "abs": "腹部",
    "hamstrings": "ハムストリング",
    "legs": "脚",
    "biceps": "二頭筋",
    "triceps": "三頭筋",
}

# 自重 → ダンベル切り替えを提案する条件
# 直近14日で「done」が10日以上 かつ equipment が bodyweight
_DUMBBELL_SUGGEST_MIN_DONE_DAYS = 10
_DUMBBELL_SUGGEST_PERIOD_DAYS = 14

_client: genai.Client | None = None

def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    return _client


def _check_dumbbell_ready(db: Session, user_id: str, equipment: str) -> bool:
    """自重ユーザーがダンベルに挑戦できる段階か判定する。"""
    if equipment != "bodyweight":
        return False
    cutoff = date.today() - timedelta(days=_DUMBBELL_SUGGEST_PERIOD_DAYS)
    logs = (
        db.query(WorkoutLog)
        .filter(WorkoutLog.user_id == user_id, WorkoutLog.date >= cutoff)
        .all()
    )
    done_days = sum(1 for l in logs if l.status == "done")
    return done_days >= _DUMBBELL_SUGGEST_MIN_DONE_DAYS


def _get_recent_context(db: Session, user_id: str) -> dict:
    """Collect last-7-day metrics and user profile for the AI prompt."""
    today = date.today()
    seven_days_ago = today - timedelta(days=7)

    logs = (
        db.query(WorkoutLog)
        .filter(WorkoutLog.user_id == user_id, WorkoutLog.date >= seven_days_ago)
        .order_by(WorkoutLog.date)
        .all()
    )

    done = sum(1 for l in logs if l.status == "done")
    partial = sum(1 for l in logs if l.status == "partial")
    skipped = sum(1 for l in logs if l.status == "skipped")
    rest = sum(1 for l in logs if l.status == "rest")
    total = len(logs)
    training_total = total - rest
    completion_rate = round((done + partial * 0.5) / training_total * 100) if training_total else 100

    day_ja = {
        "Monday": "月", "Tuesday": "火", "Wednesday": "水",
        "Thursday": "木", "Friday": "金", "Saturday": "土", "Sunday": "日",
    }
    skipped_days = list({
        day_ja.get(l.date.strftime("%A"), "") for l in logs if l.status == "skipped"
    })
    recent_comments = [l.comment for l in logs if l.comment][-3:]

    user = db.query(User).filter(User.user_id == user_id).first()
    equipment = user.equipment or "bodyweight" if user else "bodyweight"

    # 直近5日分のメニュー履歴
    five_days_ago = today - timedelta(days=5)
    recent_plans = (
        db.query(WorkoutPlan)
        .filter(WorkoutPlan.user_id == user_id, WorkoutPlan.date >= five_days_ago)
        .order_by(WorkoutPlan.date)
        .all()
    )
    recent_menus = [
        {
            "date": str(p.date),
            "exercises": [item["exercise"] for item in json.loads(p.menu_json)],
        }
        for p in recent_plans
    ]

    # 部位別ダンベル重量
    weight_rows = (
        db.query(UserDumbbellWeight)
        .filter(UserDumbbellWeight.user_id == user_id)
        .all()
    )
    dumbbell_weights = {r.body_part: r.weight_kg for r in weight_rows}

    return {
        "recent_logs": [
            {"date": str(l.date), "status": l.status, "comment": l.comment}
            for l in logs
        ],
        "metrics": {
            "completion_rate": completion_rate,
            "done": done,
            "partial": partial,
            "skipped": skipped,
            "skipped_days": skipped_days,
        },
        "recent_comments": recent_comments,
        "user": {
            "goal": user.goal or "体力維持" if user else "体力維持",
            "level": user.level or "beginner" if user else "beginner",
            "equipment": equipment,
            "dumbbell_ready": _check_dumbbell_ready(db, user_id, equipment),
            "dumbbell_weights": dumbbell_weights,
        },
        "recent_menus": recent_menus,
    }


def generate_zero_activity_message(target_date: date) -> str:
    """Generate a weekly bear-fight motivational story for users with no activity."""
    week_num = target_date.isocalendar()[1]
    prompt = f"""あなたは筋トレ習慣化botのコピーライターです。
週次レポートの代わりに送る激励メッセージを作ってください。

## 設定
- 山の中で熊と対峙するユーザーの短編ストーリー（180〜220文字）
- 今週（{target_date}・第{week_num}週）ならではの展開にすること（毎週異なる状況・季節・熊の種類・結末など）
- ストーリーの最後に、筋トレを始めるための激励の一言で締めること
- 全体250文字以内、絵文字は1〜2個まで

## 出力形式（このJSONのみ返してください）
{{"message": "ストーリー＋激励の一言"}}"""

    try:
        response = _get_client().models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text
        json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
        raw = json_match.group(1) if json_match else text
        return json.loads(raw)["message"]
    except Exception as e:
        print(f"[ai_agent] bear story generation failed: {e}")
        return (
            "山奥で巨大な熊と目が合った。逃げ場はない。\n"
            "だが、鍛えた体があれば話は別だ。\n"
            "「筋トレ」と送って、今週こそ戦いを始めよう💪"
        )


def generate_daily_menu(db: Session, user_id: str, target_date: date) -> dict:
    """Generate today's workout plan via Claude."""
    ctx = _get_recent_context(db, user_id)
    m = ctx["metrics"]

    weights = ctx["user"]["dumbbell_weights"]
    weight_lines = (
        "\n".join(f"  - {_BODY_PART_JA.get(k, k)}: {v}kg" for k, v in weights.items())
        if weights else "  （未設定）"
    )

    recent_menus = ctx.get("recent_menus", [])
    if recent_menus:
        menu_history_lines = "\n".join(
            f"  {entry['date']}: {', '.join(entry['exercises'])}"
            for entry in recent_menus
        )
        menu_history_block = f"## 直近のメニュー履歴（重複回避に使うこと）\n{menu_history_lines}"
    else:
        menu_history_block = "## 直近のメニュー履歴\n  （なし・初回）"

    prompt = f"""あなたは筋トレコーチAIです。ユーザーの過去データをもとに、
本日（{target_date}）のメニューと声かけメッセージを生成してください。

## ユーザー情報
- 目標: {ctx['user']['goal']}
- レベル: {ctx['user']['level']}
- 使用器具: {ctx['user']['equipment']}（bodyweight=自重のみ / dumbbell=ダンベル / both=両方）
- dumbbell_ready: {ctx['user']['dumbbell_ready']}（直近14日で完全実施10日以上の場合 true）

{menu_history_block}

## 部位別ダンベル重量（ユーザー設定値）
{weight_lines}

## 直近7日間の実施データ
- 継続率: {m['completion_rate']}%（完全実施 {m['done']}日 / 一部 {m['partial']}日 / 未実施 {m['skipped']}日）
- サボりがちな曜日: {', '.join(m['skipped_days']) or 'なし'}
- 直近コメント: {' / '.join(ctx['recent_comments']) or 'なし'}

## 調整ルール
- 直近のメニュー履歴に含まれる種目をそのまま繰り返さない
- 直近で鍛えた部位とは別の部位を中心に組む（部位のローテーションを意識する）
- 使用器具が bodyweight → 自重種目のみ（ダンベル種目を含めない）、weight_kg は null
- 使用器具が dumbbell / both → ダンベル種目にはユーザーの部位別重量を参考に weight_kg を設定する
  - 重量が未設定の部位は、レベルと種目から妥当な重量を推定して設定する
  - 継続率が80%以上 → 前回より+1〜2kgの増量を提案してもよい
  - 疲労コメントがある → weight_kg を設定値より1段階軽くする
- 継続率が50%未満 → メニューを2〜3種目に絞り、量を減らす
- 継続率が80%以上 → 徐々に負荷を上げる提案を入れる
- 疲労コメントがある → 休養日または軽めのメニューを提案
- dumbbell_ready が true → メッセージの末尾に「最近すごく調子がいいですね！ダンベルに挑戦する準備ができているかもしれません💪 興味があれば「ダンベルに変更」と送ってください」と一言添える（自重メニューは維持したまま）

## 出力形式（必ずこのJSONのみ返してください）
```json
{{
  "menu": [
    {{"exercise": "種目名", "sets": 3, "reps": "10回", "weight_kg": 10, "note": "ポイント"}},
    {{"exercise": "種目名（自重）", "sets": 3, "reps": "15回", "weight_kg": null, "note": "ポイント"}}
  ],
  "message": "今日のLINE通知文（絵文字可・150文字以内）",
  "reason": "このメニューにした理由（50文字以内・内部記録用）"
}}
```"""

    response = _get_client().models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    text = response.text
    json_match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    raw = json_match.group(1) if json_match else text

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # フォールバック: デフォルトメニュー
        return {
            "menu": [
                {"exercise": "腕立て伏せ", "sets": 3, "reps": "10回", "note": "肘を90度まで曲げる"},
                {"exercise": "スクワット", "sets": 3, "reps": "15回", "note": "膝がつま先を超えないように"},
            ],
            "message": "今日も一緒に頑張りましょう💪",
            "reason": "AI生成エラーのためデフォルトメニューを使用",
        }
