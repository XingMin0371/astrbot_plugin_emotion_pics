"""emotion_pics 面板接口：图库、标签、回收站、批量导入导出。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import zipfile
import json
import os
import re
import shutil
import time
import uuid
import tempfile

from quart import Response, jsonify, request

from astrbot.api import logger

MAX_PIXELS = 40_000_000




def _plugin_data_dir() -> str:
    env = os.environ.get("EMOTION_PICS_DATA")
    if env:
        return env
    env_root = os.environ.get("ASTRBOT_DATA")
    if env_root:
        return os.path.join(env_root, "plugin_data", "astrbot_plugin_emotion_pics")
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(8):
        if os.path.basename(cur) == "plugins":
            return os.path.join(os.path.dirname(cur), "plugin_data", "astrbot_plugin_emotion_pics")
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
        return os.path.realpath(os.path.join(get_astrbot_plugin_data_path(), "astrbot_plugin_emotion_pics"))
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(os.path.dirname(here), "plugin_data", "astrbot_plugin_emotion_pics")


DATA_DIR = _plugin_data_dir()
IMAGE_DIR = os.path.join(DATA_DIR, "images")
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")
TRASH_DIR = os.path.join(DATA_DIR, "trash")
SHARD_DIR = os.path.join(DATA_DIR, "index_shards")
SHARD_SIZE = 500
INDEX_PATH = os.path.join(DATA_DIR, "index.json")

_orig_logger = logger


class _SafeLogger:
    def __getattr__(self, name):
        attr = getattr(_orig_logger, name)
        if name not in ("debug", "info", "warning", "error", "exception", "critical"):
            return attr

        def wrapped(msg, *args, **kwargs):
            if os.environ.get("EMOTION_PICS_LOG_ABSOLUTE") != "1":
                try:
                    text = str(msg)
                    for key, label in ((DATA_DIR, "data"), (IMAGE_DIR, "images"), (THUMB_DIR, "thumbs")):
                        if key:
                            text = text.replace(key + os.sep, label + "/").replace(key, label)
                    msg = text
                except Exception:  # noqa: BLE001
                    pass
            return attr(msg, *args, **kwargs)

        return wrapped


logger = _SafeLogger()

ALLOWED = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DEFAULT_EMOTIONS = {
    "开心": ["哈哈", "笑死", "开森", "好耶", "太棒", "爽", "乐"],
    "温柔": ["乖", "摸摸", "抱抱", "辛苦", "别累着", "晚安", "亲亲"],
    "无奈": ["唉", "无语", "算了", "随便", "行吧", "服了", "摆烂"],
    "生气": ["气死", "讨厌", "烦", "别闹", "过分", "滚"],
    "难过": ["难过", "想哭", "委屈", "心累", "破防", "emo"],
    "害羞": ["羞", "脸红", "别夸", "不好意思", "嘿嘿"],
}

_INDEX_CACHE = {"key": None, "data": None}


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _safe_name(name: str) -> str:
    base = os.path.basename(str(name or ""))
    if not base:
        raise ValueError("非法文件名")
    return base


def _safe_image_path(name: str) -> str:
    path = os.path.join(IMAGE_DIR, _safe_name(name))
    root = os.path.abspath(IMAGE_DIR)
    if os.path.commonpath([os.path.abspath(path), root]) != root:
        raise ValueError("非法图片路径")
    return path


def _safe_trash_path(name: str) -> str:
    path = os.path.join(TRASH_DIR, _safe_name(name))
    root = os.path.abspath(TRASH_DIR)
    if os.path.commonpath([os.path.abspath(path), root]) != root:
        raise ValueError("非法回收站路径")
    return path


def _safe_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", str(value))[:32]


def _trash_days() -> int:
    try:
        value = int(os.environ.get("EMOTION_PICS_TRASH_DAYS", "7"))
    except ValueError:
        value = 7
    return max(1, min(30, value))


_LAST_PURGE = 0.0


def load_index() -> dict:
    global _INDEX_CACHE, _LAST_PURGE
    _hmac_secret()
    for path in (DATA_DIR, IMAGE_DIR, THUMB_DIR, TRASH_DIR):
        ensure_dir(path)
    try:
        st = os.stat(INDEX_PATH)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _INDEX_CACHE.get("key") == key:
        data = _INDEX_CACHE["data"]
    else:
        try:
            with open(INDEX_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            logger.warning(f"emotion_pics: index.json 读取失败，使用空索引 {type(exc).__name__}")
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("images", [])
        data.setdefault("trash", [])
        emotions = data.get("emotions")
        if not isinstance(emotions, dict) or not emotions:
            data["emotions"] = {k: list(v) for k, v in DEFAULT_EMOTIONS.items()}
        if data.get("sharded"):
            merged = []
            for part in data.get("parts", []):
                try:
                    raw = open(os.path.join(SHARD_DIR, _safe_name(part)), encoding="utf-8").read()
                    payload = json.loads(raw)
                    if isinstance(payload, dict):
                        rows = payload.get("rows") or []
                        if payload.get("sig") != hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest():
                            raise ValueError("part HMAC failed")
                        merged.extend(rows)
                    else:
                        merged.extend(payload)
                except (OSError, ValueError):
                    continue
            data = {k: v for k, v in data.items() if k not in ("sharded", "parts", "images")}
            data["images"] = merged
        if data.get("hmac") and data.get("hmac") != _index_hmac(data):
            raise ValueError("index.json HMAC 校验失败，已拒绝使用；请从备份恢复")
        if key is not None:
            _INDEX_CACHE = {"key": key, "data": data}
    if time.time() - _LAST_PURGE > 3600:
        _LAST_PURGE = time.time()
        purge_trash(data)
    return data


def purge_trash(data: dict) -> None:
    cutoff = time.time() - _trash_days() * 86400
    keep, changed = [], False
    for item in data.get("trash", []):
        try:
            when = float(item.get("deleted_ts") or 0)
        except (TypeError, ValueError):
            when = 0
        if when and when < cutoff:
            try:
                for name in (item.get("file", ""), f"{_safe_id(item.get('id'))}.jpg"):
                    path = _safe_trash_path(name)
                    if os.path.isfile(path):
                        os.remove(path)
            except (OSError, ValueError):
                pass
            changed = True
        else:
            keep.append(item)
    if changed:
        data["trash"] = keep
        save_index(data)


def save_index(data: dict) -> None:
    global _INDEX_CACHE
    ensure_dir(DATA_DIR)
    data["hmac"] = _index_hmac(data)
    images = data.get("images", [])
    if len(images) > SHARD_SIZE:
        ensure_dir(SHARD_DIR)
        parts = {}
        for item in images:
            bucket = _safe_id(str(item.get("id", "")))[:2] or "xx"
            parts.setdefault(bucket, []).append(item)
        names = []
        for bucket, rows in parts.items():
            name = f"part-{bucket}.json"
            path = os.path.join(SHARD_DIR, name)
            body = json.dumps(rows, ensure_ascii=False, sort_keys=True)
            sig = hashlib.sha256(body.encode()).hexdigest()
            payload = json.dumps({"rows": rows, "sig": sig}, ensure_ascii=False)
            if os.path.isfile(path):
                try:
                    if open(path, encoding="utf-8").read() == payload:
                        names.append(name)
                        continue
                except OSError:
                    pass
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
            names.append(name)
        meta = {k: v for k, v in data.items() if k != "images"}
        meta["sharded"] = True
        meta["parts"] = names
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, INDEX_PATH)
    else:
        if os.path.isdir(SHARD_DIR):
            shutil.rmtree(SHARD_DIR, ignore_errors=True)
        tmp = INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, INDEX_PATH)
    try:
        st = os.stat(INDEX_PATH)
        _INDEX_CACHE = {"key": (st.st_mtime_ns, st.st_size), "data": data}
    except OSError:
        pass


_THUMB_CACHE: dict[str, tuple[int, int, bytes]] = {}


def thumb_bytes(image_id: str, src: str) -> bytes:
    try:
        st = os.stat(src)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return b""
    cached = _THUMB_CACHE.get(src)
    if cached and cached[0] == key[0] and cached[1] == key[1]:
        return cached[2]
    out = os.path.join(THUMB_DIR, f"{_safe_id(image_id) or 'x'}.jpg")
    data = b""
    if not os.path.isfile(out):
        try:
            from PIL import Image as PILImage

            PILImage.MAX_IMAGE_PIXELS = MAX_PIXELS
            with PILImage.open(src) as img:
                if (img.width or 0) * (img.height or 0) > MAX_PIXELS:
                    raise ValueError("图片像素过大")
                img.seek(0)
                img.convert("RGB").save(out, "JPEG", quality=80)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"emotion_pics: 缩略图失败 {exc}")
    if os.path.isfile(out):
        try:
            data = open(out, "rb").read()
        except OSError:
            data = b""
    if not data:
        try:
            if os.path.getsize(src) <= 200 * 1024:
                data = open(src, "rb").read()
        except OSError:
            data = b""
    if len(_THUMB_CACHE) > 256:
        _THUMB_CACHE.clear()
    _THUMB_CACHE[src] = (key[0], key[1], data)
    return data


def _trash_item(data: dict, image_id: str) -> bool:
    keep, target = [], None
    for item in data["images"]:
        if item.get("id") == image_id:
            target = item
            continue
        keep.append(item)
    if not target:
        return False
    image_path = _safe_image_path(target.get("file", ""))
    ensure_dir(TRASH_DIR)
    trash_name = f"{_safe_id(image_id) or uuid.uuid4().hex[:8]}{os.path.splitext(image_path)[1] or '.png'}"
    try:
        if os.path.isfile(image_path):
            shutil.move(image_path, os.path.join(TRASH_DIR, trash_name))
    except OSError:
        pass
    target["deleted_at"] = time.strftime("%Y-%m-%d %H:%M")
    target["deleted_ts"] = time.time()
    target["file"] = trash_name
    data["images"] = keep
    data.setdefault("trash", []).append(target)
    return True


def _thumb_url(image_id: str, name: str = "") -> str:
    return ""



_HMAC_KEY_PATH = ""
_ALLOW_NEW_KEY = False


def _read_key(path: str) -> bytes:
    raw = open(path, encoding="utf-8").read().strip()
    key = bytes.fromhex(raw)
    if len(key) != 32:
        raise ValueError("HMAC key 长度不对")
    return key


def _hmac_secret() -> bytes:
    global _HMAC_KEY_PATH
    key_dir = os.path.join(DATA_DIR, "keys")
    path = os.path.join(key_dir, "hmac.key")
    ensure_dir(key_dir)
    if _HMAC_KEY_PATH and os.path.isfile(_HMAC_KEY_PATH):
        return _read_key(_HMAC_KEY_PATH)
    if os.path.isfile(path):
        _HMAC_KEY_PATH = path
        return _read_key(path)
    for legacy in (
        os.path.join(DATA_DIR, "hmac.key"),
        os.path.join(os.path.dirname(DATA_DIR), ".emotion_pics_keys", "hmac.key"),
    ):
        if not os.path.isfile(legacy):
            continue
        try:
            shutil.copy2(legacy, path)
            os.chmod(path, 0o600)
            _HMAC_KEY_PATH = path
            return _read_key(path)
        except OSError:
            try:
                key = _read_key(legacy)
                _HMAC_KEY_PATH = legacy
                logger.warning("emotion_pics: 无法复制旧 HMAC key，继续使用旧路径")
                return key
            except (OSError, ValueError) as exc:
                raise ValueError(f"旧 HMAC key 不可读或不可复制: {legacy}") from exc
    if os.path.isfile(INDEX_PATH) and not _ALLOW_NEW_KEY:
        try:
            existing = json.load(open(INDEX_PATH, encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if isinstance(existing, dict) and existing.get("hmac"):
            raise ValueError("index.json 带 HMAC 但密钥丢失，请从备份恢复或手动恢复 keys/hmac.key")
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key.hex())
    _HMAC_KEY_PATH = path
    return key


def _index_hmac(data: dict) -> str:
    payload = {k: v for k, v in data.items() if k != "hmac"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(_hmac_secret(), raw, hashlib.sha256).hexdigest()


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strip_metadata(path: str, ext: str) -> None:
    """去掉 EXIF 等元数据；GIF/WebP 动图不动。"""
    if ext in (".gif", ".webp"):
        return
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            img.load()
            fmt = img.format or "PNG"
            clean = img.copy()
        clean.info.pop("exif", None)
        tmp = path + ".clean"
        if fmt == "JPEG":
            clean.save(tmp, format=fmt, quality=95, exif=b"")
        else:
            clean.save(tmp, format=fmt, exif=b"")
        os.replace(tmp, path)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"emotion_pics: 去元数据失败，保留原图 {exc}")


def _find_duplicate(data: dict, digest: str) -> dict | None:
    changed = False
    for item in data.get("images", []):
        known = str(item.get("sha256") or "")
        if not known:
            try:
                known = _file_digest(_safe_image_path(item.get("file", "")))
            except (OSError, ValueError):
                continue
            item["sha256"] = known
            changed = True
        if known == digest:
            if changed:
                save_index(data)
            return item
    if changed:
        save_index(data)
    return None

def usage_path() -> str:
    return os.path.join(DATA_DIR, "usage.json")


def load_usage() -> dict:
    data = load_json_safe(usage_path())
    return data if isinstance(data, dict) else {}


def load_json_safe(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def record_usage(image_id: str) -> None:
    data = load_usage()
    item = data.get(str(image_id)) or {"count": 0}
    item["count"] = int(item.get("count") or 0) + 1
    item["last"] = time.strftime("%Y-%m-%d %H:%M")
    data[str(image_id)] = item
    tmp = usage_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, usage_path())


class EmotionPicsWebUI:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register(self) -> None:
        ctx = self.plugin.context
        routes = [
            ("state", self.state, ["GET"], "图库状态"),
            ("thumb", self.thumb, ["GET"], "缩略图"),
            ("thumb-data", self.thumb_data, ["GET"], "缩略图数据"),
            ("thumbs-data", self.thumbs_data, ["GET"], "批量缩略图数据"),
            ("upload", self.upload, ["POST"], "上传图片"),
            ("assign", self.assign, ["POST"], "设置情绪"),
            ("delete", self.delete, ["POST"], "删除图片"),
            ("trash", self.trash, ["GET"], "回收站"),
            ("restore", self.restore, ["POST"], "恢复图片"),
            ("trash-clear", self.trash_clear, ["POST"], "清空回收站"),
            ("tags-save", self.tags_save, ["POST"], "保存标签"),
            ("bulk-delete", self.bulk_delete, ["POST"], "批量删除"),
            ("backup", self.backup, ["POST"], "整包备份"),
            ("restore-backup", self.restore_backup, ["POST"], "从备份恢复"),
            ("export", self.export_index, ["GET"], "导出索引"),
            ("import", self.import_index, ["POST"], "导入索引"),
            ("emotion-save", self.emotion_save, ["POST"], "保存情绪词"),
            ("emotion-delete", self.emotion_delete, ["POST"], "删除情绪"),
        ]
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
        query = str(request.args.get("q") or "").strip()[:50]
        tag = str(request.args.get("tag") or "").strip()[:32]
        try:
            page = max(1, int(request.args.get("page", 1)))
            size = max(1, min(int(request.args.get("page_size", 24)), 100))
        except ValueError:
            page, size = 1, 24
        usage = load_usage()
        matched = []
        for item in data.get("images", []):
            emotion = str(item.get("emotion", ""))
            tags = [str(t) for t in item.get("tags", [])]
            if query and query not in emotion and query not in item.get("file", "") and not any(query in t for t in tags):
                continue
            if tag and tag not in tags and tag != emotion:
                continue
            try:
                path = _safe_image_path(item.get("file", ""))
            except ValueError:
                continue
            if not os.path.isfile(path):
                continue
            matched.append((item, emotion, tags, path))
        total = len(matched)
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        start = (page - 1) * size
        images = []
        for item, emotion, tags, path in matched[start:start + size]:
            images.append({
                "id": item.get("id"), "file": item.get("file"), "emotion": emotion, "tags": tags,
                "added_at": item.get("added_at", ""),
                "used_count": int((usage.get(str(item.get("id"))) or {}).get("count") or 0),
                "thumb": "", "url": "",
            })
        all_tags = sorted({str(t) for img in data.get("images", []) for t in img.get("tags", [])})
        return jsonify({
            "status": "ok",
            "data": {
                "images": images, "total": total, "page": page, "pages": pages,
                "page_size": size, "tags": all_tags, "trash_count": len(data.get("trash", [])),
                "never_used": sum(1 for i in data.get("images", []) if not (usage.get(str(i.get("id"))) or {}).get("count")),
                "emotions": data.get("emotions", {}), "probability": self.plugin._get("probability", 30),
                "enabled": self.plugin.enabled,
            },
        })

    async def thumbs_data(self):
        raw = str(request.args.get("ids") or "").strip()
        ids = [x for x in raw.split(",") if x][:60]
        data = load_index()
        by_id = {str(item.get("id")): item for item in data.get("images", [])}
        thumbs = {}
        for image_id in ids:
            item = by_id.get(image_id)
            if not item:
                continue
            try:
                path = _safe_image_path(item.get("file", ""))
                if path.lower().endswith(".gif"):
                    raw_bytes = await asyncio.to_thread(lambda p=path: open(p, "rb").read())
                    thumbs[image_id] = "data:image/gif;base64," + base64.b64encode(raw_bytes).decode()
                else:
                    content = await asyncio.to_thread(thumb_bytes, image_id, path)
                    if content:
                        thumbs[image_id] = "data:image/jpeg;base64," + base64.b64encode(content).decode()
            except (OSError, ValueError):
                continue
        return jsonify({"status": "ok", "data": {"thumbs": thumbs}})

    async def thumb_data(self):
        image_id = str(request.args.get("id") or "").strip()
        data = load_index()
        for item in data.get("images", []):
            if item.get("id") == image_id:
                path = _safe_image_path(item.get("file", ""))
                if path.lower().endswith(".gif"):
                    raw = await asyncio.to_thread(lambda: open(path, "rb").read())
                    return jsonify({"status": "ok", "data": {"thumb": "data:image/gif;base64," + base64.b64encode(raw).decode()}})
                content = await asyncio.to_thread(thumb_bytes, item.get("id", ""), path)
                return jsonify({"status": "ok", "data": {"thumb": "data:image/jpeg;base64," + base64.b64encode(content).decode() if content else ""}})
        raise ValueError("没找到这张图")

    async def thumb(self):
        image_id = str(request.args.get("id") or "").strip()
        data = load_index()
        for item in data.get("images", []):
            if item.get("id") == image_id:
                path = _safe_image_path(item.get("file", ""))
                content = thumb_bytes(item.get("id", ""), path)
                if content:
                    return Response(content, mimetype="image/jpeg")
        raise ValueError("没找到这张图")

    async def upload(self):
        files = await request.files
        upload = files.get("file") or next(iter(files.values()), None)
        if upload is None:
            raise ValueError("没有收到文件")
        name = getattr(upload, "filename", "") or "upload.png"
        ext = os.path.splitext(name)[1].lower()
        if ext not in ALLOWED:
            raise ValueError("只支持 png/jpg/gif/webp/bmp")
        image_id = uuid.uuid4().hex[:12]
        filename = f"{image_id}{ext}"
        target = os.path.join(IMAGE_DIR, filename)
        ensure_dir(IMAGE_DIR)
        await upload.save(target)
        try:
            size = os.path.getsize(target)
            if size == 0:
                raise ValueError("图片是空文件")
            if size > 8 * 1024 * 1024:
                raise ValueError("图片超过 8MB")
            try:
                from PIL import Image as PILImage

                with PILImage.open(target) as img:
                    img.verify()
            except Exception as exc:  # noqa: BLE001
                raise ValueError("不是有效图片") from exc
            digest = _file_digest(target)
            data = load_index()
            duplicate = _find_duplicate(data, digest)
            if duplicate is not None:
                os.remove(target)
                return jsonify({"status": "ok", "data": {"duplicate": True, "id": duplicate.get("id"), "file": duplicate.get("file")}})
            if bool(self.plugin._get("clean_metadata", False)):
                _strip_metadata(target, ext)
            if bool(self.plugin._get("auto_webp", False)) and ext not in (".gif", ".webp"):
                try:
                    from PIL import Image as PILImage

                    with PILImage.open(target) as img:
                        img.convert("RGB").save(os.path.join(IMAGE_DIR, f"{image_id}.webp"), "WEBP", quality=85)
                    os.remove(target)
                    filename = f"{image_id}.webp"
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"emotion_pics: 转 WebP 失败，保留原图 {exc}")
        except Exception:
            try:
                os.remove(target)
            except OSError:
                pass
            raise
        data["images"].append({
            "id": image_id, "file": filename, "emotion": "", "tags": [],
            "sha256": digest, "added_at": time.strftime("%Y-%m-%d %H:%M"),
        })
        save_index(data)
        return jsonify({"status": "ok", "data": {"id": image_id, "file": filename}})

    async def assign(self):
        body = await request.get_json() or {}
        image_id = str(body.get("id", "")).strip()
        emotion = str(body.get("emotion", "")).strip()[:32]
        data = load_index()
        for item in data["images"]:
            if item.get("id") == image_id:
                item["emotion"] = emotion
                save_index(data)
                return jsonify({"status": "ok"})
        raise ValueError("没找到这张图")

    async def tags_save(self):
        body = await request.get_json() or {}
        image_id = str(body.get("id", "")).strip()
        tags = [str(t).strip()[:16] for t in (body.get("tags") or []) if str(t).strip()][:10]
        data = load_index()
        for item in data["images"]:
            if item.get("id") == image_id:
                item["tags"] = tags
                save_index(data)
                return jsonify({"status": "ok"})
        raise ValueError("没找到这张图")

    async def delete(self):
        body = await request.get_json() or {}
        image_id = str(body.get("id", "")).strip()
        data = load_index()
        if not _trash_item(data, image_id):
            raise ValueError("没找到这张图")
        save_index(data)
        return jsonify({"status": "ok"})

    async def bulk_delete(self):
        body = await request.get_json() or {}
        ids = [str(x) for x in (body.get("ids") or [])]
        data = load_index()
        removed = sum(1 for image_id in ids if _trash_item(data, image_id))
        if removed:
            save_index(data)
        return jsonify({"status": "ok", "data": {"removed": removed}})

    async def trash(self):
        data = load_index()
        return jsonify({"status": "ok", "data": {"images": data.get("trash", []), "days": _trash_days()}})

    async def restore(self):
        body = await request.get_json() or {}
        image_id = str(body.get("id", "")).strip()
        data = load_index()
        keep, target = [], None
        for item in data.get("trash", []):
            if item.get("id") == image_id:
                target = item
                continue
            keep.append(item)
        if not target:
            raise ValueError("没找到这张图")
        path = _safe_trash_path(target.get("file", ""))
        if not os.path.isfile(path):
            raise ValueError("回收站里的文件已丢失，请彻底删除这条记录")
        if os.path.isfile(path):
            new_name = f"{_safe_id(image_id) or uuid.uuid4().hex[:8]}{os.path.splitext(path)[1] or '.png'}"
            shutil.move(path, os.path.join(IMAGE_DIR, new_name))
            target["file"] = new_name
        target.pop("deleted_at", None)
        target.pop("deleted_ts", None)
        data["trash"] = keep
        data["images"].append(target)
        save_index(data)
        return jsonify({"status": "ok"})

    async def trash_clear(self):
        data = load_index()
        for item in data.get("trash", []):
            try:
                path = _safe_trash_path(item.get("file", ""))
                if os.path.isfile(path):
                    os.remove(path)
            except (OSError, ValueError):
                pass
        data["trash"] = []
        save_index(data)
        return jsonify({"status": "ok"})

    async def backup(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = os.path.join(DATA_DIR, "backups")
        ensure_dir(backup_dir)
        target = os.path.join(backup_dir, f"snapshot-{stamp}")
        await asyncio.to_thread(self._write_snapshot, target, backup_dir, stamp)
        size = sum(os.path.getsize(os.path.join(root, name)) for root, _dirs, files in os.walk(target) for name in files)
        return jsonify({"status": "ok", "data": {"path": target, "size": size, "incremental": True}})

    def _write_snapshot(self, target: str, backup_dir: str, stamp: str) -> None:
        ensure_dir(target)
        previous = ""
        for name in sorted(os.listdir(backup_dir)):
            path = os.path.join(backup_dir, name)
            if name.startswith("snapshot-") and os.path.isdir(path) and os.path.abspath(path) != os.path.abspath(target):
                previous = path
        if os.path.isfile(INDEX_PATH):
            shutil.copy2(INDEX_PATH, os.path.join(target, "index.json"))
        manifest = {"created": stamp, "format": 2, "mode": "hardlink-incremental", "files": {}}
        for folder, prefix in ((IMAGE_DIR, "images/"), (THUMB_DIR, "thumbs/"), (TRASH_DIR, "trash/"), (SHARD_DIR, "index_shards/")):
            if not os.path.isdir(folder):
                continue
            for root, _dirs, files in os.walk(folder):
                for name in files:
                    full = os.path.join(root, name)
                    rel = prefix + os.path.relpath(full, folder).replace(os.sep, "/")
                    dest = os.path.join(target, rel)
                    ensure_dir(os.path.dirname(dest))
                    st = os.stat(full)
                    src = full
                    if previous:
                        old = os.path.join(previous, rel)
                        try:
                            ost = os.stat(old)
                            if ost.st_size == st.st_size and ost.st_mtime_ns == st.st_mtime_ns:
                                src = old
                        except OSError:
                            pass
                    try:
                        os.link(src, dest)
                    except OSError:
                        shutil.copy2(src, dest)
                    manifest["files"][rel] = {"size": st.st_size, "mtime": st.st_mtime_ns}
        with open(os.path.join(target, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)

    async def restore_backup(self):
        body = await request.get_json() or {}
        backup_dir = os.path.join(DATA_DIR, "backups")
        if not os.path.isdir(backup_dir):
            raise ValueError("还没有备份")
        name = str(body.get("name", "")).strip()
        candidates = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
                      if f.endswith(".zip") or (f.startswith("snapshot-") and os.path.isdir(os.path.join(backup_dir, f)))]
        if not candidates:
            raise ValueError("还没有备份")
        target = os.path.join(backup_dir, _safe_name(name)) if name else max(candidates, key=os.path.getmtime)
        if not (os.path.isfile(target) or os.path.isdir(target)):
            raise ValueError("备份不存在")
        data = await asyncio.to_thread(self._restore_payload, target)
        global _ALLOW_NEW_KEY
        _ALLOW_NEW_KEY = True
        try:
            save_index(data)
        finally:
            _ALLOW_NEW_KEY = False
        await asyncio.to_thread(self._rebuild_thumbs, data)
        return jsonify({"status": "ok"})

    def _restore_payload(self, target: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            if os.path.isdir(target):
                shutil.copytree(target, tmp, dirs_exist_ok=True)
            else:
                with zipfile.ZipFile(target) as z:
                    for info in z.infolist():
                        name = info.filename.replace("\\", "/")
                        if name.startswith("/") or any(part == ".." for part in name.split("/")):
                            raise ValueError("备份压缩包包含非法路径")
                        z.extract(info, tmp)
            index_src = os.path.join(tmp, "index.json")
            if not os.path.isfile(index_src):
                raise ValueError("备份里没有 index.json")
            data = json.load(open(index_src, encoding="utf-8"))
            data.pop("hmac", None)
            if data.get("sharded"):
                merged = []
                for part in data.get("parts", []):
                    try:
                        payload = json.load(open(os.path.join(tmp, "index_shards", part), encoding="utf-8"))
                        rows = payload.get("rows") if isinstance(payload, dict) else payload
                        merged.extend(rows or [])
                    except (OSError, ValueError):
                        continue
                data["images"] = merged
                data.pop("sharded", None)
                data.pop("parts", None)
            for folder in ("images", "thumbs", "trash", "index_shards"):
                src = os.path.join(tmp, folder)
                dst = os.path.join(DATA_DIR, folder)
                if os.path.isdir(src):
                    ensure_dir(dst)
                    for root, _dirs, files in os.walk(src):
                        for f in files:
                            shutil.copyfile(os.path.join(root, f), os.path.join(dst, f))
            return data

    def _rebuild_thumbs(self, data: dict) -> None:
        for item in data.get("images", []):
            try:
                path = _safe_image_path(item.get("file", ""))
                if os.path.isfile(path):
                    thumb_bytes(item.get("id", ""), path)
            except (OSError, ValueError):
                continue

    async def export_index(self):
        data = load_index()
        return Response(json.dumps(data, ensure_ascii=False, indent=2), mimetype="application/json",
                        headers={"Content-Disposition": "attachment; filename=emotion_pics_index.json"})

    async def import_index(self):
        body = await request.get_json() or {}
        if not isinstance(body, dict):
            raise ValueError("导入内容必须是 JSON 对象")
        data = load_index()
        for name, words in (body.get("emotions") or {}).items():
            if isinstance(words, list):
                data["emotions"][str(name)[:32]] = [str(w)[:32] for w in words][:50]
        existing = {str(i.get("id")) for i in data["images"]}
        for item in (body.get("images") or [])[:1000]:
            if not isinstance(item, dict) or item.get("id") in existing:
                continue
            try:
                candidate = _safe_image_path(item.get("file", "x.png"))
                if not os.path.isfile(candidate):
                    continue
                data["images"].append({
                    "id": str(item.get("id"))[:32], "file": _safe_name(item.get("file", "x.png")),
                    "emotion": str(item.get("emotion", ""))[:32],
                    "tags": [str(t)[:16] for t in item.get("tags", [])][:10],
                    "added_at": str(item.get("added_at", ""))[:16],
                })
            except ValueError:
                continue
        save_index(data)
        return jsonify({"status": "ok"})

    async def emotion_save(self):
        body = await request.get_json() or {}
        name = str(body.get("name", "")).strip()[:32]
        keywords = str(body.get("keywords", "")).strip()
        if not name:
            raise ValueError("情绪名不能为空")
        data = load_index()
        data["emotions"][name] = [w.strip()[:32] for w in keywords.replace("，", ",").split(",") if w.strip()][:50]
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
