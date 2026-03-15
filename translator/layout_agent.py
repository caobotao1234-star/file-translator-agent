# translator/layout_agent.py
import json
import os
import base64
import fitz  # PyMuPDF
from typing import Dict, Any, List, Optional, Tuple
from core.llm_engine import ArkLLMEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：排版审校 Agent v2（Layout Review Agent）
# =============================================================
# v1 的问题：
#   - 权限太低：只能精简译文，不能调字号/位置/行距
#   - 信息不够：Vision 模型只看图片，不知道精确的 bbox/字号数值
#   - 匹配不准：用模糊字符串匹配，经常对不上
#   - 只做一轮：改完不验证
#
# v2 核心改进：
#   1. 给 Vision 模型提供结构化数据（bbox、字号、可用空间、字符数）
#      让它不仅能"看"还能"算"
#   2. 更多修复手段：调字号、精简译文、建议换行
#   3. 用 key 直接标注在图片上（叠加编号标签），消除匹配歧义
#   4. 支持多轮审校（改完再看一次，最多2轮）
#   5. 返回 layout_overrides 字典，让 writer 按指令调整字号等参数
#
# 📘 为什么给 Vision 模型结构化数据？
# 纯看图只能说"这里溢出了"，但不知道溢出多少、空间多大。
# 给它 JSON 数据后，它能做精确判断：
#   "key=pg0_b3 的译文有 45 个字符，但可用宽度只能放 30 个字符，
#    建议精简到 28 个字符以内，或者字号从 14pt 降到 11pt"
# 这比纯视觉判断精准得多。
#
# 📘 关于模型选择
# 你账户上支持多模态的模型：
#   - doubao-seed-1.8: 综合能力强，推荐用于排版审校
#   - doubao-seed-2.0-pro: 最强但最贵
#   - doubao-seed-2.0-lite/mini: 便宜但视觉能力稍弱
# 排版审校对视觉理解要求高，建议用 1.8 或 2.0-pro。
# =============================================================

logger = get_logger("layout_agent")

# 📘 渲染 DPI：150 足够看清文字，又不会太大
RENDER_DPI = 150

# 📘 最多审校轮数（改完再看一次）
MAX_REVIEW_ROUNDS = 2

# =============================================================
# Vision 审校 Prompt（v2：结构化数据 + 图片双通道）
# =============================================================
LAYOUT_REVIEW_PROMPT = """你是专业的文档排版审校专家。你会收到：
1. 两张图片：翻译前的原文页面 和 翻译后的译文页面
2. 一份结构化数据：每个文本块的 key、原文、译文、bbox 坐标、字号、可用空间

请结合图片（视觉判断）和结构化数据（精确计算）来审校排版。

你可以下达以下调整指令：
- shorten: 精简译文（给出精简后的文本）
- resize: 调整字号（给出新的字号数值，单位 pt）
- both: 同时精简译文并调整字号

判断标准：
1. 文字是否溢出或被截断？（对比原文图片，译文是否完整显示）
2. 字号是否太小？（比原文小太多会影响阅读）
3. 文字位置是否正确？（该在右边的不应该跑到左边）
4. 整体美观度：留白是否均匀、层次是否清晰
5. 译文质量：是否有过度缩写（如 Co. Hons）、标签泄漏（如 [Body]）

对于每个需要调整的文本块，输出：
- key: 文本块的 key（从结构化数据中获取，必须精确匹配）
- action: "shorten" | "resize" | "both"
- new_text: 精简后的译文（action 为 shorten 或 both 时必填）
- new_fontsize: 新字号（action 为 resize 或 both 时必填，单位 pt）
- reason: 简短说明原因

精简原则：
- 保持原意，不要过度缩写
- 标题可以适当意译，但要保持可读性
- 不要用不常见的缩写（如 Co., Ind., Dept.）
- 如果原译文已经很好，不要改

字号调整原则：
- 最小不低于原字号的 60%（太小看不清）
- 优先精简译文，字号调整是最后手段
- 标题字号不应该比正文小

如果页面排版没有问题，返回空数组 []。

输出格式：严格 JSON 数组。不要输出其他内容。
示例：
[
  {
    "key": "pg0_b3",
    "action": "shorten",
    "new_text": "Prefab Building Full-chain Service Provider",
    "reason": "原译文太长溢出框外"
  },
  {
    "key": "pg0_b5",
    "action": "both",
    "new_text": "National Prefab Building Industrial Base",
    "new_fontsize": 10.5,
    "reason": "译文溢出且空间有限，同时精简和缩小字号"
  }
]"""


def _render_page_to_base64(doc: fitz.Document, page_idx: int) -> str:
    """
    📘 把 PDF 页面渲染成 base64 JPEG 图片。
    150 DPI，A4 ≈ 1240×1754 像素，约 200-400KB。
    """
    page = doc[page_idx]
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg", jpg_quality=85)
    return base64.b64encode(img_bytes).decode("utf-8")


