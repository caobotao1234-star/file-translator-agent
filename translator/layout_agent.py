# translator/layout_agent.py
import json
import os
import base64
import fitz  # PyMuPDF
from typing import Dict, Any, List, Optional, Tuple
from core.llm_engine import ArkLLMEngine
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：排版审校 Agent（Layout Review Agent）
# =============================================================
# 翻译完成后，最费人力的不是改译文，而是调排版。
# 英文比中文长 30-80%，塞回原位经常溢出、错位、字号太小。
#
# 这个 Agent 用多模态 LLM（Vision 模型）来自动审校排版：
#   1. 把翻译后的每页渲染成图片
#   2. 连同原文页面图片一起发给 Vision 模型
#   3. Vision 模型"看图"找出排版问题：
#      - 文字溢出/被截断
#      - 字号太小看不清
#      - 文字位置偏移（该在右边的跑到左边）
#      - 文字重叠
#      - 整体美观度差
#   4. 模型输出结构化的调整指令（JSON）
#   5. 工程代码根据指令修改译文（精简/缩写）
#   6. 重新写入文件
#
# 📘 为什么用多模态而不是纯规则？
# 规则只能检查"字符数超了"，但排版问题远不止这些：
#   - 视觉平衡感（左右对称、留白均匀）
#   - 字体搭配是否和谐
#   - 标题和正文的层次感
#   - 图文关系是否合理
# 这些都是"看一眼就知道"但很难用规则描述的问题。
# Vision 模型恰好擅长这类视觉判断。
#
# 📘 成本控制
# Vision 模型比纯文本模型贵，所以我们：
#   - 只在翻译完成后跑一次（不是每个 batch 都跑）
#   - 每页一张图，分辨率适中（150 DPI，够看清文字）
#   - 只对有问题的页面做二次修改
#   - 用户可以在 GUI 里开关此功能
# =============================================================

logger = get_logger("layout_agent")

# 📘 渲染 DPI：150 足够看清文字，又不会太大
# A4 页面 150 DPI ≈ 1240×1754 像素，约 200-400KB JPEG
RENDER_DPI = 150

LAYOUT_REVIEW_PROMPT = """你是专业的文档排版审校专家。你会收到两张图片：
1. 第一张是翻译前的原文页面
2. 第二张是翻译后的译文页面

请仔细对比两张图片，找出译文页面中的排版问题。

常见问题类型：
- overflow: 文字溢出框外或被截断，看不到完整内容
- too_small: 字号被压缩得太小，明显比原文小很多，难以阅读
- misaligned: 文字位置偏移，比如原文在右侧的内容跑到了左侧
- overlapping: 文字之间重叠，互相遮挡
- ugly_linebreak: 换行位置不合理，单词被截断或一行只有一两个字

对于每个发现的问题，请给出：
- problem_type: 问题类型（上述之一）
- location: 问题在页面上的大致位置描述（如"右上角标题"、"页面底部第二段"）
- original_text: 原文中对应的文字（如果能看清的话）
- translated_text: 当前的译文（如果能看清的话）
- suggestion: 具体的修改建议，比如"缩短为 XXX"或"这段可以精简"
- severity: 严重程度 high/medium/low

如果页面排版没有明显问题，返回空数组。

输出格式：严格 JSON 数组，每个元素是一个问题对象。不要输出其他内容。
示例：
[
  {
    "problem_type": "overflow",
    "location": "右上角标题区域",
    "original_text": "装配式建筑全生态产业链服务商",
    "translated_text": "Full Ecological Industrial Chain Service Provider of Prefabricated Buildings",
    "suggestion": "缩短为 'Prefab Building Full-chain Provider'",
    "severity": "high"
  }
]"""

LAYOUT_FIX_PROMPT = """你是翻译精简专家。以下译文存在排版问题（太长导致溢出或字号过小）。
请根据问题描述精简译文，使其更短但保持原意。

要求：
1. 输入N条，输出必须恰好N个元素的JSON数组
2. 每条输出精简后的译文字符串
3. 优先使用缩写、省略次要修饰词、用更短的同义词
4. 标题类文字要简洁有力
5. 如果原译文没问题（severity=low），保持原样

输出：严格JSON数组，每个元素是精简后的译文。"""


