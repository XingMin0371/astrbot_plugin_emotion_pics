"""emotion_pics：按情绪给鱼鱼的回复配图。"""

from __future__ import annotations

import json
import os
import shutil
import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain

try:
    from astrbot.api.message_components import TextPart
except Exception:  # pragma: no cover
    TextPart = None
from astrbot.api.star import Context, Star, register

try:
    from .webui import EmotionPicsWebUI
except Exception:  # pragma: no cover
    EmotionPicsWebUI = None  # type: ignore[assignment]

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


def _migrate_legacy_layout() -> None:
    """旧版把图库放在 data/emotion_pics，启动时搬到 plugin_data。"""
    if os.path.basename(os.path.dirname(DATA_DIR)) != "plugin_data":
        return
    data_root = os.path.dirname(os.path.dirname(DATA_DIR))
    legacy = os.path.join(data_root, "emotion_pics")
    if not os.path.isdir(legacy) or os.path.abspath(legacy) == os.path.abspath(DATA_DIR):
        return
    ensure_dir(DATA_DIR)

    def merge(src: str, dst: str) -> None:
        if os.path.isdir(src):
            ensure_dir(dst)
            for child in os.listdir(src):
                merge(os.path.join(src, child), os.path.join(dst, child))
            try:
                os.rmdir(src)
            except OSError:
                pass
        elif not os.path.exists(dst):
            try:
                shutil.move(src, dst)
            except OSError:
                pass

    for name in os.listdir(legacy):
        merge(os.path.join(legacy, name), os.path.join(DATA_DIR, name))
    try:
        os.rmdir(legacy)
    except OSError:
        pass


DATA_DIR = _plugin_data_dir()
INDEX_PATH = os.path.join(DATA_DIR, "index.json")
IMAGE_DIR = os.path.join(DATA_DIR, "images")

DEFAULT_EMOTIONS = {
    "开心": ["哈哈", "笑死", "开森", "好耶", "太棒", "爽", "乐"],
    "温柔": ["乖", "摸摸", "抱抱", "辛苦", "别累着", "晚安", "亲亲"],
    "无奈": ["唉", "无语", "算了", "随便", "行吧", "服了", "摆烂"],
    "生气": ["气死", "讨厌", "烦", "别闹", "过分", "滚"],
    "难过": ["难过", "想哭", "委屈", "心累", "破防", "emo"],
    "害羞": ["羞", "脸红", "别夸", "不好意思", "嘿嘿"],
}

def _safe_image_path(name: str) -> str:
    base_name = os.path.basename(str(name or ""))
    path = os.path.join(IMAGE_DIR, base_name)
    root = os.path.abspath(IMAGE_DIR)
    if not os.path.abspath(path).startswith(root + os.sep):
        raise ValueError("非法图片路径")
    return path


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


_INDEX_CACHE = {"key": None, "data": None}


def load_index() -> dict:
    global _INDEX_CACHE
    ensure_dir(DATA_DIR)
    ensure_dir(IMAGE_DIR)
    try:
        st = os.stat(INDEX_PATH)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if key is not None and _INDEX_CACHE.get("key") == key:
        return _INDEX_CACHE["data"]
    try:
        with open(INDEX_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("images", [])
    data.setdefault("trash", [])
    emotions = data.get("emotions")
    if not isinstance(emotions, dict) or not emotions:
        data["emotions"] = {k: list(v) for k, v in DEFAULT_EMOTIONS.items()}
    if key is not None:
        _INDEX_CACHE = {"key": key, "data": data}
    return data


def save_index(data: dict) -> None:
    ensure_dir(DATA_DIR)
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, INDEX_PATH)
    try:
        st = os.stat(INDEX_PATH)
        _INDEX_CACHE = {"key": (st.st_mtime_ns, st.st_size), "data": data}
    except OSError:
        pass


