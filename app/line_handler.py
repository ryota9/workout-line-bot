import base64
import datetime
import hashlib
import hmac
import os
import re
from datetime import date

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)
from sqlalchemy.orm import Session

from .models import User, UserDumbbellWeight, UserExercise, WorkoutLog, WorkoutPlan

_JST = datetime.timezone(datetime.timedelta(hours=9))
_DAY_CUTOFF_HOUR = 3  # 午前3時より前は前日扱い


def get_logical_today() -> date:
    """JST 午前3時を日付切り替えとして扱う。"""
    now = datetime.datetime.now(tz=_JST)
    if now.hour < _DAY_CUTOFF_HOUR:
        return (now - datetime.timedelta(days=1)).date()
    return now.date()


_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")

configuration = Configuration(access_token=_CHANNEL_ACCESS_TOKEN)


# ── Signature verification ────────────────────────────────────────────────────

def verify_signature(body: bytes, signature: str) -> bool:
    """Verify X-Line-Signature header."""
    digest = hmac.new(_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode() == signature


# ── Message parsing ───────────────────────────────────────────────────────────

_STATUS_KEYWORDS = {
    "done": ["した", "やった", "完了", "done", "ok", "◯", "○", "✓", "完璧", "達成"],
    "skipped": ["できなかった", "なし", "skip", "サボ", "休", "×", "✗", "無理", "疲れ"],
}


def parse_reply(text: str) -> str:
    """Map free-text reply to done / partial / skipped."""
    lower = text.lower()
    for status, keywords in _STATUS_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return status
    return "partial"


# ── Follow event handler ─────────────────────────────────────────────────────

_MSG_WELCOME = (
    "友達追加ありがとうございます🎉\n"
    "このbotは毎日あなただけの筋トレメニューをLINEでお届けします💪\n\n"
    "トレーニング完了後に「完了」と返信するだけで記録でき、\n"
    "AIが結果をもとに翌日のメニューを自動調整します！\n\n"
    "まず簡単なヒアリングをさせてください（3問だけ）。"
)


def handle_follow(event: dict, db: Session) -> str | None:
    """Handle LINE follow event (friend added)."""
    user_id: str = event["source"]["userId"]

    existing = db.query(User).filter(User.user_id == user_id).first()
    if existing:
        # ブロック解除などで再フォローされた場合はオンボーディングをやり直す
        existing.onboarding_step = "1"
        db.commit()
    else:
        db.add(User(user_id=user_id, name="未設定", onboarding_step="1"))
        db.commit()

    return _MSG_WELCOME + "\n\n" + _MSG_STEP_0


# ── Onboarding messages ───────────────────────────────────────────────────────

_MSG_STEP_0 = (
    "はじめまして！AI筋トレエージェントです💪\n"
    "いくつか質問して、あなた専用のメニューを作ります！\n\n"
    "【Q1】トレーニングの目標は？\n"
    "1️⃣ 体脂肪を減らしたい\n"
    "2️⃣ 筋肉をつけたい\n"
    "3️⃣ 体力・健康を維持したい\n"
    "4️⃣ その他（自由に入力してください）"
)

_GOAL_OPTIONS = {
    "1": "体脂肪を減らしたい",
    "2": "筋肉をつけたい",
    "3": "体力・健康を維持したい",
}

_MSG_STEP_1 = (
    "【Q2】使える器具は？\n\n"
    "1️⃣ 自重のみ（器具なし）\n"
    "2️⃣ ダンベルあり\n"
    "3️⃣ 自重＋ダンベル両方"
)

_EQUIPMENT_OPTIONS = {
    "1": "bodyweight",
    "2": "dumbbell",
    "3": "both",
}

_EQUIPMENT_KEYWORDS = {
    "dumbbell": ["ダンベル", "dumbbell", "2"],
    "both":     ["両方", "両", "どちらも", "3"],
    "bodyweight": ["自重", "なし", "器具なし", "1"],
}


def parse_equipment(text: str) -> str:
    lower = text.lower()
    for equipment, keywords in _EQUIPMENT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return equipment
    return "bodyweight"


_LEVEL_KEYWORDS = {
    "intermediate": ["中級", "週1", "週2", "している", "動いている", "2"],
    "beginner":     ["初心者", "初", "ほぼない", "なし", "1"],
}


def parse_level(text: str) -> str:
    lower = text.lower()
    for level, keywords in _LEVEL_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return level
    return "beginner"

_EQUIPMENT_LABEL = {
    "bodyweight": "自重のみ",
    "dumbbell": "ダンベルあり",
    "both": "自重＋ダンベル",
}

_MSG_STEP_2 = (
    "【Q3】現在のトレーニングレベルは？\n\n"
    "1️⃣ 初心者（運動習慣がほぼない）\n"
    "2️⃣ 中級者（週1〜2回は体を動かしている）"
)

_LEVEL_OPTIONS = {
    "1": "beginner",
    "2": "intermediate",
}


# ── Core message handler ──────────────────────────────────────────────────────

def handle_message(event: dict, db: Session) -> str | None:
    """
    Process a LINE message event with DB access.
    Returns reply text (None = no reply needed).
    """
    user_id: str = event["source"]["userId"]
    text: str = event["message"]["text"].strip()
    today = get_logical_today()

    user = db.query(User).filter(User.user_id == user_id).first()

    # ── Step 0: 新規ユーザー ──────────────────────────────────────────────────
    if not user:
        db.add(User(user_id=user_id, name="未設定", onboarding_step="1"))
        db.commit()
        return _MSG_STEP_0

    # ── オンボーディング中 ────────────────────────────────────────────────────
    step = user.onboarding_step or "complete"

    if step == "1":
        goal = _GOAL_OPTIONS.get(text, text)  # 番号 or 自由入力
        user.goal = goal
        user.onboarding_step = "2"
        db.commit()
        return f"「{goal}」ですね！\n\n" + _MSG_STEP_1

    if step == "2":
        equipment = parse_equipment(text)
        user.equipment = equipment
        user.onboarding_step = "3"
        db.commit()
        label = _EQUIPMENT_LABEL[equipment]
        return f"「{label}」で進めますね！\n\n" + _MSG_STEP_2

    if step == "3":
        level = parse_level(text)
        user.level = level
        user.onboarding_step = "complete"
        db.commit()
        level_label = "初心者" if level == "beginner" else "中級者"
        dumbbell_guide = (
            "\n💡 ダンベルを使う場合は重量も設定しておくと、\nAIがより適切なメニューを組めます。\n「重量設定」で確認・更新できます。\n"
            if user.equipment in ("dumbbell", "both") else ""
        )
        return (
            f"設定完了です🎉\n\n"
            f"📋 あなたのプロフィール\n"
            f"目標：{user.goal}\n"
            f"器具：{_EQUIPMENT_LABEL.get(user.equipment, user.equipment)}\n"
            f"レベル：{level_label}\n"
            f"{dumbbell_guide}\n"
            f"明日の朝から毎日メニューをお届けします💪\n"
            f"今すぐ試したい場合は「今日のメニュー」と送ってください！\n\n"
            f"「ヘルプ」でいつでもコマンド一覧を確認できます📖"
        )

    # ── pending_action チェック（状態依存の返答を最優先）──────────────────────
    if user.pending_action == "propose_exercise":
        exercise_name = text.strip()
        db.add(UserExercise(user_id=user_id, exercise_name=exercise_name))
        user.pending_action = None
        db.commit()
        return (
            f"「{exercise_name}」を登録しました💪\n"
            f"次回のメニュー生成から取り入れていきます！"
        )

    if user.pending_action == "change_notify_time":
        new_time = _parse_notify_time(text)
        if new_time:
            user.notify_time = new_time
            user.pending_action = None
            db.commit()
            return f"通知時刻を {new_time} に変更しました⏰\n毎朝その時間にメニューをお届けします！"
        else:
            return (
                "時刻の形式が読み取れませんでした😥\n"
                "「8時」「8:30」「午前8時」などの形式で教えてください。"
            )

    if user.pending_action == "change_goal":
        goal = _GOAL_OPTIONS.get(text.strip(), text.strip())
        user.goal = goal
        user.pending_action = None
        db.commit()
        return (
            f"目標を「{goal}」に変更しました📋\n"
            f"次回のメニュー生成から反映されます💪\n"
            f"「設定確認」で現在の設定を確認できます。"
        )

    if user.pending_action == "change_level":
        level = _LEVEL_OPTIONS.get(text.strip()) or parse_level(text)
        level_label = "初心者" if level == "beginner" else "中級者"
        user.level = level
        user.pending_action = None
        db.commit()
        return (
            f"レベルを「{level_label}」に変更しました💪\n"
            f"次回のメニュー生成から反映されます。\n"
            f"「設定確認」で現在の設定を確認できます。"
        )

    if user.pending_action == "change_equipment":
        equipment = _EQUIPMENT_OPTIONS.get(text.strip()) or parse_equipment(text)
        label = _EQUIPMENT_LABEL[equipment]
        user.equipment = equipment
        user.pending_action = None
        db.commit()
        dumbbell_tip = (
            "\n💡 ダンベルの重量は「重量設定」で登録すると\nAIがより適切なメニューを組めます！"
            if equipment in ("dumbbell", "both") else ""
        )
        return (
            f"器具を「{label}」に変更しました🏋️\n"
            f"次回のメニュー生成から反映されます。{dumbbell_tip}\n"
            f"「設定確認」で現在の設定を確認できます。"
        )

    # ── 通知時刻の変更 ────────────────────────────────────────────────────────
    if any(kw in text for kw in ["通知時刻", "通知時間", "通知を", "通知変更", "時間変更", "時刻変更"]):
        # "8時に変えて" のような文から直接パースを試みる
        quick_time = _parse_notify_time(text)
        if quick_time:
            user.notify_time = quick_time
            db.commit()
            return f"通知時刻を {quick_time} に変更しました⏰\n毎朝その時間にメニューをお届けします！"
        # 時刻が含まれていない場合は聞き返す
        user.pending_action = "change_notify_time"
        db.commit()
        return (
            f"何時に変更しますか？⏰\n"
            f"現在の設定: {user.notify_time}\n\n"
            f"「8時」「8:30」「午前7時半」などの形式で教えてください。"
        )

    # ── 目標変更 ──────────────────────────────────────────────────────────────
    if any(kw in text for kw in ["目標変更", "目標を変更", "目標変えたい", "ゴール変更"]):
        user.pending_action = "change_goal"
        db.commit()
        return (
            f"目標を変更します📋\n"
            f"現在：{user.goal or '未設定'}\n\n"
            + _MSG_STEP_0.split("【Q1】")[1]
        )

    # ── レベル変更 ────────────────────────────────────────────────────────────
    if any(kw in text for kw in ["レベル変更", "レベルを変更", "レベル変えたい", "難易度変更"]):
        level_label = "初心者" if user.level == "beginner" else "中級者"
        user.pending_action = "change_level"
        db.commit()
        return (
            f"レベルを変更します💪\n"
            f"現在：{level_label}\n\n"
            + _MSG_STEP_2

        )

    # ── 器具変更 ──────────────────────────────────────────────────────────────
    if any(kw in text for kw in ["器具変更", "器具を変更", "器具変えたい", "機材変更"]):
        current_label = _EQUIPMENT_LABEL.get(user.equipment or "bodyweight", user.equipment or "bodyweight")
        user.pending_action = "change_equipment"
        db.commit()
        return (
            f"使用器具を変更します🏋️\n"
            f"現在：{current_label}\n\n"
            + _MSG_STEP_1
        )

    # ── ヘルプ ────────────────────────────────────────────────────────────────
    if any(kw in text for kw in ["ヘルプ", "help", "使い方", "コマンド", "操作方法"]):
        return _MSG_HELP

    # ── 設定確認 ──────────────────────────────────────────────────────────────
    if any(kw in text for kw in ["設定確認", "設定を確認", "プロフィール", "マイページ"]):
        return _build_settings_reply(user, db)

    # ── ステータス確認 ────────────────────────────────────────────────────────
    if any(kw in text for kw in ["ステータス", "状況", "記録確認", "今日どう", "進捗"]):
        return _build_status_reply(user_id, today, db)

    # ── 新種目提案 ────────────────────────────────────────────────────────────
    if any(kw in text for kw in ["新種目", "種目追加", "種目提案", "追加したい"]):
        user.pending_action = "propose_exercise"
        db.commit()
        existing_exercises = db.query(UserExercise).filter(
            UserExercise.user_id == user_id
        ).all()
        existing_list = "・".join(e.exercise_name for e in existing_exercises) or "なし"
        return (
            "どんな種目を追加したいですか？\n"
            "種目名をそのまま送ってください💡\n\n"
            f"例：チンアップ、バーピー、ケトルベルスイング\n\n"
            f"現在の登録種目：{existing_list}"
        )

    # ── ダンベル重量設定 ──────────────────────────────────────────────────────
    # 「重量設定」コマンド → 現状表示 + 入力ガイド
    if any(kw in text for kw in ["重量設定", "重量確認", "重量変更", "ダンベル重量", "重さ設定", "重さ確認"]):
        status = _build_weight_status(db, user_id)
        return (
            f"{status}\n\n"
            f"更新するには部位と重量を送ってください💡\n"
            f"例：「胸:10 肩:7.5 背中:12」\n"
            f"　　「脚15kg」「ハムストリング:10」"
        )

    # 部位＋重量パターンを直接検出（"胸10kg" "肩は7kg" "胸:10 肩:7" など）
    parsed_weights = _parse_weight_inputs(text)
    if parsed_weights:
        _upsert_weights(db, user_id, parsed_weights)
        updated = "・".join(
            f"{_BODY_PART_JA.get(k, k)} {v}kg" for k, v in parsed_weights.items()
        )
        return (
            f"重量を更新しました💪\n{updated}\n\n"
            f"次回のメニューからこの重量を参考に種目を組みます！\n"
            f"「重量設定」で全部位の設定を確認できます。"
        )

    # ── 今日のメニューリクエスト ──────────────────────────────────────────────
    if any(kw in text for kw in ["今日のメニュー", "メニュー", "送って", "教えて",
                                  "筋トレ", "トレーニング", "始め", "スタート", "やろ", "やる"]):
        return _generate_menu_reply(user_id, today, db)

    # ── 能動的な休養申請 ──────────────────────────────────────────────────────
    if any(kw in text for kw in ["休みたい", "休養日", "オフ日", "今日は休", "休憩したい", "レストデイ", "rest day"]):
        existing_log = (
            db.query(WorkoutLog)
            .filter(WorkoutLog.user_id == user_id, WorkoutLog.date == today)
            .first()
        )
        if existing_log:
            existing_log.status = "rest"
            existing_log.comment = "ユーザー申請による休養日"
        else:
            db.add(WorkoutLog(
                user_id=user_id, date=today, status="rest", comment="ユーザー申請による休養日"
            ))
        db.commit()
        return (
            "🌿 今日を休養日に設定しました！\n\n"
            "しっかり休んで、明日また頑張りましょう💪\n"
            "継続率には影響しません。\n\n"
            "「筋トレ」と送ればメニューも確認できます。"
        )

    # ── ダンベルへの切り替えリクエスト ───────────────────────────────────────
    if "ダンベルに変更" in text or "ダンベル変更" in text:
        user.equipment = "dumbbell"
        db.commit()
        return (
            "了解です！🏋️ 明日からダンベルメニューに切り替えます。\n"
            "ダンベルの重さは無理のない重さからスタートして、\n"
            "フォームを優先してくださいね💪"
        )

    # ── Workout reply ─────────────────────────────────────────────────────────
    plan = (
        db.query(WorkoutPlan)
        .filter(WorkoutPlan.user_id == user_id, WorkoutPlan.date == today)
        .first()
    )
    if plan is None:
        return "本日のメニューは準備中です。もうしばらくお待ちください！"

    status = parse_reply(text)

    existing = (
        db.query(WorkoutLog)
        .filter(WorkoutLog.user_id == user_id, WorkoutLog.date == today)
        .first()
    )
    if existing:
        existing.status = status
        existing.comment = text
    else:
        db.add(WorkoutLog(user_id=user_id, date=today, status=status, comment=text))
    db.commit()

    replies = {
        "done": "完璧です！🎉 記録しました✅",
        "partial": "お疲れ様です💪 一部実施として記録しました✅",
        "skipped": "明日また頑張りましょう！ 記録しました✅",
    }
    return replies[status]


# ── LINE API helpers ──────────────────────────────────────────────────────────

def reply_message(reply_token: str, text: str) -> None:
    with ApiClient(configuration) as client:
        MessagingApi(client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(type="text", text=text)],
            )
        )


