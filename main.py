"""大肥鱼房间：把她"此刻在干什么"变成一个 3D 房间场景。

状态来源有三层：
1. 时段（0-8 睡、8-19 用电脑、19-24 床上刷手机）
2. 最近有没有聊天（有 → 立刻变成"拿手机"）
3. 她的回复（气泡里滚出她的话）

页面每 2 秒读一次 room.json，切换场景与姿势。
"""

import json
import os
import time

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

PLUGIN_NAME = "dafeiyu_room"
PAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages", "room")
OUT = os.path.join(PAGE_DIR, "room.json")
MOOD_FILE = "/AstrBot/data/plugin_data/astrbot_plugin_persona_state/mood.json"
HORMONE_FILE = "/AstrBot/data/plugin_data/dafeiyu_hormone/hormones.json"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "plugin_data", "dafeiyu_room")
DATA_DIR = os.path.normpath(DATA_DIR)
ACTIVITY_TTL = 300


def _mood() -> dict:
    """优先读新的四激素心境引擎；没有就退回旧的 PAD。"""
    try:
        with open(HORMONE_FILE, encoding="utf-8") as f:
            h = json.load(f)
        sero = float(h.get("sero", 0.6))
        dopa = float(h.get("dopa", 0.4))
        cort = float(h.get("cort", 0.2))
        oxy = float(h.get("oxy", 0.5))
        return {
            "available": True,
            "joy": max(0.0, min(1.0, dopa)),
            "calm": max(0.0, min(1.0, sero)),
            "cort": max(0.0, min(1.0, cort)),
            "oxy": max(0.0, min(1.0, oxy)),
        }
    except Exception:
        pass
    try:
        with open(MOOD_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {
            "available": True,
            "joy": max(0.0, min(1.0, float(d.get("joy", 0.5)))),
            "calm": max(0.0, min(1.0, float(d.get("calm", 0.5)))),
        }
    except Exception:
        return {"joy": 0.5, "calm": 0.5, "available": False}


class Room(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.cfg = config or {}
        self.bubbles: list[dict] = []
        self.last_activity = 0.0
        self.mode = "sleep"
        self._task = None

    def _c(self, key, default):
        try:
            v = self.cfg.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    # ---------------------------------------------------------------- 状态

    def _base_mode(self) -> str:
        h = time.localtime().tm_hour
        if 0 <= h < 8:
            return "sleep"
        if 8 <= h < 19:
            return "desk"
        if 19 <= h < 24:
            return "phone"
        return "sleep"

    def _compute_mode(self) -> str:
        now = time.time()
        quiet_min = float(self._c("quiet_minutes", 25)) * 60
        if now - self.last_activity < quiet_min:
            # 刚聊完 —— 她在拿手机
            if self._base_mode() == "sleep":
                return "phone"
            return "phone"
        return self._base_mode()

    def _label(self, mode: str) -> str:
        return {
            "sleep": "睡着了",
            "phone": "躺床上刷手机",
            "desk": "在电脑前",
            "window": "站在窗边发呆",
            "away": "不在房间",
        }.get(mode, "在房间")

    def _flush(self) -> dict:
        self.mode = self._compute_mode()
        label = self._label(self.mode)
        bubbles = list(self.bubbles[-8:])
        ext = self._activity_state()
        if ext:
            # 外部活动（刷抖音等）优先，覆盖按时段的默认状态
            self.mode = ext["mode"]
            label = ext["label"]
            for b in ext["bubbles"]:
                bubbles.append({"who": "her", "text": str(b)[:80], "ts": time.time()})
            bubbles = bubbles[-8:]
        data = {
            "mode": self.mode,
            "label": label,
            "ts": time.time(),
            "clock": time.strftime("%H:%M"),
            "bubbles": bubbles,
            "mood": _mood(),
            "activity": (ext or {}).get("app", ""),
        }
        try:
            os.makedirs(PAGE_DIR, exist_ok=True)
            tmp = OUT + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, OUT)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 写 room.json 失败：{exc!r}")
        return data

    def _add(self, who: str, text: str) -> None:
        text = " ".join(str(text or "").split())[:160]
        if not text:
            return
        self.bubbles.append({"who": who, "text": text, "ts": time.time()})
        self.bubbles = self.bubbles[-12:]
        self.last_activity = time.time()

    # ---------------------------------------------------------------- 钩子

    @filter.on_llm_request(priority=95000)
    async def on_user(self, event: AstrMessageEvent, req) -> None:
        try:
            txt = ""
            for c in (getattr(getattr(event, "message_obj", None), "message", []) or []):
                if isinstance(c, Plain) and getattr(c, "text", ""):
                    txt += c.text
            if not txt:
                return
            if str(txt).strip().startswith(("/", "／")):
                return
            self._add("you", txt)
            self._flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 记录用户消息失败：{exc!r}")

    @filter.on_decorating_result(priority=8000)
    async def on_reply(self, event: AstrMessageEvent) -> None:
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None) if result else None
            if not chain:
                return
            txt = " ".join(c.text for c in chain
                           if isinstance(c, Plain) and getattr(c, "text", ""))
            self._add("her", txt)
            self._flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 记录回复失败：{exc!r}")


    @filter.command("房间")
    async def cmd_room(self, event: AstrMessageEvent, action: str = "", minutes: str = "30"):
        """看状态或设置活动。用法：/房间  ｜  /房间 刷抖音 30 ｜  /房间 结束"""
        a = str(action or "").strip()
        if a in ("结束", "stop", "清空"):
            try:
                if os.path.exists(self.ACTIVITY_FILE):
                    os.remove(self.ACTIVITY_FILE)
            except Exception:
                pass
            self._flush()
            yield event.plain_result("已回到正常状态。")
            return
        if a:
            app = {"刷抖音": "douyin", "抖音": "douyin", "刷b站": "bilibili", "b站": "bilibili",
                   "刷快手": "kuaishou", "刷小红书": "xiaohongshu", "刷微博": "weibo",
                   "打游戏": "game", "看书": "reading", "听歌": "music"}.get(a, "douyin")
            try:
                mins = max(1, min(240, int(minutes)))
            except ValueError:
                mins = 30
            os.makedirs(DATA_DIR, exist_ok=True)
            payload = {"app": app, "label": a, "ts": time.time(), "ttl": mins * 60}
            tmp = self.ACTIVITY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.ACTIVITY_FILE)
            d = self._flush()
            yield event.plain_result(f"她现在：{d['label']}（{mins} 分钟内有效）")
            return
        d = self._flush()
        yield event.plain_result(
            f"她现在：{d['label']}（{d['clock']}）" +
            (f"\n活动来源：{d.get('activity')}" if d.get("activity") else ""))



    # ---------------------------------------------------------------- 生命周期


    # ---------------------------------------------------------------- 外部活动

    ACTIVITY_FILE = os.path.join(DATA_DIR, "activity.json")

    APP_LABEL = {
        "douyin": ("刷抖音", "phone"),
        "tiktok": ("刷 TikTok", "phone"),
        "bilibili": ("刷 B 站", "phone"),
        "kuaishou": ("刷快手", "phone"),
        "xiaohongshu": ("刷小红书", "phone"),
        "weibo": ("刷微博", "phone"),
        "game": ("打游戏", "desk"),
        "reading": ("看书", "bed"),
        "music": ("听歌", "bed"),
    }

    def _read_activity(self) -> dict | None:
        try:
            with open(self.ACTIVITY_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return None
        ts = float(d.get("ts") or 0)
        if not ts:
            try:
                ts = os.path.getmtime(self.ACTIVITY_FILE)
            except Exception:
                ts = 0
        if not ts or time.time() - ts > float(d.get("ttl") or ACTIVITY_TTL):
            return None
        return d

    def _activity_state(self) -> dict | None:
        """把外部活动翻译成房间状态。"""
        a = self._read_activity()
        if not a:
            return None
        app = str(a.get("app") or "").strip().lower()
        label, pose = self.APP_LABEL.get(app, (str(a.get("label") or "在玩手机"), "phone"))
        if a.get("label"):
            label = str(a["label"])
        bubbles = a.get("bubbles")
        if not isinstance(bubbles, list) or not bubbles:
            bubbles = {
                "douyin": ["这个笑死我", "又刷到卖货的", "再看一条就睡"],
                "bilibili": ["这个 UP 主有意思", "码住", "先看三分钟"],
            }.get(app, ["嗯…", "有意思"])
        return {
            "mode": pose,
            "label": label,
            "bubbles": bubbles[:3],
            "source": "external",
            "app": app,
        }

    async def api_activity(self, **kwargs):
        """外部上报活动：POST {"app":"douyin","label":"刷抖音","seconds":600}"""
        try:
            body = {}
            if "body" in kwargs:
                body = kwargs.get("body") or {}
            elif kwargs.get("request") is not None:
                body = await kwargs["request"].json()
            app = str(body.get("app") or "").strip().lower()
            if not app:
                return {"status": "error", "message": "缺少 app", "data": {}}
            payload = {
                "app": app,
                "label": str(body.get("label") or ""),
                "bubbles": body.get("bubbles") or [],
                "ts": time.time(),
                "ttl": int(body.get("seconds") or self.ACTIVITY_TTL),
            }
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = self.ACTIVITY_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self.ACTIVITY_FILE)
            logger.info(f"[{PLUGIN_NAME}] 收到外部活动：{app} / {payload['label'] or '-'}")
            return {"status": "ok", "message": "已记录", "data": payload}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 上报失败：{exc!r}")
            return {"status": "error", "message": str(exc)[:120], "data": {}}

    async def api_activity_stop(self, **kwargs):
        """结束活动：POST {}"""
        try:
            if os.path.exists(self.ACTIVITY_FILE):
                os.remove(self.ACTIVITY_FILE)
            logger.info(f"[{PLUGIN_NAME}] 外部活动结束")
            return {"status": "ok", "message": "已清空", "data": {}}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "message": str(exc)[:120], "data": {}}

    async def initialize(self) -> None:
        import asyncio
        self._flush()

        async def loop():
            interval = max(5, int(self._c("refresh_seconds", 10)))
            while True:
                try:
                    self._flush()
                except Exception:
                    pass
                await asyncio.sleep(interval)

        self._task = asyncio.create_task(loop())

        routes = [("/dafeiyu_room/activity", self.api_activity, "房间外部活动上报"),
                  ("/room/activity", self.api_activity, "房间外部活动上报(别名)"),
                  ("/dafeiyu_room/activity/stop", self.api_activity_stop, "房间外部活动结束"),
                  ("/room/activity/stop", self.api_activity_stop, "房间外部活动结束(别名)")]
        ok = 0
        for route, fn, desc in routes:
            try:
                self.context.register_web_api(route, fn, ["POST"], desc)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 注册 {route} 失败：{exc!r}")
        logger.info(f"[{PLUGIN_NAME}] 房间已就绪，页面数据={OUT}，活动接口 {ok} 个")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
