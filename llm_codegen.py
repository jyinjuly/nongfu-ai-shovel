"""Real-time scraping-script generation via an OpenAI-compatible LLM API.

There is no script template: the model receives the data-source config (name /
url / prompt), the example JSON that fixes the output shape,
and a fragment of the live page's table HTML, and writes the complete script
itself. The produced code is then validated locally —

  1. syntax check (compile),
  2. a ``--from-html`` dry run against the captured page HTML,
  3. structure check of the dry run's JSON output against the example.

— and failures are fed back to the model for up to MAX_ATTEMPTS rounds.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from bs4 import BeautifulSoup

MAX_ATTEMPTS = 3
CHAT_TIMEOUT_S = 300
DRYRUN_TIMEOUT_S = 120
MAX_CONTEXT_CHARS = 150_000

SYSTEM_PROMPT = """你是一名资深 Python 爬虫工程师。请根据用户提供的【数据源配置】、【示例 JSON】、【目标页面表格 HTML 片段】以及可能附带的页面截图（提示词配图），编写一个完整的、可独立运行的 Python 脚本。

硬性要求：
1. 单文件脚本；依赖只允许 playwright、beautifulsoup4 和 Python 标准库。
2. 浏览器必须用 Playwright 自带 Chromium：p.chromium.launch(headless=...)，禁止使用 channel 参数，禁止下载浏览器。
3. 抓取流程：page.goto(url, wait_until="domcontentloaded") → 等待目标表格/主要内容渲染（适当 wait_for_selector + sleep）→ 若提示词要求页面上有额外的交互（点击、展开、切换等），实现该操作并等待 → page.content() 取整页 HTML → 解析。
4. 解析逻辑由你根据真实 HTML 片段与截图自行设计（如何定位表格、如何处理分组表头与列头、如何归属列到分组）；要稳健：选择器容错、等待充分，不要假设列数固定不变。
5. 输出 JSON 结构必须与示例 JSON 完全一致：相同的顶层键与嵌套分组、相同的键名；页面中匹配不到的列直接丢弃；缺失值（如 "--"）输出为空字符串 ""；不要发明示例中不存在的键。
6. 脚本必须支持命令行参数：-o/--output（输出 JSON 路径，默认 {output_default}）、--url（覆盖 URL）、--headed（有头模式）、--from-html <file>（直接解析已保存的 HTML 文件，绝不启动浏览器）、--save-html <file>（保存 page.content() 原始 HTML）。无论哪种模式、解析结果是否为空，都必须把结果 JSON 写到 -o/--output 指定的路径（解析为空时写出空数组）。
7. 只输出 Python 代码本体：不要 markdown 代码围栏，不要任何解释文字。"""

USER_PROMPT = """【数据源配置】
名称：{name}
URL：{url}
提示词（抓取与解析要求，请严格遵守）：{prompt}

【提示词配图】{images_note}

【示例 JSON —— 输出必须严格符合此结构】
{example}

【目标页面表格 HTML 片段】{table_note}
{table_html}"""


class _EmptyContentError(RuntimeError):
    """The model returned an empty message (e.g. a reasoning model that spent
    its whole output budget thinking)."""


class ScriptGenerationError(Exception):
    """Raised when no LLM attempt passes validation; carries the attempt log."""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Page context extraction
# ---------------------------------------------------------------------------
def extract_table_context(html: str, keep_rows: int = 8,
                          max_chars: int = MAX_CONTEXT_CHARS) -> tuple[str, dict]:
    """Reduce a full page HTML to a compact context for the LLM: the main
    table with only its first ``keep_rows`` rows kept (the rest summarized in
    a comment), scripts/styles stripped. Returns (context, info)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    table = soup.find("table")
    if table is None:
        body = soup.body or soup
        return str(body)[:max_chars], {"has_table": False, "rows": 0, "kept": 0}

    rows = table.find_all("tr")
    kept = rows[:keep_rows]
    attrs = " ".join(
        f'{k}="{" ".join(v) if isinstance(v, list) else v}"'
        for k, v in table.attrs.items()
    )
    open_tag = f"<table {attrs}>" if attrs else "<table>"
    context = open_tag + "".join(str(r) for r in kept)
    if len(rows) > len(kept):
        context += f"<!-- 其余 {len(rows) - len(kept)} 行数据省略，结构与上述行相同 -->"
    context += "</table>"
    info = {"has_table": True, "rows": len(rows), "kept": len(kept)}
    if len(context) > max_chars:
        context = context[:max_chars] + "<!-- 片段过长已被截断 -->"
    return context, info