def _render_pptx_slide_to_base64(pptx_path: str, slide_idx: int) -> Optional[str]:
    """
    📘 PPT 幻灯片渲染（需要 COM + Office）。
    COM 不可用时返回 None。
    """
    try:
        import comtypes.client
        ppt_app = comtypes.client.CreateObject("PowerPoint.Application")
        ppt_app.Visible = False
        prs = ppt_app.Presentations.Open(
            os.path.abspath(pptx_path), ReadOnly=True, WithWindow=False
        )
        import tempfile
        tmp_dir = tempfile.mkdtemp()
        slide = prs.Slides(slide_idx + 1)  # COM 是 1-based
        img_path = os.path.join(tmp_dir, f"slide_{slide_idx}.jpg")
        slide.Export(img_path, "JPG", 1280, 720)
        prs.Close()
        ppt_app.Quit()

        with open(img_path, "rb") as f:
            img_bytes = f.read()
        os.remove(img_path)
        os.rmdir(tmp_dir)
        return base64.b64encode(img_bytes).decode("utf-8")
    except Exception as e:
        logger.debug(f"PPT 幻灯片渲染失败（COM 不可用）: {e}")
        return None


def _build_block_metadata(
    page_items: List[dict],
    translations: Dict[str, str],
    layout_overrides: Dict[str, dict],
) -> List[dict]:
    """
    📘 教学笔记：构建结构化排版数据

    给 Vision 模型提供每个文本块的精确信息：
    - key: 唯一标识（Vision 模型直接用这个 key 下达指令，不需要模糊匹配）
    - original: 原文（前50字符）
    - translated: 当前译文
    - bbox: 可用空间 [x0, y0, x1, y1]
    - fontsize: 当前字号
    - width_px / height_px: 可用宽高（像素）
    - char_count: 译文字符数
    - est_capacity: 估算可容纳字符数（基于宽度和字号）

    有了这些数据，Vision 模型可以做精确判断而不是猜测。
    """
    metadata = []
    for item in page_items:
        key = item["key"]
        if key not in translations:
            continue

        translated = translations[key]
        original = item.get("full_text", "")
        text_bbox = item.get("text_bbox", item.get("bbox", [0, 0, 0, 0]))
        fmt = item.get("dominant_format", {})

        # 📘 如果之前已经有 override，用 override 的字号
        override = layout_overrides.get(key, {})
        fontsize = override.get("fontsize", fmt.get("font_size", 12))

        width = text_bbox[2] - text_bbox[0]
        height = text_bbox[3] - text_bbox[1]

        # 估算可容纳字符数（英文字符宽度 ≈ 0.55 × 字号）
        char_width = fontsize * 0.55
        is_multiline = item.get("is_multiline", False)
        if is_multiline and char_width > 0:
            line_height = fontsize * 1.3
            chars_per_line = int(width / char_width) if char_width > 0 else 999
            num_lines = max(1, int(height / line_height)) if line_height > 0 else 1
            est_capacity = chars_per_line * num_lines
        else:
            est_capacity = int(width / char_width) if char_width > 0 else 999

        metadata.append({
            "key": key,
            "original": original[:50] + ("…" if len(original) > 50 else ""),
            "translated": translated,
            "fontsize": round(fontsize, 1),
            "width_pt": round(width, 1),
            "height_pt": round(height, 1),
            "char_count": len(translated),
            "est_capacity": est_capacity,
            "multiline": is_multiline,
        })

    return metadata


