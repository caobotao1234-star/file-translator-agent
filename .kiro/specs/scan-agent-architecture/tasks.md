# 实施计划：扫描件 Agent 架构

## 概述

将扫描件 PDF 翻译从固定流水线重构为 Agent 架构。按照自底向上的顺序实施：先建引擎层（ExternalLLMEngine），再扩展路由器，然后封装工具，接着实现 ScanAgent 核心循环，最后集成到 TranslatorAgent 并接入 GUI 进度事件。

## Tasks

- [x] 1. ExternalLLMEngine 外部模型引擎
  - [x] 1.1 创建 `core/external_llm_engine.py`，实现 ExternalLLMEngine 类
    - 使用 `openai` 包，通过 `base_url` 区分 Gemini/Claude/GPT/NanoBanana
    - 实现 `PROVIDER_CONFIG` 字典（base_url + env_key 映射）
    - 实现 `stream_chat(messages, tools)` 方法，yield 与 ArkLLMEngine 完全相同格式的 chunk：`{"type": "text"}`、`{"type": "tool_call"}`、`{"type": "usage"}`
    - 支持 messages 中的 `image_url` 类型（base64 图片）
    - 复用 ArkLLMEngine 相同的重试逻辑（指数退避），复用 `_is_retryable` 判断
    - API 密钥未配置时抛出明确的 `ValueError`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.6, 1.7_

  - [ ]* 1.2 为 ExternalLLMEngine 编写属性测试（Property 1: stream_chat 输出格式兼容性）
    - **Property 1: stream_chat 输出格式兼容性**
    - 生成随机 API 响应 chunk，验证每个 chunk 是 text/tool_call/usage 三种格式之一，字段类型正确
    - **Validates: Requirements 1.2**

  - [ ]* 1.3 为 ExternalLLMEngine 编写属性测试（Property 2: 图片消息格式正确性）
    - **Property 2: 图片消息格式正确性**
    - 生成随机 base64 字符串，验证构建的 messages 符合 OpenAI image_url 格式，base64 数据 round-trip 不变
    - **Validates: Requirements 1.3**

  - [ ]* 1.4 为 ExternalLLMEngine 编写属性测试（Property 3: 重试机制指数退避）
    - **Property 3: 重试机制指数退避**
    - 生成随机可重试错误序列，验证等待时间满足 `delay_i = base_delay * 2^(i-1)`，不可重试错误立即抛出
    - **Validates: Requirements 1.4**

  - [ ]* 1.5 为 ExternalLLMEngine 编写单元测试
    - 测试各 provider 的 base_url 正确性
    - 测试 API 密钥缺失时的错误信息
    - 测试不可重试错误（HTTP 400）立即抛出
    - _Requirements: 1.1, 1.6_

- [x] 2. 扩展 LLMRouter 支持外部模型
  - [x] 2.1 在 `core/llm_router.py` 中添加 `register_external` 方法
    - 接受 `name, provider, model_id, api_key, max_retries` 参数
    - 内部创建 ExternalLLMEngine 实例并存入 `self.engines`
    - api_key 为 None 时从 .env 自动读取（通过 PROVIDER_CONFIG 的 env_key）
    - 保持 `get(name)` 返回的引擎具有 `stream_chat` 方法（鸭子类型）
    - _Requirements: 1.5_

  - [ ]* 2.2 为 LLMRouter 编写属性测试（Property 4: 路由器异构引擎管理）
    - **Property 4: 路由器异构引擎管理**
    - 生成随机混合注册序列（Ark + External），验证 `get(name)` 返回正确实例，所有引擎都有 `stream_chat`
    - **Validates: Requirements 1.5**

