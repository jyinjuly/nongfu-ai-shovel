# 数据源管理平台（LLM 实时生成抓取脚本）

把「Playwright 打开页面 → 点击展开按钮 → `page.content()` → 按示例 JSON 结构解析输出」这一过程配置化：在 Web 页面上维护数据源（URL、提示词、示例 JSON），点击**初始化**由 LLM 实时生成一个可独立运行的 Python 抓取脚本。

页面分两个菜单：

- **模型配置**：LLM 接口地址 / API Key / 模型名 + 测试连接
- **数据源管理**：查询条件区（名称 / URL 模糊匹配）+ 数据源列表；新增与编辑通过弹窗完成，行内操作为 初始化（已初始化则显示 重置）/ 测试脚本 / 下载脚本 / 复制 / 编辑 / 删除

## 启动

```bash
python app.py        # 打开 http://127.0.0.1:8765
```

依赖：`flask`、`playwright`（需先 `playwright install chromium`）、`beautifulsoup4`。

## LLM 设置

「模型配置」菜单填写 OpenAI 兼容接口信息（保存在本机 SQLite 数据库 `shovel.db`，Key 掩码显示）：

- **接口地址 BASE URL**：自动拼接 `/chat/completions`，如 `https://api.deepseek.com/v1`、`https://open.bigmodel.cn/api/paas/v4`、`https://api.openai.com/v1`
- **模型名**：如 `deepseek-chat`、`glm-4.7`、`qwen-plus`、`gpt-4o`
- **API Key**

填完点「测试连接」验证可用性。

## 初始化流程（点击「初始化」按钮后）

1. **现场抓取页面**：服务端用 Playwright 打开 URL，`page.content()` 捕获整页 DOM（同时存为 `data/<数据源名称>/generated/<数据源ID>.page.html` 便于排查）
2. **LLM 写脚本**：把名称/URL/提示词/示例 JSON + 页面表格 DOM 片段（保留前 8 行，其余注释省略）+ 提示词配图（如有，视觉消息）发给 LLM，要求输出完整单文件脚本（只依赖 playwright、beautifulsoup4、标准库；自带 Chromium；输出结构与示例 JSON 完全一致；支持 `-o/--url/--headed/--from-html/--save-html` 参数）。是否点击展开按钮等操作意图由提示词描述、LLM 实现
3. **本地验证**：语法编译 → **真实执行脚本**（实时抓取目标页面，页面交互如点击展开由脚本按提示词自行完成，超时 300s）→ 校验实时抓取输出 JSON 的键/分组与示例完全一致
4. **失败重试**：验证不通过时把具体报错喂回 LLM 修正重试，最多 3 轮；全部失败则弹窗展示每次尝试的错误详情

脚本没有模板——解析代码由 LLM 每次现场编写，提示词是真正的生成指令。

示例 JSON 支持**动态占位组**：顶层键以 `*` 开头（如 `"*models"`）表示「按输出记录中 `models` 数组的每个真实值展开为同名顶层分组，内部结构与占位组的值完全一致」，适合「每个模型/日期/类别一个分组」这类顶层键名要运行时才能确定的页面；`models` 数组本身填页面上的真实值（示例里的元素只是占位示意），输出不得保留 `*...` 占位键，结构比对时也会按此规则校验。

## 数据源管理（CRUD）