# ---------------------------------------------------------------------------
# OpenAI-compatible chat client
# ---------------------------------------------------------------------------
def _chat(settings: dict, messages: list[dict], timeout: int = CHAT_TIMEOUT_S) -> str:
    base = (settings.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    payload = {"model": settings["model"], "messages": messages, "temperature": 0.2}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.get('api_key', '')}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"LLM 接口返回 {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"LLM 接口连接失败: {exc}") from exc

    try:
        choice = data["choices"][0]
        content = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"LLM 接口响应格式异常: {json.dumps(data)[:500]}") from exc

    if not content.strip():
        usage = json.dumps(data.get("usage", {}), ensure_ascii=False)
        raise _EmptyContentError(
            f"LLM 返回了空内容（finish_reason={choice.get('finish_reason')}, "
            f"usage={usage}）——常见原因是思考模型的推理耗尽了输出配额，请更换模型或调大输出上限"
        )

    return content


def _strip_fences(text: str) -> str:
    match = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return match.group(1).strip() if match else text.strip()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _shape(record: dict) -> dict:
    return {k: set(v.keys()) if isinstance(v, dict) else None for k, v in record.items()}


def _check_structure(data, example) -> tuple[bool, str]:
    records = data if isinstance(data, list) else [data]
    examples = example if isinstance(example, list) else [example]
    if not records or not all(isinstance(r, dict) for r in records):
        return False, "输出 JSON 应为非空的对象数组"

    # union shape across all example records
    want: dict = {}
    for rec in examples:
        if not isinstance(rec, dict):
            continue
        for key, value in rec.items():
            if isinstance(value, dict):
                want[key] = want.get(key) or set()
                want[key] |= set(value.keys())
            else:
                want[key] = None

    got = _shape(records[0])
    if set(got.keys()) != set(want.keys()):
        return False, (f"顶层键不一致\n  期望: {sorted(want)}\n  实际: {sorted(got)}")
    for key, sub in want.items():
        g = got.get(key)
        if sub is None and g is not None:
            return False, f"键 {key!r} 应为标量，实际是对象"
        if sub is not None:
            if g is None:
                return False, f"分组 {key!r} 应为对象，实际是标量"
            if set(g) != set(sub):
                return False, f"分组 {key!r} 的子键不一致\n  期望: {sorted(sub)}\n  实际: {sorted(g)}"
    for i, rec in enumerate(records):
        if set(_shape(rec).keys()) != set(want.keys()):
            return False, f"第 {i + 1} 条记录的顶层键与示例不一致"
    return True, f"结构校验通过，共 {len(records)} 条记录"