def push_message(user_id: str, text: str) -> None:
    with ApiClient(configuration) as client:
        MessagingApi(client).push_message(
            PushMessageRequest(
                to=user_id,
                messages=[TextMessage(type="text", text=text)],
            )
        )


def _build_status_reply(user_id: str, today, db: Session) -> str:
    """今日のステータスと直近の継続率をまとめて返す。"""
    from datetime import timedelta

    plan = db.query(WorkoutPlan).filter(
        WorkoutPlan.user_id == user_id, WorkoutPlan.date == today
    ).first()

    log = db.query(WorkoutLog).filter(
        WorkoutLog.user_id == user_id, WorkoutLog.date == today
    ).first()

    # 直近7日の継続率
    seven_days_ago = today - timedelta(days=7)
    recent_logs = db.query(WorkoutLog).filter(
        WorkoutLog.user_id == user_id,
        WorkoutLog.date >= seven_days_ago,
    ).all()
    if recent_logs:
        done = sum(1 for l in recent_logs if l.status == "done")
        partial = sum(1 for l in recent_logs if l.status == "partial")
        rate = round((done + partial * 0.5) / len(recent_logs) * 100)
    else:
        rate = 0

    menu_status = "✅ 生成済み" if plan else "⏳ 未生成"
    log_status_map = {
        "done": "✅ 完了",
        "partial": "🔶 一部実施",
        "skipped": "❌ 未実施",
        "rest": "🌿 休養日",
    }
    log_status = log_status_map.get(log.status, "📝 未記録") if log else "📝 未記録"

    return (
        f"📊 今日のステータス（{today}）\n\n"
        f"メニュー：{menu_status}\n"
        f"実施記録：{log_status}\n"
        f"直近7日の継続率：{rate}%\n\n"
        f"{'メニューを見るには「メニュー」と送ってください💪' if plan else 'メニューを作るには「筋トレ」と送ってください💪'}"
    )


