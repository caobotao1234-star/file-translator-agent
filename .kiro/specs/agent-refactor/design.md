# 翻译 Agent 重构设计文档
# 从工作流（Pipeline）到真正的 Agent（Agentic Loop）

## 1. 现状问题分析

### 当前架构
```
用户点击"开始翻译"
  -> TranslatorAgent 判断文件类型
    -> Word/PPT/普通PDF: TranslatePipeline（硬编码工作流）
      -> parser 提取文本 -> 按页分组 -> 调翻译模型 -> writer 写回
    -> 扫描件PDF: ScanAgent（半 Agent）
      -> 渲染图片 -> 预执行 OCR/CV -> Brain ReAct 循环 -> 审查 -> 写入
```

### 核心问题
1. TranslatePipeline 是纯工作流 -- 每一步都是硬编码的，模型只是被当作翻译 API
2. ScanAgent 是半 Agent -- 有 ReAct 循环，但我们预执行了 OCR/CV，规定了工具选择规则
3. 两套代码路径 -- Word/PPT/PDF 走 Pipeline，扫描件走 ScanAgent，逻辑割裂
4. 不可交互 -- 用户点开始后只能等，发现问题要重来
5. 上下文是后加的补丁 -- 跨页缓存、内容摘要都是打补丁，不是架构级设计

### 与 Claude Code 的差距
| 维度 | Claude Code | 我们的现状 |
|------|------------|-----------|
| 核心循环 | 单线程 while(tool_call) | Pipeline 硬编码步骤 |
| 决策者 | 模型自己决定 | 代码规定流程 |
| 工具 | 模型的手脚 | 流水线的工位 |
| 交互 | 随时打断纠正 | 点开始后等结果 |
| 上下文 | 扁平消息历史 | 分散在各处的缓存 |

## 2. 新架构设计

### 2.1 核心理念

借鉴 Claude Code 的设计哲学：

> 一个简单的单线程循环 + 丰富的工具集 = 可控的自主性。
> 力量来自极致的简单。

原则：
- 一个 Agent Loop，统一处理所有文件类型（Word/PPT/PDF/扫描件）
- 模型自己决定调什么工具、什么顺序、什么时候停
- System Prompt 给目标和约束，不给步骤
- 用户可以随时介入（交互式）
- 上下文是架构级的，不是补丁

### 2.2 整体架构

```
用户（GUI 聊天面板）
  |
  v
TranslationAgent（唯一入口）
  |
  v
Agent Loop: while(tool_call) -> execute -> feed back -> repeat
  |
  +-- 工具集（模型自主选择）:
  |     parse_document     -- 解析文档（自动识别类型）
  |     get_page_content   -- 获取指定页的文本内容
  |     get_page_image     -- 获取指定页的图片（扫描件/PPT）
  |     translate_page     -- 翻译一页的所有文本
  |     ocr_page           -- OCR 识别（扫描件）
  |     detect_layout      -- CV 布局检测（扫描件）
  |     generate_image     -- 图片生成（保留背景）
  |     overlay_text       -- 文字覆盖（保留背景）
  |     write_document     -- 写入翻译结果到输出文件
  |     read_memory        -- 读取跨页记忆（术语表+内容摘要）
  |     update_memory      -- 更新跨页记忆
  |     ask_user           -- 向用户提问（交互式）
  |     report_progress    -- 报告进度给 GUI
  |
  +-- 消息队列（h2A 机制）:
        用户随时可以注入新消息，Agent 下一轮循环时读取
```

### 2.3 Agent Loop 伪代码

```python
class TranslationAgent:
    def run(self, user_message: str):
        """唯一入口。用户说什么，Agent 就去做。"""
        self.messages.append({"role": "user", "content": user_message})

        for turn in range(MAX_TURNS):
            # 1. 检查用户是否有新消息（交互式）
            while self.message_queue.has_pending():
                new_msg = self.message_queue.pop()
                self.messages.append({"role": "user", "content": new_msg})

            # 2. 调用模型
            response = self.llm.chat(
                messages=self.messages,
                tools=self.tool_schemas,
            )
            self.messages.append(response.assistant_message)

            # 3. 如果模型要调工具 -> 执行 -> 反馈 -> 继续循环
            if response.tool_calls:
                for tc in response.tool_calls:
                    result = self.execute_tool(tc.name, tc.params)
                    self.messages.append(tool_result(tc.id, result))
                continue

            # 4. 如果模型返回纯文本 -> 任务完成（或等待用户下一条消息）
            if response.text:
                self.emit_to_gui(response.text)
                break
```