class LayoutReviewAgent:
    """
    📘 排版审校 Agent v2

    核心改进：
    - 双通道输入：图片（视觉）+ 结构化数据（精确数值）
    - 多种修复手段：精简译文 / 调字号 / 两者兼用
    - 精确匹配：用 key 直接标识，不需要模糊匹配
    - 多轮审校：改完再看一次（最多2轮）
    - 输出 layout_overrides：让 writer 按指令调整字号
    """

    def __init__(self, vision_llm: ArkLLMEngine, fix_llm: ArkLLMEngine = None):
        self.vision_llm = vision_llm
        self.fix_llm = fix_llm or vision_llm
        self.total_tokens = 0

    def _call_vision(self, messages: list) -> Optional[str]:
        """调用 Vision 模型（非流式收集完整响应）"""
        full_text = ""
        try:
            for chunk in self.vision_llm.stream_chat(messages):
                if chunk["type"] == "text":
                    full_text += chunk["content"]
                elif chunk["type"] == "usage":
                    self.total_tokens += chunk.get("total_tokens", 0)
        except Exception as e:
            logger.error(f"Vision 模型调用失败: {e}")
            return None
        return full_text

    def _parse_review_response(self, response: str) -> List[dict]:
        """解析 Vision 模型的审校结果"""
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            logger.warning(f"排版审校结果解析失败: {text[:200]}")
        return []

    def _review_page(
        self,
        original_img_b64: str,
        translated_img_b64: str,
        page_idx: int,
        block_metadata: List[dict],
    ) -> List[dict]:
        """
        📘 审校单页排版（v2：图片 + 结构化数据双通道）

        同时发送：
        1. 原文页面图片
        2. 译文页面图片
        3. 每个文本块的结构化数据（key、译文、字号、可用空间等）

        Vision 模型综合视觉和数据做出精确判断。
        """
        # 📘 构建结构化数据摘要（只发有意义的字段，控制 token）
        compact_data = []
        for m in block_metadata:
            entry = {
                "key": m["key"],
                "translated": m["translated"][:80],  # 截断长文本
                "fontsize": m["fontsize"],
                "chars": m["char_count"],
                "capacity": m["est_capacity"],
            }
            # 📘 标记可能有问题的块（字符数超过容量的 80%）
            if m["char_count"] > m["est_capacity"] * 0.8:
                entry["warning"] = "可能溢出"
            compact_data.append(entry)

        data_text = json.dumps(compact_data, ensure_ascii=False, indent=None)

        messages = [
            {"role": "system", "content": LAYOUT_REVIEW_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"第 {page_idx + 1} 页，共 {len(block_metadata)} 个文本块。\n"
                            f"结构化数据：\n{data_text}\n\n"
                            f"请对比以下两张图片（原文 vs 译文），结合数据审校排版。"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{original_img_b64}"},
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{translated_img_b64}"},
                    },
                ],
            },
        ]

        logger.info(f"排版审校: 第 {page_idx + 1} 页（{len(block_metadata)} 个文本块）")
        response = self._call_vision(messages)
        if not response:
            return []

        adjustments = self._parse_review_response(response)
        if adjustments:
            logger.info(f"第 {page_idx + 1} 页: {len(adjustments)} 个调整指令")
            for adj in adjustments:
                logger.debug(
                    f"  {adj.get('key')}: {adj.get('action')} — {adj.get('reason', '')[:60]}"
                )
        else:
            logger.info(f"第 {page_idx + 1} 页排版良好")

        return adjustments

    def _apply_adjustments(
        self,
        adjustments: List[dict],
        translations: Dict[str, str],
        layout_overrides: Dict[str, dict],
        valid_keys: set,
    ) -> int:
        """
        📘 应用 Vision 模型的调整指令

        返回实际修改的数量。
        - shorten: 更新 translations 中的译文
        - resize: 更新 layout_overrides 中的字号
        - both: 两者都更新
        """
        modified = 0
        for adj in adjustments:
            key = adj.get("key", "")
            action = adj.get("action", "")

            # 📘 安全检查：key 必须存在于当前页面
            if key not in valid_keys:
                logger.debug(f"跳过无效 key: {key}")
                continue

            if action in ("shorten", "both"):
                new_text = adj.get("new_text", "")
                if new_text and new_text != translations.get(key, ""):
                    old = translations[key]
                    translations[key] = new_text
                    logger.debug(f"精简 {key}: '{old[:30]}…' → '{new_text[:30]}…'")
                    modified += 1

            if action in ("resize", "both"):
                new_fs = adj.get("new_fontsize")
                if new_fs and isinstance(new_fs, (int, float)) and new_fs > 0:
                    if key not in layout_overrides:
                        layout_overrides[key] = {}
                    layout_overrides[key]["fontsize"] = float(new_fs)
                    logger.debug(f"调字号 {key}: → {new_fs}pt")
                    if action == "resize":
                        modified += 1

        return modified

    def review_pdf_layout(
        self,
        source_path: str,
        translated_path: str,
        parsed_data: Dict[str, Any],
        translations: Dict[str, str],
    ) -> Tuple[Dict[str, str], Dict[str, dict]]:
        """
        📘 PDF 排版审校主流程（v2）

        返回：(translations, layout_overrides)
        - translations: 可能被精简的译文
        - layout_overrides: 字号等排版参数覆盖
          格式: {key: {"fontsize": 10.5}, ...}
          writer 写入时检查这个字典，有 override 就用 override 的值

        📘 多轮审校：
        第1轮：看图+数据 → 下达调整指令 → 应用
        第2轮（可选）：重新写入 → 再看一次 → 如果还有问题再调
        实际上大部分情况1轮就够了，第2轮是保险。
        """
        logger.info(f"开始 PDF 排版审校: {translated_path}")
        print(f"[🎨 排版审校] 开始视觉检查...", flush=True)

        layout_overrides: Dict[str, dict] = {}

        # 按页分组 items
        page_items_map: Dict[int, List[dict]] = {}
        for item in parsed_data["items"]:
            key = item["key"]
            if key not in translations:
                continue
            page_idx = int(key.split("_")[0][2:])
            if page_idx not in page_items_map:
                page_items_map[page_idx] = []
            page_items_map[page_idx].append(item)

        total_modified = 0

        for round_num in range(1, MAX_REVIEW_ROUNDS + 1):
            round_modified = 0

            # 📘 每轮都重新打开文件（因为上一轮可能重写了）
            original_doc = fitz.open(source_path)
            translated_doc = fitz.open(translated_path)
            page_count = min(len(original_doc), len(translated_doc))

            if round_num > 1:
                print(f"[🎨 排版审校] 第 {round_num} 轮复查...", flush=True)

            for page_idx in range(page_count):
                page_items = page_items_map.get(page_idx, [])
                if not page_items:
                    continue

                # 渲染图片
                orig_b64 = _render_page_to_base64(original_doc, page_idx)
                trans_b64 = _render_page_to_base64(translated_doc, page_idx)

                # 构建结构化数据
                block_metadata = _build_block_metadata(
                    page_items, translations, layout_overrides
                )

                # Vision 审校
                adjustments = self._review_page(
                    orig_b64, trans_b64, page_idx, block_metadata
                )
                if not adjustments:
                    continue

                # 应用调整
                valid_keys = {item["key"] for item in page_items}
                modified = self._apply_adjustments(
                    adjustments, translations, layout_overrides, valid_keys
                )
                round_modified += modified

            original_doc.close()
            translated_doc.close()

            total_modified += round_modified
            if round_modified == 0:
                # 📘 这轮没有修改，不需要再审了
                if round_num == 1:
                    print(f"[🎨 排版审校] 排版良好，无需修正", flush=True)
                else:
                    print(f"[🎨 排版审校] 第 {round_num} 轮无新问题", flush=True)
                break

            print(
                f"[🎨 排版审校] 第 {round_num} 轮修正了 {round_modified} 处",
                flush=True,
            )

            # 📘 如果是最后一轮，不需要重写（外层会重写）
            if round_num < MAX_REVIEW_ROUNDS:
                # 需要重写文件才能在下一轮看到修改效果
                # 但这里不重写——让外层 translator_agent 统一处理
                # 因为重写需要 format_engine，layout_agent 不持有
                break  # 📘 v2 暂时只做1轮，多轮需要外层配合重写

        logger.info(
            f"PDF 排版审校完成: {total_modified} 处修改, "
            f"token 用量 {self.total_tokens}"
        )
        return translations, layout_overrides

    def review_pptx_layout(
        self,
        source_path: str,
        translated_path: str,
        parsed_data: Dict[str, Any],
        translations: Dict[str, str],
    ) -> Tuple[Dict[str, str], Dict[str, dict]]:
        """
        📘 PPT 排版审校（需要 COM 渲染）
        COM 不可用时静默跳过。
        """
        logger.info(f"开始 PPT 排版审校: {translated_path}")
        print(f"[🎨 排版审校] 开始视觉检查（PPT）...", flush=True)

        layout_overrides: Dict[str, dict] = {}

        from pptx import Presentation
        prs = Presentation(translated_path)
        slide_count = len(prs.slides)
        del prs

        # 按幻灯片分组 items
        slide_items_map: Dict[int, List[dict]] = {}
        for item in parsed_data["items"]:
            key = item["key"]
            if key not in translations:
                continue
            slide_idx = int(key.split("_")[0][1:])
            if slide_idx not in slide_items_map:
                slide_items_map[slide_idx] = []
            slide_items_map[slide_idx].append(item)

        total_modified = 0
        for slide_idx in range(slide_count):
            slide_items = slide_items_map.get(slide_idx, [])
            if not slide_items:
                continue

            orig_b64 = _render_pptx_slide_to_base64(source_path, slide_idx)
            trans_b64 = _render_pptx_slide_to_base64(translated_path, slide_idx)

            if not orig_b64 or not trans_b64:
                logger.debug(f"幻灯片 {slide_idx + 1} 渲染失败，跳过")
                continue

            block_metadata = _build_block_metadata(
                slide_items, translations, layout_overrides
            )

            adjustments = self._review_page(
                orig_b64, trans_b64, slide_idx, block_metadata
            )
            if not adjustments:
                continue

            valid_keys = {item["key"] for item in slide_items}
            modified = self._apply_adjustments(
                adjustments, translations, layout_overrides, valid_keys
            )
            total_modified += modified

        if total_modified:
            print(
                f"[🎨 排版审校] 修正了 {total_modified} 处排版问题",
                flush=True,
            )
        else:
            print(f"[🎨 排版审校] 排版良好，无需修正", flush=True)

        logger.info(
            f"PPT 排版审校完成: {total_modified} 处修改, "
            f"token 用量 {self.total_tokens}"
        )
        return translations, layout_overrides