@register("emotion_pics", "DSH", "情绪配图：按情绪给回复配一张图", "1.1.4")
class EmotionPics(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._last_sent: dict[str, float] = {}
        self._sent_times: list[float] = []

    def _get(self, key, default):
        try:
            if not self.config:
                return default
            if key in self.config:
                value = self.config[key]
            else:
                value = default
                for group in self.config.values():
                    if isinstance(group, dict) and key in group:
                        value = group[key]
                        break
        except Exception:
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._get("enable", True))

    @property
    def probability(self) -> float:
        try:
            pct = float(self._get("probability", 30))
        except (TypeError, ValueError):
            pct = 30.0
        return max(0.0, min(100.0, pct)) / 100.0

    def _int(self, key, default):
        try:
            return max(0, int(self._get(key, default)))
        except (TypeError, ValueError):
            return default

    def detect(self, text: str, emotions: dict[str, list[str]]) -> str:
        if not text:
            return ""
        best, best_hits = "", 0
        for name, words in emotions.items():
            hits = sum(1 for word in words if word and str(word) in text)
            if hits > best_hits:
                best, best_hits = name, hits
        return best

    def pick_image(self, emotion: str, data: dict) -> dict | None:
        pool = [img for img in data.get("images", []) if img.get("emotion") == emotion]
        if not pool:
            return None
        return random.choice(pool)

    def _in_blacklist(self, umo: str, sender: str = "") -> bool:
        users = self._get("blacklist_users", []) or []
        if isinstance(users, str):
            users = [x.strip() for x in users.replace("，", ",").split(",")]
        if sender and str(sender) in {str(x).strip() for x in users}:
            return True
        parts = str(umo or "").split(":")
        group_id = parts[2] if len(parts) >= 3 and parts[1] == "GroupMessage" else ""
        groups = self._get("blacklist_groups", []) or []
        if isinstance(groups, str):
            groups = [x.strip() for x in groups.replace("，", ",").split(",")]
        if group_id and group_id in {str(x).strip() for x in groups}:
            return True
        ranges = self._get("blacklist_ranges", []) or []
        if isinstance(ranges, str):
            ranges = [x.strip() for x in ranges.replace("，", ",").split(",")]
        now_min = time.localtime().tm_hour * 60 + time.localtime().tm_min
        for item in ranges:
            try:
                start, end = str(item).split("-", 1)
                sh, sm = [int(x) for x in start.strip().split(":")]
                eh, em = [int(x) for x in end.strip().split(":")]
                begin, finish = sh * 60 + sm, eh * 60 + em
            except (ValueError, AttributeError):
                continue
            if begin <= finish and begin <= now_min < finish:
                return True
            if begin > finish and (now_min >= begin or now_min < finish):
                return True
        return False

    def _can_send(self, umo: str) -> bool:
        now = time.time()
        if now - self._last_sent.get(umo, 0) < self._int("cooldown_minutes", 5) * 60:
            return False
        window = [t for t in self._sent_times if now - t < 3600]
        self._sent_times = window
        return len(window) < self._int("max_per_hour", 3)

    async def initialize(self) -> None:
        _migrate_legacy_layout()
        try:
            days = max(1, min(30, int(self._get("trash_days", 7))))
            os.environ["EMOTION_PICS_TRASH_DAYS"] = str(days)
        except (TypeError, ValueError):
            os.environ["EMOTION_PICS_TRASH_DAYS"] = "7"
        load_index()
        if EmotionPicsWebUI is not None:
            try:
                EmotionPicsWebUI(self).register()
                logger.info("emotion_pics: WebUI API 已注册")
            except Exception as exc:  # pragma: no cover
                logger.warning(f"emotion_pics: WebUI 注册失败 {exc}")
        logger.info("emotion_pics: 已启动")

    @filter.on_llm_request()
    async def tell_capability(self, event, req) -> None:
        """让模型知道情绪配图已启用，不需要自己发图片。"""
        try:
            if not self.enabled or self.probability <= 0:
                return
            if getattr(event, "is_stopped", lambda: False)():
                return
            text = (
                "【情绪配图】已启用：你正常回复即可，插件会按情绪自动配一张图。"
                "需要表达情绪时直接说，不要声称自己不能发图片或表情包。"
            )
            if TextPart is not None:
                req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())
            else:
                req.system_prompt = (req.system_prompt or "") + "\n\n" + text
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"emotion_pics: 能力提示注入跳过 {type(exc).__name__}")

    @filter.on_decorating_result()
    async def attach_picture(self, event: AstrMessageEvent) -> None:
        try:
            if not self.enabled or self.probability <= 0:
                return
            # 命令的输出不配图（/记忆查看 之类）
            try:
                if event.is_stopped():
                    return
            except Exception:
                pass
            result = event.get_result()
            if not result or not result.chain:
                return
            text = "".join(
                comp.text for comp in result.chain if isinstance(comp, Plain)
            ).strip()
            data = load_index()
            emotion = self.detect(text, data.get("emotions", {}))
            if not emotion:
                return
            umo = getattr(event, "unified_msg_origin", "") or ""
            sender = str(event.get_sender_id())
            if self._in_blacklist(umo, sender):
                return
            if not self._can_send(umo):
                return
            if random.random() > self.probability:
                return
            image = self.pick_image(emotion, data)
            if not image:
                return
            path = _safe_image_path(image.get("file", ""))
            if not os.path.isfile(path):
                return
            try:
                from PIL import Image as PILImage

                with PILImage.open(path) as probe:
                    probe.verify()
            except Exception:
                logger.warning(f"emotion_pics: 跳过损坏或伪造的图片 {image.get('file')}")
                return
            result.chain.append(Image.fromFileSystem(path))
            try:
                from .webui import record_usage
                record_usage(image.get("id", ""))
            except Exception:  # noqa: BLE001
                pass
            self._last_sent[umo] = time.time()
            self._sent_times.append(time.time())
            logger.info(f"emotion_pics: 情绪 {emotion} 配图 {image.get('file')}")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"emotion_pics: 配图失败 {type(exc).__name__}: {exc}")
