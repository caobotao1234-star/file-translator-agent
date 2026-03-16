# translator_gui.py — 翻译 Agent 图形界面
# =============================================================
# 📘 教学笔记：为什么用 GUI？
# =============================================================
# 命令行界面对开发者友好，但对普通用户不友好。
# GUI 的优势：
#   - 拖拽文件，不用手打路径
#   - 下拉框选模型/语言，不用记命令
#   - 进度条直观，不用盯着日志
#   - 日志区域可滚动、可搜索，比终端好用
#
# 架构要点：
#   - 翻译在 QThread 中执行，不阻塞 UI
#   - 通过 Signal 把日志/进度从工作线程传回主线程
#   - 重定向 print() 和 logging 到 GUI 日志区域
# =============================================================

import sys
import os
import logging

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QTextEdit, QProgressBar,
    QFileDialog, QGroupBox, QSpinBox, QSplitter, QListWidget,
    QListWidgetItem, QAbstractItemView, QStatusBar, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QLineEdit,
    QDialog, QDialogButtonBox, QFormLayout, QCheckBox,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMimeData
from PyQt6.QtGui import QFont, QDragEnterEvent, QDropEvent, QColor, QIcon

from translator.format_engine import FormatEngine

from config.settings import Config
from translator.translator_agent import TranslatorAgent
from core.logger import TRACE


# =============================================================
# 支持的语言列表
# =============================================================
SUPPORTED_LANGS = [
    ("中文", "Chinese"), ("英文", "English"), ("日文", "Japanese"), ("韩文", "Korean"),
    ("法文", "French"), ("德文", "German"), ("西班牙文", "Spanish"), ("俄文", "Russian"),
]


# =============================================================
# 翻译工作线程
# =============================================================
class TranslateWorker(QThread):
    """
    📘 教学笔记：为什么用 QThread？
    翻译是耗时操作（网络请求），如果在主线程跑，UI 会卡死。
    QThread 让翻译在后台执行，通过 Signal 把结果传回主线程。
    """
    log_signal = pyqtSignal(str, str)       # (消息, 级别)
    progress_signal = pyqtSignal(int, int)  # (已完成, 总数)
    # 📘 v2: 拆分初翻/审校 token 用量
    # (draft_tokens, review_tokens, total_tokens)
    token_signal = pyqtSignal(int, int, int)
    finished_signal = pyqtSignal(str)       # 输出文件路径
    error_signal = pyqtSignal(str)          # 错误信息

    def __init__(self, agent: TranslatorAgent, files: list, target_lang: str):
        super().__init__()
        self.agent = agent
        self.files = files
        self.target_lang = target_lang

    def _emit_token_usage(self):
        """📘 从 pipeline 的 Agent 池中汇总 token 用量（含排版审校）"""
        pipeline = self.agent.pipeline
        draft_t = pipeline.total_draft_tokens
        review_t = pipeline.total_review_tokens
        # 📘 排版审校 Agent 的 token 也计入审校
        if self.agent.layout_agent:
            review_t += self.agent.layout_agent.total_tokens
        self.token_signal.emit(draft_t, review_t, draft_t + review_t)

    def run(self):
        for filepath in self.files:
            # 📘 优雅停止：如果上一个文件翻译时被停止了，不再处理后续文件
            if self.agent.pipeline.is_stopped:
                break
            try:
                self.log_signal.emit(f"开始翻译: {os.path.basename(filepath)}", "info")
                output = self.agent.translate_file(
                    filepath,
                    target_lang=self.target_lang,
                )
                self._emit_token_usage()
                self.finished_signal.emit(output)
            except Exception as e:
                self._emit_token_usage()
                self.error_signal.emit(f"{os.path.basename(filepath)}: {e}")


# =============================================================
# 日志拦截器：把 print() 和 logging 重定向到 GUI
# =============================================================
class LogInterceptor(logging.Handler):
    """拦截 logging 输出，转发到 GUI 日志区域"""
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        msg = self.format(record)
        level = record.levelname.lower()
        self.signal.emit(msg, level)


class PrintInterceptor:
    """拦截 print() 输出，转发到 GUI 日志区域"""
    def __init__(self, signal, original_stdout):
        self.signal = signal
        self.original = original_stdout

    def write(self, text):
        if text.strip():
            self.signal.emit(text.strip(), "print")

    def flush(self):
        pass


