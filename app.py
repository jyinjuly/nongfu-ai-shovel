"""Data-source manager: configure scraping sources, generate Python scripts
in real time via an LLM (OpenAI-compatible API).

Each data source = {name, url, prompt, example JSON}. Clicking
"初始化" (initialize) makes the server:
  1. fetch the page live with Playwright's bundled Chromium to capture
     page.content(),
  2. send config + prompt + example JSON + table HTML fragment to the LLM,
  3. validate the produced script locally (syntax + a live run that really
     fetches the page + output-structure check), retrying with feedback up to
     3 rounds,
  4. save the script under data/<name>/generated/ for download.

Run:  python app.py   ->  http://127.0.0.1:8765
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

sys.dont_write_bytecode = True  # keep the project tree free of __pycache__/

import storage
from llm_codegen import ScriptGenerationError, generate_script, refine_script

BASE_DIR = Path(__file__).resolve().parent
# one merged tree: data/<datasource name>/uploads + data/<datasource name>/generated
DATA_DIR = BASE_DIR / "data"
RUN_TIMEOUT_S = 300

storage.init_db()

app = Flask(__name__)
app.json.sort_keys = False  # keep JSON key order as configured by the user


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


def _process_images(source: dict, entries) -> list[str]:
    """Persist prompt images for a source. ``entries`` mixes data URLs (new
    uploads from the browser) and existing filenames (kept ones); files no
    longer referenced are removed. Returns the stored filenames. The group
    directory is only created when there is something to store — creating or
    editing a source without images leaves no trace under data/."""
    directory = _source_dir(source) / "uploads"
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
            directory.mkdir(parents=True, exist_ok=True)
            try:
                (directory / name).write_bytes(base64.b64decode(match.group(2)))
            except (ValueError, OSError):
                continue
            kept.append(name)
        elif _IMAGE_NAME_RE.fullmatch(entry) and (directory / entry).exists():
            kept.append(entry)
    if directory.is_dir():
        for existing in directory.iterdir():
            if existing.name not in kept:
                existing.unlink(missing_ok=True)
        if not kept and not any(directory.iterdir()):
            try:
                directory.rmdir()  # nothing left: no empty dirs under data/
                _source_dir(source).rmdir()  # drop the group dir too when empty
            except OSError:
                pass
    return kept


_WIN_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _source_dir(source: dict) -> Path:
    """Group directory for one source: data/<datasource name>/ — the name is
    kept readable (Chinese included); only characters a Windows path cannot
    hold are replaced."""
    safe = _WIN_ILLEGAL_RE.sub("_", source["name"]).strip(" .") or "未命名"
    if safe.upper() in _WIN_RESERVED:
        safe = "_" + safe
    return DATA_DIR / safe


def _script_path(source: dict) -> Path:
    # the full id as the filename keeps artifacts unique even when two source
    # names sanitize down to the same group directory
    return _source_dir(source) / "generated" / f"{source['id']}.py"


def _download_name(source: dict) -> str:
    safe = re.sub(r"[^0-9A-Za-z._-]+", "_", source["name"]).strip("_") or "datasource"
    return f"{safe}_{source['id'][:6]}.py"


def _try_replace(src: Path, dst: Path) -> None:
    """Move one file, tolerating the transient Windows locks of a just-served
    or antivirus-scanned file: retry briefly, then leave the file behind
    (best-effort — the caller never fails because of one locked file)."""
    for _ in range(5):
        try:
            src.replace(dst)
            return
        except PermissionError:
            time.sleep(0.4)


def _owned_artifact(f: Path, sid: str) -> bool:
    """True when the file in a group's generated/ directory belongs to the
    source with this id. Must not use f.stem: double-suffix artifacts like
    '<id>.page.html' / '<id>.run.log' have stem '<id>.page' / '<id>.run'."""
    if not f.name.startswith(sid):
        return False
    rest = f.name[len(sid):]
    return rest == "" or rest.startswith("_") or rest.startswith(".")


def _move_group_files(source: dict, old_name: str) -> None:
    """Carry a source's files along when a rename changes its group directory.
    Only its own artifacts (id-prefixed files plus the upload images) move, so
    a directory shared with another colliding name keeps that source's files."""
    old_dir = _source_dir({"name": old_name})
    new_dir = _source_dir(source)
    if old_dir == new_dir or not old_dir.is_dir():
        return
    sid = source["id"]
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "generated").mkdir(exist_ok=True)
    (new_dir / "uploads").mkdir(exist_ok=True)
    old_gen = old_dir / "generated"
    if old_gen.is_dir():
        for f in old_gen.iterdir():
            if _owned_artifact(f, sid):
                _try_replace(f, new_dir / "generated" / f.name)
    old_up = old_dir / "uploads"
    if old_up.is_dir():
        for f in old_up.iterdir():
            if f.is_file():
                _try_replace(f, new_dir / "uploads" / f.name)
    for d in (old_gen, old_up, old_dir):  # drop the old directory once empty
        try:
            d.rmdir()
        except OSError:
            break