def _generate_menu_reply(user_id: str, today, db: Session) -> str:
    """ユーザーのメニューリクエストに応答する。既存メニューがあれば再表示、なければ生成。"""
    from .ai_agent import generate_daily_menu
    import json as _json

    existing_plan = (
        db.query(WorkoutPlan)
        .filter(WorkoutPlan.user_id == user_id, WorkoutPlan.date == today)
        .first()
    )

    if existing_plan:
        plan_data = {
            "menu": _json.loads(existing_plan.menu_json),
            "message": "今日のメニューはこちらです！",
        }
        return format_menu_message(plan_data)

    try:
        plan_data = generate_daily_menu(db, user_id, today)
        db.add(WorkoutPlan(
            user_id=user_id,
            date=today,
            menu_json=_json.dumps(plan_data.get("menu", []), ensure_ascii=False),
            ai_reason=plan_data.get("reason", ""),
        ))
        # 休養日ログがあっても「筋トレ」を明示的に送ったので削除して普通のメニューに戻す
        rest_log = (
            db.query(WorkoutLog)
            .filter(WorkoutLog.user_id == user_id, WorkoutLog.date == today, WorkoutLog.status == "rest")
            .first()
        )
        if rest_log:
            db.delete(rest_log)
        db.commit()
        return format_menu_message(plan_data)
    except Exception as e:
        print(f"[line_handler] menu generation failed for {user_id}: {e}")
        return "メニューの生成に失敗しました。しばらくしてからもう一度お試しください🙏"