| 功能 | 说明 |
|------|------|
| 查询 | 列表上方按 名称 / URL 关键字模糊过滤（服务端查询），支持重置 |
| 新增 / 编辑 | 弹窗内填写名称、URL、提示词（发给 LLM 的指令）、上传或粘贴示例 JSON、提示词配图 |
| 提示词配图 | 可选多张截图，随提示词以视觉消息（image_url）发给 LLM 补充页面布局说明，需模型支持视觉；存于 `data/<数据源名称>/uploads/`（仅在有配图时创建目录），删除数据源时一并清理 |
| 删除 | 删除数据源配置（连同配图目录） |
| 初始化 | 按上述流程生成脚本，弹窗预览代码 + 验证结果，可下载 |
| 测试脚本 | 真实执行已生成的脚本（`python 脚本 -o <同名>.json`，超时 300s），弹窗展示退出码 / 耗时 / 日志 / 输出 JSON，「复制结果 JSON」一键复制完整内容 |
| 下载脚本 | 下载最近一次生成的 `.py` |
| 复制 | 一键复制数据源（URL / 提示词 / 示例 JSON / 配图相同），名称为「原名 Copy」，再次复制为 Copy_2、Copy_3 …；复制品需重新初始化生成脚本 |
| 重置 | 已初始化的数据源按钮变为「重置」，点击删除 `data/<名称>/generated/` 下的脚本及全部运行产物（配图 uploads/ 保留），恢复为未初始化状态，可重新初始化 |
| AI 调试对话 | 编辑弹窗底部（或测试结果里「AI 调整脚本」进入）：反馈文字 + 截图发给 LLM 修改脚本并本地验证；点「保存」时，对话里输入的文字追加进数据源提示词、截图并入提示词配图一并保存 |

存储：SQLite 数据库 `shovel.db`（数据源配置 + LLM 设置）。首次启动时自动导入旧的 `datasources.json` / `llm_config.json`，导入后将其改名为 `*.json.migrated` 留作本机备份，不再重复导入。

文件布局：每个数据源一个目录 `data/<数据源名称>/`，内部再分 `uploads/`（提示词配图）与 `generated/`（脚本及运行产物，文件以数据源 ID 为前缀，如 `bd3312ba99e8.py`、`bd3312ba99e8.json`）；重命名数据源时整个目录随之搬移。首次启动时自动把旧的平铺 `uploads/`、`generated/` 迁移到新结构。

## 对外 REST 接口

```
GET /api/sources/<数据源ID>/data
```

给其他系统取数用：实时执行该数据源已生成的抓取脚本，**响应体就是抓取到的 JSON 数据本身**（不是包了一层的日志/状态）。错误时返回 `{"error": "..."}` 并带对应状态码：404 = 数据源不存在或脚本尚未初始化，502 = 脚本执行失败或输出不是合法 JSON，504 = 执行超时（>300s）。

```bash
curl http://127.0.0.1:8765/api/sources/bd3312ba99e8/data
```

## 生成的脚本用法

```bash
python "data/<数据源名称>/generated/<数据源ID>.py"                        # 实时抓取 -> <名称>.json
python "data/<数据源名称>/generated/<数据源ID>.py" -o out.json --headed   # 指定输出、有头模式
python "data/<数据源名称>/generated/<数据源ID>.py" --from-html saved.html # 只解析已保存的 HTML
python "data/<数据源名称>/generated/<数据源ID>.py" --save-html dump.html  # 保留原始 HTML
```

## 测试

（暂无自动化测试。生成链路需要真实 LLM Key 与可访问的目标页面。）

## 文件

- `app.py` — Flask 服务：CRUD、LLM 设置、生成（抓页→LLM→验证→保存）、下载、对外取数接口 `GET /api/sources/<id>/data`
- `storage.py` — SQLite 存储层（数据源配置、LLM 设置、旧 JSON 一次性导入）
- `llm_codegen.py` — LLM 调用、页面上下文提取、脚本验证与重试循环
- `templates/index.html` — 管理页面（单文件，无构建依赖）
- `data/` — 按数据源名称分组：`data/<名称>/uploads/` 提示词配图，`data/<名称>/generated/` 脚本及运行产物（`<数据源ID>.py/.json/.page.html/.run.log`）；验证失败的尝试会存档为 `<数据源ID>_failed_attempt<N>.py` 便于排查
- `shovel.db` — SQLite 数据库（数据源配置 / LLM 设置，gitignored）
