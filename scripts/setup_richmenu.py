"""
リッチメニューをLINEに登録するワンタイムスクリプト。
実行: PYTHONPATH= ../venv/bin/python setup_richmenu.py
"""
import io
import json
import os
import sys

import requests
from dotenv import load_dotenv
from PIL import Image

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))

TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
if not TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN が設定されていません")

HEADERS = {"Authorization": f"Bearer {TOKEN}"}

IMAGE_PATH = os.path.join(os.path.dirname(__file__), "../picture/リッチメニュー画像_大.png")

W, H = 1200, 810
BTN_W = W // 3  # 400

# ── LINE API ──────────────────────────────────────────────────────────────────

def create_rich_menu() -> str:
    payload = {
        "size": {"width": W, "height": H},
        "selected": True,
        "name": "メインメニュー",
        "chatBarText": "メニュー",
        "areas": [
            {
                "bounds": {"x": i * BTN_W, "y": 0, "width": BTN_W, "height": H},
                "action": {"type": "message", "text": text},
            }
            for i, text in enumerate(["今日のメニュー", "ステータス確認", "新種目追加したい"])
        ],
    }
    res = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers={**HEADERS, "Content-Type": "application/json"},
        data=json.dumps(payload),
    )
    res.raise_for_status()
    return res.json()["richMenuId"]


def compress_to_jpeg(image_bytes: bytes, max_bytes: int = 900_000) -> bytes:
    """PNG を JPEG に変換し 1MB 以内に収める。"""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    for quality in (85, 75, 65, 55):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes:
            print(f"JPEG 変換完了: {len(data):,} bytes (quality={quality})")
            return data
    raise RuntimeError("画像を 1MB 以下に圧縮できませんでした")


def upload_image(rich_menu_id: str, image_bytes: bytes) -> None:
    requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
        headers={**HEADERS, "Content-Type": "image/jpeg"},
        data=image_bytes,
    ).raise_for_status()


def set_default(rich_menu_id: str) -> None:
    requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers=HEADERS,
    ).raise_for_status()


def delete_existing() -> None:
    res = requests.get("https://api.line.me/v2/bot/richmenu/list", headers=HEADERS)
    for menu in res.json().get("richmenus", []):
        requests.delete(
            f"https://api.line.me/v2/bot/richmenu/{menu['richMenuId']}",
            headers=HEADERS,
        )
        print(f"削除: {menu['richMenuId']}")


# ── main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not os.path.exists(IMAGE_PATH):
        sys.exit(f"画像が見つかりません: {IMAGE_PATH}")

    print("既存メニューを削除中...")
    delete_existing()

    print(f"画像を読み込み中... ({IMAGE_PATH})")
    with open(IMAGE_PATH, "rb") as f:
        image_bytes = f.read()
    print(f"元画像サイズ: {len(image_bytes):,} bytes")
    image_bytes = compress_to_jpeg(image_bytes)

    print("リッチメニューを作成中...")
    rich_menu_id = create_rich_menu()
    print(f"作成: {rich_menu_id}")

    print("画像をアップロード中...")
    upload_image(rich_menu_id, image_bytes)

    print("デフォルトに設定中...")
    set_default(rich_menu_id)

    print("✅ リッチメニューのセットアップ完了！")
    print("LINEアプリを再起動すると反映されます。")