# ── Dumbbell weight helpers ───────────────────────────────────────────────────

_BODY_PART_MAP = {
    "胸": "chest",
    "肩": "shoulder",
    "背中": "back",
    "背": "back",
    "首": "neck",
    "腹部": "abs",
    "腹": "abs",
    "ハムストリング": "hamstrings",
    "脚": "legs",
    "足": "legs",
    "二頭筋": "biceps",
    "上腕二頭筋": "biceps",
    "二頭": "biceps",
    "三頭筋": "triceps",
    "上腕三頭筋": "triceps",
    "三頭": "triceps",
}

_BODY_PART_JA = {v: k for k, v in _BODY_PART_MAP.items() if len(k) > 1}
_BODY_PART_JA.update({
    "chest": "胸", "shoulder": "肩", "back": "背中", "neck": "首",
    "abs": "腹部", "hamstrings": "ハムストリング", "legs": "脚",
    "biceps": "二頭筋", "triceps": "三頭筋",
})

_ALL_PARTS = ["chest", "shoulder", "back", "neck", "abs", "hamstrings", "legs", "biceps", "triceps"]

# "胸:10 肩:7.5 背中:12" or "胸10kg" or "肩は7kg"
_WEIGHT_MULTI_PATTERN = re.compile(
    r"(胸|肩|背中?|首|腹部?|ハムストリング|脚|足|二頭筋?|上腕二頭筋|三頭筋?|上腕三頭筋)"
    r"[はをのの:：]?\s*(\d+(?:\.\d+)?)\s*(?:kg|キロ|ｋｇ)?",
    re.IGNORECASE,
)


