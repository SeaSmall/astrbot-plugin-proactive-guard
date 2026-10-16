# Changelog

## v1.0.1（2026-08-26）

### 🐛 修复：清理「明天」的计划后重载会重新生成「后天」的计划

- **根因**：旧实现把计划日期设为「生成当天」（`date = now`），补种窗口限定在
  06:00-11:00。用户清理掉已经设置好的「明天」的定时任务后重载插件，补种逻辑
  重新生成的是「后天」而非「明天」，行为不符合预期。
- **修复方案**：KV 存储改为**双槽位**：
  - `daily_plan`：`date = 今天`，到点派发用；
  - `daily_plan_next`：`date = 明天`，提前一天预生成。
- **新行为**：
  - 计划预生成「明天」（任意时刻可补种，20 分钟节流；`force` 忽略节流）；
  - 跨天自动把 `daily_plan_next` 提升为 `daily_plan`，并预生成新的「明天」；
  - 派发只从 `daily_plan`（`date == 今天`）取；过期计划静默丢弃；
  - 清理明天的计划后重载 → 立即重新生成「明天」，**永不生成后天**；
  - 自动迁移旧键 `daily_schedule`（v1.0.0）到新双槽位。
- **指令更新**：`/今日计划` 同时显示今日计划与明日预生成摘要；
  `/重建今日计划` 强制重新生成明天的计划（今天窗口未结束且今天计划缺失时也补一份今天的）。

### 🧪 测试

- 新增假时钟冒烟测试（`test_guard_core.py` 扩展）：覆盖「清空明天 → 重载 → 重新生成明天」
  核心回归、00:00 跨天提升、派发/删除/补发/丢弃、20 分钟节流、旧键迁移。

## v1.0.2（2026-08-27）

### 🐛 修复：定时任务未显式指定时区（默认 UTC 导致生成时间错位）

- **根因**：`_setup_scheduler` 注册 cron 时未指定 timezone，在系统时区非 Asia/Shanghai
  的部署（如 UTC）上，`0 6 * * *` 会按 UTC 6 点触发，与预期北京时间 06:00 错位。
- **修复**：新增 `timezone` 配置项（默认 `Asia/Shanghai`），cron_manager 与
  APScheduler 兜底两条路径都显式传时区（`CronTrigger.from_crontab(expr, timezone=...)`）；
  旧版 cron_manager 不支持 timezone 参数时自动回退。

## v1.0.3（2026-08-27）

### ✨ 新增：主动消息写入会话历史（修复「被动输出与主动输出」记忆断层）

- **问题**：`_send_silent` 只通过 `context.send_message` 发送主动人格消息，没有把消息
  写入该会话的 LLM 对话历史，模型后续回复时不知道机器人主动说过什么，上下文断裂。
- **修复**：发送成功后通过 `context.conversation_manager`（与 AstrBot 官方
  `persist_agent_history` 同一机制）在会话当前对话的 history 末尾追加一条
  assistant 消息；会话尚无对话 / 无 conversation_manager（旧版）时静默跳过。
- 新配置项 `record_to_history`（默认 `true`）：关闭则不写入会话历史。

## v1.0.4（2026-08-28）

### 🐛 修复：门禁误拦 daily_digest 日报的定时推送

- **问题**：`allow_senders` 默认为空时，门禁只放行本插件；daily_digest 每天 08:00 的
  定时推送在用户未活跃时被判定为「非白名单主动消息」而丢弃（用户早上收不到日报）。
- **修复**：`allow_senders` 为空（默认）时，白名单**默认包含 `daily_digest`**（日报），
  用户自定义非空白名单则完全覆盖默认值；配置面板默认值同步为 `daily_digest`。
- 如需彻底拦截日报，把 `allow_senders` 改成其它非空值即可。

## v1.1.0（2026-10-16）

### ✨ 新增：记忆库（AI 总结会话 → 存插件存储 → 生成时读取）

- **问题**：主动消息只看「人格设定 + 当前时间」，每天从零开始寒暄，跟用户聊过什么
  （在准备考试、最近在加班、养了只猫）一概不知，聊起来像陌生人。
- **新增记忆库**：`memory_cron`（默认每天 03:00）触发，把每个目标会话最近
  `memory_turns` 条对话交给 AI 总结成**要点**（称呼、身份、正在做的事、情绪、
  偏好、约定），存进插件 KV（key = `memory_store`）。
- **滚动更新**：每次把「旧摘要 + 新对话」一起交给 AI **重写**，而不是追加，
  所以记忆长度不随时间无限膨胀，始终受 `memory_max_chars` 约束。
- **生成时读取**：写消息前取记忆，注入规划代理与写手代理的提示词，让主动消息
  能接着聊过的话题往下说。
- **三级降级**：记忆库有内容 → 直接用；为空 → **当场就地总结建库**；总结失败 →
  按 `memory_raw_fallback` 决定是否退回「未经总结的原始对话」兜底。
- 新增指令 `/记忆`（查看记忆库）与 `/刷新记忆`（立即重新总结）。
- 新增配置项：`memory_enabled` / `memory_cron` / `memory_turns` /
  `memory_max_chars` / `memory_raw_fallback`。

### ✨ 新增：子代理流水线生成（规划 → 并行写手 → 审校）

- **问题**：一条提示词让 LLM 一次性产出全天 5~10 条消息，容易多处敷衍、内容雷同、
  场景错位，且任何一条写坏都没有补救。
