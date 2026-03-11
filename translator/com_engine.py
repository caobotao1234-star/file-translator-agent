# translator/com_engine.py
import os
import sys
from typing import List, Dict, Any, Optional, Tuple
from core.logger import get_logger

# =============================================================
# 📘 教学笔记：Office COM 增强引擎
# =============================================================
# python-docx 能处理段落和表格，但搞不定：
#   - 图表（Chart）的标题、坐标轴标签、图例
#   - 文本框（TextBox）里的文字
#   - SmartArt 里的文字
#
# 这些元素需要通过 Windows COM 接口调用 Word 应用程序来操作。
#
# 设计思路：
#   - 自动检测 COM 环境是否可用（有没有装 Office/WPS）
#   - 可用 → 开启增强模式，处理图表/文本框/SmartArt
#   - 不可用 → 静默降级，只用 python-docx 处理段落+表格
#   - 用户无需任何配置，启动时自动探测并告知
#
# COM 操作的基本流程：
#   1. 启动 Word 应用（后台不可见）
#   2. 打开文档
#   3. 遍历 Shapes（文本框/SmartArt）和 InlineShapes（图表）
#   4. 提取文字 → 翻译 → 写回
#   5. 保存并关闭
# =============================================================

logger = get_logger("com_engine")

# COM 是否可用的全局缓存
_com_available: Optional[bool] = None


def is_com_available() -> bool:
    """
    检测 Office COM 环境是否可用。
    结果会缓存，只检测一次。
    """
    global _com_available
    if _com_available is not None:
        return _com_available

    # 非 Windows 直接不可用
    if sys.platform != "win32":
        _com_available = False
        logger.info("非 Windows 系统，COM 增强不可用")
        return False

    try:
        import win32com.client
        # 尝试创建 Word 应用对象（不实际启动窗口）
        word = win32com.client.DispatchEx("Word.Application")
        word.Quit()
        _com_available = True
        logger.info("Office COM 环境检测通过，增强模式可用")
    except Exception as e:
        _com_available = False
        logger.info(f"Office COM 环境不可用: {e}")

    return _com_available


def extract_extra_texts(filepath: str) -> List[Dict[str, Any]]:
    """
    通过 COM 接口提取 python-docx 无法处理的文本元素。

    提取对象：
    - 文本框（TextBox）
    - SmartArt 文字
    - 图表标题、坐标轴标签、图例

    返回格式：
    [
        {"key": "shape_0", "type": "textbox", "text": "文本框内容"},
        {"key": "chart_0_title", "type": "chart", "text": "图表标题"},
        ...
    ]
    """
    if not is_com_available():
        return []

    import win32com.client
    from pywintypes import com_error

    abs_path = os.path.abspath(filepath)
    items = []

    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        doc = word.Documents.Open(abs_path, ReadOnly=True)

        # ---- 1. 遍历 Shapes（文本框、SmartArt 等浮动对象）----
        for i, shape in enumerate(doc.Shapes):
            try:
                if shape.TextFrame.HasText:
                    text = shape.TextFrame.TextRange.Text.strip()
                    if text:
                        items.append({
                            "key": f"shape_{i}",
                            "type": "textbox",
                            "text": text,
                        })
            except com_error:
                pass  # 有些 Shape 没有 TextFrame

            # SmartArt 节点
            try:
                if shape.HasSmartArt:
                    for j, node in enumerate(shape.SmartArt.AllNodes):
                        node_text = node.TextFrame2.TextRange.Text.strip()
                        if node_text:
                            items.append({
                                "key": f"smartart_{i}_{j}",
                                "type": "smartart",
                                "text": node_text,
                            })
            except (com_error, AttributeError):
                pass

            # 嵌入图表（Shape 类型）
            try:
                if shape.HasChart:
                    chart = shape.Chart
                    _extract_chart_texts(chart, f"shape_chart_{i}", items)
            except (com_error, AttributeError):
                pass

        # ---- 2. 遍历 InlineShapes（内联图表等）----
        for i, ishape in enumerate(doc.InlineShapes):
            try:
                if ishape.HasChart:
                    chart = ishape.Chart
                    _extract_chart_texts(chart, f"chart_{i}", items)
            except (com_error, AttributeError):
                pass

        logger.info(f"COM 提取完成: {len(items)} 个额外文本元素")

    except Exception as e:
        logger.error(f"COM 提取失败: {e}")
    finally:
        try:
            if doc:
                doc.Close(False)
            if word:
                word.Quit()
        except Exception:
            pass

    return items


