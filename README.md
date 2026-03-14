# file-translator-agent

An AI-powered document translation agent that translates `.docx` (Word) and `.pptx` (PowerPoint) files while preserving formatting, layout, and styles.

Built on top of Volcengine Ark (doubao) LLM API with a two-stage translation pipeline (Draft → Review) for high-quality output.

## Features

- **Word & PowerPoint translation** with full format preservation (fonts, styles, colors, bold/italic)
- **Two-stage pipeline**: Draft translation → Review/polish, using separate LLM models
- **PyQt6 GUI**: Drag-and-drop files, model selection, real-time logs, progress bar
- **Font mapping engine**: Configurable source→target font rules (e.g. 宋体 → Times New Roman)
- **Run-level format tags**: Preserves per-run formatting within paragraphs
- **Smart text fitting**: Auto-shrinks fonts when translated text overflows PPT text boxes
- **COM enhancement** (Windows + Office): Translates charts, SmartArt, and text boxes in Word
- **Multi-model support**: Switch between LLM models via dropdown (configured in `.env`)
- **Multi-language**: Supports Chinese, English, Japanese, Korean, French, German, Spanish, Russian
- **Structured logging**: TRACE / DEBUG / INFO levels, file + console output

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/YOUR_USERNAME/file-translator-agent.git
cd file-translator-agent

# 2. Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure API key
copy .env.example .env
# Edit .env and fill in your Volcengine Ark API key

# 5. Run GUI
python translator_gui.py

# Or run CLI
python translator_main.py
```

## Configuration

Edit `.env` to configure:

| Variable | Description |
|---|---|
| `ARK_API_KEY` | Your Volcengine Ark API key |
| `DEFAULT_MODEL_ID` | Default LLM model ID |
| `AVAILABLE_MODELS` | Comma-separated list of selectable models |
| `LOG_LEVEL` | `TRACE` / `DEBUG` / `INFO` (default) / `WARNING` |

## Project Structure

```
core/           # Agent framework (LLM engine, memory, orchestrator, logging)
translator/     # Translation pipeline (parser, writer, format engine, COM)
config/         # Settings and environment config
tools/          # Base tool system
agents/         # Agent registry
```

## License

[MIT](LICENSE)
