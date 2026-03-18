# 需求文档：扫描件 Agent 架构

## 简介

将当前扫描件 PDF 翻译的固定流水线（v7.1: OpenCV → RapidOCR → Vision LLM → Word）重构为真正的 Agent 架构。Agent 大脑（Gemini/Claude/GPT 等高能力模型）看到文档图片后自主决定处理策略，按需调用工具（OCR、CV 检测、Word 生成等），并自我审查输出质量。目标：任意文档类型（出生证、毕业证、表格文档等）无需改代码即可处理，端到端完成"扫描件 PDF 输入 → 翻译后 Word 输出"。

## 术语表

- **Scan_Agent**: 扫描件翻译 Agent，负责接收扫描件 PDF 并自主决策处理策略，调用工具完成端到端翻译
- **Agent_Brain**: Agent 的大脑模型，使用高能力外部模型（Gemini 2.5 Pro / Claude Sonnet 4 / GPT-4o）进行视觉理解、策略决策和工具调用
- **Tool_Registry**: 工具注册表，管理 Agent 可调用的所有工具（OCR、CV 检测、Word 生成、翻译等）
- **OCR_Tool**: 基于 RapidOCR 的文字识别工具，在子进程中运行以避免 DLL 冲突
- **CV_Tool**: 基于 OpenCV 的计算机视觉工具，检测表格线、图片区域等视觉元素
- **Translation_Tool**: 基于 doubao 模型的翻译工具，复用现有 TranslatePipeline
- **Word_Writer_Tool**: Word 文档生成工具，基于现有 scan_writer 生成翻译后的 .docx 文件
- **External_LLM_Engine**: 支持外部模型 API（非火山引擎）的 LLM 引擎，兼容 OpenAI 协议
- **Strategy_Plan**: Agent 大脑分析文档后生成的处理策略，描述文档类型、布局特征和处理步骤
- **Self_Review**: Agent 对自身输出的质量审查环节，检查翻译完整性和格式正确性
- **Multi_Model_Router**: 扩展后的模型路由器，同时管理火山引擎模型和外部模型

## 需求

### 需求 1：外部模型 API 支持

**用户故事：** 作为开发者，我希望系统支持调用外部 LLM API（Gemini / Claude / GPT），以便使用高能力模型作为 Agent 大脑。

#### 验收标准

1. THE External_LLM_Engine SHALL 支持通过 OpenAI 兼容协议调用外部模型 API（包括 Gemini、Claude、GPT 系列）
2. THE External_LLM_Engine SHALL 支持流式输出（streaming）和工具调用（tool_calls），与现有 ArkLLMEngine 保持相同的输出格式
3. THE External_LLM_Engine SHALL 支持发送图片内容（base64 编码），用于视觉理解任务
4. THE External_LLM_Engine SHALL 复用现有的重试机制（指数退避），对网络错误和限流错误进行自动重试
5. THE Multi_Model_Router SHALL 同时管理火山引擎模型和外部模型，通过统一的别名机制获取引擎实例
6. WHEN 外部模型 API 密钥未配置时, THE External_LLM_Engine SHALL 返回明确的配置缺失错误信息
7. THE External_LLM_Engine SHALL 从 .env 文件读取 API 密钥和模型配置（如 GEMINI_API_KEY、CLAUDE_API_KEY）

### 需求 2：工具注册与管理

**用户故事：** 作为开发者，我希望将 OCR、CV 检测、翻译、Word 生成等能力封装为可调用的工具，以便 Agent 大脑按需调用。

#### 验收标准

1. THE Tool_Registry SHALL 注册以下工具供 Agent_Brain 调用：OCR_Tool、CV_Tool、Translation_Tool、Word_Writer_Tool
2. THE OCR_Tool SHALL 在子进程中执行 RapidOCR，返回文字内容和位置坐标，避免 PyQt6 与 onnxruntime 的 DLL 冲突
3. THE CV_Tool SHALL 使用 OpenCV 检测页面中的表格线（水平线和垂直线）和图片区域，返回结构化的位置信息
4. THE Translation_Tool SHALL 复用现有 TranslatePipeline 和 doubao 模型进行文本翻译，保持翻译质量不变
5. THE Word_Writer_Tool SHALL 基于 Agent 提供的结构化数据生成 .docx 文件，支持表格、段落、图片等元素
6. WHEN Agent_Brain 调用工具时, THE Tool_Registry SHALL 验证参数合法性并返回结构化的执行结果
7. IF 工具执行失败, THEN THE Tool_Registry SHALL 返回包含错误类型和错误描述的结构化错误信息，供 Agent_Brain 决定后续处理

### 需求 3：Agent 大脑策略决策

