"""emotion_pics：按情绪给鱼鱼的回复配图。"""

from __future__ import annotations

import json
import os
import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:
    from .webui import EmotionPicsWebUI
except Exception:  # pragma: no cover
    EmotionPicsWebUI = None  # type: ignore[assignment]

DATA_DIR = "/AstrBot/data/emotion_pics"
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

TEXT_EMOJI = {}


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def load_index() -> dict:
    ensure_dir(DATA_DIR)
    ensure_dir(IMAGE_DIR)
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


@register("emotion_pics", "DSH", "情绪配图：按情绪给回复配一张图", "1.0.0")
class EmotionPics(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self._last_sent: dict[str, float] = {}
        self._sent_times: list[float] = []

    def _get(self, key, default):
        try:
            value = self.config.get(key, default) if self.config else default
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

    def _can_send(self, umo: str) -> bool:
        now = time.time()
        if now - self._last_sent.get(umo, 0) < self._int("cooldown_minutes", 5) * 60:
            return False
        window = [t for t in self._sent_times if now - t < 3600]
        self._sent_times = window
        return len(window) < self._int("max_per_hour", 3)

    async def initialize(self) -> None:
        load_index()
        if EmotionPicsWebUI is not None:
            try:
                EmotionPicsWebUI(self).register()
                logger.info("emotion_pics: WebUI API 已注册")
            except Exception as exc:  # pragma: no cover
                logger.warning(f"emotion_pics: WebUI 注册失败 {exc}")
        logger.info("emotion_pics: 已启动")

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
            if not self._can_send(umo):
                return
            if random.random() > self.probability:
                return
            image = self.pick_image(emotion, data)
            if not image:
                return
            path = os.path.join(IMAGE_DIR, image.get("file", ""))
            if not os.path.isfile(path):
                return
            result.chain.append(Image.fromFileSystem(path))
            self._last_sent[umo] = time.time()
            self._sent_times.append(time.time())
            logger.info(f"emotion_pics: 情绪 {emotion} 配图 {image.get('file')}")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"emotion_pics: 配图失败 {type(exc).__name__}: {exc}")