# =============================================================
# 样式表
# =============================================================
STYLESHEET = """
QMainWindow {
    background-color: #1e1e2e;
}
QWidget {
    color: #cdd6f4;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 13px;
}
QGroupBox {
    border: 1px solid #45475a;
    border-radius: 8px;
    margin-top: 12px;
    padding: 12px 8px 8px 8px;
    font-weight: bold;
    color: #cdd6f4;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
}
QPushButton {
    background-color: #89b4fa;
    color: #1e1e2e;
    border: none;
    border-radius: 6px;
    padding: 8px 20px;
    font-weight: bold;
    font-size: 13px;
}
QPushButton:hover {
    background-color: #74c7ec;
}
QPushButton:pressed {
    background-color: #89dceb;
}
QPushButton:disabled {
    background-color: #45475a;
    color: #6c7086;
}
QPushButton#stopBtn {
    background-color: #f38ba8;
}
QPushButton#stopBtn:hover {
    background-color: #eba0ac;
}
QComboBox {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
    min-width: 140px;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #313244;
    border: 1px solid #45475a;
    selection-background-color: #585b70;
}
QSpinBox {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
}
QListWidget {
    background-color: #181825;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 4px;
}
QListWidget::item {
    padding: 6px 8px;
    border-radius: 4px;
}
QListWidget::item:selected {
    background-color: #313244;
}
QTextEdit {
    background-color: #11111b;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 8px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 12px;
}
QProgressBar {
    background-color: #313244;
    border: none;
    border-radius: 6px;
    height: 22px;
    text-align: center;
    color: #1e1e2e;
    font-weight: bold;
}
QProgressBar::chunk {
    background-color: #a6e3a1;
    border-radius: 6px;
}
QStatusBar {
    background-color: #181825;
    color: #6c7086;
    border-top: 1px solid #313244;
}
QLabel#titleLabel {
    font-size: 18px;
    font-weight: bold;
    color: #89b4fa;
}
QLabel#subtitleLabel {
    font-size: 11px;
    color: #6c7086;
}
QTabWidget::pane {
    border: 1px solid #45475a;
    border-radius: 6px;
    background-color: #1e1e2e;
}
QTabBar::tab {
    background-color: #313244;
    color: #a6adc8;
    border: 1px solid #45475a;
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    padding: 6px 16px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: #1e1e2e;
    color: #89b4fa;
    font-weight: bold;
}
QTableWidget {
    background-color: #181825;
    border: 1px solid #45475a;
    border-radius: 6px;
    gridline-color: #313244;
}
QTableWidget::item {
    padding: 4px 8px;
}
QTableWidget::item:selected {
    background-color: #313244;
}
QHeaderView::section {
    background-color: #313244;
    color: #cdd6f4;
    border: 1px solid #45475a;
    padding: 6px 8px;
    font-weight: bold;
}
QLineEdit {
    background-color: #313244;
    border: 1px solid #45475a;
    border-radius: 6px;
    padding: 6px 10px;
    color: #cdd6f4;
}
QDialog {
    background-color: #1e1e2e;
}
"""


# =============================================================
# 文件列表（支持拖拽）
# =============================================================
class FileListWidget(QListWidget):
    """支持拖拽添加文件的列表"""
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumHeight(120)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(('.docx', '.pptx', '.pdf')):
                # 避免重复
                exists = any(
                    self.item(i).data(Qt.ItemDataRole.UserRole) == path
                    for i in range(self.count())
                )
                if not exists:
                    item = QListWidgetItem(f"📄 {os.path.basename(path)}")
                    item.setData(Qt.ItemDataRole.UserRole, path)
                    self.addItem(item)

    def get_files(self) -> list:
        return [
            self.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.count())
        ]