- [x] 3. 配置扩展：Agent Brain 配置
  - [x] 3.1 在 `config/settings.py` 的 Config 类中添加 `get_agent_brain_config()` 静态方法
    - 读取 AGENT_BRAIN_PROVIDER、AGENT_BRAIN_MODEL、AGENT_BRAIN_API_KEY（优先）或各 provider 专用 key
    - 读取可选参数：AGENT_BRAIN_MAX_TOKENS（默认 8192）、AGENT_BRAIN_TEMPERATURE（默认 0.1）、AGENT_BRAIN_MAX_RETRIES（默认 3）
    - 未配置 provider 时返回 None
    - _Requirements: 6.1, 6.2, 6.3_

  - [x] 3.2 添加模型能力检测方法 `validate_agent_brain_model()`
    - 维护已知支持视觉+工具调用的模型列表（gemini-2.5-pro, claude-sonnet-4, gpt-4o, nanobanana-pro 等）
    - 不支持时输出警告日志并返回警告信息
    - _Requirements: 6.4, 6.5_

  - [ ]* 3.3 为配置编写属性测试（Property 5: 配置解析 round-trip）
    - **Property 5: 配置解析 round-trip**
    - 生成随机环境变量组合，验证 `get_agent_brain_config()` 返回值与环境变量一致，可选参数有默认值
    - **Validates: Requirements 6.1, 6.2**

  - [ ]* 3.4 为配置编写属性测试（Property 16: 模型能力检测）
    - **Property 16: 模型能力检测**
    - 验证已知模型名称的能力检测结果正确（gemini-2.5-pro → True, 未知模型 → 警告）
    - **Validates: Requirements 6.4**

- [x] 4. Checkpoint — 确保引擎层和配置层测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 5. 工具注册与实现
  - [x] 5.1 创建 `tools/scan_tools.py`，实现 OCRTool
    - 继承 BaseTool，name="ocr_extract_text"
    - 在子进程中执行 RapidOCR（复用 scan_parser.py 的子进程 OCR 逻辑），避免 PyQt6 + onnxruntime DLL 冲突
    - 通过 ScanAgent 上下文（`self.context`）访问页面图片，不通过参数传递 base64
    - 返回 JSON 字符串：`[{"text": "...", "bbox": [x1,y1,x2,y2], "confidence": 0.95}, ...]`
    - 参数校验：缺少 page_index 时返回结构化错误
    - _Requirements: 2.1, 2.2, 2.6, 2.7_

  - [x] 5.2 在 `tools/scan_tools.py` 中实现 CVTool
    - 继承 BaseTool，name="cv_detect_layout"
    - 使用 OpenCV 检测表格线（水平线+垂直线）和图片区域
    - 返回 JSON：`{"has_table": bool, "h_lines": [...], "v_lines": [...], "image_regions": [...]}`
    - 通过上下文访问页面图片
    - _Requirements: 2.1, 2.3, 2.6, 2.7_

  - [x] 5.3 在 `tools/scan_tools.py` 中实现 TranslationTool
    - 继承 BaseTool，name="translate_texts"
    - 内部调用 TranslatePipeline.translate_batch()，复用 doubao 模型
    - 参数：texts（数组）、target_lang（字符串）
    - 返回 JSON：`{"translations": {"原文1": "译文1", ...}}`
    - _Requirements: 2.1, 2.4, 2.6, 2.7_

  - [x] 5.4 在 `tools/scan_tools.py` 中实现 WordWriterTool
    - 继承 BaseTool，name="generate_word_document"
    - 内部调用 scan_writer 的 write_scan_pdf()
    - 参数：page_structures、translations、output_path
    - 返回生成的文件路径
    - _Requirements: 2.1, 2.5, 2.6, 2.7_

  - [ ]* 5.5 为工具编写属性测试（Property 6: CV_Tool 输出结构完整性）
    - **Property 6: CV_Tool 输出结构完整性**
    - 生成随机尺寸的 numpy 数组作为图片，验证返回值包含 has_table/h_lines/v_lines/image_regions，has_table=True 时线列表非空
    - **Validates: Requirements 2.3**

  - [ ]* 5.6 为工具编写属性测试（Property 7: 工具执行安全性）
    - **Property 7: 工具执行安全性**
    - 生成随机参数字典（含缺失 required 字段），验证返回参数校验错误；工具执行异常时返回结构化错误信息
    - **Validates: Requirements 2.6, 2.7**

  - [ ]* 5.7 为工具编写属性测试（Property 8: Word 生成 round-trip）
    - **Property 8: Word 生成 round-trip**
    - 生成随机合法 page_structures 和 translations，验证生成的 .docx 文件存在且可被 python-docx 打开，表格数量一致
    - **Validates: Requirements 2.5**