- **新增三段式流水线**（`subagent_enabled`，默认开启）：
  1. **规划代理**：只看日期 + 人格 + 记忆 + 时间点列表，为每个时间点出一句**内容要点**；
  2. **写手代理**：**每个时间点一次独立调用**，只拿到自己那条的要点与记忆，写一条
     20~60 字的消息；按 `subagent_concurrency`（默认 3）限流并行，
     `subagent_parallel=false` 则串行；
  3. **审校代理**：统一语气、去重、修正场景错位。**只能改文案**——返回条数与时间点
     对不上时直接丢弃审校结果、回退写手原稿，保证不会少发或错时。
- **失败隔离**：规划失败 → 回退单次调用（v1.0.x 行为）；部分写手失败 → 保留成功的
  那几条；审校失败 → 用写手原稿。任一环节出问题都不会让整天丢失。
- 新增配置项：`subagent_enabled` / `subagent_parallel` / `subagent_concurrency` /
  `subagent_review`；`message_prompt` 仅在关闭子代理时使用。

### 🐛 修复：三段子代理提示词拼出「你是你是一个…」

- **根因**：`PLANNER_PROMPT` / `WRITER_PROMPT` / `REVIEWER_PROMPT` 写成
  `你是{persona}`，而 `{persona}` 展开后本身就是一句完整自我描述
  （「你是一个温柔…的 AI 陪伴者」），拼接后变成「你是你是一个温柔…」。
- **修复**：三处模板改为以 `{persona}` 开头，并在常量旁加注释说明**不要**写成
  「你是{persona}」。

### 🐛 修复：LLM 返回空内容时解析函数抛 `TypeError` 崩溃

- **根因**：`_parse_items` / `_parse_points` 直接对 LLM 返回值做 `re.search`，
  模型返回 `None` 或空串时抛 `TypeError: expected string or bytes-like object,
  got 'NoneType'`；`_review_drafts` 未做空值防护，会把 `None` 灌进解析器。
- **修复**：两个解析函数增加 `isinstance(text, str)` 与空串防护，直接返回空结果；
  `_review_drafts` 增加「空草稿 / 空输出」分支，记录 warning 并回退写手原稿；
  `_clean_message` 同样改为显式类型检查。

### 🧪 测试

- 新增完整桩测试套件 `pgtest_full.py`（19 组、**107 项断言全部通过**），覆盖：
  提示词模板不出现「你是你是一个」、`_history_lines` 多模态解析、`refresh_memory`
  写 KV 与滚动合并、`_memory_text` 单/多会话渲染、`_collect_memory` 三级降级（含
  总开关关闭）、`_truncate_memory` 长度约束与 200 字下限、`_clean_message` 8 组清洗、
  `_parse_points` JSON/行解析/补齐/字段兼容、子代理全流程、审校条数相符/不符/异常
  三种走向、写手部分失败、规划失败、全失败不写 KV、单次路径带记忆、写历史→总结闭环、
  `_send_silent` 自动写历史、配置 schema 26 键完整性与描述/类型校验。

## v1.1.1（2026-10-16）

### 🐛 修复

- **「回退单次生成」此前是假的**：`_generate_with_subagents()` 在规划失败或未产出要点时，
  只打一条 `…回退单次生成` 的 warning 就 `return []`；而调用方拿到空列表后直接
  `if not items:` 报「计划生成失败」并 return —— 回退**从未真正发生**，
  README「每日计划（生成层）」里写的「规划失败 → 回退单次调用」也因此名不副实。
  现在 `_generate_and_store()` 在 `if not items:` 时会**真的**再走一次单次调用
  （子代理开启时先 warning 说明回退原因），并带 `try/except`：兜底也失败（例如 LLM 全挂）
  时只记 warning、不把异常抛给调度器 —— 保持「`_generate_and_store()` 不抛异常」这一原有约定，
  `_ensure_schedule()` 的 20 分钟重试节流与 `_on_gen_time()` 的错误处理因此不受影响。
- `_parse_items()` / `_parse_points()` 对 `None` 输入会抛
  `TypeError: expected string or bytes-like object, got 'NoneType'`；
  `_review_drafts()` 未防空（审校代理返回空串时后续按行处理会崩）；`_clean_message()` 未做类型检查。
  均已补显式类型判断。
- 三段子代理提示词（`PLANNER_PROMPT` / `WRITER_PROMPT` / `REVIEWER_PROMPT`）原写作 `你是{persona}`，
  而 `persona` 由 `_persona_text()` 拼出来本身就是「你是一个温柔、自然、不油腻的朋友型 AI 陪伴者…」，
  渲染结果是 **「你是你是一个温柔…」**。三处改为 `{persona}`。
  （`DEFAULT_PROMPT` 里的 `你是{persona}` 保持不变 —— 那条模板确实需要「你是」这个词。）

### 🧪 测试

- 新增回归脚本 `pgtest_full.py`：19 组、**107 项断言全部通过**。覆盖子代理全流程
  （审校条数相符 / 不符 / 异常三种走向、写手部分失败、规划失败、全失败不写 KV、
  单次路径带记忆、写历史→总结闭环、`_send_silent` 自动写历史、配置 schema 26 键完整性与描述/类型校验）。

### 🔧 其它

- metadata 版本 → 1.1.1。
- 整合包 `astrbot-plugin-elaina-suite` v1.1.1 同步了本版的全部修复与这两项能力。