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
    finished_signal = pyqtSignal(str)       # 输出文件路径
    error_signal = pyqtSignal(str)          # 错误信息

    def __init__(self, agent: TranslatorAgent, files: list, target_lang: str):
        super().__init__()
        self.agent = agent
        self.files = files
        self.target_lang = target_lang

    def run(self):
        for filepath in self.files:
            try:
                self.log_signal.emit(f"开始翻译: {os.path.basename(filepath)}", "info")
                output = self.agent.translate_file(
                    filepath,
                    target_lang=self.target_lang,
                )
                self.finished_signal.emit(output)
            except Exception as e:
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

        # 日志级别
        log_row = QHBoxLayout()
        log_row.addWidget(QLabel("日志级别"))
        self.log_combo = QComboBox()
        self.log_combo.addItem("INFO（默认）", "INFO")
        self.log_combo.addItem("DEBUG（摘要）", "DEBUG")
        self.log_combo.addItem("TRACE（完整对话）", "TRACE")
        log_row.addWidget(self.log_combo, 1)
        sg_layout.addLayout(log_row)

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

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%v / %m 段")
        right_layout.addWidget(self.progress_bar)

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

        self._append_log(f"初翻模型: {draft_model}", "info")
        self._append_log(f"审校模型: {review_model or '跳过'}", "info")
        self._append_log(f"目标语言: {target_lang}  |  批量: {batch_size}", "info")
        self._append_log(f"文件数: {len(files)}", "info")
        self._append_log("─" * 50, "info")

        # 创建 Agent（使用 GUI 中编辑的格式引擎）
        try:
            self.agent = TranslatorAgent(
                draft_model_id=draft_model,
                review_model_id=review_model,
                batch_size=batch_size,
                debug=True,
            )
            # 📘 教学笔记：共享格式引擎
            # GUI 里编辑的字体/样式映射表存在 self.format_engine 里，
            # 这里把它注入到 Agent，这样用户在 GUI 里改的规则立刻生效。
            self.agent.format_engine = self.format_panel.get_engine()
        except Exception as e:
            self._append_log(f"Agent 初始化失败: {e}", "error")
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
                if on_progress:
                    on_progress(completed, total)
            return original_translate_document(parsed_data, target_lang, gui_progress)

        self.agent.pipeline.translate_document = patched_translate_document

        # 连接信号并启动
        self.worker.log_signal.connect(self._append_log)
        self.worker.progress_signal.connect(self._on_progress)
        self.worker.finished_signal.connect(self._on_file_done)
        self.worker.error_signal.connect(self._on_file_error)
        self.worker.finished.connect(self._on_all_done)
        self.worker.start()

        self._set_running(True)
        self.statusBar().showMessage("翻译中...")
        self.progress_bar.setValue(0)

    def _on_stop(self):
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self._append_log("用户手动停止翻译", "warning")
            self._set_running(False)
            self.statusBar().showMessage("已停止")

    def _on_file_done(self, output_path: str):
        self._append_log(f"✅ 翻译完成: {output_path}", "info")

    def _on_file_error(self, error_msg: str):
        self._append_log(f"❌ 翻译失败: {error_msg}", "error")

    def _on_progress(self, completed: int, total: int):
        """在主线程中安全更新进度条"""
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(completed)

    def _on_all_done(self):
        self._set_running(False)
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
        self.log_combo.setEnabled(not running)
        self.format_panel.setEnabled(not running)

    def _apply_log_level(self, level_name: str):
        """运行时切换所有 logger 的终端 handler 级别"""
        level_map = {"TRACE": TRACE, "DEBUG": logging.DEBUG, "INFO": logging.INFO}
        level = level_map.get(level_name, logging.INFO)
        for name in list(logging.Logger.manager.loggerDict):
            lgr = logging.getLogger(name)
            if lgr.level > level:
                lgr.setLevel(level)
            for h in lgr.handlers:
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    h.setLevel(level)

    def closeEvent(self, event):
        """关闭窗口时恢复 stdout"""
        sys.stdout = self._original_stdout
        if self.worker and self.worker.isRunning():
            self.worker.terminate()
            self.worker.wait(2000)
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
