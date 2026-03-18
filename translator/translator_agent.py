# translator/translator_agent.py
import os
from typing import Optional
from config.settings import Config
from core.llm_engine import ArkLLMEngine
from core.llm_router import LLMRouter
from core.logger import get_logger
from translator.docx_parser import parse_docx
from translator.docx_writer import write_docx
from translator.pptx_parser import parse_pptx
from translator.pptx_writer import write_pptx
from translator.pdf_parser import parse_pdf
from translator.pdf_writer import write_pdf
from translator.scan_parser import detect_scan_pdf, parse_scan_pdf
from translator.scan_writer import write_scan_pdf
from translator.format_engine import FormatEngine
from translator.translate_pipeline import TranslatePipeline
from translator.com_engine import is_com_available, extract_extra_texts, write_extra_texts
from translator.layout_agent import LayoutReviewAgent

# =============================================================
# 📘 教学笔记：翻译 Agent 主控制器
# =============================================================
# 这是翻译 Agent 的"总指挥"，串联整个流程：
#   1. 解析 Word 文档
#   2. 调用翻译流水线（初翻 + 审校）
#   3. 通过格式引擎映射字体
#   4. 生成翻译后的 Word 文档
#
# 同时它也管理用户交互：
#   - 用户可以通过对话修改格式规则
#   - 用户可以指定源语言和目标语言
#   - 用户可以查看当前格式规则
# =============================================================

logger = get_logger("translator_agent")

OUTPUT_DIR = "output"