def _extract_chart_texts(chart, prefix: str, items: List[Dict]):
    """从一个图表对象中提取可翻译的文字"""
    from pywintypes import com_error

    # 图表标题
    try:
        if chart.HasTitle:
            title_text = chart.ChartTitle.Text.strip()
            if title_text:
                items.append({
                    "key": f"{prefix}_title",
                    "type": "chart_title",
                    "text": title_text,
                })
    except (com_error, AttributeError):
        pass

    # 坐标轴标题
    for axis_group in [1]:  # xlPrimary = 1
        for axis_type in [1, 2]:  # xlCategory=1, xlValue=2
            try:
                axis = chart.Axes(axis_type, axis_group)
                if axis.HasTitle:
                    axis_text = axis.AxisTitle.Text.strip()
                    if axis_text:
                        items.append({
                            "key": f"{prefix}_axis_{axis_type}",
                            "type": "chart_axis",
                            "text": axis_text,
                        })
            except (com_error, AttributeError):
                pass

    # 图例（整体文本，不逐条拆）
    try:
        if chart.HasLegend:
            # 图例的各条目
            for j in range(1, chart.Legend.LegendEntries().Count + 1):
                try:
                    entry_text = chart.Legend.LegendEntries(j).LegendKey.Name
                    # LegendKey.Name 不一定有文字，跳过
                except (com_error, AttributeError):
                    pass
    except (com_error, AttributeError):
        pass

def write_extra_texts(filepath: str, translated_items: List[Dict[str, Any]]) -> int:
    """
    通过 COM 接口将翻译后的文本写回文档中的图表/文本框/SmartArt。

    📘 教学笔记：
    写回的核心思路是"按 key 定位 → 替换文本"。
    key 在 extract_extra_texts() 中生成，格式如：
      - shape_0          → doc.Shapes(1) 的 TextFrame
      - smartart_2_1     → doc.Shapes(3) 的 SmartArt 第2个节点
      - chart_0_title    → doc.InlineShapes(1) 的图表标题
      - shape_chart_1_axis_2 → doc.Shapes(2) 的图表坐标轴

    注意 COM 的索引从 1 开始，而我们的 key 里用的是 0-based。

    参数：
        filepath: 要写回的 docx 文件路径（会直接修改此文件）
        translated_items: extract_extra_texts() 返回的列表，
                          但每项多了 "translated" 字段

    返回：成功写回的元素数量
    """
    if not is_com_available():
        return 0

    if not translated_items:
        return 0

    import win32com.client
    from pywintypes import com_error

    abs_path = os.path.abspath(filepath)
    word = None
    doc = None
    written = 0

    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        doc = word.Documents.Open(abs_path)

        for item in translated_items:
            key = item["key"]
            translated = item.get("translated", "")
            if not translated:
                continue

            try:
                if key.startswith("shape_chart_"):
                    # 浮动图表：shape_chart_{shape_idx}_{sub}
                    written += _write_chart_text(doc.Shapes, key, "shape_chart_", translated)

                elif key.startswith("chart_"):
                    # 内联图表：chart_{ishape_idx}_{sub}
                    written += _write_chart_text(doc.InlineShapes, key, "chart_", translated)

                elif key.startswith("smartart_"):
                    # smartart_{shape_idx}_{node_idx}
                    parts = key.split("_")
                    shape_idx = int(parts[1])
                    node_idx = int(parts[2])
                    shape = doc.Shapes(shape_idx + 1)  # COM 1-based
                    shape.SmartArt.AllNodes(node_idx + 1).TextFrame2.TextRange.Text = translated
                    written += 1

                elif key.startswith("shape_"):
                    # 文本框：shape_{idx}
                    shape_idx = int(key.split("_")[1])
                    shape = doc.Shapes(shape_idx + 1)  # COM 1-based
                    shape.TextFrame.TextRange.Text = translated
                    written += 1

                logger.debug(f"写回成功: {key}")

            except (com_error, AttributeError, IndexError) as e:
                logger.warning(f"写回失败 key={key}: {e}")

        doc.Save()
        logger.info(f"COM 写回完成: {written}/{len(translated_items)} 个元素")

    except Exception as e:
        logger.error(f"COM 写回失败: {e}")
    finally:
        try:
            if doc:
                doc.Close(False)
            if word:
                word.Quit()
        except Exception:
            pass

    return written


def _write_chart_text(shapes_collection, key: str, prefix: str, translated: str) -> int:
    """
    写回图表文本（标题、坐标轴）。

    key 格式示例：
      chart_0_title     → InlineShapes(1) 的图表标题
      chart_0_axis_1    → InlineShapes(1) 的 xlCategory 轴标题
      shape_chart_2_title → Shapes(3) 的图表标题
    """
    from pywintypes import com_error

    # 解析 key：去掉 prefix，剩下 {idx}_{sub_type}[_{sub_idx}]
    remainder = key[len(prefix):]  # e.g. "0_title" or "0_axis_2"
    parts = remainder.split("_")
    shape_idx = int(parts[0])
    sub_type = parts[1] if len(parts) > 1 else ""

    try:
        shape = shapes_collection(shape_idx + 1)  # COM 1-based
        chart = shape.Chart if hasattr(shape, 'Chart') else shape.Chart
    except (com_error, AttributeError):
        return 0

    try:
        if sub_type == "title":
            chart.ChartTitle.Text = translated
            return 1
        elif sub_type == "axis":
            axis_type = int(parts[2]) if len(parts) > 2 else 1
            axis = chart.Axes(axis_type, 1)  # xlPrimary = 1
            axis.AxisTitle.Text = translated
            return 1
    except (com_error, AttributeError) as e:
        logger.warning(f"图表文本写回失败 {key}: {e}")

    return 0