- [x] 6. Checkpoint — 确保工具层测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 7. ScanAgent 核心实现
  - [x] 7.1 创建 `translator/scan_agent.py`，实现 ScanAgent 类骨架
    - 构造函数接收 brain_engine、translate_pipeline、format_engine、max_tool_calls=10、max_review_retries=2
    - 注册 4 个工具到内部 tool_registry（OCRTool、CVTool、TranslationTool、WordWriterTool）
    - 定义 SCAN_AGENT_SYSTEM_PROMPT（Agent 大脑的系统提示词，包含工具说明、输出格式、key 命名规则）
    - _Requirements: 2.1, 3.3_

  - [x] 7.2 实现 `process_scan_pdf()` 主方法
    - PDF 渲染为页面图片（每页 base64）
    - 逐页调用 `_process_single_page()`
    - 汇总所有页面的 page_structures 和 items
    - 调用 WordWriterTool 生成最终 .docx
    - 返回与 parse_scan_pdf 兼容的数据格式（含 source、source_type、filepath、items、page_structures、page_images、stats）
    - _Requirements: 3.1, 3.7, 5.1_

  - [x] 7.3 实现 `_process_single_page()` ReAct 循环
    - 发送页面图片 + system prompt 给 Agent Brain
    - Brain 返回 tool_call → 执行工具 → 将结果作为 tool message 反馈给 Brain → 继续循环
    - Brain 返回 text（最终 JSON）→ 解析结构化数据 → 结束循环
    - 工具调用次数上限 max_tool_calls，达到上限强制结束并返回已有结果
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6_

  - [x] 7.4 实现 `_self_review()` 自我审查
    - 将页面图片 + 提取结果发送给 Agent Brain，要求检查：文字提取完整性、翻译覆盖率、结构正确性
    - 审查未通过时重新调用相关工具补充，最多重试 max_review_retries 次
    - 2 次重试后仍未通过则标记该页质量问题并继续
    - 在 stats.review_results 中记录每页审查结果（page、passed、reason、retries）
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x] 7.5 实现进度事件和统计信息
    - 通过 on_event 回调发射 AgentEvent，包含 page_index、step（分析/OCR/翻译/生成）、progress_pct
    - 分别统计 brain_tokens 和 translate_tokens
    - 统计各工具调用次数和总耗时
    - _Requirements: 5.3, 5.5, 5.6_

  - [ ]* 7.6 为 ScanAgent 编写属性测试（Property 9: PDF 页面渲染完整性）
    - **Property 9: PDF 页面渲染完整性**
    - 验证 N 页 PDF 渲染后 page_images 长度等于 N，每个元素为非空 bytes
    - **Validates: Requirements 3.1**

  - [ ]* 7.7 为 ScanAgent 编写属性测试（Property 10: 工具调用次数上限）
    - **Property 10: 工具调用次数上限**
    - Mock Agent Brain 持续返回 tool_call，验证总调用次数不超过 max_tool_calls
    - **Validates: Requirements 3.6**

  - [ ]* 7.8 为 ScanAgent 编写属性测试（Property 11: 多页逐页处理）
    - **Property 11: 多页逐页处理**
    - 验证 N 页 PDF 恰好调用 N 次 _process_single_page，page_structures 长度等于 N
    - **Validates: Requirements 3.7**

  - [ ]* 7.9 为 ScanAgent 编写属性测试（Property 12: 自我审查必执行 + 结果记录）
    - **Property 12: 自我审查必执行 + 结果记录**
    - 验证 stats.review_results 包含每页审查记录，未通过时 retries ≤ max_review_retries
    - **Validates: Requirements 4.1, 4.3, 4.4, 4.5**

  - [ ]* 7.10 为 ScanAgent 编写属性测试（Property 13: 返回值格式兼容性）
    - **Property 13: 返回值格式兼容性**
    - 验证返回值包含 source/source_type/filepath/items/page_structures/page_images，items 中 key 符合命名规则
    - **Validates: Requirements 5.1**

  - [ ]* 7.11 为 ScanAgent 编写属性测试（Property 14: 进度事件发射）
    - **Property 14: 进度事件发射**
    - 验证每页处理过程中至少发射一个包含 page_index/step/progress_pct 的 AgentEvent
    - **Validates: Requirements 5.3**

  - [ ]* 7.12 为 ScanAgent 编写属性测试（Property 15: 统计信息完整性）
    - **Property 15: 统计信息完整性**
    - 验证 stats 包含 total_time_seconds/brain_tokens/translate_tokens/tool_calls 四个维度
    - **Validates: Requirements 5.5, 5.6**