关键点：
- 没有 if/else 判断文件类型，模型自己看到文件后决定怎么处理
- 没有硬编码的步骤顺序，模型自己规划
- 用户消息通过队列注入，不打断循环
- 工具执行结果直接反馈到消息历史，模型看到后决定下一步

### 2.4 System Prompt 设计

不再给步骤，只给目标和约束：

```
你是专业文档翻译 Agent。用户会给你文档和翻译需求，你自主完成翻译。

## 你的目标（按优先级）
1. 排版必须与原文一致
2. 翻译准确、地道、符合上下文
3. 高效完成，不浪费资源

## 你的能力
你有一系列工具可以使用。你自己决定用什么工具、什么顺序。
- 先用 parse_document 了解文档结构
- 用 get_page_content / get_page_image 查看具体页面
- 用 translate_page 翻译（你也可以直接在回复中翻译）
- 用 read_memory / update_memory 维护跨页一致性
- 用 write_document 输出结果
- 不确定的地方用 ask_user 问用户

## 约束
- 翻译必须地道自然，不要 Chinglish
- 专有名词跨页保持一致（用 memory 工具）
- 每完成一页，用 report_progress 通知用户
- 如果遇到不确定的翻译（人名、专业术语），主动问用户
```

注意：没有"第一步做什么、第二步做什么"。模型自己规划。

### 2.5 工具设计

每个工具是独立的、无状态的函数。模型通过 JSON schema 了解工具能力。

#### 核心工具（所有文件类型通用）

| 工具名 | 功能 | 输入 | 输出 |
|--------|------|------|------|
| parse_document | 解析文档结构 | filepath | 页数、类型、每页段落数概览 |
| get_page_content | 获取一页的文本 | page_index | 该页所有文本段落（含格式信息） |
| get_page_image | 获取一页的图片 | page_index | base64 图片（扫描件/PPT 渲染） |
| translate_page | 翻译一页文本 | page_index, texts[], target_lang | translations[] |
| write_document | 写入翻译结果 | translations{}, output_path | 输出文件路径 |
| read_memory | 读取跨页记忆 | - | 术语表 + 内容摘要 + 翻译缓存 |
| update_memory | 更新跨页记忆 | terms[], summary | 确认 |
| ask_user | 向用户提问 | question | 用户回答 |
| report_progress | 报告进度 | page, total, message | 确认 |

#### 扫描件专用工具（模型看到是扫描件后自己决定用）

| 工具名 | 功能 |
|--------|------|
| ocr_page | OCR 文字识别（返回文字+坐标） |
| detect_layout | CV 布局检测（表格线、图片区域） |
| generate_image | 图片生成 LLM（保留背景替换文字） |
| overlay_text | Pillow 文字覆盖（纯色背景） |
| crop_region | 裁剪图片区域（签名、盖章） |

#### 关键设计决策

1. translate_page vs 模型直接翻译：
   - 工具内部调用便宜的翻译模型（doubao），适合大量纯文本
   - 模型也可以选择自己直接翻译（看到图片时更准确）
   - 模型自己决定用哪种方式，我们不规定

2. ask_user 工具：
   - 这是交互式的关键。模型调用 ask_user 后，Agent Loop 暂停
   - GUI 显示问题，等待用户回答
   - 用户回答后注入消息队列，Agent Loop 继续
   - 模型可以决定什么时候问、问什么

3. memory 工具：
   - 替代之前的 cross_page_context 补丁
   - 模型自己决定什么时候读、什么时候写
   - 内容包括：术语表、内容摘要、翻译缓存、用户偏好

### 2.6 交互式设计

#### GUI 改造

现有 GUI（translator_gui.py）改造为聊天式界面：