**用户故事：** 作为用户，我希望 Agent 看到文档图片后自主判断文档类型并决定处理策略，无需人工干预或修改代码。

#### 验收标准

1. WHEN 接收到扫描件 PDF 时, THE Scan_Agent SHALL 将每页渲染为图片并发送给 Agent_Brain 进行视觉分析
2. WHEN Agent_Brain 看到页面图片后, THE Agent_Brain SHALL 生成 Strategy_Plan，包含文档类型识别（如证件、表格文档、纯文本等）、布局特征描述和处理步骤序列
3. THE Agent_Brain SHALL 根据 Strategy_Plan 自主决定调用哪些工具以及调用顺序，而非遵循固定流水线
4. WHEN 处理包含表格的文档时, THE Agent_Brain SHALL 调用 CV_Tool 检测表格线，再调用 OCR_Tool 提取文字，最后综合视觉信息确定表格结构
5. WHEN 处理不包含表格的文档（如证件、毕业证）时, THE Agent_Brain SHALL 跳过 CV_Tool 的表格检测，直接使用视觉理解和 OCR 提取内容
6. THE Agent_Brain SHALL 在单次对话循环中处理完一页文档，工具调用次数上限为 10 次
7. WHEN 处理多页 PDF 时, THE Scan_Agent SHALL 逐页调用 Agent_Brain 处理，并汇总所有页面的结构化结果

### 需求 4：自我审查与纠错

**用户故事：** 作为用户，我希望 Agent 能审查自己的输出质量，在发现问题时自动纠正，确保翻译结果的完整性和准确性。

#### 验收标准

1. WHEN Agent_Brain 完成一页的结构提取和翻译后, THE Scan_Agent SHALL 执行 Self_Review 环节
2. WHILE 执行 Self_Review 时, THE Agent_Brain SHALL 检查以下质量维度：文字提取完整性（无遗漏）、翻译覆盖率（所有文本均已翻译）、结构正确性（表格行列数与原文一致）
3. IF Self_Review 发现文字遗漏或翻译缺失, THEN THE Agent_Brain SHALL 重新调用相关工具补充缺失内容，最多重试 2 次
4. IF Self_Review 在 2 次重试后仍未通过, THEN THE Scan_Agent SHALL 在输出中标记该页存在质量问题并继续处理后续页面
5. THE Scan_Agent SHALL 在处理日志中记录每页的 Self_Review 结果（通过/未通过及原因）

### 需求 5：端到端流程集成

**用户故事：** 作为用户，我希望扫描件翻译的端到端流程（PDF 输入 → 翻译后 Word 输出）与现有系统无缝集成，通过 GUI 即可使用。

#### 验收标准

1. THE Scan_Agent SHALL 提供与现有 parse_scan_pdf 相同的调用接口，返回兼容的结构化数据格式，使 TranslatorAgent 无需大幅修改
2. WHEN 用户在 GUI 中选择扫描件 PDF 并点击翻译时, THE TranslatorAgent SHALL 自动检测并使用 Scan_Agent 进行处理
3. THE Scan_Agent SHALL 通过事件机制（AgentEvent）向 GUI 报告处理进度，包括当前页码、当前步骤（分析/OCR/翻译/生成）和完成百分比
4. WHEN Agent_Brain 模型未配置时, THE Scan_Agent SHALL 回退到现有 v7.1 固定流水线，确保基本功能可用
5. THE Scan_Agent SHALL 将 Agent_Brain 的 token 消耗和翻译模型的 token 消耗分别统计并报告
6. THE Scan_Agent SHALL 在处理完成后输出总耗时、各工具调用次数和 token 消耗的汇总信息

### 需求 6：配置与模型管理

**用户故事：** 作为开发者，我希望通过 .env 文件和配置系统管理 Agent 大脑模型的选择和参数，无需修改代码即可切换模型。

#### 验收标准

1. THE Config SHALL 支持通过 .env 文件配置 Agent 大脑模型，包括 AGENT_BRAIN_PROVIDER（gemini/claude/openai）、AGENT_BRAIN_MODEL、AGENT_BRAIN_API_KEY
2. THE Config SHALL 支持配置 Agent 大脑模型的参数：最大 token 数、温度（temperature）、最大重试次数
3. WHEN 多个外部模型 API 密钥均已配置时, THE Config SHALL 支持通过 AGENT_BRAIN_PROVIDER 指定使用哪个模型
4. THE Config SHALL 提供模型能力自动检测，验证配置的模型是否支持视觉输入和工具调用
5. IF 配置的模型不支持视觉输入或工具调用, THEN THE Config SHALL 在启动时输出警告信息并建议更换模型