def _render_page_to_base64(doc: fitz.Document, page_idx: int) -> str:
    """
    📘 教学笔记：把 PDF 页面渲染成 base64 图片

    PyMuPDF 的 page.get_pixmap() 可以把任意页面渲染成位图。
    我们用 JPEG 格式（比 PNG 小很多），quality=85 平衡清晰度和大小。
    然后 base64 编码，直接嵌入 API 请求的 image_url 字段。
    """
    page = doc[page_idx]
    # 📘 zoom = DPI / 72（PyMuPDF 默认 72 DPI）
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)
    img_bytes = pix.tobytes("jpeg", jpg_quality=85)
    return base64.b64encode(img_bytes).decode("utf-8")


def _render_pptx_slide_to_base64(pptx_path: str, slide_idx: int) -> Optional[str]:
    """
    📘 教学笔记：PPT 幻灯片渲染

    python-pptx 没有渲染能力，需要借助外部工具。
    方案：用 COM（Windows + Office）导出为图片。
    如果 COM 不可用，返回 None（跳过视觉审校）。
    """
    try:
        import comtypes.client
        ppt_app = comtypes.client.CreateObject("PowerPoint.Application")
        ppt_app.Visible = False
        prs = ppt_app.Presentations.Open(
            os.path.abspath(pptx_path), ReadOnly=True, WithWindow=False
        )
        import tempfile, os
        tmp_dir = tempfile.mkdtemp()
        # 导出单张幻灯片为 JPEG
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



