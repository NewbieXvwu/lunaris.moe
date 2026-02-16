# lunaris

[lunaris.moe](https://lunaris.moe) 的完整离线镜像。lunaris.moe 是一个原神角色数据库与配装规划器。

本仓库包含 lunaris.moe 网站的完整本地副本（前端 + API 数据），可以完全离线浏览。启动本地服务器，打开浏览器即可使用。

[English / 英文文档](README.md)

## 快速开始

环境要求：Python 3.9+，[httpx](https://www.python-httpx.org/)

```bash
pip install httpx starlette hypercorn
python lunaris.py serve
```

在浏览器中打开 `http://localhost:9000/`，即可离线浏览。

## 本地服务器

```bash
python lunaris.py serve
```

启动本地 HTTP 服务器提供镜像站点。服务器会自动将硬编码的 `https://lunaris.moe` 和 `https://api.lunaris.moe` URL 重写为本地地址，确保离线浏览无缝运行。

服务器支持两种引擎，启动时自动选择：

- **ASGI 模式**（推荐）：使用 [Starlette](https://www.starlette.io/) + [Hypercorn](https://hypercorn.readthedocs.io/)，异步 IO 高并发。安装方式：`pip install starlette hypercorn`。
- **stdlib 模式**（备用）：使用 Python 内置的 `http.server`，无额外依赖。适合单用户浏览。

两种模式均支持 ETag/304 条件请求和分级 Cache-Control 头。如需 gzip 压缩，请先运行 `python lunaris.py precompress` 生成 `.gz` 旁文件——服务器会在客户端支持 gzip 时直接发送预压缩文件，避免运行时压缩开销。

选项：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port N` | 9000 | 监听端口（或通过 `LUNARIS_SERVE_PORT` 环境变量设置） |
| `--bind ADDR` | 0.0.0.0 | 绑定地址 |
| `--no-rewrite` | 默认开启重写 | 禁用 URL 重写 |

接口：

- `http://localhost:9000/` -- 浏览站点
- `http://localhost:9000/__api__/...` -- API 数据
- `http://localhost:9000/__verify__/stats` -- 请求统计
- `http://localhost:9000/__verify__/unexpected-missing` -- 缺失资源报告

## 镜像数据

- 前端：约 9,800 个文件（HTML、JS、CSS、字体、图片）
- API：约 5,600 个文件（JSON 数据、角色图标、武器/圣遗物素材）
- 游戏数据版本：6.3.52、6.3.53、6.3.54
- 语言：英语、简体中文、俄语、葡萄牙语

## 生成校验文件

```bash
python lunaris.py sha256
```

生成 `SHA256SUMS.tsv`，包含 `site/` 和 `api/` 下每个文件的哈希值、大小和路径。

## 项目结构

```
lunaris.py              CLI 入口
lunaris/
  common.py             共享常量、工具函数、下载器
  backup.py             阶段 1：HTTP 爬取
  replay.py             阶段 2：Playwright 动态回放
  closure.py            阶段 3：闭包验证
  scan.py               阶段 4：Playwright 全站扫描
  audit.py              阶段 5：UI 交互审计
  server.py             本地服务器
site/                   镜像前端（lunaris.moe）
api/                    镜像 API 数据（api.lunaris.moe）
manifest.jsonl          下载日志（URL、状态、哈希）
SHA256SUMS.tsv          文件完整性校验
```

## 重新抓取

仓库中的镜像数据已经是完整的，通常不需要重新抓取。仅当上游站点更新且你需要新快照时才需要执行此操作。

额外依赖：[Playwright](https://playwright.dev/python/)

```bash
pip install playwright
playwright install chromium
```

```bash
python lunaris.py backup
```

执行 5 阶段抓取流水线：

1. **HTTP 爬取** -- 下载种子 URL、前端资源、API 版本列表、角色数据及语义推导的资源 URL。
2. **动态回放** -- 使用 Playwright 访问采样页面，捕获运行时网络请求以发现额外资源。
3. **闭包验证** -- 扫描所有已下载文件中的嵌入 URL，补抓缺失资源。
4. **全站扫描** -- 使用 Playwright 访问每个路由，记录失败并下载缺失资源。
5. **UI 交互审计** -- 点击 tab、展开折叠面板、滚动页面以触发懒加载资源。

选项：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--concurrency N` | 12 | 下载并发数 |
| `--port N` | 9001 | UI 审计阶段的本地服务器端口 |
| `--skip-browser` | 关 | 跳过 Playwright 阶段（仅 HTTP 爬取） |

## 预压缩

```bash
python lunaris.py precompress
```

扫描 `site/` 和 `api/` 下的可压缩文本文件（`.js .css .html .json .svg .txt .xml`），以最高压缩级别生成 `.gz` 旁文件。≤256 字节的文件会被跳过。重复运行时只压缩比现有 `.gz` 更新的文件。

## 服务端调优

使用 ASGI 模式时，可使用以下额外参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `-w, --workers N` | min(4, CPU 核心数) | Hypercorn worker 数 |
| `--backlog N` | 512 | 连接队列大小 |
| `--access-log PATH` | 关闭 | Access log 路径（`-` 为 stdout） |

## 致谢

[lunaris.moe](https://lunaris.moe) 由 Kuroo 创建。本仓库仅包含离线镜像工具和镜像数据，所有原始内容归其各自作者所有。
