"""Data-source manager: configure scraping sources, generate Python scripts
in real time via an LLM (OpenAI-compatible API).

Each data source = {name, url, prompt, example JSON}. Clicking
"初始化" (initialize) makes the server:
  1. fetch the page live with Playwright's bundled Chromium to capture
     page.content(),
  2. send config + prompt + example JSON + table HTML fragment to the LLM,
  3. validate the produced script locally (syntax + --from-html dry run +
     output-structure check), retrying with feedback up to 3 rounds,
  4. save the script under generated/ for download.

Run:  python app.py   ->  http://127.0.0.1:8765
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

from llm_codegen import ScriptGenerationError, generate_script

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "datasources.json"
LLM_CFG_PATH = BASE_DIR / "llm_config.json"
GENERATED_DIR = BASE_DIR / "generated"
UPLOAD_DIR = BASE_DIR / "uploads"
GENERATED_DIR.mkdir(exist_ok=True)
RUN_TIMEOUT_S = 300

app = Flask(__name__)
app.json.sort_keys = False  # keep JSON key order as configured by the user
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Storage (JSON files)
# ---------------------------------------------------------------------------
def _load_sources() -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save_sources(sources: list[dict]) -> None:
    DB_PATH.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_settings() -> dict:
    if not LLM_CFG_PATH.exists():
        return {"base_url": "", "api_key": "", "model": ""}
    try:
        return json.loads(LLM_CFG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"base_url": "", "api_key": "", "model": ""}


def _save_settings(settings: dict) -> None:
    LLM_CFG_PATH.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _validate_payload(payload: dict) -> tuple[dict | None, str | None]:
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    prompt = (payload.get("prompt") or "").strip()
    if not name:
        return None, "名称不能为空"
    if not url:
        return None, "URL 不能为空"

    example = payload.get("example")
    if isinstance(example, str):
        try:
            example = json.loads(example) if example.strip() else None
        except json.JSONDecodeError as exc:
            return None, f"示例 JSON 解析失败: {exc}"
    if not isinstance(example, (dict, list)) or not example:
        return None, "示例 JSON 不能为空（对象或对象数组）"
    records = example if isinstance(example, list) else [example]
    if not all(isinstance(r, dict) and r for r in records):
        return None, "示例 JSON 的每条记录都必须是非空对象"

    return {
        "name": name,
        "url": url if re.match(r"^https?://", url) else "https://" + url,
        "prompt": prompt,
        "example": example,
    }, None


_DATA_URL_RE = re.compile(r"data:image/(png|jpeg|jpg|gif|webp);base64,(.*)", re.S)
_IMAGE_NAME_RE = re.compile(r"[\w.-]+\.(png|jpg|jpeg|gif|webp)")


def _process_images(source_id: str, entries) -> list[str]:
    """Persist prompt images for a source. ``entries`` mixes data URLs (new
    uploads from the browser) and existing filenames (kept ones); files no
    longer referenced are removed. Returns the stored filenames."""
    directory = UPLOAD_DIR / source_id
    directory.mkdir(parents=True, exist_ok=True)
    kept: list[str] = []
    for entry in entries or []:
        if not isinstance(entry, str):
            continue
        if entry.startswith("data:image/"):
            match = _DATA_URL_RE.match(entry)
            if not match:
                continue
            ext = "jpg" if match.group(1) in ("jpeg", "jpg") else match.group(1)
            name = f"img_{len(kept) + 1}_{int(time.time() * 1000) % 10**9}.{ext}"
            try:
                (directory / name).write_bytes(base64.b64decode(match.group(2)))
            except (ValueError, OSError):
                continue
            kept.append(name)
        elif _IMAGE_NAME_RE.fullmatch(entry) and (directory / entry).exists():
            kept.append(entry)
    for existing in directory.iterdir():
        if existing.name not in kept:
            existing.unlink(missing_ok=True)
    return kept


def _script_path(source: dict) -> Path:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", source["name"]).strip("_") or "datasource"
    # id suffix keeps two sources (e.g. Chinese names collapsing to the same
    # ASCII stem) from clobbering each other's generated script
    return GENERATED_DIR / f"{safe}_{source['id'][:6]}.py"


# ---------------------------------------------------------------------------
# Live page capture (shared by the generate flow)
# ---------------------------------------------------------------------------
def fetch_page_html(url: str, headless: bool = True) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(url, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_selector("table", timeout=60_000)
        time.sleep(3)
        html = page.content()
        browser.close()
        return html


# ---------------------------------------------------------------------------
# Pages & API
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


# --- LLM settings -----------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    settings = _load_settings()
    key = settings.get("api_key", "")
    return jsonify({
        "base_url": settings.get("base_url", ""),
        "model": settings.get("model", ""),
        "api_key_masked": (key[:3] + "***" + key[-4:]) if len(key) > 8 else ("已设置" if key else ""),
        "has_key": bool(key),
    })


@app.put("/api/settings")
def put_settings():
    payload = request.get_json(silent=True) or {}
    settings = _load_settings()
    if "base_url" in payload:
        settings["base_url"] = (payload.get("base_url") or "").strip()
    if "model" in payload:
        settings["model"] = (payload.get("model") or "").strip()
    key = (payload.get("api_key") or "").strip()
    if key and "***" not in key:  # empty/masked value = keep existing
        settings["api_key"] = key
    _save_settings(settings)
    return get_settings()


@app.post("/api/settings/test")
def test_settings():
    settings = _load_settings()
    labels = {"base_url": "接口地址", "api_key": "API Key", "model": "模型名"}
    missing = [labels[k] for k in ("base_url", "api_key", "model") if not settings.get(k)]
    if missing:
        return jsonify({"error": "请先完整填写：" + "、".join(missing)}), 400
    from llm_codegen import _chat
    try:
        reply = _chat(settings, [{"role": "user", "content": "回复两个字：正常"}], timeout=30)
        return jsonify({"ok": True, "reply": reply.strip()[:50]})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 502


# --- data sources -------------------------------------------------------------
@app.get("/api/sources")
def list_sources():
    name = (request.args.get("name") or "").strip().lower()
    url = (request.args.get("url") or "").strip().lower()
    with _lock:
        sources = _load_sources()
    if name:
        sources = [s for s in sources if name in s["name"].lower()]
    if url:
        sources = [s for s in sources if url in s["url"].lower()]
    return jsonify(sources)


@app.post("/api/sources")
def create_source():
    payload = request.get_json(silent=True) or {}
    fields, error = _validate_payload(payload)
    if error:
        return jsonify({"error": error}), 400
    now = datetime.now().isoformat(timespec="seconds")
    source = {"id": uuid.uuid4().hex[:12], **fields, "created_at": now, "updated_at": now}
    source["images"] = _process_images(source["id"], payload.get("images"))
    with _lock:
        sources = _load_sources()
        sources.append(source)
        _save_sources(sources)
    return jsonify(source), 201


@app.put("/api/sources/<source_id>")
def update_source(source_id: str):
    payload = request.get_json(silent=True) or {}
    fields, error = _validate_payload(payload)
    if error:
        return jsonify({"error": error}), 400
    with _lock:
        sources = _load_sources()
        for source in sources:
            if source["id"] == source_id:
                source.update(fields)
                source["images"] = _process_images(source_id, payload.get("images"))
                source["updated_at"] = datetime.now().isoformat(timespec="seconds")
                _save_sources(sources)
                return jsonify(source)
    return jsonify({"error": "数据源不存在"}), 404


@app.delete("/api/sources/<source_id>")
def delete_source(source_id: str):
    with _lock:
        sources = _load_sources()
        remaining = [s for s in sources if s["id"] != source_id]
        if len(remaining) == len(sources):
            return jsonify({"error": "数据源不存在"}), 404
        _save_sources(remaining)
    directory = UPLOAD_DIR / source_id
    if directory.exists():
        for f in directory.iterdir():
            f.unlink(missing_ok=True)
        directory.rmdir()
    return jsonify({"ok": True})


@app.get("/api/sources/<source_id>/images/<name>")
def get_image(source_id: str, name: str):
    if not _IMAGE_NAME_RE.fullmatch(name):
        return jsonify({"error": "非法文件名"}), 400
    path = UPLOAD_DIR / source_id / name
    if not path.exists():
        return jsonify({"error": "图片不存在"}), 404
    return send_file(path)


@app.post("/api/sources/<source_id>/generate")
def generate(source_id: str):
    with _lock:
        source = next((s for s in _load_sources() if s["id"] == source_id), None)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404

    # 1. capture the live page so the LLM writes against the real DOM
    reference_html = None
    fetch_note = None
    try:
        reference_html = fetch_page_html(source["url"])
        Path(_script_path(source)).with_suffix(".page.html").write_text(
            reference_html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - report any capture failure to the UI
        fetch_note = f"页面抓取失败（{exc}），已改为仅依据提示词与示例 JSON 生成"

    # prompt images (if any) as data URLs for the vision-capable LLM
    image_data_urls: list[str] = []
    for name in source.get("images") or []:
        path = UPLOAD_DIR / source_id / name
        if path.exists():
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            image_data_urls.append(f"data:image/{path.suffix.lstrip('.')};base64,{encoded}")

    # 2-3. LLM writes the script; local validation + feedback retries
    stem = _script_path(source).stem

    def keep_failed_code(attempt_no: int, code: str, ok: bool, message: str) -> None:
        if not ok:  # keep rejected attempts on disk for diagnosis
            (GENERATED_DIR / f"{stem}_failed_attempt{attempt_no}.py").write_text(
                code, encoding="utf-8")

    try:
        code, attempts = generate_script(_load_settings(), source, reference_html,
                                         on_attempt=keep_failed_code,
                                         images=image_data_urls)
    except ScriptGenerationError as exc:
        return jsonify({"error": str(exc), "attempts": exc.attempts,
                        "fetch_note": fetch_note}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "fetch_note": fetch_note}), 400

    # 4. persist
    path = _script_path(source)
    path.write_text(code, encoding="utf-8")
    return jsonify({
        "path": str(path),
        "filename": path.name,
        "code": code,
        "attempts": attempts,
        "validation": attempts[-1]["message"],
        "fetch_note": fetch_note,
    })


@app.get("/api/sources/<source_id>/download")
def download(source_id: str):
    with _lock:
        source = next((s for s in _load_sources() if s["id"] == source_id), None)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    path = _script_path(source)
    if not path.exists():
        return jsonify({"error": "脚本尚未生成，请先点击「初始化」"}), 404
    return send_file(path, as_attachment=True, download_name=path.name)


@app.post("/api/sources/<source_id>/run")
def run_script(source_id: str):
    """Execute the generated script for real (live fetch) and return its log
    plus the output JSON text."""
    with _lock:
        source = next((s for s in _load_sources() if s["id"] == source_id), None)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    script_path = _script_path(source)
    if not script_path.exists():
        return jsonify({"error": "脚本尚未生成，请先点击「初始化」"}), 404

    out_path = script_path.with_suffix(".json")
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(script_path), "-o", str(out_path)],
            capture_output=True, text=True, errors="replace",
            timeout=RUN_TIMEOUT_S, cwd=str(GENERATED_DIR),
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return jsonify({
            "ok": False, "exit_code": None,
            "duration_s": round(time.time() - started, 1),
            "stdout": "", "stderr": f"执行超时（>{RUN_TIMEOUT_S}s），已终止",
            "output_path": str(out_path), "result_text": "",
        })
    duration = round(time.time() - started, 1)

    result_text = ""
    if out_path.exists():
        try:
            result_text = out_path.read_text(encoding="utf-8")
        except OSError:
            result_text = ""
    return jsonify({
        "ok": exit_code == 0 and bool(result_text),
        "exit_code": exit_code,
        "duration_s": duration,
        "stdout": stdout,
        "stderr": stderr,
        "output_path": str(out_path),
        "result_text": result_text,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