```
+------------------------------------------+
| 翻译 Agent                          [设置] |
+------------------------------------------+
| [拖入文件区域]                              |
|                                          |
| 用户: 翻译这个PPT，目标英文，公司名保留中文    |
| Agent: 收到。解析中... 23页，262段。         |
|        先翻第1页给你看看。                   |
| Agent: 第1页: 营销赋能 -> Marketing          |
|        Empowerment... (完整预览)            |
| 用户: 营销赋能翻译成 Empower Marketing 更好   |
| Agent: 好的，已更新。继续翻译剩余22页...      |
| Agent: [进度条 5/23]                        |
| Agent: 第8页有个表格，两列还是三列？          |
| 用户: 三列                                  |
| Agent: 收到。                               |
| Agent: [进度条 23/23] 全部完成！             |
|        输出: output/xxx_translated.pptx      |
|                                          |
| [输入框: 输入消息...]              [发送]    |
+------------------------------------------+
```

#### 消息队列机制（h2A）

```python
class MessageQueue:
    """用户消息异步注入队列"""
    def __init__(self):
        self._queue = queue.Queue()

    def inject(self, message: str):
        """GUI 线程调用：用户发送消息"""
        self._queue.put(message)

    def has_pending(self) -> bool:
        return not self._queue.empty()

    def pop(self) -> str:
        return self._queue.get_nowait()
```

#### 交互流程

1. 用户拖入文件 + 输入需求 -> Agent 开始工作
2. Agent 每完成一步都通过 report_progress 通知 GUI
3. Agent 遇到不确定的地方调用 ask_user -> GUI 显示问题 -> 等待用户回答
4. 用户随时可以在输入框打字 -> 消息注入队列 -> Agent 下一轮循环时读取
5. 用户可以说"停"-> Agent 优雅停止
6. 用户可以说"第3页重新翻" -> Agent 只重做第3页

#### ask_user 的实现

```python
def execute_ask_user(params):
    """Agent 调用 ask_user 工具时的处理"""
    question = params["question"]
    # 1. 通过 GUI 显示问题
    gui.display_agent_question(question)
    # 2. 阻塞等待用户回答（带超时）
    answer = gui.wait_for_user_answer(timeout=300)
    if answer is None:
        return "用户未回答，请自行决定并继续"
    return answer
```

### 2.7 上下文管理

#### 扁平消息历史

所有信息都在一个 messages 列表里，不再分散：

```python
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": "翻译这个PPT，目标英文"},
    {"role": "assistant", "content": None, "tool_calls": [parse_document(...)]},
    {"role": "tool", "content": "23页，262段，PPT类型"},
    {"role": "assistant", "content": None, "tool_calls": [get_page_content(0)]},
    {"role": "tool", "content": "第1页: 营销赋能, 数字出海..."},
    {"role": "assistant", "content": None, "tool_calls": [translate_page(0, ...)]},
    {"role": "tool", "content": "translations: {营销赋能: Empower Marketing, ...}"},
    {"role": "user", "content": "公司名不要翻译"},  # <- 用户中途介入
    {"role": "assistant", "content": "好的，已记录。后续保留公司名原文。"},
    ...
]
```

模型天然能看到所有历史，不需要我们手动注入上下文。

#### Context Window 管理

参考 Claude Code 的 Compressor 机制：
- 当消息历史接近 token 上限（~90%）时，自动压缩
- 压缩策略：保留 system prompt + 最近 N 轮 + 摘要
- 关键信息（术语表、用户偏好）写入 memory 工具，不怕被压缩丢失

#### 跨文件记忆

如果用户连续翻译多个文件（同一个项目的多个文档），
memory 工具的内容跨文件保留，确保术语一致。

### 2.8 模型选择策略

不再硬编码"翻译用 doubao、规划用 gemini"。

Agent 使用一个主模型（用户在 GUI 中选择），这个模型负责：
- 理解文档
- 规划翻译策略
- 直接翻译（如果它能看到图片）
- 调用工具（包括调用便宜模型翻译大量文本）

translate_page 工具内部可以用便宜模型（doubao），但这是工具的实现细节，
Agent 不需要知道。Agent 只知道"调 translate_page 可以翻译一页文本"。