def _parse_weight_inputs(text: str) -> dict[str, str]:
    """テキストから {body_part_key: weight_str} を抽出する。複数部位対応。"""
    results = {}
    for m in _WEIGHT_MULTI_PATTERN.finditer(text):
        part_ja = m.group(1)
        weight = m.group(2)
        # 最長一致で部位を特定
        key = next(
            (v for k, v in sorted(_BODY_PART_MAP.items(), key=lambda x: -len(x[0]))
             if part_ja.startswith(k)),
            None,
        )
        if key:
            results[key] = weight
    return results


def _upsert_weights(db, user_id: str, weight_dict: dict[str, str]) -> None:
    for part, kg in weight_dict.items():
        existing = (
            db.query(UserDumbbellWeight)
            .filter(UserDumbbellWeight.user_id == user_id, UserDumbbellWeight.body_part == part)
            .first()
        )
        if existing:
            existing.weight_kg = kg
        else:
            db.add(UserDumbbellWeight(user_id=user_id, body_part=part, weight_kg=kg))
    db.commit()


def _build_weight_status(db, user_id: str) -> str:
    rows = db.query(UserDumbbellWeight).filter(UserDumbbellWeight.user_id == user_id).all()
    weight_map = {r.body_part: r.weight_kg for r in rows}
    lines = [
        f"  {_BODY_PART_JA.get(p, p)}: {weight_map[p]}kg" if p in weight_map else f"  {_BODY_PART_JA.get(p, p)}: 未設定"
        for p in _ALL_PARTS
    ]
    return "🏋️ 部位別ダンベル重量\n" + "\n".join(lines)