class TranslatorAgent:
    """
    翻译 Agent：Word 进，Word 出，保持格式。
    """

    def __init__(
        self,
        draft_model_id: str = None,
        review_model_id: str = None,
        vision_model_id: str = None,
        brain_model_id: str = None,
        image_model_id: str = None,
        batch_size: int = 10,
        max_workers: int = 1,
        debug: bool = False,
    ):
        """
        参数：
            draft_model_id: 初翻模型（支持 "provider:model" 或纯模型ID）
            review_model_id: 审校模型（为 None 则跳过审校）
            vision_model_id: 排版审校 Vision 模型
            brain_model_id: 规划者 / Agent Brain（扫描件分析决策大脑）
            image_model_id: 图片生成模型（如 gemini-3-pro-image-preview）
            batch_size: 每批翻译的段落数
            max_workers: 并行线程数
            debug: 调试模式
        """
        self.debug = debug
        self.format_engine = FormatEngine()

        # 📘 教学笔记：统一模型路由
        # 所有模型（火山引擎 + Gemini + Claude 等）通过 register_model 注册，
        # Router 自动识别 provider 并创建对应的引擎。
        # GUI 传过来的 model_id 可能是 "doubao-seed-1-8" 或 "gemini:gemini-3.1-pro"。
        self.router = LLMRouter(api_key=Config.ARK_API_KEY)
        self.router.register_model("draft", model_str=draft_model_id or Config.DEFAULT_MODEL_ID)
        review_id = review_model_id or draft_model_id or Config.DEFAULT_MODEL_ID
        self.router.register_model("review", model_str=review_id)

        # 📘 教学笔记：Vision 模型（排版审校）
        # 多模态模型能"看图"，用于翻译后的排版质量检查。
        # 如果用户没指定 vision_model_id，跳过排版审校。
        self.layout_agent = None
        if vision_model_id:
            self.router.register_model("vision", model_str=vision_model_id)
            self.layout_agent = LayoutReviewAgent(
                vision_llm=self.router.get("vision"),
                fix_llm=self.router.get("review"),
            )
            logger.info(f"排版审校 Agent 已启用 (Vision: {vision_model_id})")

        # 📘 教学笔记：图片生成模型（如 gemini-3-pro-image-preview）
        # 某些任务（如扫描件中的图表重绘）可能需要图片生成能力。
        # 注册后其他 Agent 可以通过 router.get("image_gen") 获取。
        if image_model_id:
            self.router.register_model("image_gen", model_str=image_model_id)
            logger.info(f"图片生成模型已注册: {image_model_id}")

        # 初始化翻译流水线（初翻 + 审校双 Agent）
        self.pipeline = TranslatePipeline(
            draft_llm=self.router.get("draft"),
            review_llm=self.router.get("review"),
            batch_size=batch_size,
            max_workers=max_workers,
            debug=debug,
        )

        # 📘 教学笔记：COM 增强模式自动检测
        # 启动时探测一次 COM 环境，结果缓存，后续不再重复检测。
        # 有 COM → 能处理图表/文本框/SmartArt
        # 无 COM → 静默降级，只处理段落+表格（python-docx 能力范围）
        self.com_enabled = is_com_available()

        # 📘 教学笔记：Agent Brain（规划者 / 扫描件翻译大脑）
        # GUI 选择优先，.env 配置作为 fallback。
        # 未配置时自动回退到 v7.1 固定流水线，不影响其他功能。
        if brain_model_id:
            # 📘 GUI 指定了规划者模型 → 直接用 register_model
            try:
                self.router.register_model("agent_brain", model_str=brain_model_id)
                provider, model = Config.parse_model_id(brain_model_id)
                supported, warning = Config.validate_agent_brain_model(provider, model)
                if warning:
                    logger.warning(f"Agent Brain 模型警告: {warning}")
                logger.info(f"Agent Brain 已启用 (GUI): {brain_model_id}")
            except Exception as e:
                logger.warning(f"Agent Brain 注册失败，将使用 v7.1 流水线: {e}")
        else:
            # 📘 GUI 未选择 → 尝试从 .env 读取
            brain_config = Config.get_agent_brain_config()
            if brain_config:
                try:
                    self.router.register_external(
                        name="agent_brain",
                        provider=brain_config["provider"],
                        model_id=brain_config["model"],
                        api_key=brain_config["api_key"],
                        max_retries=brain_config.get("max_retries", 3),
                    )
                    supported, warning = Config.validate_agent_brain_model(
                        brain_config["provider"], brain_config["model"]
                    )
                    if warning:
                        logger.warning(f"Agent Brain 模型警告: {warning}")
                    logger.info(
                        f"Agent Brain 已启用 (.env): {brain_config['provider']}/{brain_config['model']}"
                    )
                except Exception as e:
                    logger.warning(f"Agent Brain 注册失败，将使用 v7.1 流水线: {e}")

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        logger.info(f"翻译 Agent 初始化完成 (COM 增强: {'✅ 开启' if self.com_enabled else '❌ 关闭'})")

    def translate_file(
        self,
        input_path: str,
        output_path: str = None,
        target_lang: str = "英文",
    ) -> str:
        """
        翻译文档（支持 .docx 和 .pptx）。

        📘 教学笔记：源语言自动识别，目标语言用户指定
        源语言不需要用户选——LLM 自己能识别原文是什么语言。
        但目标语言必须用户指定，因为中英混合文档无法自动判断"翻成什么"。
        翻译 prompt 里不再指定源语言，只告诉 LLM "翻译成XX"。

        参数：
            input_path: 输入文件路径（.docx 或 .pptx）
            output_path: 输出文件路径（默认自动生成）
            target_lang: 目标语言

        返回：输出文件路径
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"文件不存在: {input_path}")

        ext = os.path.splitext(input_path)[1].lower()
        if ext not in (".docx", ".pptx", ".pdf"):
            raise ValueError(f"不支持的文件格式: {ext}，仅支持 .docx、.pptx 和 .pdf")

        # 自动生成输出路径（保持原扩展名）
        if output_path is None:
            basename = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(OUTPUT_DIR, f"{basename}_translated{ext}")

        logger.info(f"开始翻译: {input_path} -> {output_path}")

        # 1. 解析文档（按格式分发）
        print(f"[📄 解析文档] {input_path}")
        is_scan = False  # 📘 标记是否为扫描件
        if ext == ".docx":
            parsed_data = parse_docx(input_path)
            para_count = sum(1 for i in parsed_data["items"]
                             if i["type"] == "paragraph" and not i.get("is_empty"))
        elif ext == ".pptx":
            parsed_data = parse_pptx(input_path)
            para_count = sum(1 for i in parsed_data["items"]
                             if i["type"] == "slide_text")
        else:  # .pdf
            # 📘 教学笔记：扫描件自动检测
            # 先检测是否为扫描件（每页文本块数 < 阈值）。
            # 扫描件走 OCR 解析，普通 PDF 走文本提取。
            # 后续翻译流程完全一样，只有写入策略不同。
            is_scan = detect_scan_pdf(input_path)
            if is_scan:
                # 📘 扫描件输出 .docx 而不是 .pdf
                base, _ = os.path.splitext(output_path)
                output_path = base + ".docx"

                # 📘 教学笔记：Agent Brain 模式 vs v7.1 固定流水线
                # 如果配置了 Agent Brain（Gemini/Claude/GPT），用 ScanAgent 端到端处理。
                # ScanAgent 内部完成分析+翻译+Word生成，直接返回输出路径。
                # 未配置时回退到 v7.1 固定流水线（CV + OCR + Vision LLM）。
                if "agent_brain" in self.router.engines:
                    print(f"[🤖 Agent 模式] 使用 Agent Brain 处理扫描件...", flush=True)
                    try:
                        from translator.scan_agent import ScanAgent
                        scan_agent = ScanAgent(
                            brain_engine=self.router.get("agent_brain"),
                            translate_pipeline=self.pipeline,
                            format_engine=self.format_engine,
                            image_gen_engine=(
                                self.router.get("image_gen")
                                if "image_gen" in self.router.engines
                                else None
                            ),
                        )
                        result = scan_agent.process_scan_pdf(
                            filepath=input_path,
                            output_path=output_path,
                            target_lang=target_lang,
                        )
                        # 📘 Agent 模式端到端完成，直接返回
                        return result["output_path"]
                    except Exception as e:
                        logger.error(f"Agent 模式失败，回退到 v7.1: {e}")
                        print(f"[⚠️ Agent 回退] {e}，使用 v7.1 流水线...", flush=True)

                # 📘 v7.1 固定流水线（Agent Brain 未配置或 Agent 模式失败时的回退）
                print(f"[🔍 v7.1 模式] CV + OCR + Vision LLM 混合识别...", flush=True)
                vision_engine = None
                if "vision" in self.router.engines:
                    vision_engine = self.router.get("vision")
                elif self.router.get("draft"):
                    vision_engine = self.router.get("draft")
                parsed_data = parse_scan_pdf(input_path, vision_llm=vision_engine)
            else:
                parsed_data = parse_pdf(input_path)
            para_count = sum(1 for i in parsed_data["items"]
                             if i["type"] == "pdf_block")

        cell_count = sum(1 for i in parsed_data["items"]
                         if i["type"] == "table_cell")
        total_count = para_count + cell_count
        print(f"[📄 解析完成] {para_count} 个文本段落 + {cell_count} 个表格单元格 = {total_count} 个翻译单元")

        # 2. 翻译（流水线通用，不区分文件格式）
        def on_progress(completed, total):
            print(f"[🔄 翻译进度] {completed}/{total}", flush=True)

        # 📘 教学笔记：优雅停止 — 重置停止标志
        # 每次翻译开始前清除上次的停止标志，否则上次停止后再点开始会立刻停止。
        self.pipeline.reset_stop()

        translations = self.pipeline.translate_document(
            parsed_data,
            target_lang=target_lang,
            on_progress=on_progress,
        )

        # 📘 教学笔记：部分写入
        # 即使用户中途停止，translations 里也有已完成的部分。
        # 未翻译的段落不会出现在 translations 字典里，
        # writer 会保留原文（因为找不到对应的 key）。
        was_stopped = self.pipeline.is_stopped
        translated_count = len(translations)
        if was_stopped:
            print(f"[⚠️ 提前停止] 已翻译 {translated_count}/{total_count} 个单元，正在写入已完成部分...")

        # 3. 生成文档（按格式分发）— 第一次写入
        print(f"[📝 生成文档] 应用格式规则并写入...")
        if ext == ".docx":
            write_docx(parsed_data, translations, output_path, self.format_engine,
                       source_path=input_path)
        elif ext == ".pptx":
            write_pptx(parsed_data, translations, output_path, self.format_engine,
                       source_path=input_path)
        elif is_scan:
            # 📘 扫描件 v5：基于结构化识别，生成全新 Word 文档
            output_path = write_scan_pdf(parsed_data, translations, output_path, self.format_engine,
                           source_path=input_path)
        else:  # 普通 PDF
            write_pdf(parsed_data, translations, output_path, self.format_engine,
                      source_path=input_path)

        # 4. 排版审校（Vision 模型看图+数据 → 下达调整指令 → 重新写入）
        # 📘 教学笔记：排版审校 v2
        # Vision 模型同时接收图片和结构化数据（bbox、字号、可用空间），
        # 可以下达两种指令：精简译文（shorten）和调整字号（resize）。
        # 返回 layout_overrides 字典，writer 写入时按指令调整字号。
        if self.layout_agent and not was_stopped and not is_scan:
            layout_overrides = {}
            layout_modified = False

            if ext == ".pdf":
                snapshot = dict(translations)
                translations, layout_overrides = self.layout_agent.review_pdf_layout(
                    source_path=input_path,
                    translated_path=output_path,
                    parsed_data=parsed_data,
                    translations=translations,
                )
                layout_modified = (
                    bool(layout_overrides)
                    or any(translations.get(k) != snapshot.get(k) for k in translations)
                )
            elif ext == ".pptx":
                snapshot = dict(translations)
                translations, layout_overrides = self.layout_agent.review_pptx_layout(
                    source_path=input_path,
                    translated_path=output_path,
                    parsed_data=parsed_data,
                    translations=translations,
                )
                layout_modified = (
                    bool(layout_overrides)
                    or any(translations.get(k) != snapshot.get(k) for k in translations)
                )

            # 📘 如果排版审校有任何修改（译文或字号），重新写入
            if layout_modified:
                print(f"[📝 重新写入] 应用排版修正...", flush=True)
                if ext == ".pptx":
                    write_pptx(parsed_data, translations, output_path, self.format_engine,
                               source_path=input_path)
                else:  # 普通 PDF
                    write_pdf(parsed_data, translations, output_path, self.format_engine,
                              source_path=input_path, layout_overrides=layout_overrides)

        # 5. COM 增强：处理图表/文本框/SmartArt（仅 Word）
        # 📘 教学笔记：PPT 的文本框/SmartArt 已经被 python-pptx 处理了
        if ext == ".docx" and self.com_enabled and not was_stopped:
            self._com_enhance(input_path, output_path, target_lang)

        print(f"[{'⚠️ 部分翻译' if was_stopped else '✅ 翻译完成'}] 输出文件: {output_path}")
        return output_path

    def _com_enhance(self, input_path: str, output_path: str, target_lang: str):
        """COM 增强处理：图表/文本框/SmartArt（仅 Word）"""
        print(f"[🔍 COM 增强] 检测图表/文本框/SmartArt...")
        extra_items = extract_extra_texts(input_path)
        if extra_items:
            print(f"[🔍 COM 增强] 发现 {len(extra_items)} 个额外元素，翻译中...")
            extra_texts = [item["text"] for item in extra_items]
            extra_translations = self.pipeline.translate_batch(
                extra_texts,
                target_lang=target_lang,
            )
            for item, trans in zip(extra_items, extra_translations):
                item["translated"] = trans

            print(f"[📝 COM 写回] 将译文写入图表/文本框...")
            written = write_extra_texts(output_path, extra_items)
            print(f"[✅ COM 完成] 成功写回 {written} 个元素")
        else:
            print(f"[ℹ️ COM 增强] 未发现需要额外处理的元素")

    def update_font_rule(self, source_font: str, target_font: str):
        """更新字体映射规则"""
        self.format_engine.set_font_mapping(source_font, target_font)
        print(f"[⚙️ 格式规则] 字体映射已更新: {source_font} -> {target_font}")

    def update_style_rule(self, style_name: str, font_name: str = None, bold: bool = None):
        """更新样式映射规则"""
        rule = {}
        if font_name:
            rule["font_name"] = font_name
        if bold is not None:
            rule["bold"] = bold
        self.format_engine.set_style_mapping(style_name, rule)
        print(f"[⚙️ 格式规则] 样式映射已更新: {style_name} -> {rule}")

    def show_format_rules(self):
        """显示当前格式规则"""
        print(self.format_engine.get_rules_summary())