```
Agent（主模型: gemini/claude/doubao）
  |
  +-- translate_page 工具（内部用 doubao，便宜）
  +-- generate_image 工具（内部用 gemini-image，专业）
  +-- 其他工具（纯代码，不用模型）
```

## 3. 与现有代码的关系

### 3.1 保留的部分（工具内部实现）

这些代码质量好，只是从"流水线工位"变成"Agent 的工具"：

| 现有代码 | 新角色 |
|---------|--------|
| pptx_parser.py | parse_document 工具内部（PPT 分支） |
| docx_parser.py | parse_document 工具内部（Word 分支） |
| pdf_parser.py | parse_document 工具内部（PDF 分支） |
| scan_parser.py | parse_document 工具内部（扫描件检测） |
| pptx_writer.py | write_document 工具内部（PPT 分支） |
| docx_writer.py | write_document 工具内部（Word 分支） |
| pdf_writer.py | write_document 工具内部（PDF 分支） |
| scan_writer.py | write_document 工具内部（扫描件 PDF 分支） |
| translate_pipeline.py | translate_page 工具内部（批量翻译逻辑） |
| format_engine.py | write_document 工具内部（格式规则） |
| com_engine.py | parse_document 工具内部（COM 增强） |
| scan_tools.py 中的 OCRTool | ocr_page 工具 |
| scan_tools.py 中的 CVTool | detect_layout 工具 |
| scan_tools.py 中的 ImageGenTool | generate_image 工具 |
| scan_tools.py 中的 OverlayTextTool | overlay_text 工具 |
| external_llm_engine.py | Agent Loop 的 LLM 调用层 |
| llm_router.py | 模型注册和路由（保留） |

### 3.2 删除/重构的部分

| 现有代码 | 处理方式 |
|---------|---------|
| translator_agent.py | 删除，被 TranslationAgent 替代 |
| scan_agent.py | 删除，Agent Loop 统一处理 |
| translate_pipeline.py 的 translate_document | 删除，Agent 自己决定翻译策略 |
| layout_agent.py | 合并到工具中 |
| orchestrator.py | 删除，Agent Loop 替代 |

### 3.3 新增的部分

| 新文件 | 功能 |
|--------|------|
| core/agent_loop.py | 核心 Agent Loop（while tool_call） |
| core/message_queue.py | 用户消息异步注入队列 |
| core/context_manager.py | 上下文压缩和管理 |
| tools/doc_tools.py | 文档解析/写入工具 |
| tools/translate_tools.py | 翻译工具 |
| tools/vision_tools.py | OCR/CV/图片生成工具 |
| tools/interaction_tools.py | ask_user/report_progress |
| tools/memory_tools.py | 跨页/跨文件记忆 |
| translator_gui_v2.py | 聊天式 GUI |

## 4. 交互式翻译的典型场景

### 场景 1：普通 PPT 翻译
```
用户: 翻译这个PPT，目标英文
Agent: [调用 parse_document] 检测到 PPT，23页，262段。开始翻译。
Agent: [调用 get_page_content(0)] 第1页：营销赋能·智汇全球...
Agent: [调用 translate_page(0)] 翻译完成。
Agent: [调用 report_progress] 第1页完成 (1/23)
Agent: [调用 update_memory] 记录术语：鑫火智汇=Xinhuo Zhihui
... (Agent 自主逐页翻译)
Agent: [调用 write_document] 输出完成。
Agent: 全部翻译完成！输出文件: output/xxx_translated.pptx
```

### 场景 2：扫描件 PDF（Agent 自己发现是扫描件）
```
用户: 翻译这个PDF
Agent: [调用 parse_document] 检测到 PDF，22页。文本很少，可能是扫描件。
Agent: [调用 get_page_image(0)] 看到第1页图片...确认是扫描件。
Agent: [调用 ocr_page(0)] OCR 识别到 9 个文字块。
Agent: [调用 detect_layout(0)] 检测到表格和图片区域。
Agent: 这页有复杂背景，我用图片生成来处理。
Agent: [调用 generate_image(0, ...)] 生成完成。
Agent: [调用 report_progress] 第1页完成 (1/22)
...
```

