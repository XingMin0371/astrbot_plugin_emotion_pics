"""emotion_pics 的面板接口：上传图片、标情绪、编辑关键词。"""

from __future__ import annotations

import base64
import json
import os
import uuid

from quart import jsonify, request

from astrbot.api import logger

def _plugin_data_dir() -> str:
    env = os.environ.get("EMOTION_PICS_DATA")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    plugins_dir = os.path.dirname(here)
    data_dir = os.path.dirname(plugins_dir) if os.path.basename(plugins_dir) == "plugins" else "/AstrBot/data"
    return os.path.join(data_dir, "plugin_data", "astrbot_plugin_emotion_pics")


DATA_DIR = _plugin_data_dir()
IMAGE_DIR = os.path.join(DATA_DIR, "images")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")

ALLOWED = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

DEFAULT_EMOTIONS = {
    "开心": ["哈哈", "笑死", "开森", "好耶", "太棒", "爽", "乐"],
    "温柔": ["乖", "摸摸", "抱抱", "辛苦", "别累着", "晚安", "亲亲"],
    "无奈": ["唉", "无语", "算了", "随便", "行吧", "服了", "摆烂"],
    "生气": ["气死", "讨厌", "烦", "别闹", "过分", "滚"],
    "难过": ["难过", "想哭", "委屈", "心累", "破防", "emo"],
    "害羞": ["羞", "脸红", "别夸", "不好意思", "嘿嘿"],
}


def _safe_image_path(name: str) -> str:
    base = os.path.basename(str(name or ""))
    path = os.path.join(IMAGE_DIR, base)
    root = os.path.abspath(IMAGE_DIR)
    if not os.path.abspath(path).startswith(root + os.sep):
        raise ValueError("非法图片路径")
    return path


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def load_index() -> dict:
    ensure_dir(DATA_DIR)
    ensure_dir(IMAGE_DIR)
    ensure_dir(THUMB_DIR)
    try:
        with open(INDEX_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("images", [])
    emotions = data.get("emotions")
    if not isinstance(emotions, dict) or not emotions:
        data["emotions"] = {k: list(v) for k, v in DEFAULT_EMOTIONS.items()}
    return data


def save_index(data: dict) -> None:
    ensure_dir(DATA_DIR)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_PATH)


def make_thumb(image_id: str, src: str) -> str:
    """生成 220px 缩略图并返回 data URI；无法处理时回退原图（小文件）或返回空串。"""
    out = os.path.join(THUMB_DIR, f"{image_id}.jpg")
    if not os.path.isfile(out):
        try:
            from PIL import Image as PILImage

            with PILImage.open(src) as img:
                img = img.convert("RGB")
                img.thumbnail((220, 220))
                img.save(out, "JPEG", quality=80)
        except Exception as exc:  # pragma: no cover
            logger.debug(f"emotion_pics: 缩略图失败 {exc}")
    if os.path.isfile(out):
        try:
            with open(out, "rb") as fh:
                return "data:image/jpeg;base64," + base64.b64encode(fh.read()).decode()
        except OSError:
            pass
    # 回退：小图直接内联
    try:
        if os.path.getsize(src) <= 400 * 1024:
            ext = os.path.splitext(src)[1].lower().lstrip(".")
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext or 'png'}"
            with open(src, "rb") as fh:
                return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()
    except OSError:
        pass
    return ""


