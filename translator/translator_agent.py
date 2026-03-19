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

# =============================================================
# 📘 教学笔记：翻译 Agent 主控制器（v5 架构）
# =============================================================
# v5 架构简化了模型角色：
#   - 翻译模型：负责文本翻译（doubao 等）
#   - 规划者：Agent Brain，扫描件分析决策大脑（Gemini/Claude）
#   - 图片生成：生图模型（Gemini image）
#   - 审校：由规划者统一管理（内容+排版），不再独立配置
#
# 📘 去掉了什么？
#   - 独立审校模型（review_model_id）→ 规划者统管
#   - 排版审校模型（vision_model_id）→ 规划者统管
#   - LayoutReviewAgent → 删除，功能合并到规划者
# =============================================================

logger = get_logger("translator_agent")

OUTPUT_DIR = "output"


class TranslatorAgent:
    """
    翻译 Agent v5：翻译模型 + 规划者 + 图片生成。
    审校由规划者统一管理。
    """

    def __init__(
        self,
        translate_model_id: str = None,
        brain_model_id: str = None,
        image_model_id: str = None,
        batch_size: int = 10,
        max_workers: int = 1,
        debug: bool = False,
    ):
        """
        📘 参数说明（v5 简化版）：
            translate_model_id: 翻译模型（支持 "provider:model" 或纯模型ID）
            brain_model_id: 规划者 / Agent Brain（扫描件分析决策大脑，同时负责审校）
            image_model_id: 图片生成模型（如 gemini-3-pro-image-preview）
            batch_size: 每批翻译的段落数
            max_workers: 并行线程数
            debug: 调试模式
        """
        self.debug = debug
        self.format_engine = FormatEngine()

        # 📘 教学笔记：统一模型路由
        self.router = LLMRouter(api_key=Config.ARK_API_KEY)
        self.router.register_model("translate", model_str=translate_model_id or Config.DEFAULT_MODEL_ID)

        # 📘 教学笔记：图片生成模型（如 gemini-3-pro-image-preview）
        if image_model_id:
            self.router.register_model("image_gen", model_str=image_model_id)
            logger.info(f"图片生成模型已注册: {image_model_id}")

        # 📘 初始化翻译流水线（v5: 纯翻译，无审校）
        self.pipeline = TranslatePipeline(
            translate_llm=self.router.get("translate"),
            batch_size=batch_size,
            max_workers=max_workers,
            debug=debug,
        )

        # 📘 教学笔记：COM 增强模式自动检测
        self.com_enabled = is_com_available()

        # 📘 教学笔记：Agent Brain（规划者 / 扫描件翻译大脑 / 统一审校）
        if brain_model_id:
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
        翻译文档（支持 .docx、.pptx、.pdf）。

        📘 v5 简化：去掉了独立审校和排版审校步骤。
        审校由规划者（Agent Brain）在扫描件处理中统一管理。
        普通文档（Word/PPT/PDF）直接翻译输出。
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"文件不存在: {input_path}")

        ext = os.path.splitext(input_path)[1].lower()
        if ext not in (".docx", ".pptx", ".pdf"):
            raise ValueError(f"不支持的文件格式: {ext}，仅支持 .docx、.pptx 和 .pdf")

        if output_path is None:
            basename = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(OUTPUT_DIR, f"{basename}_translated{ext}")

        logger.info(f"开始翻译: {input_path} -> {output_path}")

        # 1. 解析文档（按格式分发）
        print(f"[📄 解析文档] {input_path}")
        is_scan = False
        if ext == ".docx":
            parsed_data = parse_docx(input_path)
            para_count = sum(1 for i in parsed_data["items"]
                             if i["type"] == "paragraph" and not i.get("is_empty"))
        elif ext == ".pptx":
            parsed_data = parse_pptx(input_path)
            para_count = sum(1 for i in parsed_data["items"]
                             if i["type"] == "slide_text")
        else:  # .pdf
            is_scan = detect_scan_pdf(input_path)
            if is_scan:
                base, _ = os.path.splitext(output_path)
                output_path = base + ".docx"

                # 📘 Agent Brain 模式：端到端处理扫描件
                if "agent_brain" in self.router.engines:
                    print(f"[🤖 Agent 模式] 使用 Agent Brain 处理扫描件...", flush=True)
                    try:
                        from translator.scan_agent import ScanAgent

                        # 📘 token 更新回调：ScanAgent 每次 token 变化时
                        # 实时更新 _last_scan_stats，供 GUI worker 读取
                        def _on_scan_token_update(agent):
                            self._last_scan_stats = agent.stats
                            # 📘 通知 GUI 刷新 token 显示
                            cb = getattr(self, '_gui_token_callback', None)
                            if cb:
                                cb()

                        scan_agent = ScanAgent(
                            brain_engine=self.router.get("agent_brain"),
                            translate_pipeline=self.pipeline,
                            format_engine=self.format_engine,
                            image_gen_engine=(
                                self.router.get("image_gen")
                                if "image_gen" in self.router.engines
                                else None
                            ),
                            on_token_update=_on_scan_token_update,
                        )
                        result = scan_agent.process_scan_pdf(
                            filepath=input_path,
                            output_path=output_path,
                            target_lang=target_lang,
                        )
                        # 📘 最终缓存 ScanAgent 的 stats
                        self._last_scan_stats = scan_agent.stats
                        return result["output_path"]
                    except Exception as e:
                        logger.error(f"Agent 模式失败，回退到 v7.1: {e}")
                        print(f"[⚠️ Agent 回退] {e}，使用 v7.1 流水线...", flush=True)

                # 📘 v7.1 固定流水线回退
                print(f"[🔍 v7.1 模式] CV + OCR + Vision LLM 混合识别...", flush=True)
                vision_engine = self.router.get("translate")
                parsed_data = parse_scan_pdf(input_path, vision_llm=vision_engine)
            else:
                parsed_data = parse_pdf(input_path)
            para_count = sum(1 for i in parsed_data["items"]
                             if i["type"] == "pdf_block")

        cell_count = sum(1 for i in parsed_data["items"]
                         if i["type"] == "table_cell")
        total_count = para_count + cell_count
        print(f"[📄 解析完成] {para_count} 个文本段落 + {cell_count} 个表格单元格 = {total_count} 个翻译单元")

        # 2. 翻译
        def on_progress(completed, total):
            print(f"[🔄 翻译进度] {completed}/{total}", flush=True)

        self.pipeline.reset_stop()

        translations = self.pipeline.translate_document(
            parsed_data,
            target_lang=target_lang,
            on_progress=on_progress,
        )

        was_stopped = self.pipeline.is_stopped
        translated_count = len(translations)
        if was_stopped:
            print(f"[⚠️ 提前停止] 已翻译 {translated_count}/{total_count} 个单元，正在写入已完成部分...")

        # 3. 生成文档（按格式分发）
        print(f"[📝 生成文档] 应用格式规则并写入...")
        if ext == ".docx":
            write_docx(parsed_data, translations, output_path, self.format_engine,
                       source_path=input_path)
        elif ext == ".pptx":
            write_pptx(parsed_data, translations, output_path, self.format_engine,
                       source_path=input_path)
        elif is_scan:
            output_path = write_scan_pdf(parsed_data, translations, output_path, self.format_engine,
                           source_path=input_path)
        else:
            write_pdf(parsed_data, translations, output_path, self.format_engine,
                      source_path=input_path)

            # 📘 Step 3.5: PDF 排版修正 Agent（翻译后自动检测溢出并修正）
            # 只对普通 PDF（非扫描件）生效，扫描件由 ScanAgent 统一处理
            if not self.pipeline.is_stopped:
                try:
                    from translator.layout_agent import PDFLayoutAgent

                    brain_engine = (
                        self.router.get("agent_brain")
                        if "agent_brain" in self.router.engines
                        else None
                    )
                    layout_agent = PDFLayoutAgent(
                        brain_engine=brain_engine,
                        translate_pipeline=self.pipeline,
                        format_engine=self.format_engine,
                    )
                    updated_translations, layout_overrides = layout_agent.review_and_fix(
                        source_path=input_path,
                        parsed_data=parsed_data,
                        translations=translations,
                        target_lang=target_lang,
                    )
                    # 📘 如果有修正，重新写入 PDF
                    if layout_overrides:
                        logger.info(f"排版修正：{len(layout_overrides)} 个覆盖，重新写入 PDF")
                        write_pdf(
                            parsed_data, updated_translations, output_path,
                            self.format_engine, source_path=input_path,
                            layout_overrides=layout_overrides,
                        )
                    # 📘 缓存 layout agent stats 供 GUI token 统计
                    self._last_layout_stats = layout_agent.stats
                except Exception as e:
                    logger.warning(f"PDF 排版修正失败（不影响翻译结果）: {e}")

        # 4. COM 增强：处理图表/文本框/SmartArt（仅 Word）
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