- [x] 8. Checkpoint — 确保 ScanAgent 核心测试通过
  - 确保所有测试通过，如有疑问请询问用户。

- [x] 9. 端到端集成：TranslatorAgent 接入 ScanAgent
  - [x] 9.1 修改 `translator/translator_agent.py`，在 `__init__` 中注册 Agent Brain 模型
    - 调用 `Config.get_agent_brain_config()` 获取配置
    - 配置存在时通过 `router.register_external("agent_brain", ...)` 注册外部模型
    - 调用 `Config.validate_agent_brain_model()` 检查模型能力，不支持时输出警告
    - _Requirements: 5.2, 5.4, 6.1, 6.3_

  - [x] 9.2 修改 `translator/translator_agent.py` 的 `translate_file()` 方法
    - 在 `is_scan=True` 分支中添加 Agent Brain 检测：`if "agent_brain" in self.router.engines`
    - 已配置时创建 ScanAgent 实例并调用 `process_scan_pdf()`，直接返回输出路径
    - 未配置时回退到现有 v7.1 流水线（`parse_scan_pdf`），确保基本功能可用
    - 将 ScanAgent 的 AgentEvent 转发给 GUI 进度回调
    - _Requirements: 5.1, 5.2, 5.4_

  - [ ]* 9.3 为集成编写单元测试
    - 测试 Agent Brain 未配置时回退到 v7.1 流水线
    - 测试 Agent Brain 已配置时使用 ScanAgent
    - 测试 ScanAgent 异常时回退到 v7.1
    - _Requirements: 5.2, 5.4_

- [x] 10. 安装依赖并验证
  - [x] 10.1 安装 `openai` 包到 volcengine venv
    - 执行 `volcengine\Scripts\pip.exe install openai`
    - 确认 `hypothesis` 测试库已安装（`volcengine\Scripts\pip.exe install hypothesis`）
    - _Requirements: 1.1_

- [x] 11. Final Checkpoint — 确保所有测试通过
  - 确保所有测试通过，如有疑问请询问用户。

## Notes

- 标记 `*` 的任务为可选任务，可跳过以加速 MVP 开发
- 每个任务引用了具体的需求条款，确保可追溯性
- 属性测试验证系统的普遍正确性属性，单元测试验证具体示例和边界条件
- RapidOCR 必须在子进程中运行（PyQt6 + onnxruntime DLL 冲突），这是硬性约束
- 外部模型未配置时必须回退到 v7.1 流水线，这是安全网
- 仅影响扫描件 PDF 翻译（is_scan=True），Word/PPT/普通 PDF 流程完全不动