class LayoutReviewAgent:
    """
    📘 排版审校 Agent：用 Vision 模型看图找排版问题，自动精简译文。

    工作流程：
    1. review_pdf_layout(): PDF 专用，逐页渲染+审校
    2. review_pptx_layout(): PPT 专用，需要 COM 渲染
    3. 内部调用 _review_page() 做单页审校
    4. 发现问题后调用 _fix_translations() 精简译文
    5. 返回修改后的 translations dict
    """

    def __init__(self, vision_llm: ArkLLMEngine, fix_llm: ArkLLMEngine = None):
        """
        参数：
            vision_llm: 多模态 Vision 模型（如 doubao-1.5-vision-pro-32k）
            fix_llm: 用于精简译文的文本模型（可复用初翻/审校模型）
                     如果为 None，则用 vision_llm 兼任
        """
        self.vision_llm = vision_llm
        self.fix_llm = fix_llm or vision_llm
        self.total_tokens = 0

    def _call_vision(self, messages: list) -> Optional[str]:
        """
        📘 调用 Vision 模型（非流式收集完整响应）

        Vision 模型的 messages 格式和普通文本模型一样，
        只是 content 字段可以是数组，包含 text 和 image_url 类型。
        Ark SDK 兼容 OpenAI 格式，直接用 stream_chat 就行。
        """
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

    def _call_fix_llm(self, prompt: str) -> Optional[List[str]]:
        """调用文本模型精简译文"""
        messages = [
            {"role": "system", "content": LAYOUT_FIX_PROMPT},
            {"role": "user", "content": prompt},
        ]
        full_text = ""
        try:
            for chunk in self.fix_llm.stream_chat(messages):
                if chunk["type"] == "text":
                    full_text += chunk["content"]
                elif chunk["type"] == "usage":
                    self.total_tokens += chunk.get("total_tokens", 0)
        except Exception as e:
            logger.error(f"精简模型调用失败: {e}")
            return None

        # 解析 JSON
        text = full_text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return [str(item) for item in result]
        except json.JSONDecodeError:
            logger.warning(f"精简结果 JSON 解析失败: {text[:200]}")
        return None

    def _review_page(
        self,
        original_img_b64: str,
        translated_img_b64: str,
        page_idx: int,
    ) -> List[dict]:
        """
        📘 审校单页排版

        发送原文+译文两张图片给 Vision 模型，
        让它对比找出排版问题。

        返回问题列表（可能为空 = 没问题）。
        """
        messages = [
            {"role": "system", "content": LAYOUT_REVIEW_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"这是第 {page_idx + 1} 页。请对比原文和译文的排版。"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{original_img_b64}"
                        },
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{translated_img_b64}"
                        },
                    },
                ],
            },
        ]

        logger.info(f"排版审校: 第 {page_idx + 1} 页")
        response = self._call_vision(messages)
        if not response:
            return []

        # 解析 JSON
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            problems = json.loads(text)
            if isinstance(problems, list):
                high_medium = [p for p in problems if p.get("severity") in ("high", "medium")]
                if high_medium:
                    logger.info(
                        f"第 {page_idx + 1} 页发现 {len(high_medium)} 个排版问题"
                        f"（共 {len(problems)} 个）"
                    )
                    for p in high_medium:
                        logger.debug(
                            f"  [{p.get('severity')}] {p.get('problem_type')}: "
                            f"{p.get('location')} — {p.get('suggestion', '')[:60]}"
                        )
                else:
                    logger.info(f"第 {page_idx + 1} 页排版良好")
                return problems
        except json.JSONDecodeError:
            logger.warning(f"排版审校结果解析失败: {text[:200]}")
        return []

    def _match_problems_to_keys(
        self,
        problems: List[dict],
        page_items: List[dict],
        translations: Dict[str, str],
    ) -> List[Tuple[str, dict, str]]:
        """
        📘 教学笔记：把 Vision 模型发现的问题匹配到具体的翻译 key

        Vision 模型输出的是"位置描述"（如"右上角标题"）和"当前译文"，
        我们需要把它匹配到 translations 字典里的具体 key。

        匹配策略：
        1. 如果模型给出了 translated_text，用模糊匹配找最相似的 key
        2. 如果没有，用 original_text 匹配原文
        3. 匹配不上就跳过（宁可漏改也不要改错）
        """
        matched = []
        for problem in problems:
            if problem.get("severity") == "low":
                continue

            translated_text = problem.get("translated_text", "")
            original_text = problem.get("original_text", "")
            best_key = None
            best_score = 0

            for item in page_items:
                key = item["key"]
                if key not in translations:
                    continue

                # 尝试匹配译文
                if translated_text:
                    trans = translations[key]
                    score = _fuzzy_match_score(translated_text, trans)
                    if score > best_score:
                        best_score = score
                        best_key = key

                # 尝试匹配原文
                if original_text:
                    orig = item.get("full_text", "")
                    score = _fuzzy_match_score(original_text, orig)
                    if score > best_score:
                        best_score = score
                        best_key = key

            if best_key and best_score > 0.3:
                matched.append((best_key, problem, translations[best_key]))
                logger.debug(
                    f"匹配问题 → key={best_key} (score={best_score:.2f}): "
                    f"{problem.get('problem_type')}"
                )
            else:
                logger.debug(
                    f"未匹配到 key: {problem.get('location')} "
                    f"— {problem.get('translated_text', '')[:30]}"
                )

        return matched

    def _fix_translations(
        self,
        matched_problems: List[Tuple[str, dict, str]],
        translations: Dict[str, str],
    ) -> Dict[str, str]:
        """
        📘 把有问题的译文发给 LLM 精简，更新 translations

        只修改有问题的 key，其他保持不变。
        """
        if not matched_problems:
            return translations

        # 构建精简请求
        fix_items = []
        keys_to_fix = []
        for key, problem, current_trans in matched_problems:
            fix_items.append({
                "当前译文": current_trans,
                "问题类型": problem.get("problem_type", ""),
                "建议": problem.get("suggestion", ""),
                "严重程度": problem.get("severity", "medium"),
            })
            keys_to_fix.append(key)

        n = len(fix_items)
        prompt = (
            f"以下 {n} 条译文存在排版问题，请逐条精简。"
            f"输出恰好 {n} 个元素的 JSON 数组。\n"
            f"问题列表：{json.dumps(fix_items, ensure_ascii=False)}"
        )

        print(f"  [🔧 排版修正] 精简 {n} 条译文...", flush=True)
        fixed = self._call_fix_llm(prompt)

        if fixed and len(fixed) == n:
            for key, new_trans in zip(keys_to_fix, fixed):
                old = translations[key]
                if new_trans != old:
                    logger.debug(f"排版修正 {key}: '{old[:30]}' → '{new_trans[:30]}'")
                    translations[key] = new_trans
            print(f"  [✅ 排版修正完成] {n} 条译文已精简", flush=True)
        else:
            logger.warning(f"排版修正失败: 期望 {n} 条，得到 {len(fixed) if fixed else 0} 条")
            print(f"  [⚠️ 排版修正失败] 保持原译文", flush=True)

        return translations

    def review_pdf_layout(
        self,
        source_path: str,
        translated_path: str,
        parsed_data: Dict[str, Any],
        translations: Dict[str, str],
    ) -> Dict[str, str]:
        """
        📘 PDF 排版审校主流程

        1. 打开原文和译文 PDF
        2. 逐页渲染成图片
        3. 发给 Vision 模型审校
        4. 匹配问题到具体 key
        5. 精简有问题的译文
        6. 返回修改后的 translations
        """
        logger.info(f"开始 PDF 排版审校: {translated_path}")
        print(f"[🎨 排版审校] 开始视觉检查...", flush=True)

        original_doc = fitz.open(source_path)
        translated_doc = fitz.open(translated_path)

        page_count = min(len(original_doc), len(translated_doc))
        all_problems = []

        # 按页分组 items
        page_items_map: Dict[int, List[dict]] = {}
        for item in parsed_data["items"]:
            key = item["key"]
            # 从 key 提取页码: pg0_b1 → 0, pg0_b1s0 → 0
            page_idx = int(key.split("_")[0][2:])
            if page_idx not in page_items_map:
                page_items_map[page_idx] = []
            page_items_map[page_idx].append(item)

        for page_idx in range(page_count):
            # 渲染两张图片
            orig_b64 = _render_page_to_base64(original_doc, page_idx)
            trans_b64 = _render_page_to_base64(translated_doc, page_idx)

            # Vision 审校
            problems = self._review_page(orig_b64, trans_b64, page_idx)
            if not problems:
                continue

            # 匹配到具体 key
            page_items = page_items_map.get(page_idx, [])
            matched = self._match_problems_to_keys(problems, page_items, translations)
            all_problems.extend(matched)

        original_doc.close()
        translated_doc.close()

        # 精简有问题的译文
        if all_problems:
            print(
                f"[🎨 排版审校] 发现 {len(all_problems)} 个需要修正的问题",
                flush=True,
            )
            translations = self._fix_translations(all_problems, translations)
        else:
            print(f"[🎨 排版审校] 排版良好，无需修正", flush=True)

        logger.info(
            f"PDF 排版审校完成: {len(all_problems)} 个问题, "
            f"token 用量 {self.total_tokens}"
        )
        return translations

    def review_pptx_layout(
        self,
        source_path: str,
        translated_path: str,
        parsed_data: Dict[str, Any],
        translations: Dict[str, str],
    ) -> Dict[str, str]:
        """
        📘 PPT 排版审校

        PPT 需要 COM 渲染幻灯片为图片。
        如果 COM 不可用，静默跳过。
        """
        logger.info(f"开始 PPT 排版审校: {translated_path}")
        print(f"[🎨 排版审校] 开始视觉检查（PPT）...", flush=True)

        # 检测幻灯片数量
        from pptx import Presentation
        prs = Presentation(translated_path)
        slide_count = len(prs.slides)
        del prs

        # 按幻灯片分组 items
        slide_items_map: Dict[int, List[dict]] = {}
        for item in parsed_data["items"]:
            key = item["key"]
            # s0_sh1_p0 → slide 0
            slide_idx = int(key.split("_")[0][1:])
            if slide_idx not in slide_items_map:
                slide_items_map[slide_idx] = []
            slide_items_map[slide_idx].append(item)

        all_problems = []
        for slide_idx in range(slide_count):
            orig_b64 = _render_pptx_slide_to_base64(source_path, slide_idx)
            trans_b64 = _render_pptx_slide_to_base64(translated_path, slide_idx)

            if not orig_b64 or not trans_b64:
                logger.debug(f"幻灯片 {slide_idx + 1} 渲染失败，跳过")
                continue

            problems = self._review_page(orig_b64, trans_b64, slide_idx)
            if not problems:
                continue

            slide_items = slide_items_map.get(slide_idx, [])
            matched = self._match_problems_to_keys(problems, slide_items, translations)
            all_problems.extend(matched)

        if all_problems:
            print(
                f"[🎨 排版审校] 发现 {len(all_problems)} 个需要修正的问题",
                flush=True,
            )
            translations = self._fix_translations(all_problems, translations)
        else:
            print(f"[🎨 排版审校] 排版良好，无需修正", flush=True)

        logger.info(
            f"PPT 排版审校完成: {len(all_problems)} 个问题, "
            f"token 用量 {self.total_tokens}"
        )
        return translations


def _fuzzy_match_score(query: str, target: str) -> float:
    """
    📘 简单的模糊匹配评分

    计算 query 和 target 之间的相似度（0~1）。
    用字符级别的 Jaccard 相似度 + 子串匹配加分。
    """
    if not query or not target:
        return 0.0

    # 清理
    q = query.strip().lower()
    t = target.strip().lower()

    # 完全匹配
    if q == t:
        return 1.0

    # 子串匹配（query 是 target 的子串，或反过来）
    if q in t or t in q:
        shorter = min(len(q), len(t))
        longer = max(len(q), len(t))
        return 0.5 + 0.5 * (shorter / longer)

    # Jaccard（字符级）
    set_q = set(q)
    set_t = set(t)
    intersection = len(set_q & set_t)
    union = len(set_q | set_t)
    if union == 0:
        return 0.0
    return intersection / union
