"""
proactive_guard —— AstrBot 主动消息门禁 + 每日定时人格消息插件

两大功能：
1. 门禁：拦截「本插件之外」的 AI 主动发言。
   用户未发送消息时，任何 AI 对话的生成与发送都会被阻止
   （白名单插件、用户最近活跃会话的回复除外）。
2. 每日人格消息：每天在后台预生成「明天」的 5-10 条消息，
   随机分配到 window_start ~ window_end（默认 07:00-23:00）的时间点，
   由 AI 根据人格设定与时间点撰写各不相同的内容，存入内部存储；
   到点自动发送该条并删除；跨天自动把预生成的明日计划提升为今日计划，
   日复一日。全程后台静默运行，不向用户发送任何报备/确认消息。

记忆（v1.1.0）：
- 写入：主动消息发出后追加到会话历史（assistant 角色），见 _record_to_history；
- 读取：生成明天的计划前，先从会话历史里捞出近期真实对话，作为「记忆」
  注入提示词，让主动消息能接上之前聊过的话题，而不是每天凭空寒暄。
  由 memory_enabled / memory_turns 控制。

子代理生成（v1.1.0）：
- 主动消息不再由「一把梭」的单次 LLM 调用产出，而是走子代理流水线：
  规划代理（分时段给一句「写什么」的要点）-> 并行写手代理（每个时间点
  独立成文，各有自己的上下文与记忆片段）-> 审校代理（统一语气、去重、
  修掉不合场景的内容），最后归一为 [{time, text}]。
  由 subagent_enabled 控制，关闭则回退单次调用（v1.0.x 行为）。

KV 存储（v1.0.1 双槽位）：
- daily_plan       ：date = 今天，到点派发用
- daily_plan_next  ：date = 明天，提前预生成
清理明天的计划后重载 -> 立即重新生成明天，绝不生成后天。

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
from pathlib import Path

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

DEFAULT_PROMPT = """{persona}{world_block}
今天是{date}。请在下面 {count} 个时间点，各写一条你主动发给用户的简短消息（20~60字）。
要求：
1. 大部分（约 {share_ratio}%）写**你自己正在经历的事**：此刻在做什么、去了哪里、遇到谁、看到什么、心情如何；要具体、有画面感，符合上面的【世界观设定】（若已配置）与你的身份
2. 其余写**对用户的关心**：接着他最近在做的事（军训、作业、熬夜、身体不舒服…）具体问一句或提醒一句，不要「吃了吗」「多喝水」这类模板话
3. 每条贴合对应时间点（早晨/上午/中午/下午/傍晚/晚上/睡前），但不要写成定时问候模板
4. 每条内容必须不同，第一人称，自然口语化
5. 只输出一个 JSON 数组，格式：[{"time": "07:35", "text": "消息内容"}, ...]
6. 不要输出 JSON 以外的任何文字
时间点：{time_list}"""

WORLD_BLOCK_TEMPLATE = """
【世界观设定】（你在其中的世界、身份与所处环境；你的分享必须落在这个框架内，不要出现设定之外的概念）：
{world}"""

# --- 子代理流水线用的三个提示词 ------------------------------------------ #
# 注意：{persona} 展开后本身就是一句完整的自我描述（如「你是一个温柔…的 AI 陪伴者」），
# 所以这里不要写成「你是{persona}」，否则会拼出「你是你是一个…」。
# {world_block} 是 _world_block() 的产物：没配 world_setting 时是空串，配了就是上面那段。
# 规划代理：把「今天要发什么」拆成每个时间点一句要点（不写正文，只给方向）
PLANNER_PROMPT = """{persona}{world_block}
今天是{date}。请为下面 {count} 个时间点各规划**一句话要点**，说明那一刻你要主动发给用户什么。
{memory_block}

核心：你是主动分享的一方，不是客服。要点要落在「你自己正在经历什么」上。
1. 其中约 {share_count} 条的要点必须是**你自己的经历与见闻**：此刻在做什么、去了哪里、遇到谁、看到什么、用了什么魔法、天气与心情、忽然想到的事。要具体到小事，不要写「问候一下」「关心他吃饭」这种空泛指令。
2. 其余约 {care_count} 条的要点是**对用户的关心或呼应**：基于记忆里他最近在做的事（军训、作业、熬夜、身体不舒服…）具体地问一句或提醒一句，不要模板话。
3. 所有要点都必须落在上面的【世界观设定】（若已配置）与人格身份之内，不要出现设定之外的现代概念（除非设定里就有）。
4. 若有「近期对话记忆」，关心类要点可以呼应它；分享类要点不要每条都从记忆起头。
5. 各时间点不要重复，一天下来像一条自然流动的线：分享与关心交替，别连着几条都在问同一件事。
6. 只输出一个 JSON 数组：[{{"time": "07:35", "point": "要点"}}, ...]
7. 不要输出 JSON 以外的任何文字
时间点：{time_list}"""

# 写手代理：只负责一个时间点的一条消息
WRITER_PROMPT = """{persona}{world_block}
今天是{date}，现在是{time}。请用第一人称给用户写一条 20~60 字的主动消息。
{memory_block}
这条消息的要点：{point}

要求：
1. 要点若是「分享自己的经历」，就具体讲你此刻正在做的事、看到的东西、心里的感觉，让人有画面感；不要写成问候。
2. 要点若是「关心用户」，就问得具体、有来由（接着记忆里他正在忙的事），不要泛泛的「吃了吗」「多喝水」。
3. 符合上面的【世界观设定】（若已配置）与你的身份，不要出现设定外的概念；不要自称 AI，不要提及「设定」「记忆」「提示词」。
4. 允许句末自然带一句轻问，但不要每条都以问句结尾，更不要连珠炮式反问。
5. 只输出这条消息的正文，不要输出引号、不要输出时间、不要任何解释说明"""

# 审校代理：统一语气、去重、修掉不合场景的内容
REVIEWER_PROMPT = """{persona}{world_block}
下面是一天里要发给用户的一批消息草稿（JSON 数组），请你审校后输出最终版本。

审校要求：
1. 语气统一、自然，像一个真实的人在同一天里陆续发的，不要每条都像模板
2. 去掉重复、雷同、空泛的内容；同一件事不要反复提
3. 修掉与时间点场景不符的内容（如早晨发「晚安」）
4. 每条保持 20~60 字，不要加时间前缀，不要加编号
5. 条数、时间点必须与输入**完全一致**，不要增删
6. 检查「分享自己 vs 关心用户」的分布：如果几乎每条都在问用户吃饭睡觉、缺少分享自己经历的内容，就把其中几条改写成第一人称分享自己的见闻；如果一条关心都没有，把最贴合场景的一条改成具体的关心
7. 所有内容必须在【世界观设定】（若已配置）与人格之内；出现「作为AI」「语言模型」「设定」「记忆」这类出戏或元叙述的词，一律改掉
8. 只输出一个 JSON 数组：[{{"time": "07:35", "text": "消息内容"}}, ...]，不要输出 JSON 以外的任何文字

草稿：
{draft}"""

MEMORY_BLOCK_TEMPLATE = """
近期对话记忆（用户与你的真实聊天片段，按时间从早到晚）：
{memory}
（以上只是背景，不要直接复述、不要提及「记忆」「记录」这类词）"""

# 记忆库的 KV key（结构见 refresh_memory 的 docstring）
MEMORY_KEY = "memory_store"

# 用户自己写在 AstrBot 工作区里的笔记（workspaces/<会话>/user_profile.md）。
# 这份是「事实」而不是「AI 的滚动摘要」：用户改了笔记就该立刻生效。
MEMORY_NOTES_TEMPLATE = """
【用户笔记】用户本人写下的事实（优先级高于下面的记忆摘要，两者冲突时以笔记为准）：
{notes}"""

# AstrBot 会把当前时间塞进用户消息里：
# <system_reminder>Current datetime: 2026-10-13 20:33 (CST), Weekday: Tuesday</system_reminder>
# 记忆必须知道「哪句话是哪天说的」，否则旧待办会一直被当成仍然成立。
_TS_RE = re.compile(r"Current datetime:\s*(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})")
_SYS_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S)

# 记忆总结代理：把「旧摘要 + 新对话」滚动重写成一份聚焦的用户画像
MEMORY_PROMPT = """你是一个记忆整理助手。你的任务是把对话里**关于这位用户这个人**的信息记下来，
写成一份随时能查的个人档案。