def write_extra_texts(filepath: str, translated_items: List[Dict[str, Any]]) -> int:
    """
    通过 COM 接口将翻译后的文本写回文档中的图表/文本框/SmartArt。

    📘 教学笔记：
    写回的核心思路是"按 key 定位 → 替换文本"。
    key 在 extract_extra_texts() 中生成，格式如：
      - shape_0          → doc.Shapes(1) 的 TextFrame
      - smartart_2_1     → doc.Shapes(3) 的 SmartArt 第2个节点
      - chart_0_title    → doc.InlineShapes(1) 的图表标题
      - shape_chart_1_axis_2 → doc.Shapes(2) 的图表坐标轴

    注意 COM 的索引从 1 开始，而我们的 key 里用的是 0-based。

    参数：
        filepath: 要写回的 docx 文件路径（会直接修改此文件）
        translated_items: extract_extra_texts() 返回的列表，
                          但每项多了 "translated" 字段

    返回：成功写回的元素数量
    """
    if not is_com_available():
        return 0

    if not translated_items:
        return 0

    import win32com.client
    from pywintypes import com_error

    abs_path = os.path.abspath(filepath)
    word = None
    doc = None
    written = 0

    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        doc = word.Documents.Open(abs_path)

        for item in translated_items:
            key = item["key"]
            translated = item.get("translated", "")
            if not translated:
                continue

            try:
                if key.startswith("shape_chart_"):
                    # 浮动图表：shape_chart_{shape_idx}_{sub}
                    written += _write_chart_text(doc.Shapes, key, "shape_chart_", translated)

                elif key.startswith("chart_"):
                    # 内联图表：chart_{ishape_idx}_{sub}
                    written += _write_chart_text(doc.InlineShapes, key, "chart_", translated)

                elif key.startswith("smartart_"):
                    # smartart_{shape_idx}_{node_idx}
                    parts = key.split("_")
                    shape_idx = int(parts[1])
                    node_idx = int(parts[2])
                    shape = doc.Shapes(shape_idx + 1)  # COM 1-based
                    shape.SmartArt.AllNodes(node_idx + 1).TextFrame2.TextRange.Text = translated
                    written += 1

                elif key.startswith("shape_"):
                    # 文本框：shape_{idx}
                    shape_idx = int(key.split("_")[1])
                    shape = doc.Shapes(shape_idx + 1)  # COM 1-based
                    shape.TextFrame.TextRange.Text = translated
                    written += 1

                logger.debug(f"写回成功: {key}")

            except (com_error, AttributeError, IndexError) as e:
                logger.warning(f"写回失败 key={key}: {e}")

        doc.Save()
        logger.info(f"COM 写回完成: {written}/{len(translated_items)} 个元素")

    except Exception as e:
        logger.error(f"COM 写回失败: {e}")
    finally:
        try:
            if doc:
                doc.Close(False)
            if word:
                word.Quit()
        except Exception:
            pass

    return written


def _write_chart_text(shapes_collection, key: str, prefix: str, translated: str) -> int:
    """
    写回图表文本（标题、坐标轴）。

    key 格式示例：
      chart_0_title     → InlineShapes(1) 的图表标题
      chart_0_axis_1    → InlineShapes(1) 的 xlCategory 轴标题
      shape_chart_2_title → Shapes(3) 的图表标题
    """
    from pywintypes import com_error

    # 解析 key：去掉 prefix，剩下 {idx}_{sub_type}[_{sub_idx}]
    remainder = key[len(prefix):]  # e.g. "0_title" or "0_axis_2"
    parts = remainder.split("_")
    shape_idx = int(parts[0])
    sub_type = parts[1] if len(parts) > 1 else ""

    try:
        shape = shapes_collection(shape_idx + 1)  # COM 1-based
        chart = shape.Chart
    except (com_error, AttributeError):
        return 0

    try:
        if sub_type == "title":
            chart.ChartTitle.Text = translated
            return 1
        elif sub_type == "axis":
            axis_type = int(parts[2]) if len(parts) > 2 else 1
            axis = chart.Axes(axis_type, 1)  # xlPrimary = 1
            axis.AxisTitle.Text = translated
            return 1
    except (com_error, AttributeError) as e:
        logger.warning(f"图表文本写回失败 {key}: {e}")

    return 0
