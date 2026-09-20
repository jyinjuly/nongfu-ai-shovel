# 数据源管理平台（LLM 实时生成抓取脚本）

把「Playwright 打开页面 → 点击展开按钮 → `page.content()` → 按示例 JSON 结构解析输出」这一过程配置化：在 Web 页面上维护数据源（URL、提示词、示例 JSON），点击**初始化**由 LLM 实时生成一个可独立运行的 Python 抓取脚本。

页面分两个菜单：

- **模型配置**：LLM 接口地址 / API Key / 模型名 + 测试连接
- **数据源管理**：查询条件区（名称 / URL 模糊匹配）+ 数据源列表；新增与编辑通过弹窗完成，行内操作为 初始化 / 下载脚本 / 编辑 / 删除

## 启动

```bash
python app.py        # 打开 http://127.0.0.1:8765
```

依赖：`flask`、`playwright`（需先 `playwright install chromium`）、`beautifulsoup4`。

## LLM 设置

「模型配置」菜单填写 OpenAI 兼容接口信息（保存在本机 `llm_config.json`，Key 掩码显示）：

- **接口地址 BASE URL**：自动拼接 `/chat/completions`，如 `https://api.deepseek.com/v1`、`https://open.bigmodel.cn/api/paas/v4`、`https://api.openai.com/v1`
- **模型名**：如 `deepseek-chat`、`glm-4.7`、`qwen-plus`、`gpt-4o`
- **API Key**

填完点「测试连接」验证可用性。

## 初始化流程（点击「初始化」按钮后）

1. **现场抓取页面**：服务端用 Playwright 打开 URL，`page.content()` 捕获整页 DOM（同时存为 `generated/*.page.html` 便于排查）
2. **LLM 写脚本**：把名称/URL/提示词/示例 JSON + 页面表格 DOM 片段（保留前 8 行，其余注释省略）+ 提示词配图（如有，视觉消息）发给 LLM，要求输出完整单文件脚本（只依赖 playwright、beautifulsoup4、标准库；自带 Chromium；输出结构与示例 JSON 完全一致；支持 `-o/--url/--headed/--from-html/--save-html` 参数）。是否点击展开按钮等操作意图由提示词描述、LLM 实现
3. **本地验证**：语法编译 → 用抓到的 HTML 干跑 `--from-html` → 校验输出 JSON 的键/分组与示例完全一致
4. **失败重试**：验证不通过时把具体报错喂回 LLM 修正重试，最多 3 轮；全部失败则弹窗展示每次尝试的错误详情

脚本没有模板——解析代码由 LLM 每次现场编写，提示词是真正的生成指令。

## 数据源管理（CRUD）

| 功能 | 说明 |
|------|------|
| 查询 | 列表上方按 名称 / URL 关键字模糊过滤（服务端查询），支持重置 |
| 新增 / 编辑 | 弹窗内填写名称、URL、提示词（发给 LLM 的指令）、上传或粘贴示例 JSON、提示词配图 |
| 提示词配图 | 可选多张截图，随提示词以视觉消息（image_url）发给 LLM 补充页面布局说明，需模型支持视觉；存于 `uploads/<数据源ID>/`，删除数据源时一并清理 |
| 删除 | 删除数据源配置（连同配图目录） |
| 初始化 | 按上述流程生成脚本，弹窗预览代码 + 验证结果，可下载 |
| 测试脚本 | 真实执行已生成的脚本（`python 脚本 -o generated/<同名>.json`，超时 300s），弹窗展示退出码 / 耗时 / 日志 / 输出 JSON，「复制结果 JSON」一键复制完整内容 |
| 下载脚本 | 下载最近一次生成的 `.py` |

存储：`datasources.json`。

## 生成的脚本用法

```bash
python generated/xxx.py                        # 实时抓取 -> <名称>.json
python generated/xxx.py -o out.json --headed   # 指定输出、有头模式
python generated/xxx.py --from-html saved.html # 只解析已保存的 HTML
python generated/xxx.py --save-html dump.html  # 保留原始 HTML
```

## 测试

`tests/mock_llm_server.py` 是一个 OpenAI 兼容的 Mock LLM 服务（第 1 次调用返回语法错误脚本，之后返回可用的解析脚本），用于不依赖真实 LLM Key 测试完整生成链路（抓页 → LLM → 验证 → 重试 → 保存）。用法：

```bash
python tests/mock_llm_server.py &   # 监听 127.0.0.1:8799
# 页面「LLM 设置」填 http://127.0.0.1:8799/v1 / test / mock-model，然后点初始化
```

## 文件

- `app.py` — Flask 服务：CRUD、LLM 设置、生成（抓页→LLM→验证→保存）、下载
- `llm_codegen.py` — LLM 调用、页面上下文提取、脚本验证与重试循环
- `templates/index.html` — 管理页面（单文件，无构建依赖）
- `generated/` — 生成的脚本与调试用页面 HTML（文件名含数据源 ID 后缀）；验证失败的尝试会存档为 `*_failed_attempt<N>.py` 便于排查
- `datasources.json` / `llm_config.json` — 数据源配置 / LLM 设置