已有记忆（上一轮的总结，可能为空）：
{old}

用户笔记（用户本人写的，事实优先级最高；与记忆冲突时以它为准）：
{notes}

新的对话记录（从早到晚，每行前的 [日期 时间] 是这句话说出的时间）：
{raw}

要记什么（这是重点）：
1. **发生过的事**：用户做过什么、遇到什么、去了哪、事情进展到哪一步
2. **爱好与偏好**：喜欢/讨厌什么、口味、习惯、作息、喜欢的表达方式与称呼
3. **正在做的事**：项目、学业、考试、工作、行程、长期目标，以及各自的进度
4. **人际关系**：家人、朋友、同学、同事，用户怎么称呼他们、关系如何、发生过什么
5. **情绪与状态**：最近的心情、身体状态、压力来源
6. **明确约定**：用户和你说好要做的事、提过的计划

不要记什么：
- **不要写任何关于你自己（助手）的描述**：你的自称、口头禅、语气、性格、你做过什么、你承诺过什么。
  你的行为风格由人格设定负责，写进记忆只会和人格设定重复、互相打架
- 不要记「用户问了你一个问题」「你回答了」这类流水账
- 不要把「用户这一次让你做什么」当成长期事实（一次性指令不是档案）
- 不要记一次性琐事；有日期的事可以记但要带日期，超过 7 天的旧安排不再保留
- 不要编造对话里没出现过的信息

处理旧记忆里「待办 / 未完成 / 计划」这类条目的规则（最容易出错，务必遵守）：
- 如果新对话里出现了「已经做了 / 领过了 / 办完了」这类说法，立刻删掉对应待办，改成「已…（日期）」
- 如果用户笔记里已经写了结果，以笔记为准，旧的待办一律删掉
- 旧待办如果在新对话里完全没被提起，且看不出是长期目标，也删掉——不要假设它仍然成立
- 宁可少记，也不要把已经过期的事当成正在发生

写法：
- 第三人称，主语是「用户…」，短句分条，每行一条
- **同一件事只写一次**；旧记忆与新对话重复的合并成一条，不要两处都留
- 新旧信息冲突时以新的为准
- 整份档案不超过 {max_chars} 字；超了先删最不重要、最可能过期的条目
- 只输出记忆正文，不要标题、不要编号、不要解释你做了什么"""

MEMORY_MERGE_PROMPT = """下面是同一个用户在不同会话里留下的记忆摘要，内容有重复，也可能互相矛盾。
请把它们合并成**一份**去重后的个人档案。

{items}

合并要求：
1. 只保留关于**用户这个人**的信息：发生过的事、爱好与偏好、正在做的事、人际关系、情绪状态、明确约定
2. **删掉所有关于助手（你）自己的描述**：自称、语气、性格、做过什么、承诺过什么——这些由人格设定负责
3. 同一件事只留一条；不同会话说法冲突时，保留更新、更具体的那条
4. 有日期的安排保留日期，超过 7 天的旧安排删掉
5. 第三人称「用户…」，短句分条，每行一条，不超过 {max_chars} 字
6. 只输出记忆正文，不要标题、不要解释"""

# 记忆里出现这些开头的行 = 在描述助手自己，注入前直接丢掉（兜底，防止模型不听话）
_MEMORY_SELF_SUBJECTS = ("助手", "ai", "你", "机器人", "bot", "assistant", "模型")

# 发送前的事实复核：计划是提前生成的，记忆却随时可能更新，
# 于是会出现「记忆里写着卡已领、消息还在提醒去领卡」。这条提示词只做一件事：
# 拿最新记忆核对这一条待发消息，冲突就改写或丢弃。
FACT_CHECK_PROMPT = """你要在一条主动关心消息**发出去之前**做最后核对。

【最新记忆】
{memory}

【待发送的消息】
{message}

请判断这条消息是否与记忆矛盾，尤其是：
- 提醒用户去做记忆里已经**完成**的事（最典型：卡已经领了还在提醒去领）
- 描述与人设/记忆里的身份、地点、作息明显不符的状态
- 把已经过期的安排当成还没发生

只输出一个 JSON，不要任何解释：
{{"conflict": true/false, "reason": "一句话原因", "rewritten": "改写后的消息"}}

要求：
- conflict 为 false 时：rewritten 留空字符串
- conflict 为 true 时：如果这条消息改掉冲突部分后仍然值得发，就给出 rewritten（保持原本的语气、长度、口语风格，不要解释你改了什么）；如果整条消息的前提都不成立了，rewritten 留空字符串
- 不要输出 JSON 以外的任何字符"""


def _loose_llm_text(text, for_json: bool = False) -> str:
    """把 LLM 输出里常见的非标准写法拉回可解析形态。

    实测踩过的雷（NAS 上 00:00 的计划生成直接失败）：
      * 输出被 ```json 代码围栏包住；
      * 时间写成全角冒号「07：35」或「07.35」；
      * JSON 里用了全角引号/花括号/方括号；
      * 数组末尾多一个逗号（json.loads 直接抛）。
    这里只做「结构层」归一，正文措辞一个字都不改（引号归一仅在 for_json 时做）。
    """
    if not text:
        return ""
    t = str(text).replace("\ufeff", "")
    # 去掉 ```json / ``` 围栏
    t = re.sub(r"```[A-Za-z0-9_+-]*", "", t)
    # 时间里的全角冒号 / 点号 / 中点：07：35 -> 07:35
    t = re.sub(r"(\d{1,2})\s*[：﹕．·]\s*(\d{2})", r"\1:\2", t)
    # JSON 结构用的全角括号
    t = (
        t.replace("｛", "{")
        .replace("｝", "}")
        .replace("［", "[")
        .replace("］", "]")
    )
    # 尾逗号：{"a":1,} / [1,2,]
    t = re.sub(r",\s*([}\]])", r"\1", t)
    if for_json:
        t = (
            t.replace("“", '"')
            .replace("”", '"')
            .replace("‘", "'")
            .replace("’", "'")
        )
    return t