class EmotionPicsWebUI:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register(self) -> None:
        ctx = self.plugin.context
        routes = [
            ("state", self.state, ["GET"], "图库状态"),
            ("upload", self.upload, ["POST"], "上传图片"),
            ("assign", self.assign, ["POST"], "设置情绪"),
            ("delete", self.delete, ["POST"], "删除图片"),
            ("emotion-save", self.emotion_save, ["POST"], "保存情绪词"),
            ("emotion-delete", self.emotion_delete, ["POST"], "删除情绪"),
        ]
        # 面板桥接按插件名拼路由；目录名和 metadata 名不一致时两个都注册
        for prefix in ("/astrbot_plugin_emotion_pics", "/emotion_pics"):
            for name, handler, methods, desc in routes:
                ctx.register_web_api(f"{prefix}/{name}", self._wrap(handler), methods, desc)

    @staticmethod
    def _wrap(handler):
        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except ValueError as exc:
                return jsonify({"status": "error", "message": str(exc)})
            except Exception as exc:  # pragma: no cover
                logger.error(f"emotion_pics webui: {type(exc).__name__}: {exc}")
                return jsonify({"status": "error", "message": f"{type(exc).__name__}: {exc}"})

        return wrapped

    async def state(self):
        data = load_index()
        images = []
        for item in data.get("images", []):
            path = _safe_image_path(item.get("file", ""))
            if not os.path.isfile(path):
                continue
            images.append({
                "id": item.get("id"),
                "file": item.get("file"),
                "emotion": item.get("emotion", ""),
                "added_at": item.get("added_at", ""),
                "thumb": make_thumb(item.get("id", ""), path),
            })
        return jsonify({
            "status": "ok",
            "data": {
                "images": images,
                "emotions": data.get("emotions", {}),
                "probability": self.plugin._get("probability", 30),
                "enabled": self.plugin.enabled,
            },
        })

    async def upload(self):
        files = await request.files
        upload = files.get("file")
        if upload is None:
            for value in files.values():
                upload = value
                break
        if upload is None:
            raise ValueError("没有收到文件")
        name = getattr(upload, "filename", "") or "upload.png"
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED:
            raise ValueError("只支持 png/jpg/gif/webp/bmp")
        image_id = uuid.uuid4().hex[:12]
        filename = f"{image_id}{ext}"
        ensure_dir(IMAGE_DIR)
        target = os.path.join(IMAGE_DIR, filename)
        await upload.save(target)
        if os.path.getsize(target) > 8 * 1024 * 1024:
            os.remove(target)
            raise ValueError("图片超过 8MB")
        data = load_index()
        data["images"].append({
            "id": image_id,
            "file": filename,
            "emotion": "",
            "added_at": __import__("time").strftime("%Y-%m-%d %H:%M"),
        })
        save_index(data)
        make_thumb(image_id, os.path.join(IMAGE_DIR, filename))
        return jsonify({"status": "ok", "data": {"id": image_id, "file": filename}})

    async def assign(self):
        body = await request.get_json() or {}
        image_id = str(body.get("id", "")).strip()
        emotion = str(body.get("emotion", "")).strip()
        data = load_index()
        for item in data["images"]:
            if item.get("id") == image_id:
                item["emotion"] = emotion
                save_index(data)
                return jsonify({"status": "ok"})
        raise ValueError("没找到这张图")

    async def delete(self):
        body = await request.get_json() or {}
        image_id = str(body.get("id", "")).strip()
        data = load_index()
        keep, target = [], None
        for item in data["images"]:
            if item.get("id") == image_id:
                target = item
                continue
            keep.append(item)
        if not target:
            raise ValueError("没找到这张图")
        data["images"] = keep
        save_index(data)
        for path in (
            os.path.join(IMAGE_DIR, target.get("file", "")),
            os.path.join(THUMB_DIR, f"{image_id}.jpg"),
        ):
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except OSError:
                pass
        return jsonify({"status": "ok"})

    async def emotion_save(self):
        body = await request.get_json() or {}
        name = str(body.get("name", "")).strip()
        keywords = str(body.get("keywords", "")).strip()
        if not name:
            raise ValueError("情绪名不能为空")
        words = [w.strip() for w in keywords.replace("，", ",").split(",") if w.strip()]
        data = load_index()
        data["emotions"][name] = words
        save_index(data)
        return jsonify({"status": "ok"})

    async def emotion_delete(self):
        body = await request.get_json() or {}
        name = str(body.get("name", "")).strip()
        data = load_index()
        if name in data["emotions"]:
            del data["emotions"][name]
            for item in data["images"]:
                if item.get("emotion") == name:
                    item["emotion"] = ""
            save_index(data)
        return jsonify({"status": "ok"})