_MSG_HELP = """\
📖 コマンド一覧

🏋️ トレーニング
「筋トレ」→ 今日のメニューを表示
「完了」「やった」→ 完了を記録
「できなかった」「なし」→ スキップを記録
「今日は休みたい」→ 休養日を設定（継続率に影響なし）

📊 確認
「ステータス確認」→ 今日の状況・継続率
「設定確認」→ プロフィール・通知設定・重量

⚙️ 設定変更
「目標変更」→ トレーニング目標を変更
「レベル変更」→ 初心者／中級者を変更
「器具変更」→ 使用器具を変更
「重量設定」→ ダンベル重量の確認・更新
　例：「胸:10 肩:7.5 背中:12」
「通知時刻変更」→ 通知時刻の変更
　例：「通知を8時に変えて」
「新種目追加したい」→ 種目の追加登録

❓ ヘルプ
「ヘルプ」→ このコマンド一覧を表示"""


def _build_settings_reply(user: "User", db: Session) -> str:
    level_label = "初心者" if user.level == "beginner" else "中級者"
    equipment_label = _EQUIPMENT_LABEL.get(user.equipment or "bodyweight", user.equipment or "bodyweight")
    weight_status = _build_weight_status(db, user.user_id)

    return (
        f"📋 現在の設定\n\n"
        f"目標：{user.goal or '未設定'}\n"
        f"器具：{equipment_label}\n"
        f"レベル：{level_label}\n"
        f"通知時刻：{user.notify_time or '07:00'}\n\n"
        f"{weight_status}\n\n"
        f"変更したい項目を送ってください：\n"
        f"「目標変更」「レベル変更」「器具変更」\n"
        f"「重量設定」「通知時刻変更」\n"
        f"「ヘルプ」で全コマンドを確認できます"
    )


def _parse_notify_time(text: str) -> str | None:
    """
    Parse a Japanese time expression and return "HH:MM" string.
    Handles: "8時", "8:30", "08:00", "午前7時半", "夜9時", etc.
    Returns None if no valid time found.
    """
    # HH:MM / H:MM 形式
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    # 午前/午後 + 時 + 分 (optional)
    m = re.search(r"(午前|午後|朝|夜|夕方)?(\d{1,2})時(半|(\d{1,2})分)?", text)
    if m:
        prefix = m.group(1) or ""
        h = int(m.group(2))
        min_part = m.group(3)
        if min_part == "半":
            mi = 30
        elif min_part:
            mi = int(m.group(4))
        else:
            mi = 0

        if prefix in ("午後", "夜", "夕方") and h < 12:
            h += 12
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    return None


def format_menu_message(plan_data: dict) -> str:
    """Build the daily notification text from AI plan data."""
    menu = plan_data.get("menu", [])
    intro = plan_data.get("message", "今日も一緒に頑張りましょう！")

    lines = []
    for item in menu:
        weight_str = f"  {item['weight_kg']}kg" if item.get("weight_kg") else ""
        line = f"▶ {item['exercise']}  {item['sets']}セット × {item['reps']}{weight_str}"
        if item.get("note"):
            line += f"\n  💡 {item['note']}"
        lines.append(line)

    menu_block = "\n".join(lines)
    return f"{intro}\n\n【本日のメニュー】\n{menu_block}\n\n終わったら「完了」と返信してね！"