def _json_objects(text: str) -> list[dict]:
    """从文本里尽力捞出 JSON 对象：先整数组，再逐个 {...} 兜底。"""
    out: list[dict] = []
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
        except Exception:
            arr = None
        if isinstance(arr, list):
            out.extend(o for o in arr if isinstance(o, dict))
            return out
        for chunk in re.findall(r"\{[^{}]*\}", m.group(0), re.S):
            try:
                obj = json.loads(chunk)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        if out:
            return out
    for chunk in re.findall(r"\{[^{}]*\}", text, re.S):
        try:
            obj = json.loads(chunk)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out



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
        # 记忆刷新互斥：定时任务、生成前冷却刷新、/刷新记忆 可能同时触发
        self._memory_lock = asyncio.Lock()
        self._last_gen_attempt = 0.0
        self._migrated = False

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
        if val and str(val).strip():
            return [s.strip() for s in str(val).splitlines() if s.strip()]
        # 默认放行用户自己的 daily_digest（日报）插件，否则其定时推送会被门禁拦截；
        # 如需完全拦截，在配置里把 allow_senders 设为空以外的自定义值即可（非空配置优先）
        return ["daily_digest"]

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
        # 默认 03:20：排在 03:00 的记忆刷新之后，保证「生成用的记忆」是刚刷过的
        gen_cron = str(self.config.get("gen_time") or "20 3 * * *").strip()
        tz = str(self.config.get("timezone") or "Asia/Shanghai").strip()
        cm = getattr(self.context, "cron_manager", None)
        if cm is not None and hasattr(cm, "add_basic_job"):
            try:
                try:
                    j1 = await cm.add_basic_job(
                        name="proactive_guard_gen",
                        cron_expression=gen_cron,
                        handler=self._on_gen_time,
                        description="每日生成人格消息计划（后台）",
                        enabled=True,
                        persistent=False,
                        timezone=tz,
                    )
                except TypeError:
                    # 旧版 cron_manager 不支持 timezone 参数
                    j1 = await cm.add_basic_job(
                        name="proactive_guard_gen",
                        cron_expression=gen_cron,
                        handler=self._on_gen_time,
                        description="每日生成人格消息计划（后台）",
                        enabled=True,
                        persistent=False,
                    )
                self._cron_job_ids.append(getattr(j1, "job_id", None))
                try:
                    j2 = await cm.add_basic_job(
                        name="proactive_guard_minute",
                        cron_expression="* * * * *",
                        handler=self._on_minute,
                        description="每分钟检查待发送的人格消息",
                        enabled=True,
                        persistent=False,
                        timezone=tz,
                    )
                except TypeError:
                    j2 = await cm.add_basic_job(
                        name="proactive_guard_minute",
                        cron_expression="* * * * *",
                        handler=self._on_minute,
                        description="每分钟检查待发送的人格消息",
                        enabled=True,
                        persistent=False,
                    )
                self._cron_job_ids.append(getattr(j2, "job_id", None))
                # 记忆总结任务（可关）
                if self._cfg_bool("memory_enabled", True):
                    mem_cron = str(
                        self.config.get("memory_cron") or "0 3 * * *"
                    ).strip()
                    try:
                        j3 = await cm.add_basic_job(
                            name="proactive_guard_memory",
                            cron_expression=mem_cron,
                            handler=self._on_memory_time,
                            description="把近期对话总结进记忆库（后台静默）",
                            enabled=True,
                            persistent=False,
                            timezone=tz,
                        )
                    except TypeError:
                        j3 = await cm.add_basic_job(
                            name="proactive_guard_memory",
                            cron_expression=mem_cron,
                            handler=self._on_memory_time,
                            description="把近期对话总结进记忆库（后台静默）",
                            enabled=True,
                            persistent=False,
                        )
                    self._cron_job_ids.append(getattr(j3, "job_id", None))
                    logger.info(f"[proactive_guard] 记忆总结任务已注册: {mem_cron}（时区 {tz}）")
                logger.info(
                    f"[proactive_guard] 定时任务已注册（cron_manager）: 生成 {gen_cron} "
                    f"/ 分钟检查 * * * * *（时区 {tz}）"
                )
                return
            except Exception as e:
                logger.warning(f"[proactive_guard] cron_manager 注册失败，回退 APScheduler: {e}")
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger

            self._fallback_sched = AsyncIOScheduler(timezone=tz)
            self._fallback_sched.add_job(
                self._on_gen_time,
                CronTrigger.from_crontab(gen_cron, timezone=tz),
                id="pg_gen",
                misfire_grace_time=300,
            )
            self._fallback_sched.add_job(
                self._on_minute,
                CronTrigger.from_crontab("* * * * *", timezone=tz),
                id="pg_min",
                misfire_grace_time=30,
            )
            if self._cfg_bool("memory_enabled", True):
                mem_cron = str(self.config.get("memory_cron") or "0 3 * * *").strip()
                self._fallback_sched.add_job(
                    self._on_memory_time,
                    CronTrigger.from_crontab(mem_cron, timezone=tz),
                    id="pg_mem",
                    misfire_grace_time=600,
                )
            self._fallback_sched.start()
            logger.info(
                f"[proactive_guard] 定时任务已注册（APScheduler）: 生成 {gen_cron} "
                f"/ 分钟检查（时区 {tz}）"
            )
        except Exception as e:
            logger.error(f"[proactive_guard] 定时任务注册失败: {e}")

    async def _on_gen_time(self) -> None:
        """到生成时间：先刷记忆 → 必要时重建今日剩余计划 → 再预生成明日计划。

        顺序很重要：预生成机制下今天的计划是昨天写的，可能比记忆旧十几个小时。
        所以这里先强制刷一次记忆（不靠 30 分钟冷却），再拿新记忆去校正今天还没发的那几条。
        """
        try:
            if self._cfg_bool("memory_enabled", True) and self._cfg_bool(
                "memory_refresh_before_gen", True
            ):
                try:
                    await self.refresh_memory()
                except Exception as e:
                    logger.warning(f"[proactive_guard] 生成前刷新记忆失败: {e}")
            if self._cfg_bool("dispatch_regenerate_on_memory_change", True):
                try:
                    # 顺序：先把昨日预生成的那份提升为今日计划，再检查它是否基于旧记忆。
                    # 反过来会漏：提升发生在检查之后，等于永远检查的是「昨天那份」。
                    await self._ensure_schedule(force=False)
                    await self._regenerate_today_if_stale()
                    return
                except Exception as e:
                    logger.warning(f"[proactive_guard] 重建今日计划失败: {e}")
            await self._ensure_schedule(force=False)
        except Exception as e:
            logger.error(f"[proactive_guard] 生成计划失败: {e}", exc_info=True)

    async def _on_memory_time(self) -> None:
        """到记忆总结时间：把近期对话交给 AI 总结，写进记忆库（后台静默）。"""
        try:
            await self.refresh_memory()
        except Exception as e:
            logger.error(f"[proactive_guard] 记忆总结失败: {e}", exc_info=True)

    async def _on_minute(self) -> None:
        """每分钟：发送到点的消息，并兜底补种计划。"""
        try:
            await self._dispatch_due()
            await self._ensure_schedule(force=False)
        except Exception as e:
            logger.error(f"[proactive_guard] 分钟检查任务失败: {e}", exc_info=True)

    # ================================================================== #
    # 每日计划：生成 / 存储 / 发送 / 删除
    #
    # KV 双槽位设计（v1.0.1）：
    #   - daily_plan       ：date = 今天，到点派发用
    #   - daily_plan_next  ：date = 明天，预生成（提前一天生成）
    # 这样「清理明天的计划后重载」只会重新生成明天，绝不生成后天。
    # ================================================================== #
    async def _migrate_old_schedule(self) -> None:
        """v1.0.0 -> v1.0.1 迁移：把旧键 daily_schedule 拆进双槽位后删除旧键。"""
        if self._migrated:
            return
        self._migrated = True
        try:
            old = await self.get_kv_data("daily_schedule", None)
            if not old:
                return
            today = datetime.now().strftime("%Y-%m-%d")
            tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
            d = old.get("date")
            if d == today and old.get("items"):
                await self.put_kv_data("daily_plan", old)
            elif d == tomorrow and old.get("items"):
                await self.put_kv_data("daily_plan_next", old)
            await self.put_kv_data("daily_schedule", None)  # 清掉旧键
            logger.info("[proactive_guard] 已迁移旧版 daily_schedule 到新双槽位")
        except Exception as e:
            logger.warning(f"[proactive_guard] 旧计划迁移失败: {e}")

    async def _ensure_schedule(self, force: bool) -> None:
        """确保：daily_plan.date == 今天，daily_plan_next.date == 明天。

        - 若 daily_plan 过期：把 daily_plan_next 提升为 daily_plan（若其 date 已是
          今天），否则丢弃过期计划；
        - 若 daily_plan_next 不是明天：生成明天的计划。补种任意时刻可进行
          （无小时窗口限制），非 force 时受 20 分钟节流；force 忽略节流
          （清理明天的计划后重载 -> 立即重新生成明天，绝不生成后天）。
        """
        if not self._cfg_bool("enabled", True):
            return
        await self._migrate_old_schedule()
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

        # a) 今天的槽位过期：提升 daily_plan_next 或丢弃
        plan = await self._load_schedule("daily_plan")
        if plan.get("date") != today or not plan.get("items"):
            next_plan = await self._load_schedule("daily_plan_next")
            if next_plan.get("date") == today and next_plan.get("items"):
                await self.put_kv_data("daily_plan", next_plan)
                await self.put_kv_data("daily_plan_next", {})
                logger.info("[proactive_guard] 已将预生成的明日计划提升为今日计划")
            else:
                await self.put_kv_data("daily_plan", {})  # 过期计划静默丢弃

        # b) 明天的槽位缺失：生成明天的计划
        next_plan = await self._load_schedule("daily_plan_next")
        if next_plan.get("date") != tomorrow or not next_plan.get("items"):
            if not force and time.time() - self._last_gen_attempt < 20 * 60:
                return  # 20 分钟节流
            async with self._gen_lock:
                next_plan = await self._load_schedule("daily_plan_next")
                if next_plan.get("date") == tomorrow and next_plan.get("items"):
                    return
                self._last_gen_attempt = time.time()
                await self._generate_and_store(target_date=tomorrow)

    async def _generate_and_store(
        self, target_date: str, times: list[str] | None = None
    ) -> None:
        """后台为指定日期生成计划并写入 KV 存储（静默，不通知用户）。

        目标日期为明天 -> 存 daily_plan_next（预生成）；
        目标日期为今天 -> 存 daily_plan（/重建今日计划 在窗口未结束时补今天的场景）。

        times 显式给出时只用这些时间点（重建「今日剩余计划」时用来保留原有排期）。

        v1.1.0：生成前先读取「记忆」（会话历史里的真实对话），
        再交给子代理流水线（规划 -> 并行写手 -> 审校）产出内容。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        slot = "daily_plan" if target_date == today else "daily_plan_next"
        if times:
            times = [self._normalize_time(str(t)) for t in times if str(t).strip()]
            count = len(times)
        else:
            count = random.randint(
                self._cfg_int("msg_count_min", 5), self._cfg_int("msg_count_max", 10)
            )
            times = self._random_times(count)
        time_list = "、".join(times)
        persona = await self._resolve_persona()
        world_block = self._world_block()
        share_ratio = self._share_ratio()
        share_count, care_count = self._share_counts(count, share_ratio)

        # ---- 记忆：从会话历史里捞近期真实对话 ----
        memory = await self._collect_memory()
        memory_block = ""
        if memory:
            memory_block = MEMORY_BLOCK_TEMPLATE.format(memory=memory)
            logger.debug(
                f"[proactive_guard] 已读取记忆 {len(memory)} 字，用于生成（{target_date}）"
            )

        subagent_on = self._cfg_bool("subagent_enabled", True)
        items = []
        if subagent_on:
            items = await self._generate_with_subagents(
                target_date,
                times,
                time_list,
                count,
                persona,
                memory_block,
                world_block=world_block,
                share_count=share_count,
                care_count=care_count,
            )
        if not items:
            # 兜底：单次调用（v1.0.x 行为）。子代理关闭、或规划代理失败/未产出要点时走这里，
            # 保证「回退单次生成」这句日志名副其实（不再拿到空列表就直接报错）。
            if subagent_on:
                logger.warning(
                    f"[proactive_guard] 子代理流水线未产出结果，回退单次生成（{target_date}）"
                )
            prompt = (
                (self.config.get("message_prompt") or DEFAULT_PROMPT)
                # 老模板写的是「你是{persona}」，而 persona 本身以「你是一个…」开头，
                # 直接替换会得到「你是你是一个…」。先把这种写法整体吃掉。
                .replace("你是{persona}", persona)
                .replace("{world_block}", world_block)
                .replace("{persona}", persona)
                .replace("{share_ratio}", str(share_ratio))
                .replace("{date}", target_date)
                .replace("{time_list}", time_list)
                .replace("{count}", str(count))
            )
            if memory_block:
                prompt = f"{prompt}\n{memory_block}"
            try:
                text = await self._llm_chat(prompt)
                items = self._parse_items(text, times)
            except Exception as e:
                # 兜底也失败（例如 LLM 全挂）：只记日志，不把异常抛给调度器
                logger.warning(f"[proactive_guard] 兜底单次生成失败（{target_date}）: {e}")
                items = []

        if not items:
            logger.error(
                f"[proactive_guard] 计划生成失败（{target_date}）：无法解析 LLM 输出，稍后重试"
            )
            return
        # 记录「这份计划是基于哪一版记忆生成的」。派发时会拿它和当前记忆比对，
        # 不一致就说明记忆在生成之后又更新过，需要在发送前复核（见 _dispatch_due）。
        memory_updated = await self._memory_updated()
        await self.put_kv_data(
            slot,
            {
                "date": target_date,
                "items": items,
                "memory_updated": memory_updated,
            },
        )
        logger.info(
            f"[proactive_guard] 计划已生成（{target_date}）：{len(items)} 条消息 "
            f"@ {time_list}（后台静默，记忆 {'有' if memory else '无'}，"
            f"子代理 {'开' if subagent_on else '关'}）"
        )

    # ------------------------------------------------------------------ #
    # 记忆库：AI 总结会话 -> 存 KV -> 生成时读取
    # ------------------------------------------------------------------ #
    async def refresh_memory(self, umo: str | None = None) -> str:
        """把近期对话交给 AI 总结，写入记忆库（KV）。

        记忆库结构（KV key = MEMORY_KEY）：
        {
          "updated": "2026-10-15 03:00",
          "items": [{"umo": "...", "summary": "...", "updated": "..."}]
        }

        与原始历史不同，这里存的是**AI 提炼过的要点**（称呼、正在做的事、
        情绪状态、提过的偏好与约定），比整段对话更省 token、也更聚焦，
        并且会滚动更新（每次把「旧摘要 + 新对话」一起交给 AI 重写）。
        """
        if not self._cfg_bool("memory_enabled", True):
            return ""
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if cm is None or not hasattr(cm, "get_conversation"):
                return ""
            targets = [umo] if umo else await self._collect_target_sessions()
            if not targets:
                return ""
            store = await self._load_memory_store()
            items = {str(it.get("umo", "")): it for it in store.get("items", []) if it}
            max_chars = max(200, self._cfg_int("memory_max_chars", 1500))
            turns = max(2, self._cfg_int("memory_turns", 20))
            window_hours = max(0, self._cfg_int("memory_window_hours", 48))
            rewrite_stale = self._cfg_bool("memory_rewrite_stale", True)
            notes = await self._collect_user_notes()
            for target in targets:
                try:
                    lines = await self._history_lines(cm, target, turns, window_hours)
                    old = str(items.get(target, {}).get("summary", "")).strip()
                    # 没有新对话的会话原来直接 continue，导致旧摘要里的错误永远不被修正
                    # （比如「待办：万事达卡」即使人早领了也一直留着）。这里让旧记忆
                    # 也按新规则过一次，只整理、不新增。
                    if not lines and not (rewrite_stale and old):
                        continue
                    if lines:
                        raw = "\n".join(lines)
                    else:
                        raw = (
                            "（这个会话近期没有新对话。请只整理上面的旧记忆：删掉已完成、"
                            "已过期、被用户笔记否定的条目，合并重复项，保留仍然成立的事实；"
                            "不要新增任何对话里没出现过的信息。）"
                        )
                    prompt = MEMORY_PROMPT.format(
                        old=old or "（暂无）",
                        notes=notes or "（无）",
                        raw=raw[-max_chars * 2 :],
                        max_chars=max_chars,
                    )
                    summary = (await self._llm_chat(prompt)).strip()
                    summary = self._clean_message(summary) if "\n" not in summary else summary
                    if not summary:
                        continue
                    items[target] = {
                        "umo": target,
                        "summary": summary[:max_chars],
                        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    }
                except Exception as e:
                    logger.warning(f"[proactive_guard] 总结会话记忆失败（{target}）: {e}")
            if not items:
                return ""
            # 多会话 -> 合并成一份去重档案。原来三分段并列，同一件事（熬夜、不吃早饭）
            # 会被写三遍，注入时既费 token 又像复读。
            merged = ""
            if len(items) > 1:
                try:
                    packed = "\n\n".join(
                        f"【会话 {i + 1}】\n{str(it.get('summary', '')).strip()}"
                        for i, it in enumerate(items.values())
                    )
                    merged = await self._llm_chat(
                        MEMORY_MERGE_PROMPT.format(items=packed, max_chars=max_chars)
                    )
                    merged = self._filter_memory_lines(merged)[:max_chars]
                except Exception as e:
                    logger.warning(f"[proactive_guard] 记忆合并失败，沿用分会话记忆: {e}")
                    merged = ""
            store = {
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "items": list(items.values()),
                "merged": merged,
            }
            await self.put_kv_data(MEMORY_KEY, store)
            logger.info(
                f"[proactive_guard] 记忆已更新：{len(items)} 个会话"
                f"（{', '.join(str(it.get('umo', '')) for it in items.values())}）"
                f"，合并档案 {'已生成' if merged else '未生成'}"
            )
            return self._memory_text(store)
        except Exception as e:
            logger.warning(f"[proactive_guard] 刷新记忆失败: {e}")
            return ""

    async def _load_memory_store(self) -> dict:
        try:
            data = await self.get_kv_data(MEMORY_KEY, None)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except Exception:
            pass
        return {"items": []}

    @staticmethod
    def _filter_memory_lines(text: str) -> str:
        """丢掉「在描述助手自己」的条目，并按内容去重。

        兜底层：即使提示词写明不要写 AI 自我介绍，模型仍会加上「助手自称魔女」
        「助手会提醒喝水」这类行。这些由人格设定负责，留在记忆里只会重复并挤占预算。
        """
        out: list[str] = []
        seen: set[str] = set()
        for raw in str(text or "").splitlines():
            line = raw.strip().lstrip("-*•·　").strip()
            if not line:
                continue
            head = line[:10].lower()
            if head.startswith(_MEMORY_SELF_SUBJECTS) or "助手" in head:
                continue
            key = re.sub(r"[\s，。；、,.：:!！?？~～]+", "", line)
            if key in seen:
                continue
            seen.add(key)
            out.append(line)
        return "\n".join(out)

    def _memory_text(self, store: dict) -> str:
        """把记忆库渲染成注入提示词的一段文本。

        优先用合并后的单一档案（merged）；没有（单会话 / 合并失败）才回退分会话拼装。
        两种情况都过 _filter_memory_lines。
        """
        store = store or {}
        merged = self._filter_memory_lines(str(store.get("merged") or ""))
        if merged:
            return merged
        items = [it for it in store.get("items", []) if it and it.get("summary")]
        if not items:
            return ""
        if len(items) == 1:
            return self._filter_memory_lines(str(items[0]["summary"]).strip())
        # 跨会话拼装：同名事实只留第一条（按「去掉标点后的正文」判重，
        # 不能用「【umo】+正文」判重，否则前缀不同会让重复行全部留下）
        lines: list[str] = []
        seen: set[str] = set()
        for it in items:
            body = self._filter_memory_lines(str(it["summary"]).strip())
            if not body:
                continue
            key = re.sub(r"[\s，。；、,.：:!！?？~～]+", "", body)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"【{it.get('umo', '')}】{body}")
        return "\n".join(lines)

    def _workspaces_dir(self) -> Path:
        """AstrBot 数据目录下的 workspaces（插件在 <data>/plugins/<name>/main.py）。"""
        try:
            return Path(__file__).resolve().parent.parent.parent / "workspaces"
        except Exception:
            return Path("")

    async def _collect_user_notes(self) -> str:
        """读取用户自己写在工作区里的 user_profile.md。

        为什么必须读它：记忆摘要由 AI 滚动生成，用户改的事实（例如「卡已领」）
        要等下一轮总结才可能被吸收，而且常常被旧摘要里的待办压住。笔记是用户
        亲手写的**事实源**，直接拼进上下文，改写立刻生效、绕开摘要滞后。
        """
        if not self._cfg_bool("memory_notes_enabled", True):
            return ""
        budget = max(200, self._cfg_int("memory_notes_chars", 1200))
        try:
            root = self._workspaces_dir()
            if not root or not root.is_dir():
                return ""
            files = sorted(
                root.glob("*/user_profile.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception as e:
            logger.debug(f"[proactive_guard] 扫描用户笔记失败: {e}")
            return ""
        chunks: list[str] = []
        seen: set[str] = set()
        used = 0
        for f in files[:5]:
            try:
                body = f.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            body = re.sub(r"\n{3,}", "\n\n", body)[:budget]
            key = re.sub(r"\s+", "", body)
            if not body or key in seen:
                continue
            seen.add(key)
            chunks.append(body)
            used += len(body)
            if used >= budget:
                break
        if not chunks:
            return ""
        logger.debug(f"[proactive_guard] 已读取用户笔记 {used} 字（{len(chunks)} 份）")
        return MEMORY_NOTES_TEMPLATE.format(notes="\n".join(chunks))

    async def _collect_memory(self) -> str:
        """取记忆文本：AI 总结的记忆库 + 用户笔记；为空则实时总结；再不行退回原始历史。

        三级降级是为了保证「第一次运行、还没到总结时间」时也有记忆可用。
        另外在返回前按 `memory_refresh_cooldown` 冷却做一次就地刷新——只靠每天
        03:00 那一次，白天发生的事最多要滞后近 24 小时才可能进入主动消息。
        """
        if not self._cfg_bool("memory_enabled", True):
            return ""
        notes = await self._collect_user_notes()
        try:
            store = await self._load_memory_store()
            if await self._memory_refresh_due(store):
                async with self._memory_lock:
                    # 双检：等锁期间别人可能刚刷过
                    store = await self._load_memory_store()
                    if await self._memory_refresh_due(store):
                        logger.debug("[proactive_guard] 记忆已过期，生成前就地刷新")
                        await self.refresh_memory()
                        store = await self._load_memory_store()
            text = self._memory_text(store)
            if not text:
                # 记忆库还空着：立刻总结一次（首次生成计划时就地建库）
                text = await self.refresh_memory()
            if text:
                merged = self._truncate_memory(text)
                return (notes + "\n" + merged) if notes else merged
        except Exception as e:
            logger.debug(f"[proactive_guard] 读取记忆库失败，回退原始历史: {e}")
        if not self._cfg_bool("memory_raw_fallback", True):
            return notes
        raw = self._truncate_memory(await self._collect_raw_history())
        if notes and raw:
            return notes + "\n" + raw
        return notes or raw

    async def _memory_refresh_due(self, store: dict) -> bool:
        """记忆是否到了该刷新的时间（按 memory_refresh_cooldown 分钟冷却）。"""
        try:
            cooldown = max(5, self._cfg_int("memory_refresh_cooldown", 30))
            stamp = str((store or {}).get("updated", "") or "").strip()
            if not stamp:
                return True
            last = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
            return (datetime.now() - last).total_seconds() >= cooldown * 60
        except Exception:
            return False

    def _truncate_memory(self, text: str) -> str:
        budget = max(200, self._cfg_int("memory_max_chars", 1500))
        if len(text) <= budget:
            return text
        kept = text[-budget:]
        nl = kept.find("\n")
        if nl != -1:
            kept = kept[nl + 1 :]
        return kept

    async def _collect_raw_history(self) -> str:
        """兜底：未经总结的原始对话历史（短、杂，不如记忆库聚焦）。"""
        turns = max(1, self._cfg_int("memory_turns", 20))
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if cm is None or not hasattr(cm, "get_conversation"):
                return ""
            targets = await self._collect_target_sessions()
            if not targets:
                return ""
            merged: list[str] = []
            for umo in targets[:5]:
                lines = await self._history_lines(cm, umo, turns)
                if len(lines) > len(merged):
                    merged = lines
            return "\n".join(merged)
        except Exception as e:
            logger.debug(f"[proactive_guard] 读取原始历史失败: {e}")
            return ""

    async def _history_lines(
        self, cm, umo: str, turns: int, window_hours: int | None = None
    ) -> list[str]:
        """把单个会话的历史整理成「[MM-DD HH:MM] 用户：…」行列表。

        `window_hours > 0` 时按时间窗过滤，而不是只数条数——只取「最近 N 条」
        会让一句纠正很快滑出窗口，旧摘要里的错误待办就被原样继承下去。
        时间来自 AstrBot 注入在用户消息里的 <system_reminder>Current datetime: …；
        拿不到时间戳的老数据退化为「取最近 turns 条」。
        """
        try:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return []
            conv = await cm.get_conversation(umo, cid)
            raw = getattr(conv, "history", None) if conv is not None else None
            if not raw:
                return []
            history = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(history, list):
                return []
            cutoff = None
            if window_hours and window_hours > 0:
                cutoff = datetime.now() - timedelta(hours=window_hours)
            records: list[tuple[datetime | None, str]] = []
            last_ts: datetime | None = None
            for msg in history:
                if not isinstance(msg, dict):
                    continue
                role = str(msg.get("role", ""))
                content = msg.get("content", "")
                # content 可能是多模态数组，只取文本部分（think 段不带 text，自动跳过）
                if isinstance(content, list):
                    parts = [
                        str(p.get("text", ""))
                        for p in content
                        if isinstance(p, dict) and p.get("text")
                    ]
                    content = " ".join(parts)
                content = str(content)
                m = _TS_RE.search(content)
                if m:
                    try:
                        last_ts = datetime.strptime(
                            f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M"
                        )
                    except ValueError:
                        pass
                content = _SYS_REMINDER_RE.sub("", content).strip().replace("\n", " ")
                if not content:
                    continue
                if role == "user":
                    records.append((last_ts, f"用户：{content}"))
                elif role == "assistant":
                    records.append((last_ts, f"你：{content}"))
            if cutoff is not None:
                kept = [r for r in records if r[0] is None or r[0] >= cutoff]
                # 全被时间窗滤掉（长时间没聊）时保留尾巴，别让记忆凭空失忆
                if kept:
                    records = kept
            if len(records) > turns:
                records = records[-turns:]
            out: list[str] = []
            for ts, line in records:
                out.append(f"[{ts.strftime('%m-%d %H:%M')}] {line}" if ts else line)
            return out
        except Exception as e:
            logger.debug(f"[proactive_guard] 读取会话历史失败（{umo}）: {e}")
            return []

    # ------------------------------------------------------------------ #
    # 人格 / 世界观 / 分享比例
    # ------------------------------------------------------------------ #

    def _world_block(self) -> str:
        """世界观设定块。没填就留空，提示词里不出现这一段。"""
        world = str(self.config.get("world_setting") or "").strip()
        if not world:
            return ""
        return WORLD_BLOCK_TEMPLATE.format(world=world)

    def _share_ratio(self) -> int:
        """自我分享占比（0~100）。其余部分留给对用户的关心。"""
        try:
            ratio = int(self.config.get("self_share_ratio", 70))
        except (TypeError, ValueError):
            ratio = 70
        return max(0, min(100, ratio))

    @staticmethod
    def _share_counts(count: int, ratio: int) -> tuple[int, int]:
        """把占比换算成条数，并保证「关心用户」那一类至少 1 条。

        用户明确要求两类都要：不能为了自我分享把关心挤成 0 条。
        """
        count = max(1, int(count or 1))
        share = max(0, min(count, round(count * ratio / 100)))
        care = count - share
        if count >= 2 and care == 0:
            care = 1
            share = count - 1
        return share, care

    # ------------------------------------------------------------------ #
    # 子代理流水线：规划 -> 并行写手 -> 审校
    # ------------------------------------------------------------------ #
    async def _generate_with_subagents(
        self,
        target_date: str,
        times: list[str],
        time_list: str,
        count: int,
        persona: str,
        memory_block: str,
        world_block: str = "",
        share_count: int = 0,
        care_count: int = 0,
    ) -> list[dict]:
        """用三个「子代理」协作产出一天的消息。

        子代理在这里就是三次独立、职责单一、上下文各自收敛的 LLM 调用：
        规划代理只出要点（不写正文），写手代理一次只写一条（上下文最小、
        最不容易被别的时段带偏），审校代理统管全局语气与去重。
        比单次「一把梭」更稳：某个时间点写砸了不会拖垮整天，而且每个写手
        只看到自己那一条的记忆片段，不会串味。
        """
        # --- 1) 规划代理：每个时间点一句要点 ---
        planner_prompt = PLANNER_PROMPT.format(
            persona=persona,
            world_block=world_block,
            date=target_date,
            count=count,
            time_list=time_list,
            memory_block=memory_block,
            share_count=share_count,
            care_count=care_count,
        )
        try:
            plan_text = await self._llm_chat(planner_prompt)
            points = self._parse_points(plan_text, times)
        except Exception as e:
            logger.warning(f"[proactive_guard] 规划代理失败，回退单次生成: {e}")
            return []
        if not points:
            logger.warning("[proactive_guard] 规划代理未产出可用要点，回退单次生成")
            return []

        # --- 2) 写手代理：每人只写自己那条，并行 ---
        async def write_one(t: str, point: str) -> dict | None:
            prompt = WRITER_PROMPT.format(
                persona=persona,
                world_block=world_block,
                date=target_date,
                time=t,
                point=point,
                memory_block=memory_block,
            )
            try:
                text_out = await self._llm_chat(prompt)
            except Exception as e:
                logger.warning(f"[proactive_guard] 写手代理失败（{t}）: {e}")
                return None
            text_out = self._clean_message(text_out)
            if not text_out:
                logger.debug(f"[proactive_guard] 写手代理返回空内容（{t}）")
                return None
            return {"time": t, "text": text_out}

        concurrency = max(1, min(self._cfg_int("subagent_concurrency", 3), len(points)))
        if not self._cfg_bool("subagent_parallel", True):
            concurrency = 1
        sem = asyncio.Semaphore(concurrency)

        async def limited(t: str, point: str) -> dict | None:
            async with sem:
                return await write_one(t, point)

        results = await asyncio.gather(
            *(limited(t, p) for t, p in points), return_exceptions=True
        )
        drafts: list[dict] = []
        for r in results:
            if isinstance(r, dict) and r.get("text"):
                drafts.append(r)
        if not drafts:
            logger.warning("[proactive_guard] 写手代理全部失败，回退单次生成")
            return []

        # --- 3) 审校代理：统一语气、去重、修场景错位 ---
        if self._cfg_bool("subagent_review", True) and len(drafts) > 0:
            reviewed = await self._review_drafts(drafts, persona, world_block)
            if reviewed:
                drafts = reviewed

        drafts.sort(key=lambda x: x.get("time", ""))
        return drafts

    async def _review_drafts(
        self, drafts: list[dict], persona: str, world_block: str = ""
    ) -> list[dict]:
        """审校代理：只允许它改文案，条数与时间点以输入为准（防止被 AI 增删）。"""
        if not drafts:
            return []
        times = [d["time"] for d in drafts]
        prompt = REVIEWER_PROMPT.format(
            persona=persona,
            world_block=world_block,
            draft=json.dumps(drafts, ensure_ascii=False),
        )
        try:
            text = await self._llm_chat(prompt)
        except Exception as e:
            logger.warning(f"[proactive_guard] 审校代理失败，使用写手原稿: {e}")
            return []
        if not isinstance(text, str) or not text.strip():
            logger.warning("[proactive_guard] 审校代理返回空内容，使用写手原稿")
            return []
        items = self._parse_items(text, times)
        if len(items) != len(times):
            logger.warning(
                f"[proactive_guard] 审校代理输出条数不符（{len(items)}/{len(times)}），使用写手原稿"
            )
            return []
        return items

    def _parse_points(self, text: str, expected_times: list[str]) -> list[tuple[str, str]]:
        """解析规划代理的 [{time, point}]。字段兼容 point/plan/topic/text。"""
        out: list[tuple[str, str]] = []
        if not isinstance(text, str) or not text.strip():
            return [(t, "自然地聊一句，贴合这个时间的场景") for t in expected_times]

        def _take(obj: dict) -> None:
            t = self._normalize_time(str(obj.get("time", "")).strip())
            point = str(
                obj.get("point")
                or obj.get("plan")
                or obj.get("topic")
                or obj.get("text")
                or obj.get("content")
                or ""
            ).strip()
            if t in expected_times and point:
                out.append((t, point))

        for obj in _json_objects(_loose_llm_text(text, for_json=True)):
            _take(obj)

        if not out:
            # 行解析：HH:MM 要点
            for line in _loose_llm_text(text).splitlines():
                line = re.sub(r"^(?:\d+\s*[.、)）\-]\s*)+", "", line.strip())
                line = line.strip("*_`> ").strip()
                m2 = re.match(r"^(\d{1,2}:\d{2})\s*[:|\-—]?\s*(.+)$", line)
                if m2:
                    t = self._normalize_time(m2.group(1))
                    point = m2.group(2).strip().strip("*_` ").strip()
                    if t in expected_times and point:
                        out.append((t, point))
        if not out:
            logger.warning(
                f"[proactive_guard] 规划代理输出解析不出要点（期望 {expected_times}），"
                f"原始输出前 300 字：{_loose_llm_text(text)[:300]!r}"
            )
        # 去重 + 按输入时间顺序对齐（缺失的时间点用通用要点补齐，避免整天少几条）
        seen: dict[str, str] = {}
        for t, p in out:
            if t not in seen:
                seen[t] = p
        return [(t, seen.get(t, "自然地聊一句，贴合这个时间的场景")) for t in expected_times]

    @staticmethod
    def _clean_message(text: str) -> str:
        """清掉写手代理可能多输出的包装：引号、时间前缀、编号、解释。"""
        if not isinstance(text, str):
            return ""
        t = text.strip()
        if not t:
            return ""
        # 去掉整体包裹的引号
        t = t.strip().strip('"').strip("“”").strip("「」").strip("'").strip()
        # 去掉行首时间前缀："07:35 早上好" / "07:35：早上好"
        t = re.sub(r"^\d{1,2}:\d{2}\s*[:：\-|]?\s*", "", t)
        # 去掉行首编号
        t = re.sub(r"^(?:\d+\s*[.、)）\-]\s*)+", "", t)
        # 只保留第一行有效内容
        for line in t.splitlines():
            line = line.strip()
            if line:
                return line.strip()
        return ""


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
        """把 7:5 / 07：05 / 7.5 / 07点05 统一成 07:05；认不出就原样返回。"""
        t = str(t or "").strip().strip("`'\"“”[]【】()（）")
        # 全角冒号 / 点号 / 中文「点」
        t = re.sub(r"(\d{1,2})\s*[：﹕．·.点]\s*(\d{1,2})", r"\1:\2", t)
        m = re.match(r"^(\d{1,2})\s*:\s*(\d{1,2})", t)
        if m:
            try:
                h, mi = int(m.group(1)), int(m.group(2))
            except Exception:
                return t
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return f"{h:02d}:{mi:02d}"
        return t

    def _parse_items(self, text: str, expected_times: list[str]) -> list[dict]:
        """解析 [{time, text}]。JSON 优先，失败退化到逐行 HH:MM …，都失败返回 []。"""
        items: list[dict] = []
        if not isinstance(text, str) or not text.strip():
            return items

        def _take(obj: dict) -> None:
            t = self._normalize_time(str(obj.get("time", "")).strip())
            msg = str(
                obj.get("text")
                or obj.get("message")
                or obj.get("content")
                or obj.get("msg")
                or ""
            ).strip()
            if t in expected_times and msg:
                items.append({"time": t, "text": msg})

        # 1) JSON（允许代码围栏/全角标点/尾逗号）
        for obj in _json_objects(_loose_llm_text(text, for_json=True)):
            _take(obj)

        # 2) 行解析：HH:MM|text / HH:MM: text / HH:MM text（去掉行首编号与 markdown 强调）
        if not items:
            for line in _loose_llm_text(text).splitlines():
                line = re.sub(r"^(?:\d+\s*[.、)）\-]\s*)+", "", line.strip())
                line = line.strip("*_`> ").strip()
                m2 = re.match(
                    r"^(\d{1,2}:\d{2})\s*[:|\-—]?\s*(.+)$", line
                )
                if m2:
                    t = self._normalize_time(m2.group(1))
                    msg = m2.group(2).strip().strip("*_` ").strip()
                    if t in expected_times and msg:
                        items.append({"time": t, "text": msg})

        # 3) 去重（同一时间只保留一条）
        seen: set[str] = set()
        dedup: list[dict] = []
        for it in items:
            if it["time"] not in seen:
                seen.add(it["time"])
                dedup.append(it)
        if not dedup:
            logger.warning(
                f"[proactive_guard] 解析不出任何时间点（期望 {expected_times}），"
                f"原始输出前 300 字：{_loose_llm_text(text)[:300]!r}"
            )
        return dedup

    async def _dispatch_due(self) -> None:
        """只从 daily_plan（date==今天）派发：到点发送并删除该时间点；
        错过在补偿窗口内补发，超窗丢弃；date<今天的旧计划静默丢弃（由 _ensure_schedule 清理）。

        发送前会做两道保护：
        1. 记忆水位比对：计划的 memory_updated 与当前记忆不一致 => 记忆更新过，必须复核；
        2. 逐条事实校验：拿最新记忆核对这一条消息，冲突则改写或丢弃。
        这两道是给「预生成计划天然滞后」兜底的——计划可能比记忆旧十几小时。
        """
        plan = await self._load_schedule("daily_plan")
        if not plan or not plan.get("items"):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        if plan.get("date") != today:
            return  # 非今天的旧计划，静默丢弃
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        grace = self._cfg_int("missed_grace_minutes", 30)
        due: list[dict] = []
        removed: list[str] = []
        for it in plan["items"]:
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

        # 记忆比生成计划时新了 => 计划内容可能已经过时
        memory_updated = await self._memory_updated()
        plan_watermark = str(plan.get("memory_updated") or "")
        memory_changed = bool(memory_updated) and plan_watermark != memory_updated
        fact_check = self._cfg_bool("dispatch_fact_check", True)
        memory = ""
        if fact_check or memory_changed:
            memory = await self._collect_memory()
        if memory_changed:
            logger.info(
                f"[proactive_guard] 记忆已更新（计划基于 {plan_watermark or '空'}，"
                f"当前 {memory_updated}），发送前逐条复核"
            )

        for it in due:
            text = str(it.get("text", ""))
            if fact_check and memory:
                checked = await self._fact_check_message(text, memory)
                if not checked:
                    removed.append(str(it.get("time", "")))
                    logger.info(
                        f"[proactive_guard] {it.get('time')} 的消息与最新记忆冲突，"
                        f"已丢弃：{text[:60]!r}"
                    )
                    continue
                text = checked
            await self._send_silent(text, targets)
            removed.append(str(it.get("time", "")))
            logger.info(f"[proactive_guard] 已发送 {it.get('time')} 的消息并移除该时间点")

        if removed or memory_changed:
            plan["items"] = [
                it for it in plan["items"] if str(it.get("time", "")) not in removed
            ]
            if memory_updated:
                plan["memory_updated"] = memory_updated
            await self.put_kv_data("daily_plan", plan)

    async def _memory_updated(self) -> str:
        """当前记忆库的更新时间（水位）。空表示还没有记忆。"""
        store = await self._load_memory_store()
        return str(store.get("updated") or "")

    async def _fact_check_message(self, text: str, memory: str) -> str:
        """发送前拿最新记忆复核一条待发消息。

        返回可直接发送的文本（可能被改写）；返回空字符串表示这条应当丢弃。
        LLM 调用失败时原样返回——宁可发出去，也不要因为校验挂了整天不说话。
        """
        text = str(text or "").strip()
        if not text or not memory:
            return text
        try:
            out = await self._llm_chat(
                FACT_CHECK_PROMPT.format(message=text[:1500], memory=memory[:3000])
            )
        except Exception as e:
            logger.warning(f"[proactive_guard] 发送前事实校验失败，按原文发送: {e}")
            return text
        data = None
        for obj in _json_objects(_loose_llm_text(out, for_json=True)):
            data = obj
            break
        if not isinstance(data, dict):
            return text
        conflict = data.get("conflict")
        if isinstance(conflict, str):
            conflict = conflict.strip().lower() in (
                "true",
                "1",
                "yes",
                "y",
                "是",
                "有",
                "冲突",
            )
        if not conflict:
            return text
        reason = str(data.get("reason") or "").strip()
        rewritten = self._clean_message(
            str(data.get("rewritten") or data.get("rewrite") or "")
        )
        if rewritten:
            logger.info(
                f"[proactive_guard] 事实校验改写消息（{reason or '冲突'}）："
                f"{text[:40]!r} -> {rewritten[:40]!r}"
            )
            return rewritten
        return ""

    async def _regenerate_today_if_stale(self) -> None:
        """今日计划若是用旧记忆生成的，就按最新记忆重写「还没到点」的那些时间点。

        预生成机制下今天的计划是昨天写的（最长滞后近两天），典型症状就是
        记忆里已经写着「卡已领」，今天的消息仍在提醒去领卡。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        plan = await self._load_schedule("daily_plan")
        if plan.get("date") != today or not plan.get("items"):
            return
        memory_updated = await self._memory_updated()
        if not memory_updated:
            return
        if str(plan.get("memory_updated") or "") == memory_updated:
            return
        now_min = datetime.now().hour * 60 + datetime.now().minute
        remaining = [
            str(it.get("time", ""))
            for it in plan["items"]
            if self._hhmm_to_min(str(it.get("time", ""))) >= now_min
        ]
        if not remaining:
            plan["memory_updated"] = memory_updated
            await self.put_kv_data("daily_plan", plan)
            return
        async with self._gen_lock:
            logger.info(
                f"[proactive_guard] 记忆已更新（{memory_updated}），重建今日剩余 "
                f"{len(remaining)} 条计划 @ {'、'.join(remaining)}"
            )
            await self._generate_and_store(target_date=today, times=remaining)

    # ------------------------------------------------------------------ #
    # 发送（绕过门禁，静默）
    # ------------------------------------------------------------------ #
    async def _send_silent(self, text: str, targets: list[str]) -> None:
        if not targets:
            return
        chunks = self._split_text(text)
        for umo in targets:
            ok = False
            for chunk in chunks:
                try:
                    chain = MessageChain().message(chunk)
                    self._bypass_cnt += 1
                    try:
                        ok = bool(await self.context.send_message(umo, chain)) or ok
                    finally:
                        self._bypass_cnt -= 1
                    await asyncio.sleep(0.3)
                except Exception as e:
                    logger.error(f"[proactive_guard] 发送到 {umo} 失败: {e}")
            if ok and self._cfg_bool("record_to_history", True):
                await self._record_to_history(umo, text)

    async def _record_to_history(self, umo: str, text: str) -> None:
        """把主动消息写入该会话的 LLM 对话历史，避免「被动输出与主动输出」记忆断层。

        与 AstrBot 官方 persist_agent_history 同一机制：通过 conversation_manager
        在会话当前对话的 history 末尾追加一条 assistant 消息，模型后续回复时即可看到。
        """
        if not self._cfg_bool("record_to_history", True):
            return
        try:
            cm = getattr(self.context, "conversation_manager", None)
            if cm is None or not hasattr(cm, "update_conversation"):
                return
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return  # 会话还没有对话，跳过
            conv = await cm.get_conversation(umo, cid)
            history: list[dict] = []
            if conv is not None and getattr(conv, "history", None):
                try:
                    history = json.loads(conv.history) or []
                except Exception:
                    history = []
            if not isinstance(history, list):
                history = []
            history.append({"role": "assistant", "content": text})
            await cm.update_conversation(umo, cid, history=history)
            logger.debug(f"[proactive_guard] 已把主动消息写入会话历史（{umo}）")
        except Exception as e:
            logger.warning(f"[proactive_guard] 写入会话历史失败（{umo}）: {e}")

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
    async def _load_schedule(self, key: str) -> dict:
        try:
            return (await self.get_kv_data(key, None)) or {}
        except Exception:
            return {}

    # ================================================================== #
    # 指令（用户主动触发时才回复）
    # ================================================================== #
    @filter.command("今日计划")
    async def today_plan_command(self, event: AstrMessageEvent):
        """查看今日待发送的人格消息计划 + 明日预生成摘要"""
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        plan = await self._load_schedule("daily_plan")
        next_plan = await self._load_schedule("daily_plan_next")
        lines: list[str] = []
        if plan.get("date") == today and plan.get("items"):
            lines.append(f"📋 今日计划（{today}）")
            for it in sorted(plan["items"], key=lambda x: x.get("time", "")):
                lines.append(f"- {it.get('time')} {str(it.get('text', ''))[:40]}")
        else:
            lines.append(f"📭 今日（{today}）暂无待发送的消息计划。")
        lines.append("")
        if next_plan.get("date") == tomorrow and next_plan.get("items"):
            lines.append(f"🗓️ 明日计划已预生成（{tomorrow}，共 {len(next_plan['items'])} 条）")
            for it in sorted(next_plan["items"], key=lambda x: x.get("time", ""))[:5]:
                lines.append(f"- {it.get('time')} {str(it.get('text', ''))[:40]}")
            if len(next_plan["items"]) > 5:
                lines.append(f"  … 共 {len(next_plan['items'])} 条")
        else:
            lines.append(f"🗓️ 明日（{tomorrow}）计划尚未生成。")
        yield event.plain_result("\n".join(lines))

    @filter.command("重建今日计划")
    async def rebuild_plan_command(self, event: AstrMessageEvent):
        """立即在后台重新生成明天的计划；若今天的窗口未结束且今天计划缺失，也补一份今天的"""
        yield event.plain_result("⏳ 正在后台重建计划…")
        asyncio.create_task(self._rebuild_plans())

    @filter.command("记忆")
    async def memory_command(self, event: AstrMessageEvent):
        """查看当前记忆库内容。

        重点是展示「真正注入给 AI 的那份档案」（merged，已剔除 AI 自述 + 去重），
        分会话明细放后面折叠显示，方便核对来源。
        """
        store = await self._load_memory_store()
        items = [it for it in store.get("items", []) if it]
        lines: list[str] = ["🧠 记忆库"]
        if not items:
            lines.append("（还没有记忆。到 memory_cron 时间会自动总结，或现在用 /刷新记忆 立刻生成）")
        else:
            lines.append(f"最后更新：{store.get('updated', '未知')}　会话数：{len(items)}")
            archive = self._memory_text(store)
            lines.append("")
            lines.append("—— 注入给 AI 的档案 ——")
            lines.append(archive or "（空：记忆被过滤后没有剩下可用内容）")
            if len(items) > 1:
                lines.append("")
                lines.append("—— 分会话明细 ——")
                for it in items:
                    body = str(it.get("summary", "")).strip()
                    lines.append(f"【{it.get('umo', '')}】{it.get('updated', '')}")
                    lines.append(body[:400] + ("…" if len(body) > 400 else ""))
        if not self._cfg_bool("memory_enabled", True):
            lines.append("")
            lines.append("⚠️ 记忆功能当前已关闭（memory_enabled = false）")
        yield event.plain_result("\n".join(lines))

    @filter.command("刷新记忆")
    async def refresh_memory_command(self, event: AstrMessageEvent):
        """立刻让 AI 总结一次对话，写入记忆库"""
        if not self._cfg_bool("memory_enabled", True):
            yield event.plain_result("⚠️ 记忆功能已关闭，请先在配置里打开 memory_enabled。")
            return
        yield event.plain_result("⏳ 正在总结记忆…")
        asyncio.create_task(self.refresh_memory())

    @filter.command("记忆重置")
    async def reset_memory_command(self, event: AstrMessageEvent):
        """清空记忆库并按当前规则重建（用来洗掉历史遗留的错误条目）。"""
        if not self._cfg_bool("memory_enabled", True):
            yield event.plain_result("⚠️ 记忆功能已关闭，请先在配置里打开 memory_enabled。")
            return
        # 必须走 KV 接口写空，不能直接改数据库：AstrBot 的 preferences 在内存里有副本，
        # 直接改 sqlite 会被下一次写回覆盖掉。
        await self.put_kv_data(MEMORY_KEY, {"updated": "", "items": [], "merged": ""})
        yield event.plain_result(
            "🧹 记忆库已清空，正在按「近期对话 + 用户笔记」重建…（完成后用 /记忆 查看）"
        )
        asyncio.create_task(self.refresh_memory())

    async def _rebuild_plans(self) -> None:
        """强制重建：明天的计划无条件重新生成；今天的窗口未结束且今天计划缺失时补今天的。"""
        if not self._cfg_bool("enabled", True):
            return
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        plan = await self._load_schedule("daily_plan")
        window_end = self._hhmm_to_min(str(self.config.get("window_end") or "23:00"))
        now_min = datetime.now().hour * 60 + datetime.now().minute
        # 今天的窗口未结束且今天计划缺失 -> 补一份今天的
        if (plan.get("date") != today or not plan.get("items")) and now_min < window_end:
            async with self._gen_lock:
                await self._generate_and_store(target_date=today)
        # 无条件重新生成明天的计划（force 忽略节流）
        async with self._gen_lock:
            await self._generate_and_store(target_date=tomorrow)

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