def validate_script(code: str, reference_html: str | None,
                    example) -> tuple[bool, str]:
    try:
        compile(code, "generated_script.py", "exec")
    except SyntaxError as exc:
        return False, f"语法错误: {exc}"

    if not reference_html:
        return True, "通过语法检查（无参考 HTML，跳过运行验证）"

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        (td_path / "page.html").write_text(reference_html, encoding="utf-8")
        (td_path / "script.py").write_text(code, encoding="utf-8")
        out_path = td_path / "out.json"
        try:
            proc = subprocess.run(
                [sys.executable, str(td_path / "script.py"),
                 "--from-html", str(td_path / "page.html"), "-o", str(out_path)],
                capture_output=True, text=True, timeout=DRYRUN_TIMEOUT_S, cwd=td,
            )
        except subprocess.TimeoutExpired:
            return False, (f"运行验证超时（>{DRYRUN_TIMEOUT_S}s）——"
                           "请确认 --from-html 模式不启动浏览器且解析高效")

        # always surface what the script printed; it is the only diagnostic the
        # model gets on retry
        output_tail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-1500:]

        if proc.returncode != 0:
            return False, (f"脚本运行失败（exit {proc.returncode}）：\n{output_tail or '(无输出)'}")

        output_file = out_path if out_path.exists() else None
        note = ""
        if output_file is None:
            # tolerate a script that ignored -o but wrote its default name into cwd
            others = sorted(td_path.glob("*.json"))
            if others:
                output_file = others[0]
                note = f"⚠ 脚本未把输出写到 -o 指定路径（写到了 {output_file.name}），请修正；"
            else:
                return False, (
                    "脚本运行成功但没有写出输出 JSON：-o 指定路径和工作目录下都没有 .json 文件。"
                    "无论解析结果是否为空都必须写出 -o 指定的文件。"
                    f"脚本 stdout/stderr 末尾内容：\n{output_tail or '(无输出)'}"
                )
        try:
            data = json.loads(output_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return False, (f"输出文件不是合法 JSON: {exc}\n"
                           f"脚本 stdout/stderr 末尾内容：\n{output_tail or '(无输出)'}")
    ok, message = _check_structure(data, example)
    if ok and note:
        message = f"{note} {message}"
    return ok, message


# ---------------------------------------------------------------------------
# Generation loop
# ---------------------------------------------------------------------------
def generate_script(settings: dict, config: dict, reference_html: str | None,
                    on_attempt=None, images: list[str] | None = None) -> tuple[str, list[dict]]:
    """Call the LLM to write the whole script, validate, retry with feedback.

    Returns (code, attempts). ``on_attempt(attempt_no, code, ok, message)`` is
    called after every validation round (e.g. to keep failed code for
    debugging). ``images`` are data URLs attached to the prompt as vision
    content blocks. Raises ScriptGenerationError when every attempt fails, and
    RuntimeError for configuration/connection problems.
    """
    missing = [k for k in ("base_url", "api_key", "model") if not settings.get(k)]
    if missing:
        raise RuntimeError("LLM 未配置：请先在「LLM 设置」中填写 " +
                           "、".join({"base_url": "接口地址", "api_key": "API Key", "model": "模型名"}[k] for k in missing))

    default_output = re.sub(r"[^0-9A-Za-z._-]+", "_", config["name"]).strip("_") or "output"
    system = SYSTEM_PROMPT.format(output_default=f"{default_output}.json")

    if reference_html:
        table_html, info = extract_table_context(reference_html)
        table_note = f"完整表格共 {info['rows']} 行，下方保留前 {info['kept']} 行" \
            if info["has_table"] else "页面中未找到 <table>，以下为去掉脚本/样式后的页面片段（请自行判断结构）"
    else:
        table_html, table_note = "（未能获取页面 HTML——请依据提示词与示例 JSON 编写，运行时做好容错与等待）", ""

    user_text = USER_PROMPT.format(
        name=config["name"],
        url=config["url"],
        prompt=config.get("prompt") or "（未填写）",
        images_note=f"已随本消息附带 {len(images)} 张截图，请结合截图理解页面布局" if images else "（无）",
        example=json.dumps(config["example"], ensure_ascii=False, indent=2),
        table_note=table_note,
        table_html=table_html,
    )
    if images:
        # OpenAI vision format: text + image data URLs
        blocks: list[dict] = [{"type": "text", "text": user_text}]
        for data_url in images:
            blocks.append({"type": "image_url", "image_url": {"url": data_url}})
        user_message = {"role": "user", "content": blocks}
    else:
        user_message = {"role": "user", "content": user_text}

    messages: list[dict] = [
        {"role": "system", "content": system},
        user_message,
    ]

    attempts: list[dict] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = _chat(settings, messages)
        except _EmptyContentError as exc:
            attempts.append({"attempt": attempt, "ok": False, "message": str(exc)})
            if on_attempt:
                try:
                    on_attempt(attempt, "", False, str(exc))
                except Exception:  # noqa: BLE001
                    pass
            messages.append({"role": "user", "content": (
                f"上一次调用没有返回任何代码：{exc}\n请直接输出完整的 Python 脚本代码本体，"
                "不要输出空内容。"
            )})
            continue
        code = _strip_fences(raw)
        ok, message = validate_script(code, reference_html, config["example"])
        attempts.append({"attempt": attempt, "ok": ok, "message": message})
        if on_attempt:
            try:
                on_attempt(attempt, code, ok, message)
            except Exception:  # noqa: BLE001 - debugging hook must not break the loop
                pass
        if ok:
            return code, attempts
        messages.append({"role": "assistant", "content": code})
        messages.append({"role": "user", "content": (
            f"上述脚本验证未通过：\n\n{message}\n\n"
            "请修复问题后重新输出完整脚本（仍然只输出代码本体，不要解释）。"
        )})
    raise ScriptGenerationError(f"{MAX_ATTEMPTS} 次尝试均未通过验证，最后一次错误：\n{attempts[-1]['message']}", attempts)
