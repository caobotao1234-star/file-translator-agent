# agent_gui.py
# =============================================================
# 📘 教学笔记：交互式翻译 Agent GUI（Phase 3）
# =============================================================
# 聊天式界面：用户和 Agent 对话，Agent 自主翻译文档。
# 用户可以随时介入（纠正翻译、修改需求、提问）。
#
# 与旧 GUI（translator_gui.py）的区别：
#   旧: 点开始 → 等结果 → 看日志
#   新: 拖入文件 → 对话 → Agent 边做边汇报 → 用户随时介入
# =============================================================

import sys
import os
import threading
import queue

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QLineEdit, QPushButton, QLabel, QComboBox,
    QFileDialog, QSplitter, QGroupBox, QProgressBar,
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QFont

from config.settings import Config
from core.logger import get_logger

logger = get_logger("agent_gui")


class AgentWorker(QThread):
    """
    📘 Agent 后台线程

    Agent Loop 跑在这个线程里，不阻塞 GUI。
    通过信号把 Agent 的输出发送到 GUI。
    """
    message_signal = pyqtSignal(str, str)  # (role, content)
    tool_signal = pyqtSignal(str, str)  # (tool_name, params_short)
    progress_signal = pyqtSignal(int, int, str)  # (current, total, message)
    done_signal = pyqtSignal()
    ask_signal = pyqtSignal(str)  # Agent 提问

    def __init__(self, agent, user_message: str):
        super().__init__()
        self.agent = agent
        self.user_message = user_message
        self._answer_queue = queue.Queue()

    def run(self):
        try:
            self.agent.run(self.user_message)
        except Exception as e:
            self.message_signal.emit("system", f"Agent 出错: {e}")
        finally:
            self.done_signal.emit()

    def provide_answer(self, answer: str):
        """GUI 线程调用：回答 Agent 的提问"""
        self._answer_queue.put(answer)


class ChatPanel(QWidget):
    """
    📘 聊天面板

    显示用户和 Agent 的对话，支持工具调用显示和进度条。
    """

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # 聊天记录
        self.chat_display = QTextEdit()
        self.chat_display.setReadOnly(True)
        self.chat_display.setFont(QFont("Consolas", 10))
        self.chat_display.setStyleSheet(
            "QTextEdit { background-color: #1e1e1e; color: #d4d4d4; "
            "border: 1px solid #333; padding: 8px; }"
        )
        layout.addWidget(self.chat_display)

        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet(
            "QProgressBar { border: 1px solid #555; border-radius: 3px; "
            "text-align: center; background: #2d2d2d; color: #fff; }"
            "QProgressBar::chunk { background-color: #0078d4; }"
        )
        layout.addWidget(self.progress_bar)

        # 输入区
        input_layout = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setPlaceholderText("输入消息... (Enter 发送)")
        self.input_field.setFont(QFont("Microsoft YaHei", 10))
        self.input_field.setStyleSheet(
            "QLineEdit { background-color: #2d2d2d; color: #fff; "
            "border: 1px solid #555; padding: 8px; border-radius: 4px; }"
        )
        self.send_btn = QPushButton("发送")
        self.send_btn.setStyleSheet(
            "QPushButton { background-color: #0078d4; color: white; "
            "border: none; padding: 8px 16px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #1a8ae8; }"
        )
        input_layout.addWidget(self.input_field)
        input_layout.addWidget(self.send_btn)
        layout.addLayout(input_layout)

    def append_message(self, role: str, content: str):
        """添加一条消息到聊天记录"""
        if role == "user":
            html = f'<p style="color:#569cd6;"><b>👤 你:</b> {_escape_html(content)}</p>'
        elif role == "assistant":
            html = f'<p style="color:#d4d4d4;"><b>🤖 Agent:</b> {_escape_html(content)}</p>'
        elif role == "tool":
            html = f'<p style="color:#808080; font-size:9pt;">  🔧 {_escape_html(content)}</p>'
        elif role == "system":
            html = f'<p style="color:#ce9178;"><i>{_escape_html(content)}</i></p>'
        else:
            html = f'<p>{_escape_html(content)}</p>'
        self.chat_display.append(html)

    def set_progress(self, current: int, total: int, message: str):
        if total > 0:
            self.progress_bar.setVisible(True)
            self.progress_bar.setMaximum(total)
            self.progress_bar.setValue(current)
            self.progress_bar.setFormat(f"{current}/{total} - {message}")
        else:
            self.progress_bar.setVisible(False)


def _escape_html(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace("\n", "<br>"))