def _delete_source_files(source: dict) -> None:
    """Remove a source's artifacts; the group directory itself is deleted only
    when nothing belonging to another source is left inside."""
    group = _source_dir(source)
    sid = source["id"]
    gen = group / "generated"
    if gen.is_dir():
        for f in gen.iterdir():
            if _owned_artifact(f, sid):
                try:
                    f.unlink(missing_ok=True)
                except OSError:  # a locked file keeps the directory around
                    pass
    up = group / "uploads"
    if up.is_dir():
        for f in up.iterdir():
            if f.is_file():
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
    for d in (gen, up, group):
        try:
            d.rmdir()
        except OSError:
            break


def _migrate_legacy_layout() -> None:
    """One-time move from the old flat layout (uploads/<id>/, generated/
    <name>_<id6>.*) into data/<name>/{uploads,generated}/ with id-prefixed
    filenames. Files matching no known source are left where they are."""
    old_generated = BASE_DIR / "generated"
    old_uploads = BASE_DIR / "uploads"
    if not (old_generated.exists() or old_uploads.exists()):
        return
    moved = 0
    for source in storage.list_sources():
        group = _source_dir(source)
        sid, sid6 = source["id"], source["id"][:6]
        old_up = old_uploads / sid
        if old_up.is_dir():
            (group / "uploads").mkdir(parents=True, exist_ok=True)
            for f in old_up.iterdir():
                try:
                    shutil.move(str(f), group / "uploads" / f.name)
                    moved += 1
                except OSError:
                    pass
            try:
                old_up.rmdir()
            except OSError:
                pass
        matches = [f for f in old_generated.glob(f"*{sid6}*") if f.is_file()]
        if matches:
            gen_dir = group / "generated"
            gen_dir.mkdir(parents=True, exist_ok=True)
            for f in matches:
                rest = f.stem[f.stem.rfind(sid6) + len(sid6):]
                try:
                    shutil.move(str(f), gen_dir / f"{sid}{rest}{f.suffix}")
                    moved += 1
                except OSError:
                    pass
    for d in (old_uploads, old_generated):  # drop the old dirs once empty;
        if d.is_dir() and not any(d.iterdir()):  # a locked dir waits for a later start
            try:
                d.rmdir()
            except OSError:
                pass
    if moved:
        print(f"[layout] 已迁移 {moved} 个文件到 data/<数据源名称>/ 新目录结构")