### 场景 3：用户中途纠正
```
用户: 翻译这个Word文档，目标日文
Agent: [开始翻译...]
Agent: 第3页完成 (3/15)
用户: 等一下，"技术总监"翻译成什么了？
Agent: [调用 read_memory] 查看... "技术总监" 翻译为 "技術ディレクター"
用户: 改成 CTO 就行
Agent: 好的，已更新术语表。技术总监=CTO。第3页已修正，继续翻译...
```

### 场景 4：Agent 主动提问
```
Agent: [翻译到第5页]
Agent: [调用 ask_user] 第5页有个人名"王建国"，需要音译还是保留中文？
用户: 音译，Wang Jianguo
Agent: 收到，已记录。继续...
```

## 5. 实施计划

### Phase 1: 核心 Agent Loop（1-2天）
- core/agent_loop.py -- while(tool_call) 循环
- core/message_queue.py -- 消息队列
- 基础工具：parse_document, translate_page, write_document
- 能跑通一个 PPT 翻译

### Phase 2: 完整工具集（1-2天）
- 扫描件工具：ocr_page, detect_layout, generate_image, overlay_text
- 记忆工具：read_memory, update_memory
- 交互工具：ask_user, report_progress
- 能跑通所有文件类型

### Phase 3: 交互式 GUI（1-2天）
- 聊天式界面
- 消息队列集成
- 进度显示
- 用户中途介入

### Phase 4: 上下文管理（1天）
- Context Window 压缩
- 跨文件记忆持久化
- Token 统计

### 原则
- 每个 Phase 完成后都能跑，不是最后才能跑
- 现有的 parser/writer 代码直接复用，只是包装成工具
- 翻译质量不能下降（千万不要影响质量）
- 新旧 GUI 并存一段时间，确认新版稳定后再删旧版

## 6. 风险和应对

| 风险 | 应对 |
|------|------|
| 模型不按预期调工具 | System Prompt 迭代 + few-shot 示例 |
| 模型乱翻译（没有 Pipeline 的约束） | translate_page 工具内部仍用专业翻译模型 |
| Token 消耗增加（消息历史更长） | Context 压缩 + memory 工具外置关键信息 |
| 交互式增加复杂度 | ask_user 有超时兜底，用户不回答就自行决定 |
| Gemini API 配额限制 | 主模型可以用 doubao（便宜），只在需要看图时用 gemini |
| 重构期间现有功能不可用 | 新旧并存，feature branch 开发 |

## 7. Phase 1 测试发现的问题

### 7.1 Turn 过多导致速度慢（优先级：高）

Phase 1 实测 23 页 PPT，Agent 每页需要 4-5 个 turn：
```
get_page_content(N) → translate_page(N) → update_memory → report_progress
```
每个 turn 都要调一次 Brain LLM 来"思考下一步做什么"，23 页 = 100+ 次 Brain 调用。
旧架构只需要 23 次翻译调用（无 Brain 思考开销）。

#### 优化方案

1. 批量操作工具：让 get_page_content 支持一次拿多页，translate_page 支持一次翻多页
2. System Prompt 引导：告诉 Agent "你可以一次处理多页，不需要逐页调工具"
3. 合并工具调用：LLM 支持一次返回多个 tool_call（parallel tool calls），
   Agent Loop 已经支持，但需要 prompt 引导模型这样做
4. 减少不必要的 turn：update_memory 和 report_progress 可以合并到 translate 后的同一轮
5. 考虑"自动驾驶模式"：对于简单文档（纯文本 PPT/Word），Agent 可以一次性
   拿到所有页面内容 → 一次性翻译 → 一次性写入，只需 3-4 个 turn 完成整个文档

#### 目标
23 页 PPT 从 100+ turns 降到 10-15 turns，速度接近旧架构。

### 7.2 Gemini 代理超时（优先级：低）

4月1日测试时 Gemini 通过代理连接超时，doubao 正常。
这是网络问题不是代码问题，但需要在 agent_main.py 中加超时处理，
避免无限等待。
