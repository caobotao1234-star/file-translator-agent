# translator/translator_agent.py
import os
from typing import Optional
from config.settings import Config
from core.llm_engine import ArkLLMEngine
from core.llm_router import LLMRouter
from core.logger import get_logger
from translator.docx_parser import parse_docx
from translator.docx_writer import write_docx
from translator.format_engine import FormatEngine
from translator.translate_pipeline import TranslatePipeline

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
        batch_size: int = 10,
        debug: bool = False,
    ):
        """
        参数：
            draft_model_id: 初翻模型 ID（默认用 Config 中的模型）
            review_model_id: 审校模型 ID（为 None 则跳过审校）
            batch_size: 每批翻译的段落数
            debug: 调试模式
        """
        self.debug = debug
        self.format_engine = FormatEngine()

        # 初始化 LLM 路由
        self.router = LLMRouter(api_key=Config.ARK_API_KEY)
        self.router.register("draft", model_id=draft_model_id or Config.DEFAULT_MODEL_ID)
        # 审校模型：如果没有单独指定，默认和初翻用同一个模型
        review_id = review_model_id or draft_model_id or Config.DEFAULT_MODEL_ID
        self.router.register("review", model_id=review_id)

        # 初始化翻译流水线（初翻 + 审校双 Agent）
        self.pipeline = TranslatePipeline(
            draft_llm=self.router.get("draft"),
            review_llm=self.router.get("review"),
            batch_size=batch_size,
            debug=debug,
        )

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        logger.info("翻译 Agent 初始化完成")

    def translate_file(
        self,
        input_path: str,
        output_path: str = None,
        source_lang: str = "中文",
        target_lang: str = "英文",
    ) -> str:
        """
        翻译一个 Word 文档。

        参数：
            input_path: 输入文件路径
            output_path: 输出文件路径（默认自动生成）
            source_lang: 源语言
            target_lang: 目标语言

        返回：输出文件路径
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"文件不存在: {input_path}")

        # 自动生成输出路径
        if output_path is None:
            basename = os.path.splitext(os.path.basename(input_path))[0]
            output_path = os.path.join(OUTPUT_DIR, f"{basename}_translated.docx")

        logger.info(f"开始翻译: {input_path} -> {output_path}")

        # 1. 解析文档
        print(f"[📄 解析文档] {input_path}")
        parsed_data = parse_docx(input_path)
        content_count = sum(1 for p in parsed_data["paragraphs"] if not p["is_empty"])
        print(f"[📄 解析完成] {content_count} 个段落待翻译")

        # 2. 翻译
        def on_progress(completed, total):
            print(f"[🔄 翻译进度] {completed}/{total} 段落", flush=True)

        translations = self.pipeline.translate_document(
            parsed_data,
            source_lang=source_lang,
            target_lang=target_lang,
            on_progress=on_progress,
        )

        # 3. 生成文档
        print(f"[📝 生成文档] 应用格式规则并写入...")
        write_docx(parsed_data, translations, output_path, self.format_engine,
                   source_path=input_path)
        print(f"[✅ 翻译完成] 输出文件: {output_path}")

        return output_path

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