_migrate_legacy_layout()


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
    resp = app.response_class(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-cache"  # frontend edits must show up on reload
    return resp


# --- LLM settings -----------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    settings = storage.load_settings()
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
    settings = storage.load_settings()
    if "base_url" in payload:
        settings["base_url"] = (payload.get("base_url") or "").strip()
    if "model" in payload:
        settings["model"] = (payload.get("model") or "").strip()
    key = (payload.get("api_key") or "").strip()
    if key and "***" not in key:  # empty/masked value = keep existing
        settings["api_key"] = key
    storage.save_settings(settings)
    return get_settings()


@app.post("/api/settings/test")
def test_settings():
    settings = storage.load_settings()
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
    sources = storage.list_sources()
    if name:
        sources = [s for s in sources if name in s["name"].lower()]
    if url:
        sources = [s for s in sources if url in s["url"].lower()]
    for s in sources:  # has a generated script? drives the 初始化/重置 button
        s["initialized"] = _script_path(s).exists()
    return jsonify(sources)


@app.post("/api/sources")
def create_source():
    payload = request.get_json(silent=True) or {}
    fields, error = _validate_payload(payload)
    if error:
        return jsonify({"error": error}), 400
    now = datetime.now().isoformat(timespec="seconds")
    source = {"id": uuid.uuid4().hex[:12], **fields, "created_at": now, "updated_at": now}
    source["images"] = _process_images(source, payload.get("images"))
    storage.insert_source(source)
    return jsonify(source), 201


@app.put("/api/sources/<source_id>")
def update_source(source_id: str):
    payload = request.get_json(silent=True) or {}
    fields, error = _validate_payload(payload)
    if error:
        return jsonify({"error": error}), 400
    existing = storage.get_source(source_id)
    if existing is None:
        return jsonify({"error": "数据源不存在"}), 404
    renamed = {**existing, **fields}
    _move_group_files(renamed, existing["name"])  # a rename moves the files too
    fields["images"] = _process_images(renamed, payload.get("images"))
    fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
    storage.update_source(source_id, fields)
    return jsonify({**existing, **fields})


@app.delete("/api/sources/<source_id>")
def delete_source(source_id: str):
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    storage.delete_source(source_id)
    _delete_source_files(source)
    return jsonify({"ok": True})


@app.post("/api/sources/<source_id>/copy")
def copy_source(source_id: str):
    """Duplicate a source: same url/prompt/example/images, new id, and a name
    of '<name> Copy', then '<name> Copy_2', '<name> Copy_3', … The generated
    script is NOT copied — the new source needs its own 初始化."""
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404

    existing = {s["name"] for s in storage.list_sources()}
    name = f"{source['name']} Copy"
    n = 2
    while name in existing:
        name = f"{source['name']} Copy_{n}"
        n += 1

    now = datetime.now().isoformat(timespec="seconds")
    new_source = {"id": uuid.uuid4().hex[:12], "name": name,
                  "url": source["url"], "prompt": source["prompt"],
                  "example": source["example"], "images": [],
                  "created_at": now, "updated_at": now}

    # carry the prompt images over (same filenames, new group directory)
    old_uploads = _source_dir(source) / "uploads"
    if source.get("images") and old_uploads.is_dir():
        new_uploads = _source_dir(new_source) / "uploads"
        new_uploads.mkdir(parents=True, exist_ok=True)
        kept = []
        for fname in source["images"]:
            f = old_uploads / fname
            if f.is_file():
                try:
                    shutil.copy2(str(f), str(new_uploads / fname))
                    kept.append(fname)
                except OSError:
                    pass
        new_source["images"] = kept

    storage.insert_source(new_source)
    return jsonify(new_source), 201


@app.post("/api/sources/<source_id>/reset")
def reset_source(source_id: str):
    """Undo 初始化: remove the source's artifacts under data/<name>/generated/
    (script, outputs, page snapshot, logs). Prompt images (uploads/) are part
    of the config and are kept."""
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    gen = _source_dir(source) / "generated"
    sid = source["id"]
    if gen.is_dir():
        for f in gen.iterdir():
            if _owned_artifact(f, sid):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
        try:
            gen.rmdir()  # only when nothing (of any source) is left inside
        except OSError:
            pass
    return jsonify({"ok": True})


@app.get("/api/sources/<source_id>/images/<name>")
def get_image(source_id: str, name: str):
    if not _IMAGE_NAME_RE.fullmatch(name):
        return jsonify({"error": "非法文件名"}), 400
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    path = _source_dir(source) / "uploads" / name
    if not path.exists():
        return jsonify({"error": "图片不存在"}), 404
    return send_file(path)


# ---------------------------------------------------------------------------
# Live generation progress (polled by the UI during POST /generate)
# ---------------------------------------------------------------------------
_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _progress_reset(source_id: str) -> None:
    with _progress_lock:
        _progress[source_id] = {"events": [], "done": False, "ok": False}


def _progress_emit(source_id: str, text: str, status: str = "info") -> None:
    """Add one progress step. status: running | ok | fail | info | phase.
    A result (ok/fail/info) replaces a trailing running event, so each step
    stays one line: spinner while in progress, outcome in place when done."""
    with _progress_lock:
        entry = _progress.setdefault(
            source_id, {"events": [], "done": False, "ok": False})
        events = entry["events"]
        if status in ("ok", "fail", "info") and events and events[-1]["status"] == "running":
            events[-1] = {"text": text, "status": status}
        else:
            events.append({"text": text, "status": status})


def _progress_done(source_id: str, ok: bool) -> None:
    with _progress_lock:
        entry = _progress.setdefault(
            source_id, {"events": [], "done": False, "ok": False})
        entry["done"] = True
        entry["ok"] = ok


@app.get("/api/sources/<source_id>/generate/progress")
def generate_progress(source_id: str):
    """Step list of the latest POST /generate run for this source (in-memory,
    per-process; a new run resets it). The UI polls this every second."""
    with _progress_lock:
        entry = _progress.get(source_id)
        if entry is None:
            return jsonify({"events": [], "done": True, "ok": False})
        return jsonify({"events": list(entry["events"]),
                        "done": entry["done"], "ok": entry["ok"]})


@app.post("/api/sources/<source_id>/generate")
def generate(source_id: str):
    _progress_reset(source_id)
    _progress_emit(source_id, "阶段 1 · 准备", "phase")
    _progress_emit(source_id, "读取数据源配置", "running")
    source = storage.get_source(source_id)
    if source is None:
        _progress_emit(source_id, "数据源不存在", "fail")
        _progress_done(source_id, False)
        return jsonify({"error": "数据源不存在"}), 404
    _progress_emit(source_id, "读取数据源配置成功", "ok")

    # 1. capture the live page so the LLM writes against the real DOM
    path = _script_path(source)
    reference_html = None
    fetch_note = None
    _progress_emit(source_id, "抓取参考页面", "running")
    try:
        reference_html = fetch_page_html(source["url"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.with_suffix(".page.html").write_text(
            reference_html, encoding="utf-8")
        _progress_emit(source_id, f"抓取参考页面成功（{len(reference_html) // 1000} KB）", "ok")
    except Exception as exc:  # noqa: BLE001 - report any capture failure to the UI
        fetch_note = f"页面抓取失败（{exc}），已改为仅依据提示词与示例 JSON 生成"
        _progress_emit(source_id, f"抓取参考页面失败：{exc}", "fail")

    # prompt images (if any) as data URLs for the vision-capable LLM
    image_data_urls: list[str] = []
    for name in source.get("images") or []:
        img_path = _source_dir(source) / "uploads" / name
        if img_path.exists():
            encoded = base64.b64encode(img_path.read_bytes()).decode("ascii")
            image_data_urls.append(f"data:image/{img_path.suffix.lstrip('.')};base64,{encoded}")
    _progress_emit(source_id, f"获取提示词配图成功（{len(image_data_urls)} 张）" if image_data_urls
                   else "获取提示词配图成功（无）", "ok")

    settings = storage.load_settings()
    _progress_emit(source_id, f"读取 LLM 设置成功（{settings.get('model') or '未配置'}）", "ok")

    _progress_emit(source_id, "阶段 2 · LLM 生成与验证", "phase")

    # 2-3. LLM writes the script; local validation + feedback retries

    def keep_failed_code(attempt_no: int, code: str, ok: bool, message: str) -> None:
        if not ok:  # keep rejected attempts on disk for diagnosis
            (path.parent / f"{path.stem}_failed_attempt{attempt_no}.py").write_text(
                code, encoding="utf-8")

    try:
        code, attempts = generate_script(
            settings, source, reference_html,
            on_attempt=keep_failed_code, images=image_data_urls,
            on_event=lambda text, status="info": _progress_emit(source_id, text, status))
    except ScriptGenerationError as exc:
        _progress_emit(source_id, "生成失败：全部尝试均未通过验证", "fail")
        _progress_done(source_id, False)
        return jsonify({"error": str(exc), "attempts": exc.attempts,
                        "fetch_note": fetch_note}), 502
    except RuntimeError as exc:
        _progress_emit(source_id, f"生成失败：{exc}", "fail")
        _progress_done(source_id, False)
        return jsonify({"error": str(exc), "fetch_note": fetch_note}), 400

    # 4. persist
    _progress_emit(source_id, "阶段 3 · 保存", "phase")
    _progress_emit(source_id, f"保存脚本 {path.name}", "running")
    path.write_text(code, encoding="utf-8")
    _progress_emit(source_id, f"生成成功：已保存到 {path.name}", "ok")
    _progress_done(source_id, True)
    return jsonify({
        "path": str(path),
        "filename": path.name,
        "code": code,
        "attempts": attempts,
        "validation": attempts[-1]["message"],
        "fetch_note": fetch_note,
    })


@app.post("/api/sources/<source_id>/refine")
def refine(source_id: str):
    """Chat-based script debugging: apply the user's feedback to the generated
    script via the LLM, validate locally, and persist on success."""
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip()
    prior = [str(x).strip() for x in (payload.get("history") or []) if str(x).strip()]
    # chat images: data URLs only, this round only — never written to disk
    images = [x for x in (payload.get("images") or [])
              if isinstance(x, str) and _DATA_URL_RE.match(x)][:8]
    if not message:
        return jsonify({"error": "请输入修改要求"}), 400
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    path = _script_path(source)
    if not path.exists():
        return jsonify({"error": "脚本尚未生成，请先点击「初始化」"}), 400
    try:
        current_code = path.read_text(encoding="utf-8")
    except OSError as exc:
        return jsonify({"error": f"读取脚本失败: {exc}"}), 500

    # context for the model: the captured page (if any) and the last real run
    reference_html = None
    page_html_path = path.with_suffix(".page.html")
    if page_html_path.exists():
        try:
            reference_html = page_html_path.read_text(encoding="utf-8")
        except OSError:
            reference_html = None
    out_path = path.with_suffix(".json")
    result_excerpt = ""
    if out_path.exists():
        try:
            result_excerpt = out_path.read_text(encoding="utf-8")[:6000]
        except OSError:
            result_excerpt = ""
    log_path = path.with_suffix(".run.log")
    log_excerpt = ""
    if log_path.exists():
        try:
            log_excerpt = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            log_excerpt = ""

    stem = path.stem

    def keep_failed_code(attempt_no: int, code: str, ok: bool, msg: str) -> None:
        if not ok:
            (path.parent / f"{stem}_refine_failed_attempt{attempt_no}.py").write_text(
                code, encoding="utf-8")

    try:
        code, attempts = refine_script(
            storage.load_settings(), source, current_code, reference_html, message,
            prior_feedbacks=prior, result_excerpt=result_excerpt,
            log_excerpt=log_excerpt, on_attempt=keep_failed_code, images=images)
    except ScriptGenerationError as exc:
        return jsonify({"error": str(exc), "attempts": exc.attempts}), 502
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 400

    path.write_text(code, encoding="utf-8")
    return jsonify({"code": code, "attempts": attempts,
                    "validation": attempts[-1]["message"]})


@app.get("/api/sources/<source_id>/download")
def download(source_id: str):
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    path = _script_path(source)
    if not path.exists():
        return jsonify({"error": "脚本尚未生成，请先点击「初始化」"}), 404
    return send_file(path, as_attachment=True, download_name=_download_name(source))


def _execute_script(script_path: Path) -> dict:
    """Run a generated script live (`-o out.json`), persist its run log for the
    refine flow, and return run diagnostics including the output JSON text."""
    out_path = script_path.with_suffix(".json")
    log_path = script_path.with_suffix(".run.log")
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-B", str(script_path), "-o", str(out_path)],
            capture_output=True, text=True, errors="replace",
            timeout=RUN_TIMEOUT_S, cwd=str(script_path.parent),
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        log_path.write_text(f"执行超时（>{RUN_TIMEOUT_S}s），已终止", encoding="utf-8")
        return {
            "ok": False, "exit_code": None,
            "duration_s": round(time.time() - started, 1),
            "stdout": "", "stderr": f"执行超时（>{RUN_TIMEOUT_S}s），已终止",
            "output_path": str(out_path), "result_text": "",
        }
    duration = round(time.time() - started, 1)

    # persist the run log so a later refine round can show it to the LLM
    log_path.write_text(
        ((stdout or "") + "\n--- stderr ---\n" + (stderr or "")).strip(),
        encoding="utf-8")

    result_text = ""
    if out_path.exists():
        try:
            result_text = out_path.read_text(encoding="utf-8")
        except OSError:
            result_text = ""
    return {
        "ok": exit_code == 0 and bool(result_text),
        "exit_code": exit_code,
        "duration_s": duration,
        "stdout": stdout,
        "stderr": stderr,
        "output_path": str(out_path),
        "result_text": result_text,
    }


@app.post("/api/sources/<source_id>/run")
def run_script(source_id: str):
    """Execute the generated script for real (live fetch) and return its log
    plus the output JSON text."""
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    script_path = _script_path(source)
    if not script_path.exists():
        return jsonify({"error": "脚本尚未生成，请先点击「初始化」"}), 404
    return jsonify(_execute_script(script_path))


@app.get("/api/sources/<source_id>/data")
def get_source_data(source_id: str):
    """RESTful data access for other systems: run the source's script live and
    return the scraped JSON itself as the response body."""
    source = storage.get_source(source_id)
    if source is None:
        return jsonify({"error": "数据源不存在"}), 404
    script_path = _script_path(source)
    if not script_path.exists():
        return jsonify({"error": "脚本尚未生成，请联系管理员处理！"}), 404

    info = _execute_script(script_path)
    if not info["ok"]:
        if info["exit_code"] is None:
            return jsonify({"error": f"脚本执行超时（>{RUN_TIMEOUT_S}s），已终止"}), 504
        detail = info["stderr"].strip() or info["stdout"].strip() or "无输出"
        return jsonify({"error": f"脚本执行失败（exit {info['exit_code']}）：{detail}"}), 502
    try:
        data = json.loads(info["result_text"])
    except json.JSONDecodeError:
        return jsonify({"error": "脚本输出不是合法 JSON"}), 502
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)
