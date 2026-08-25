"""
proactive_guard —— AstrBot 主动消息门禁 + 每日定时人格消息插件

两大功能：
1. 门禁：拦截「本插件之外」的 AI 主动发言。
   用户未发送消息时，任何 AI 对话的生成与发送都会被阻止
   （白名单插件、用户最近活跃会话的回复除外）。
2. 每日人格消息：每天 gen_time（默认 06:00）在后台生成 5-10 条消息，
   随机分配到 window_start ~ window_end（默认 07:00-23:00）的时间点，
   由 AI 根据人格设定与时间点撰写各不相同的内容，存入内部存储；
   到点自动发送该条并删除；第二天再生成新计划，日复一日。
   全程后台静默运行，不向用户发送任何报备/确认消息。

要求：AstrBot 4.x（插件 API 按 4.x 编写）
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import re
import sys
import time
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star

try:
    from astrbot.api.event import MessageChain
except ImportError:  # 旧版本兼容
    from astrbot.api.message_components import MessageChain

FALLBACK_PERSONA = (
    "你是一个温柔、自然、不油腻的朋友型 AI 陪伴者。说话像真实的朋友，"
    "简洁自然，会关心人但不过度肉麻，不滥用表情，不叫用户「亲」「宝」。"
)

DEFAULT_PROMPT = """你是{persona}
今天是{date}。请在下面给出的 {count} 个时间点，分别给用户写一条简短消息（20~60字），要求：
1. 每条消息要贴合对应时间点的场景（早晨问候、上午加油、中午提醒休息吃饭、下午闲聊、傍晚关心、晚上问候、睡前道晚安等）
2. 每条内容必须不同，风格自然口语化，像真人朋友随手发的一条
3. 只输出一个 JSON 数组，格式：[{"time": "07:35", "text": "消息内容"}, ...]
4. 不要输出 JSON 以外的任何文字
时间点：{time_list}"""


class ProactiveGuardPlugin(Star):
    """主动消息门禁 + 每日定时人格消息"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._bypass_cnt = 0
        self._orig_send_message = None
        self._cron_job_ids: list[str] = []
        self._fallback_sched = None
        self._last_user_activity: dict[str, float] = {}
        self._gen_lock = asyncio.Lock()
        self._last_gen_attempt = 0.0

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    async def initialize(self) -> None:
        """插件激活时：安装门禁 -> 暂停内置主动任务 -> 注册定时任务"""
        if self._cfg_bool("enabled", True):
            if self._cfg_bool("block_proactive", True):
                self._install_gate()
            await self._pause_active_agent_jobs()
            await self._setup_scheduler()

    async def terminate(self) -> None:
        """插件禁用/重载时：卸载门禁、删除定时任务"""
        ctx = self.context
        if getattr(ctx, "_proactive_guard_installed", False):
            try:
                ctx.send_message = self._orig_send_message
                setattr(ctx, "_proactive_guard_installed", False)
                logger.info("[proactive_guard] 已卸载主动消息门禁")
            except Exception as e:
                logger.warning(f"[proactive_guard] 卸载门禁失败: {e}")
        try:
            cm = getattr(self.context, "cron_manager", None)
            if cm is not None and hasattr(cm, "delete_job"):
                for jid in self._cron_job_ids:
                    if jid:
                        try:
                            await cm.delete_job(jid)
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            if self._fallback_sched is not None:
                self._fallback_sched.shutdown(wait=False)
                self._fallback_sched = None
        except Exception:
            pass

    # ================================================================== #
    # 门禁：拦截非本插件的主动发送
    # ================================================================== #
    def _install_gate(self) -> None:
        ctx = self.context
        if getattr(ctx, "_proactive_guard_installed", False):
            return
        orig = ctx.send_message
        self._orig_send_message = orig

        async def wrapped(session, message_chain, *args, **kwargs):
            if self._allow_send(session):
                return await orig(session, message_chain, *args, **kwargs)
            try:
                caller = self._caller_module()
                logger.warning(
                    f"[proactive_guard] 已拦截非白名单主动消息 -> {session}（来源模块: {caller}）"
                )
            except Exception:
                pass
            return False

        ctx.send_message = wrapped
        setattr(ctx, "_proactive_guard_installed", True)
        logger.info("[proactive_guard] 主动消息门禁已安装（仅放行本插件/白名单/活跃会话回复）")

    def _allow_send(self, session) -> bool:
        if self._bypass_cnt > 0:
            return True
        caller = self._caller_module()
        for prefix in self._allowlist():
            if prefix and prefix in caller:
                return True
        if not self._cfg_bool("strict_mode", False):
            try:
                s = str(session) if session else ""
                if s and self._recent_user_activity(s):
                    return True
            except Exception:
                pass
        return False

    def _allowlist(self) -> list[str]:
        val = self.config.get("allow_senders")
        if not val:
            return []
        return [s.strip() for s in str(val).splitlines() if s.strip()]

    def _caller_module(self) -> str:
        # 按模块对象身份跳过本插件自身的帧（不受模块命名影响）
        my_mod = sys.modules.get(self.__class__.__module__, None)
        frame = inspect.currentframe()
        while frame is not None:
            mod = frame.f_globals.get("__name__", "") or ""
            if my_mod is not None and frame.f_globals is getattr(
                my_mod, "__dict__", None
            ):
                frame = frame.f_back
                continue
            if mod and "proactive_guard" not in mod:
                return mod
            frame = frame.f_back
        return "unknown"

    # ------------------------------------------------------------------ #
    # 用户活跃度记录（用于区分「回复用户」与「主动发言」）
    # ------------------------------------------------------------------ #
    @filter.event_message_type(EventMessageType.ALL)
    async def _on_user_message(self, event: AstrMessageEvent):
        """观察所有用户消息，记录会话最近活跃时间（不产生任何回复）。"""
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if umo:
                self._last_user_activity[umo] = time.time()
                if len(self._last_user_activity) > 500:
                    # 简单裁剪：删除最早的一半
                    items = sorted(
                        self._last_user_activity.items(), key=lambda kv: kv[1]
                    )
                    self._last_user_activity = dict(items[len(items) // 2 :])
        except Exception:
            pass

    def _recent_user_activity(self, session_str: str) -> bool:
        last = self._last_user_activity.get(session_str)
        if not last:
            return False
        window = self._cfg_int("active_window_minutes", 30) * 60
        return time.time() - last <= window

    # ------------------------------------------------------------------ #
    # 暂停 AstrBot 内置「主动型 Agent」定时任务（可逆）
    # ------------------------------------------------------------------ #
    async def _pause_active_agent_jobs(self) -> None:
        if not self._cfg_bool("pause_active_agent_jobs", True):
            return
        try:
            cm = getattr(self.context, "cron_manager", None)
            if cm is None or not hasattr(cm, "list_jobs"):
                return
            jobs = await cm.list_jobs()
            paused = 0
            for job in jobs:
                jt = getattr(job, "job_type", "") or ""
                enabled = getattr(job, "enabled", True)
                if jt == "active_agent" and enabled:
                    try:
                        await cm.update_job(getattr(job, "job_id", ""), enabled=False)
                        paused += 1
                    except Exception as e:
                        logger.warning(f"[proactive_guard] 暂停主动任务失败: {e}")
            if paused:
                logger.info(f"[proactive_guard] 已暂停 {paused} 个主动型 Agent 定时任务（可恢复）")
        except Exception as e:
            logger.warning(f"[proactive_guard] 暂停主动任务出错: {e}")

    # ================================================================== #
    # 定时任务：每日生成 + 每分钟检查
    # ================================================================== #
    async def _setup_scheduler(self) -> None:
        gen_cron = str(self.config.get("gen_time") or "0 6 * * *").strip()
        cm = getattr(self.context, "cron_manager", None)
        if cm is not None and hasattr(cm, "add_basic_job"):
            try:
                j1 = await cm.add_basic_job(
                    name="proactive_guard_gen",
                    cron_expression=gen_cron,
                    handler=self._on_gen_time,
                    description="每日生成人格消息计划（后台）",
                    enabled=True,
                    persistent=False,
                )
                self._cron_job_ids.append(getattr(j1, "job_id", None))
                j2 = await cm.add_basic_job(
                    name="proactive_guard_minute",
                    cron_expression="* * * * *",
                    handler=self._on_minute,
                    description="每分钟检查待发送的人格消息",
                    enabled=True,
                    persistent=False,
                )
                self._cron_job_ids.append(getattr(j2, "job_id", None))
                logger.info(
                    f"[proactive_guard] 定时任务已注册（cron_manager）: 生成 {gen_cron} / 分钟检查 * * * * *"
                )
                return
            except Exception as e:
                logger.warning(f"[proactive_guard] cron_manager 注册失败，回退 APScheduler: {e}")
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            self._fallback_sched = AsyncIOScheduler()
            self._fallback_sched.add_job(
                self._on_gen_time,
                CronTrigger.from_crontab(gen_cron),
                id="pg_gen",
                misfire_grace_time=300,
            )
            self._fallback_sched.add_job(
                self._on_minute,
                CronTrigger.from_crontab("* * * * *"),
                id="pg_min",
                misfire_grace_time=30,
            )
            self._fallback_sched.start()
            logger.info(f"[proactive_guard] 定时任务已注册（APScheduler）: 生成 {gen_cron} / 分钟检查")
        except Exception as e:
            logger.error(f"[proactive_guard] 定时任务注册失败: {e}")

    async def _on_gen_time(self) -> None:
        """到生成时间：确保今日计划存在（后台静默）。"""
        try:
            await self._ensure_schedule(force=False)
        except Exception as e:
            logger.error(f"[proactive_guard] 生成今日计划失败: {e}", exc_info=True)

    async def _on_minute(self) -> None:
        """每分钟：发送到点的消息，并兜底补种今日计划。"""
        try:
            await self._dispatch_due()
            await self._ensure_schedule(force=False)
        except Exception as e:
            logger.error(f"[proactive_guard] 分钟检查任务失败: {e}", exc_info=True)

    # ================================================================== #
    # 每日计划：生成 / 存储 / 发送 / 删除
    # ================================================================== #
    async def _ensure_schedule(self, force: bool) -> None:
        if not self._cfg_bool("enabled", True):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        sched = await self._load_schedule()
        if sched and sched.get("date") == today and sched.get("items"):
            return
        if not force:
            # 生成失败后的补种：仅在生成时间后的 5 小时内尝试，且每次间隔 >= 20 分钟
            gen_cron = str(self.config.get("gen_time") or "0 6 * * *").strip()
            gen_hour = 6
            try:
                parts = gen_cron.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    gen_hour = int(parts[1])
            except Exception:
                pass
            now = datetime.now()
            if not (gen_hour <= now.hour < gen_hour + 5):
                return
            if time.time() - self._last_gen_attempt < 20 * 60:
                return
        async with self._gen_lock:
            sched = await self._load_schedule()
            if sched and sched.get("date") == today and sched.get("items"):
                return
            self._last_gen_attempt = time.time()
            await self._generate_and_store()

    async def _generate_and_store(self) -> None:
        """后台生成今日计划并写入 KV 存储（静默，不通知用户）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        count = random.randint(
            self._cfg_int("msg_count_min", 5), self._cfg_int("msg_count_max", 10)
        )
        times = self._random_times(count)
        time_list = "、".join(times)
        persona = await self._resolve_persona()
        prompt = (self.config.get("message_prompt") or DEFAULT_PROMPT).replace(
            "{date}", today
        ).replace("{persona}", persona).replace(
            "{time_list}", time_list
        ).replace("{count}", str(count))
        text = await self._llm_chat(prompt)
        items = self._parse_items(text, times)
        if not items:
            logger.error("[proactive_guard] 今日计划生成失败：无法解析 LLM 输出，稍后重试")
            return
        await self.put_kv_data("daily_schedule", {"date": today, "items": items})
        logger.info(f"[proactive_guard] 今日计划已生成：{len(items)} 条消息 @ {time_list}（后台静默）")

    async def _resolve_persona(self) -> str:
        """获取 AstrBot 当前人格设定（不单独配置）。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is not None:
            try:
                # v3 人格（AstrBot 4.x 默认）：按配置的 default_personality 解析
                get_default = getattr(pm, "get_default_persona_v3", None)
                if callable(get_default):
                    p = await get_default()
                    text = self._persona_text(p)
                    if text:
                        return text
                # 兜底：初始化时选中的 v3 人格 / v2 人格
                for attr in ("selected_default_persona_v3", "selected_default_persona"):
                    p = getattr(pm, attr, None)
                    if p is not None:
                        text = self._persona_text(p)
                        if text:
                            return text
            except Exception as e:
                logger.debug(f"[proactive_guard] 读取 AstrBot 人格失败: {e}")
        return FALLBACK_PERSONA

    @staticmethod
    def _persona_text(p) -> str:
        """从 Personality / Persona 对象中提取设定文本。"""
        if p is None:
            return ""
        prompt = ""
        name = ""
        try:
            prompt = getattr(p, "prompt", None) or (
                p.get("prompt") if isinstance(p, dict) else None
            )
            name = getattr(p, "name", None) or (
                p.get("name") if isinstance(p, dict) else None
            )
        except Exception:
            pass
        if not prompt:
            try:
                prompt = getattr(p, "persona", None) or (
                    p.get("persona") if isinstance(p, dict) else None
                )
            except Exception:
                pass
        prompt = str(prompt or "").strip()
        if not prompt:
            return ""
        name = str(name or "").strip()
        if name:
            return f"人格名称：{name}\n人格设定：{prompt}"
        return f"人格设定：{prompt}"

    def _random_times(self, count: int) -> list[str]:
        start = self._hhmm_to_min(str(self.config.get("window_start") or "07:00"))
        end = self._hhmm_to_min(str(self.config.get("window_end") or "23:00"))
        if end <= start:
            end = start + 16 * 60
        pool = list(range(start, end + 1))
        random.shuffle(pool)
        chosen = sorted(pool[:count])
        return [f"{m // 60:02d}:{m % 60:02d}" for m in chosen]

    @staticmethod
    def _hhmm_to_min(s: str) -> int:
        try:
            h, m = s.strip().split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return 7 * 60

    @staticmethod
    def _normalize_time(t: str) -> str:
        t = t.strip()
        try:
            h, m = t.split(":")
            return f"{int(h):02d}:{int(m):02d}"
        except Exception:
            return t

    def _parse_items(self, text: str, expected_times: list[str]) -> list[dict]:
        items: list[dict] = []
        # 1) JSON 数组
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            try:
                arr = json.loads(m.group(0))
                if isinstance(arr, list):
                    for obj in arr:
                        if not isinstance(obj, dict):
                            continue
                        t = self._normalize_time(str(obj.get("time", "")).strip())
                        msg = str(
                            obj.get("text") or obj.get("message") or ""
                        ).strip()
                        if t in expected_times and msg:
                            items.append({"time": t, "text": msg})
            except Exception:
                pass
        # 2) 行解析：HH:MM|text / HH:MM: text / HH:MM text（去掉行首编号后匹配）
        if not items:
            for line in text.splitlines():
                line = re.sub(r"^(?:\d+\s*[.、)）\-]\s*)+", "", line.strip())
                m2 = re.match(
                    r"^(\d{1,2}:\d{2})\s*[:|\-]\s*(.+)$", line
                ) or re.match(r"^(\d{1,2}:\d{2})\s+(.+)$", line)
                if m2:
                    t = self._normalize_time(m2.group(1))
                    msg = m2.group(2).strip()
                    if t in expected_times and msg:
                        items.append({"time": t, "text": msg})
        # 3) 去重（同一时间只保留一条）
        seen: set[str] = set()
        dedup: list[dict] = []
        for it in items:
            if it["time"] not in seen:
                seen.add(it["time"])
                dedup.append(it)
        return dedup

    async def _dispatch_due(self) -> None:
        """到点发送并删除该时间点；过期在补偿窗口内补发，超窗丢弃。"""
        sched = await self._load_schedule()
        if not sched or not sched.get("items"):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if sched.get("date") != today:
            return
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        grace = self._cfg_int("missed_grace_minutes", 30)
        due: list[dict] = []
        removed: list[str] = []
        for it in sched["items"]:
            it_min = self._hhmm_to_min(str(it.get("time", "")))
            if it_min == now_min:
                due.append(it)
            elif it_min < now_min:
                if now_min - it_min <= grace:
                    due.append(it)  # 补发
                else:
                    removed.append(str(it.get("time", "")))  # 超窗丢弃
        if not due and not removed:
            return
        targets = await self._collect_target_sessions()
        for it in due:
            await self._send_silent(str(it.get("text", "")), targets)
            removed.append(str(it.get("time", "")))
            logger.info(f"[proactive_guard] 已发送 {it.get('time')} 的消息并移除该时间点")
        if removed:
            sched["items"] = [
                it for it in sched["items"] if str(it.get("time", "")) not in removed
            ]
            await self.put_kv_data("daily_schedule", sched)

    # ------------------------------------------------------------------ #
    # 发送（绕过门禁，静默）
    # ------------------------------------------------------------------ #
    async def _send_silent(self, text: str, targets: list[str]) -> None:
        if not targets:
            return
        chunks = self._split_text(text)
        for umo in targets:
            for chunk in chunks:
                try:
                    chain = MessageChain().message(chunk)
                    self._bypass_cnt += 1
                    try:
                        await self.context.send_message(umo, chain)
                    finally:
                        self._bypass_cnt -= 1
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error(f"[proactive_guard] 发送到 {umo} 失败: {e}")

    async def _collect_target_sessions(self) -> list[str]:
        sessions: set[str] = set()
        cfg_targets = self.config.get("target_sessions")
        if cfg_targets:
            for line in str(cfg_targets).splitlines():
                line = line.strip()
                if line and ":" in line:
                    sessions.add(line)
            return sorted(sessions)
        try:
            db = self.context.get_db()
            from sqlalchemy import select

            from astrbot.core.db.po import ConversationV2

            async with db.get_db() as sess:
                res = await sess.execute(select(ConversationV2.user_id).distinct())
                for row in res:
                    v = str(row[0]) if row[0] else ""
                    if ":" not in v:
                        continue
                    if self._cfg_bool("only_private", True) and not self._is_private(v):
                        continue
                    sessions.add(v)
        except Exception as e:
            logger.warning(f"[proactive_guard] 数据库枚举会话失败: {e}")
        return sorted(sessions)

    @staticmethod
    def _is_private(umo: str) -> bool:
        low = umo.lower()
        return "friend" in low or "private" in low or "c2c" in low or "privatemsg" in low

    # ------------------------------------------------------------------ #
    # KV 存储
    # ------------------------------------------------------------------ #
    async def _load_schedule(self) -> dict:
        try:
            return (await self.get_kv_data("daily_schedule", None)) or {}
        except Exception:
            return {}

    # ================================================================== #
    # 指令（用户主动触发时才回复）
    # ================================================================== #
    @filter.command("今日计划")
    async def today_plan_command(self, event: AstrMessageEvent):
        """查看今日待发送的人格消息计划"""
        sched = await self._load_schedule()
        today = datetime.now().strftime("%Y-%m-%d")
        if not sched or sched.get("date") != today or not sched.get("items"):
            yield event.plain_result("📭 今日暂无待发送的消息计划。")
            return
        lines = [f"📋 今日计划（{today}）"]
        for it in sorted(sched["items"], key=lambda x: x.get("time", "")):
            lines.append(f"- {it.get('time')} {str(it.get('text', ''))[:40]}")
        yield event.plain_result("\n".join(lines))

    @filter.command("重建今日计划")
    async def rebuild_plan_command(self, event: AstrMessageEvent):
        """立即在后台重新生成今日计划"""
        yield event.plain_result("⏳ 正在后台重建今日计划…")
        asyncio.create_task(self._ensure_schedule(force=True))

    # ================================================================== #
    # LLM 调用（新 API 优先，旧 API 回退）
    # ================================================================== #
    async def _llm_chat(self, prompt: str) -> str:
        ctx = self.context
        try:
            prov_id = None
            get_using = getattr(ctx, "get_using_provider_async", None)
            if callable(get_using):
                prov = await get_using()
                meta = getattr(prov, "meta", None) if prov else None
                if callable(meta):
                    prov_id = getattr(meta(), "id", None)
            if prov_id and callable(getattr(ctx, "llm_generate", None)):
                resp = await ctx.llm_generate(
                    chat_provider_id=prov_id,
                    prompt=prompt,
                    system_prompt="你是一个内容生成助手，严格按照要求输出。",
                )
                text = self._llm_text(resp)
                if text:
                    return text
        except Exception as e:
            logger.debug(f"[proactive_guard] llm_generate 调用失败: {e}")
        try:
            get_prov = getattr(ctx, "get_using_provider", None)
            if callable(get_prov):
                prov = get_prov()
                if prov is not None and callable(getattr(prov, "text_chat", None)):
                    resp = await prov.text_chat(
                        prompt=prompt, session_id=None, image_urls=[]
                    )
                    text = self._llm_text(resp)
                    if text:
                        return text
        except Exception as e:
            logger.debug(f"[proactive_guard] text_chat 调用失败: {e}")
        raise RuntimeError("无法获取可用的 LLM 提供商")

    @staticmethod
    def _llm_text(resp) -> str:
        if resp is None:
            return ""
        for attr in ("completion_text", "result", "text"):
            try:
                v = getattr(resp, attr, None)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            except Exception:
                continue
        try:
            rc = getattr(resp, "result_chain", None)
            if rc is not None and hasattr(rc, "get_plain_text"):
                t = rc.get_plain_text()
                if t:
                    return t
        except Exception:
            pass
        return ""

    # ================================================================== #
    # 工具方法
    # ================================================================== #
    def _cfg_bool(self, key: str, default: bool) -> bool:
        v = self.config.get(key, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _split_text(text: str, limit: int = 3800) -> list[str]:
        text = text.strip()
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        cur = ""
        for line in text.splitlines(keepends=True):
            while len(line) > limit:
                if cur:
                    chunks.append(cur)
                    cur = ""
                chunks.append(line[:limit])
                line = line[limit:]
            if cur and len(cur) + len(line) > limit:
                chunks.append(cur)
                cur = line
            else:
                cur += line
        if cur:
            chunks.append(cur)
        return chunks