# =============================================================
# 格式映射面板（字体 + 样式映射表）
# =============================================================
class FormatMappingPanel(QWidget):
    """
    📘 教学笔记：格式映射 GUI
    翻译不只是文字转换，字体也要跟着变。
    比如中文用"宋体"，翻译成英文后应该用"Times New Roman"。
    这个面板让用户可视化地编辑这些映射规则。
    """

    def __init__(self, format_engine: FormatEngine):
        super().__init__()
        self.engine = format_engine
        self._build_ui()
        self._refresh_tables()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        tabs = QTabWidget()
        layout.addWidget(tabs)

        # ---- Tab 1: 字体映射 ----
        font_tab = QWidget()
        ft_layout = QVBoxLayout(font_tab)

        # 📘 默认字体：所有未在映射表中的字体都映射成这个
        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("默认字体（兜底）:"))
        self.default_font_input = QLineEdit()
        self.default_font_input.setPlaceholderText("留空则保持原字体，如 Times New Roman")
        self.default_font_input.setText(self.engine.default_font)
        default_row.addWidget(self.default_font_input)
        ft_layout.addLayout(default_row)

        self.font_table = QTableWidget(0, 2)
        self.font_table.setHorizontalHeaderLabels(["源字体", "目标字体"])
        self.font_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.font_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.font_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        ft_layout.addWidget(self.font_table)

        ft_btn_row = QHBoxLayout()
        self.btn_font_add = QPushButton("+ 添加")
        self.btn_font_del = QPushButton("- 删除")
        self.btn_font_save = QPushButton("💾 保存")
        ft_btn_row.addWidget(self.btn_font_add)
        ft_btn_row.addWidget(self.btn_font_del)
        ft_btn_row.addStretch()
        ft_btn_row.addWidget(self.btn_font_save)
        ft_layout.addLayout(ft_btn_row)

        tabs.addTab(font_tab, "🔤 字体映射")

        # ---- Tab 2: 样式映射 ----
        style_tab = QWidget()
        st_layout = QVBoxLayout(style_tab)

        self.style_table = QTableWidget(0, 3)
        self.style_table.setHorizontalHeaderLabels(["样式名", "目标字体", "加粗"])
        self.style_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.style_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.style_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        st_layout.addWidget(self.style_table)

        st_btn_row = QHBoxLayout()
        self.btn_style_add = QPushButton("+ 添加")
        self.btn_style_del = QPushButton("- 删除")
        self.btn_style_save = QPushButton("💾 保存")
        st_btn_row.addWidget(self.btn_style_add)
        st_btn_row.addWidget(self.btn_style_del)
        st_btn_row.addStretch()
        st_btn_row.addWidget(self.btn_style_save)
        st_layout.addLayout(st_btn_row)

        tabs.addTab(style_tab, "📐 样式映射")

        # 信号
        self.btn_font_add.clicked.connect(self._on_font_add)
        self.btn_font_del.clicked.connect(self._on_font_del)
        self.btn_font_save.clicked.connect(self._on_font_save)
        self.btn_style_add.clicked.connect(self._on_style_add)
        self.btn_style_del.clicked.connect(self._on_style_del)
        self.btn_style_save.clicked.connect(self._on_style_save)

    def _refresh_tables(self):
        """从 FormatEngine 刷新表格数据"""
        # 默认字体
        self.default_font_input.setText(self.engine.default_font)

        # 字体映射
        self.font_table.setRowCount(0)
        for src, tgt in self.engine.font_map.items():
            row = self.font_table.rowCount()
            self.font_table.insertRow(row)
            self.font_table.setItem(row, 0, QTableWidgetItem(src))
            self.font_table.setItem(row, 1, QTableWidgetItem(tgt))

        # 样式映射
        self.style_table.setRowCount(0)
        for style_name, rule in self.engine.style_map.items():
            row = self.style_table.rowCount()
            self.style_table.insertRow(row)
            self.style_table.setItem(row, 0, QTableWidgetItem(style_name))
            self.style_table.setItem(row, 1, QTableWidgetItem(rule.get("font_name", "")))
            bold_item = QTableWidgetItem("✓" if rule.get("bold") else "")
            bold_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.style_table.setItem(row, 2, bold_item)

    def _on_font_add(self):
        row = self.font_table.rowCount()
        self.font_table.insertRow(row)
        self.font_table.setItem(row, 0, QTableWidgetItem(""))
        self.font_table.setItem(row, 1, QTableWidgetItem(""))
        self.font_table.editItem(self.font_table.item(row, 0))

    def _on_font_del(self):
        row = self.font_table.currentRow()
        if row >= 0:
            self.font_table.removeRow(row)

    def _on_font_save(self):
        """从表格读取数据，写回 FormatEngine 并持久化"""
        new_map = {}
        for row in range(self.font_table.rowCount()):
            src = (self.font_table.item(row, 0).text() or "").strip()
            tgt = (self.font_table.item(row, 1).text() or "").strip()
            if src and tgt:
                new_map[src] = tgt
        self.engine.font_map = new_map
        # 📘 保存默认字体
        self.engine.default_font = self.default_font_input.text().strip()
        self.engine._save_user_rules()
        self._refresh_tables()

    def _on_style_add(self):
        row = self.style_table.rowCount()
        self.style_table.insertRow(row)
        self.style_table.setItem(row, 0, QTableWidgetItem(""))
        self.style_table.setItem(row, 1, QTableWidgetItem(""))
        self.style_table.setItem(row, 2, QTableWidgetItem(""))
        self.style_table.editItem(self.style_table.item(row, 0))

    def _on_style_del(self):
        row = self.style_table.currentRow()
        if row >= 0:
            self.style_table.removeRow(row)

    def _on_style_save(self):
        """从表格读取数据，写回 FormatEngine 并持久化"""
        new_map = {}
        for row in range(self.style_table.rowCount()):
            name = (self.style_table.item(row, 0).text() or "").strip()
            font = (self.style_table.item(row, 1).text() or "").strip()
            bold_text = (self.style_table.item(row, 2).text() or "").strip()
            if name:
                rule = {}
                if font:
                    rule["font_name"] = font
                if bold_text in ("✓", "1", "true", "True", "是", "yes"):
                    rule["bold"] = True
                if rule:
                    new_map[name] = rule
        self.engine.style_map = new_map
        self.engine._save_user_rules()
        self._refresh_tables()

    def get_engine(self) -> FormatEngine:
        return self.engine