class MainWindow(QMainWindow):
    """
    📘 主窗口：聊天式翻译 Agent

    布局：
    +------------------------------------------+
    | 翻译 Agent                          [设置] |
    +------------------------------------------+
    | [文件区]  |  [聊天面板]                     |
    | 拖入文件  |  对话记录                       |
    | 模型选择  |  工具调用                       |
    | 目标语言  |  进度条                         |
    |          |  [输入框] [发送]                 |
    +------------------------------------------+
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("翻译 Agent (v2 - 交互式)")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)

        self.agent = None
        self.worker = None
        self._files = []

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # ── 左侧：设置面板 ──
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_panel.setFixedWidth(280)

        # 文件区
        file_group = QGroupBox("文件")
        file_layout = QVBoxLayout(file_group)
        self.file_label = QLabel("拖入文件或点击添加")
        self.file_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.file_label.setStyleSheet(
            "QLabel { border: 2px dashed #555; padding: 20px; "
            "color: #888; border-radius: 8px; }"
        )
        self.file_label.setAcceptDrops(True)
        add_btn = QPushButton("添加文件")
        add_btn.clicked.connect(self._on_add_files)
        file_layout.addWidget(self.file_label)
        file_layout.addWidget(add_btn)
        left_layout.addWidget(file_group)

        # 模型选择
        model_group = QGroupBox("模型")
        model_layout = QVBoxLayout(model_group)

        model_layout.addWidget(QLabel("Agent 主模型:"))
        self.brain_combo = QComboBox()
        available = Config.get_available_models()
        for name, mid in available.items():
            self.brain_combo.addItem(name, mid)
        model_layout.addWidget(self.brain_combo)

        model_layout.addWidget(QLabel("目标语言:"))
        self.lang_combo = QComboBox()
        for lang in ["英文", "中文", "日文", "韩文", "法文", "德文", "西班牙文", "俄文"]:
            self.lang_combo.addItem(lang)
        model_layout.addWidget(self.lang_combo)

        left_layout.addWidget(model_group)

        # 开始按钮
        self.start_btn = QPushButton("🚀 开始翻译")
        self.start_btn.setStyleSheet(
            "QPushButton { background-color: #0078d4; color: white; "
            "font-size: 14px; padding: 12px; border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #1a8ae8; }"
            "QPushButton:disabled { background-color: #555; }"
        )
        self.start_btn.clicked.connect(self._on_start)
        left_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("⏹ 停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        left_layout.addWidget(self.stop_btn)

        # Token 统计
        self.stats_label = QLabel("等待开始...")
        self.stats_label.setStyleSheet("color: #888; font-size: 9pt;")
        left_layout.addWidget(self.stats_label)

        left_layout.addStretch()
        main_layout.addWidget(left_panel)

        # ── 右侧：聊天面板 ──
        self.chat_panel = ChatPanel()
        self.chat_panel.input_field.returnPressed.connect(self._on_send_message)
        self.chat_panel.send_btn.clicked.connect(self._on_send_message)
        main_layout.addWidget(self.chat_panel, stretch=1)

        # 启用拖放
        self.setAcceptDrops(True)

    # ── 事件处理 ──

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                ext = os.path.splitext(path)[1].lower()
                if ext in (".pptx", ".docx", ".pdf"):
                    if path not in self._files:
                        self._files.append(path)
        self._update_file_label()

    def _on_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "",
            "文档文件 (*.pptx *.docx *.pdf);;所有文件 (*)",
        )
        for f in files:
            if f not in self._files:
                self._files.append(f)
        self._update_file_label()

    def _update_file_label(self):
        if self._files:
            names = [os.path.basename(f) for f in self._files]
            self.file_label.setText("\n".join(names))
            self.file_label.setStyleSheet(
                "QLabel { border: 2px solid #0078d4; padding: 10px; "
                "color: #d4d4d4; border-radius: 8px; }"
            )
        else:
            self.file_label.setText("拖入文件或点击添加")
            self.file_label.setStyleSheet(
                "QLabel { border: 2px dashed #555; padding: 20px; "
                "color: #888; border-radius: 8px; }"
            )

    def _on_start(self):
        if not self._files:
            self.chat_panel.append_message("system", "请先添加文件")
            return

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        # 构建 Agent
        brain_model = self.brain_combo.currentData()
        target_lang = self.lang_combo.currentText()

        self.chat_panel.append_message("system",
            f"初始化 Agent... 模型: {brain_model}")

        from core.llm_router import LLMRouter
        from core.agent_loop import AgentLoop
        from translator.translate_pipeline import TranslatePipeline
        from translator.format_engine import FormatEngine
        from tools.doc_tools import ParseDocumentTool, GetPageContentTool, WriteDocumentTool
        from tools.translate_tools import TranslatePageTool
        from tools.memory_tools import MemoryStore, ReadMemoryTool, UpdateMemoryTool
        from tools.interaction_tools import AskUserTool, ReportProgressTool
        from tools.format_tools import InspectOutputTool, AdjustFormatTool
        from tools.vision_tools import GetPageImageTool, create_scan_tools
        from prompts.agent_prompts import TRANSLATION_AGENT_PROMPT

        router = LLMRouter(api_key=Config.ARK_API_KEY)
        router.register_model("translate", model_str=Config.DEFAULT_MODEL_ID)
        router.register_model("agent_brain", model_str=brain_model)
        brain_engine = router.get("agent_brain")

        pipeline = TranslatePipeline(
            translate_llm=router.get("translate"),
            batch_size=20, max_workers=1,
        )
        fmt = FormatEngine()
        parse_tool = ParseDocumentTool(format_engine=fmt)
        page_image_tool = GetPageImageTool()
        parse_tool._page_image_tool = page_image_tool
        memory = MemoryStore()

        # ask_user 回调：通过信号让 GUI 显示问题
        def on_ask(question):
            if self.worker:
                self.worker.ask_signal.emit(question)
                try:
                    return self.worker._answer_queue.get(timeout=300)
                except queue.Empty:
                    return None
            return None

        tools = [
            parse_tool,
            GetPageContentTool(parse_tool),
            page_image_tool,
            WriteDocumentTool(parse_tool, fmt),
            TranslatePageTool(translate_pipeline=pipeline),
            InspectOutputTool(),
            AdjustFormatTool(),
            ReadMemoryTool(memory),
            UpdateMemoryTool(memory),
            AskUserTool(on_ask=on_ask),
            ReportProgressTool(on_progress=self._on_progress_callback),
        ]

        scan_tools, scan_ctx = create_scan_tools(page_image_tool=page_image_tool)
        tools.extend(scan_tools)
        parse_tool._scan_context = scan_ctx

        self.agent = AgentLoop(
            llm_engine=brain_engine,
            tools=tools,
            system_prompt=TRANSLATION_AGENT_PROMPT,
            on_message=lambda r, c: self._on_agent_message(r, c),
            on_tool_call=lambda n, p: self._on_agent_tool(n, p),
        )

        # 构建用户消息
        os.makedirs("output", exist_ok=True)
        file_list = "\n".join(
            f"- {f} -> output/{os.path.splitext(os.path.basename(f))[0]}_agent{os.path.splitext(f)[1]}"
            for f in self._files
        )
        user_msg = (
            f"请翻译以下文档，目标语言: {target_lang}\n{file_list}\n"
            f"要求: 翻译准确地道，排版美观专业。"
        )

        # 启动后台线程
        self.worker = AgentWorker(self.agent, user_msg)
        self.worker.message_signal.connect(
            lambda r, c: self.chat_panel.append_message(r, c))
        self.worker.tool_signal.connect(
            lambda n, p: self.chat_panel.append_message("tool", f"{n}({p})"))
        self.worker.progress_signal.connect(
            lambda cur, tot, msg: self.chat_panel.set_progress(cur, tot, msg))
        self.worker.ask_signal.connect(self._on_agent_ask)
        self.worker.done_signal.connect(self._on_done)
        self.worker.start()

    def _on_stop(self):
        if self.agent:
            self.agent.stop()
            self.chat_panel.append_message("system", "正在停止...")

    def _on_send_message(self):
        """用户在输入框发送消息"""
        text = self.chat_panel.input_field.text().strip()
        if not text:
            return
        self.chat_panel.input_field.clear()
        self.chat_panel.append_message("user", text)

        # 注入到 Agent 的消息队列
        if self.agent:
            self.agent.message_queue.inject(text)

    def _on_agent_message(self, role: str, content: str):
        """Agent 输出回调（从后台线程）"""
        self.chat_panel.append_message(role, content)

    def _on_agent_tool(self, name: str, params: dict):
        """工具调用回调"""
        short = str(params)[:80]
        self.chat_panel.append_message("tool", f"{name}({short})")

    def _on_agent_ask(self, question: str):
        """Agent 提问回调"""
        self.chat_panel.append_message("assistant", f"❓ {question}")
        self.chat_panel.input_field.setFocus()
        self.chat_panel.input_field.setPlaceholderText("请回答 Agent 的问题...")

    def _on_progress_callback(self, current: int, total: int, message: str):
        """进度回调（从工具线程）"""
        self.chat_panel.set_progress(current, total, message)

    def _on_done(self):
        """Agent 完成"""
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.chat_panel.progress_bar.setVisible(False)
        if self.agent:
            s = self.agent.stats
            self.stats_label.setText(
                f"轮次: {s['turns']} | 工具: {s['tool_calls']} | "
                f"Tokens: {s['prompt_tokens']}+{s['completion_tokens']}"
            )
        self.chat_panel.append_message("system", "Agent 已完成任务。")

    def closeEvent(self, event):
        if self.agent:
            self.agent.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # 深色主题
    from PyQt6.QtGui import QPalette, QColor
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.Base, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.Text, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(212, 212, 212))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 212))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