# =============================================================
# 主窗口
# =============================================================
class MainWindow(QMainWindow):
    # 内部信号（工作线程 → 主线程）
    _log_signal = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.worker = None
        self.agent = None
        self.available_models = Config.get_available_models()
        self.format_engine = FormatEngine()  # 共享格式引擎，GUI 编辑后传给 Agent

        self.setWindowTitle("📖 翻译 Agent")
        self.setMinimumSize(1060, 700)
        self.resize(1200, 780)

        self._log_signal.connect(self._append_log)
        self._build_ui()
        self._setup_log_redirect()
        self._append_log("翻译 Agent GUI 已启动，请添加文件开始翻译", "info")

    # ---------------------------------------------------------
    # UI 构建
    # ---------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(16, 12, 16, 8)
        root_layout.setSpacing(8)

        # ---- 顶部标题 ----
        title = QLabel("📖 翻译 Agent")
        title.setObjectName("titleLabel")
        subtitle = QLabel("拖入 .docx / .pptx / .pdf 文件，选择语言和模型，一键翻译")
        subtitle.setObjectName("subtitleLabel")
        root_layout.addWidget(title)
        root_layout.addWidget(subtitle)

        # ---- 主体区域（左右分栏）----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, 1)

        # == 左侧面板 ==
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        # 文件列表
        file_group = QGroupBox("📁 待翻译文件")
        file_gl = QVBoxLayout(file_group)
        self.file_list = FileListWidget()
        file_gl.addWidget(self.file_list)
        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("添加文件")
        self.btn_remove = QPushButton("移除选中")
        self.btn_clear = QPushButton("清空")
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        btn_row.addWidget(self.btn_clear)
        file_gl.addLayout(btn_row)
        left_layout.addWidget(file_group)

        # 翻译设置
        settings_group = QGroupBox("⚙️ 翻译设置")
        sg_layout = QVBoxLayout(settings_group)

        # 目标语言
        lang_row = QHBoxLayout()
        lang_row.addWidget(QLabel("目标语言"))
        self.lang_combo = QComboBox()
        for cn, en in SUPPORTED_LANGS:
            self.lang_combo.addItem(f"{cn} ({en})", cn)
        self.lang_combo.setCurrentIndex(1)  # 默认英文
        lang_row.addWidget(self.lang_combo, 1)
        sg_layout.addLayout(lang_row)

        # 初翻模型
        draft_row = QHBoxLayout()
        draft_row.addWidget(QLabel("初翻模型"))
        self.draft_combo = QComboBox()
        default_idx = 0
        for i, (name, mid) in enumerate(self.available_models.items()):
            self.draft_combo.addItem(name, mid)
            if mid == Config.DEFAULT_MODEL_ID:
                default_idx = i
        self.draft_combo.setCurrentIndex(default_idx)
        draft_row.addWidget(self.draft_combo, 1)
        sg_layout.addLayout(draft_row)

        # 审校模型
        review_row = QHBoxLayout()
        review_row.addWidget(QLabel("审校模型"))
        self.review_combo = QComboBox()
        self.review_combo.addItem("与初翻相同", "__same__")
        self.review_combo.addItem("跳过审校", "__skip__")
        for name, mid in self.available_models.items():
            self.review_combo.addItem(name, mid)
        review_row.addWidget(self.review_combo, 1)
        sg_layout.addLayout(review_row)

        # 批量大小
        batch_row = QHBoxLayout()
        batch_row.addWidget(QLabel("批量大小"))
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(5, 50)
        self.batch_spin.setValue(20)
        self.batch_spin.setSuffix(" 段/批")
        batch_row.addWidget(self.batch_spin, 1)
        sg_layout.addLayout(batch_row)

        # 并行线程数
        worker_row = QHBoxLayout()
        worker_row.addWidget(QLabel("并行线程"))
        self.worker_spin = QSpinBox()
        self.worker_spin.setRange(1, 10)
        self.worker_spin.setValue(5)
        self.worker_spin.setSuffix(" 线程")
        self.worker_spin.setToolTip(
            "同时发多少个LLM请求。\n"
            "1=串行，3~5=推荐，>5可能触发API限流"
        )
        worker_row.addWidget(self.worker_spin, 1)
        sg_layout.addLayout(worker_row)

        # 日志级别
        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("日志级别"))
        self.log_combo = QComboBox()
        self.log_combo.addItem("INFO（默认）", "INFO")
        self.log_combo.addItem("DEBUG（摘要）", "DEBUG")
        self.log_combo.addItem("TRACE（完整对话）", "TRACE")
        log_row.addWidget(self.log_combo, 1)
        sg_layout.addLayout(log_row)

        # 📘 排版审校（Vision 模型下拉框）
        layout_row = QHBoxLayout()
        layout_row.addWidget(QLabel("排版审校"))
        self.vision_combo = QComboBox()
        self.vision_combo.setToolTip(
            "翻译完成后，用多模态 Vision 模型逐页审校排版。\n"
            "自动发现文字溢出、位置偏移、字号过小等问题并修正。\n"
            "会增加额外的 API 调用成本。"
        )
        self.vision_combo.addItem("关闭", "__off__")
        vision_models = Config.get_vision_models()
        for name, mid in vision_models.items():
            self.vision_combo.addItem(f"✅ {name}", mid)
        layout_row.addWidget(self.vision_combo, 1)
        sg_layout.addLayout(layout_row)

        # 📘 扫描件排版模式
        scan_row = QHBoxLayout()
        scan_row.addWidget(QLabel("扫描件排版"))
        self.scan_mode_combo = QComboBox()
        self.scan_mode_combo.setToolTip(
            "扫描件 PDF 的字号策略：\n"
            "自适应字号：每个文字块保持原始字号，放不下自动缩小\n"
            "原文对齐：原文同字号的块，译文也用同一字号（保持层级）\n"
            "指定字号：全部文本框使用右侧输入的字号"
        )
        self.scan_mode_combo.addItem("自适应字号", "adaptive")
        self.scan_mode_combo.addItem("原文对齐（证件推荐）", "aligned")
        self.scan_mode_combo.addItem("指定字号", "fixed")
        scan_row.addWidget(self.scan_mode_combo, 1)

        # 📘 指定字号输入框（仅 fixed 模式可用）
        self.fixed_fontsize_spin = QSpinBox()
        self.fixed_fontsize_spin.setRange(6, 72)
        self.fixed_fontsize_spin.setValue(10)
        self.fixed_fontsize_spin.setSuffix(" pt")
        self.fixed_fontsize_spin.setFixedWidth(80)
        self.fixed_fontsize_spin.setEnabled(False)
        self.fixed_fontsize_spin.setToolTip("指定字号模式下，全部文本框使用此字号")
        scan_row.addWidget(self.fixed_fontsize_spin)

        # 📘 切换模式时，启用/禁用字号输入框
        self.scan_mode_combo.currentIndexChanged.connect(
            lambda: self.fixed_fontsize_spin.setEnabled(
                self.scan_mode_combo.currentData() == "fixed"
            )
        )
        sg_layout.addLayout(scan_row)

        left_layout.addWidget(settings_group)

        # 格式映射面板
        self.format_panel = FormatMappingPanel(self.format_engine)
        left_layout.addWidget(self.format_panel, 1)

        # 操作按钮
        self.btn_start = QPushButton("▶  开始翻译")
        self.btn_start.setFixedHeight(44)
        self.btn_start.setStyleSheet(
            "font-size: 15px; background-color: #a6e3a1; color: #1e1e2e;"
        )
        left_layout.addWidget(self.btn_start)

        self.btn_stop = QPushButton("■  停止")
        self.btn_stop.setObjectName("stopBtn")
        self.btn_stop.setFixedHeight(36)
        self.btn_stop.setEnabled(False)
        left_layout.addWidget(self.btn_stop)

        splitter.addWidget(left_panel)

        # == 右侧面板（日志 + 进度）==
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        log_group = QGroupBox("📋 运行日志")
        log_gl = QVBoxLayout(log_group)
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        log_gl.addWidget(self.log_area)

        log_btn_row = QHBoxLayout()
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_open_output = QPushButton("打开输出目录")
        log_btn_row.addStretch()
        log_btn_row.addWidget(self.btn_clear_log)
        log_btn_row.addWidget(self.btn_open_output)
        log_gl.addLayout(log_btn_row)
        right_layout.addWidget(log_group, 1)

        # 进度条 + Token 统计
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m 段")
        right_layout.addWidget(self.progress_bar)

        # 📘 教学笔记：Token 用量统计
        # LLM API 按 token 计费，实时显示用量让用户心里有数。
        self.token_label = QLabel("Token 用量: —")
        self.token_label.setStyleSheet("color: #6c7086; font-size: 12px; padding: 2px 4px;")
        right_layout.addWidget(self.token_label)

        splitter.addWidget(right_panel)
        splitter.setSizes([400, 660])

        # 状态栏
        self.statusBar().showMessage("就绪")

        # ---- 信号连接 ----
        self.btn_add.clicked.connect(self._on_add_files)
        self.btn_remove.clicked.connect(self._on_remove_files)
        self.btn_clear.clicked.connect(lambda: self.file_list.clear())
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_clear_log.clicked.connect(lambda: self.log_area.clear())
        self.btn_open_output.clicked.connect(self._on_open_output)

    # ---------------------------------------------------------
    # 日志重定向
    # ---------------------------------------------------------
    def _setup_log_redirect(self):
        """把 logging 和 print 都重定向到 GUI 日志区域"""
        # 拦截 logging
        handler = LogInterceptor(self._log_signal)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        handler.setLevel(TRACE)
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(TRACE)

        # 📘 启动时就压制第三方库噪音
        for name in [
            "httpcore", "httpcore.http11", "httpcore.connection",
            "httpx", "volcenginesdkarkruntime", "urllib3",
            "hpack", "h2", "h11",
        ]:
            logging.getLogger(name).setLevel(logging.WARNING)

        # 拦截 print
        self._original_stdout = sys.stdout
        sys.stdout = PrintInterceptor(self._log_signal, sys.stdout)

    # ---------------------------------------------------------
    # 日志显示
    # ---------------------------------------------------------
    def _append_log(self, msg: str, level: str = "info"):
        """往日志区域追加一条消息（带颜色）"""
        color_map = {
            "trace":   "#6c7086",
            "debug":   "#94e2d5",
            "info":    "#cdd6f4",
            "warning": "#f9e2af",
            "error":   "#f38ba8",
            "print":   "#a6adc8",
        }
        color = color_map.get(level.lower(), "#cdd6f4")
        escaped = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        self.log_area.append(f'<span style="color:{color}">{escaped}</span>')

    # ---------------------------------------------------------
    # 文件操作
    # ---------------------------------------------------------
    def _on_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文档", "",
            "文档文件 (*.docx *.pptx *.pdf);;Word (*.docx);;PowerPoint (*.pptx);;PDF (*.pdf)"
        )
        for path in files:
            exists = any(
                self.file_list.item(i).data(Qt.ItemDataRole.UserRole) == path
                for i in range(self.file_list.count())
            )
            if not exists:
                item = QListWidgetItem(f"📄 {os.path.basename(path)}")
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.file_list.addItem(item)

    def _on_remove_files(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _on_open_output(self):
        output_dir = os.path.abspath("output")
        os.makedirs(output_dir, exist_ok=True)
        os.startfile(output_dir)

    # ---------------------------------------------------------
    # 翻译控制
    # ---------------------------------------------------------
    def _on_start(self):
        files = self.file_list.get_files()
        if not files:
            self._append_log("请先添加待翻译文件", "warning")
            return

        target_lang = self.lang_combo.currentData()
        draft_model = self.draft_combo.currentData()
        review_choice = self.review_combo.currentData()
        batch_size = self.batch_spin.value()
        max_workers = self.worker_spin.value()

        # 确定审校模型
        if review_choice == "__skip__":
            review_model = None
        elif review_choice == "__same__":
            review_model = draft_model
        else:
            review_model = review_choice

        # 设置日志级别
        log_level = self.log_combo.currentData()
        self._apply_log_level(log_level)

        # 排版审校
        vision_choice = self.vision_combo.currentData()
        vision_model = None if vision_choice == "__off__" else vision_choice

        # 扫描件排版模式
        scan_mode = self.scan_mode_combo.currentData()
        fixed_fontsize = float(self.fixed_fontsize_spin.value())

        self._append_log(f"初翻模型: {draft_model}", "info")
        self._append_log(f"审校模型: {review_model or '跳过'}", "info")
        self._append_log(f"目标语言: {target_lang}  |  批量: {batch_size}  |  线程: {max_workers}", "info")
        if vision_model:
            self._append_log(f"排版审校: {vision_model}", "info")
        if scan_mode == "aligned":
            self._append_log(f"扫描件排版: 原文对齐", "info")
        elif scan_mode == "fixed":
            self._append_log(f"扫描件排版: 指定字号 {fixed_fontsize}pt", "info")
        self._append_log(f"文件数: {len(files)}", "info")
        self._append_log("─" * 50, "info")

        # 📘 教学笔记：立即反馈
        # 先禁用按钮、显示状态，再做耗时的 Agent 初始化。
        # 用户点击后立刻看到 UI 变化，不会以为没反应。
        self._set_running(True)
        self.statusBar().showMessage("正在初始化翻译引擎...")
        self.progress_bar.setValue(0)
        self.token_label.setText("Token 用量: —")
        QApplication.processEvents()  # 强制刷新 UI

        # 创建 Agent（使用 GUI 中编辑的格式引擎）
        try:
            self.agent = TranslatorAgent(
                draft_model_id=draft_model,
                review_model_id=review_model,
                vision_model_id=vision_model,
                batch_size=batch_size,
                max_workers=max_workers,
                debug=True,
                scan_mode=scan_mode,
                fixed_fontsize=fixed_fontsize,
            )
            # 📘 教学笔记：共享格式引擎
            # GUI 里编辑的字体/样式映射表存在 self.format_engine 里，
            # 这里把它注入到 Agent，这样用户在 GUI 里改的规则立刻生效。
            self.agent.format_engine = self.format_panel.get_engine()
        except Exception as e:
            self._append_log(f"Agent 初始化失败: {e}", "error")
            self._set_running(False)
            self.statusBar().showMessage("初始化失败")
            return

        # 📘 教学笔记：跨线程 GUI 更新必须用 Signal
        # patched_translate_document 在工作线程里执行，
        # 不能直接操作 progress_bar（Qt 会崩溃/闪退）。
        # 正确做法：通过 worker.progress_signal 把数据传回主线程。

        # 先创建 worker（后面 patch 要引用它的 signal）
        self.worker = TranslateWorker(self.agent, files, target_lang)

        original_translate_document = self.agent.pipeline.translate_document
        worker_ref = self.worker  # 闭包捕获，避免 self.worker 被替换

        def patched_translate_document(parsed_data, target_lang="英文", on_progress=None):
            def gui_progress(completed, total):
                worker_ref.progress_signal.emit(completed, total)
                # 📘 每批翻译完成后更新 token 用量
                worker_ref._emit_token_usage()
                if on_progress:
                    on_progress(completed, total)
            return original_translate_document(parsed_data, target_lang, gui_progress)

        self.agent.pipeline.translate_document = patched_translate_document

        # 连接信号并启动
        self.worker.log_signal.connect(self._append_log)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.token_signal.connect(self._on_token_update)
        self.worker.finished_signal.connect(self._on_file_done)
        self.worker.error_signal.connect(self._on_file_error)
        self.worker.finished.connect(self._on_all_done)
        self.worker.start()

        self.statusBar().showMessage("翻译中...")

    def _on_stop(self):
        """
        📘 教学笔记：优雅停止 vs 强杀
        旧方案：self.worker.terminate() — 直接杀线程，已翻译内容全部丢失。
        新方案：设置 pipeline 的停止标志，让翻译循环在当前 batch 完成后自然退出，
        然后 worker.run() 正常走到写入步骤，把已翻译的部分写入文件。
        用户不会丢失任何已完成的翻译。
        """
        if self.worker and self.worker.isRunning() and self.agent:
            self.agent.pipeline.request_stop()
            self.btn_stop.setEnabled(False)  # 防止重复点击
            self._append_log("正在停止...当前批次完成后将写入已翻译内容", "warning")
            self.statusBar().showMessage("正在停止...")

    def _on_file_done(self, output_path: str):
        self._append_log(f"✅ 翻译完成: {output_path}", "info")

    def _on_file_error(self, error_msg: str):
        self._append_log(f"❌ 翻译失败: {error_msg}", "error")

    def _on_progress(self, completed: int, total: int):
        """在主线程中安全更新进度条"""
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(completed)

    def _on_token_update(self, draft_tokens: int, review_tokens: int, total_tokens: int):
        """📘 在主线程中安全更新 Token 用量显示（初翻/审校分开）"""
        self.token_label.setText(
            f"Token: {total_tokens:,}  (初翻 {draft_tokens:,} + 审校 {review_tokens:,})"
        )

    def _on_all_done(self):
        self._set_running(False)
        # 📘 判断是正常完成还是提前停止
        was_stopped = self.agent and self.agent.pipeline.is_stopped
        if was_stopped:
            self.statusBar().showMessage("已停止（部分翻译已写入）")
            self._append_log("═" * 50, "info")
            self._append_log("⚠️ 翻译已停止，已完成部分已写入文件", "warning")
        else:
            self.statusBar().showMessage("全部完成")
            self._append_log("═" * 50, "info")
            self._append_log("🎉 所有文件翻译完成", "info")

    def _set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.btn_add.setEnabled(not running)
        self.btn_remove.setEnabled(not running)
        self.btn_clear.setEnabled(not running)
        self.draft_combo.setEnabled(not running)
        self.review_combo.setEnabled(not running)
        self.lang_combo.setEnabled(not running)
        self.batch_spin.setEnabled(not running)
        self.worker_spin.setEnabled(not running)
        self.log_combo.setEnabled(not running)
        self.vision_combo.setEnabled(not running)
        self.scan_mode_combo.setEnabled(not running)
        self.fixed_fontsize_spin.setEnabled(not running and self.scan_mode_combo.currentData() == "fixed")
        self.format_panel.setEnabled(not running)

    def _apply_log_level(self, level_name: str):
        """
        运行时切换日志级别，同时压制第三方库的噪音日志。

        📘 教学笔记：为什么要设 os.environ？
        _apply_log_level 在 _on_start 里调用，但 Agent/Pipeline 是之后才创建的。
        Agent 创建时 get_logger() 会读 core.logger._CONSOLE_LEVEL（模块加载时缓存的值），
        所以必须同时更新环境变量和 core.logger 模块里的缓存值，
        这样后续新建的 logger 也能用正确的级别。
        """
        import core.logger as logger_module

        level_map = {"TRACE": TRACE, "DEBUG": logging.DEBUG, "INFO": logging.INFO}
        level = level_map.get(level_name, logging.INFO)

        # 📘 更新环境变量和模块缓存，影响后续新建的 logger
        os.environ["LOG_LEVEL"] = level_name
        logger_module._CONSOLE_LEVEL = level_name

        # 📘 更新所有已存在的 logger
        for name in list(logging.Logger.manager.loggerDict):
            lgr = logging.getLogger(name)
            # 设置 logger 自身的级别（取当前和目标的较低值，确保能输出）
            lgr.setLevel(min(lgr.level, level) if lgr.level > 0 else level)
            # 设置所有终端 handler 的级别
            for h in lgr.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.setLevel(level)

        # 📘 教学笔记：压制第三方库噪音
        # httpcore、httpx、volcengine SDK 在 DEBUG 级别会输出大量 HTTP 连接细节
        # （TCP 握手、TLS 协商、keep-alive 等），完全淹没我们自己的日志。
        # 强制把它们设为 WARNING，只让我们自己的 logger 输出 DEBUG。
        noisy_loggers = [
            "httpcore", "httpcore.http11", "httpcore.connection",
            "httpx", "volcenginesdkarkruntime", "urllib3",
            "hpack", "h2", "h11",
        ]
        for name in noisy_loggers:
            logging.getLogger(name).setLevel(logging.WARNING)

    def closeEvent(self, event):
        """关闭窗口时恢复 stdout"""
        sys.stdout = self._original_stdout
        if self.worker and self.worker.isRunning():
            if self.agent:
                self.agent.pipeline.request_stop()
            self.worker.wait(5000)
        event.accept()


# =============================================================
# 入口
# =============================================================
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
