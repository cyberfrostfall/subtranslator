# SubTranslator

自动字幕翻译服务 - 实时监控媒体目录，自动将外语字幕翻译为双语字幕，适配 Plex/Jellyfin/Emby 等媒体服务器。

## 功能特性

- 实时监控：自动检测新增的字幕文件
- 双语字幕：翻译后保留原文，译文在上，原文在下
- 多语言支持：可配置目标语言（中文、日语、韩语、法语等 12 种语言）
- 高性能、低成本：每 30 条连续字幕合并为一次请求，多批并发翻译，译文能参考上下文
- 智能检测：基于比例阈值自动跳过已翻译的双语字幕
- 失败重试：API 调用失败自动重试
- Web UI：现代化监控界面，实时查看翻译状态
- 内存管理：已处理文件记录自动过期清理（1 小时 TTL）

## 支持的字幕格式

- SRT (.srt)
- ASS (.ass)
- SSA (.ssa)
- WebVTT (.vtt)

## 快速开始

### Docker Compose（推荐）

1. 创建 `.env` 文件：

```env
API_KEY=sk-your-api-key
PROXY_URL=https://your-api-service.example/v1/chat/completions
MODEL=your-model-name
MOVIES_DIR=/mnt/user/media/media/movies
TV_DIR=/mnt/user/media/media/tv
CUSTOM_DIRS=
MAX_WORKERS=10
API_RETRY_COUNT=3
LOG_LEVEL=INFO
WEB_PORT=8095
TARGET_LANG=zh
```

2. 拉取镜像并启动服务：

```console
docker compose pull
docker compose up -d
```

3. 访问 Web UI：`http://your-server-ip:8095`

### Docker Run

```bash
docker run -d \
  --name subtranslator \
  -p 8095:8095 \
  -v /path/to/config:/config \
  -v /path/to/media:/mnt/user/media \
  -e API_KEY=your-api-key \
  -e PROXY_URL=https://your-api-service.example/v1/chat/completions \
  -e MODEL=your-model-name \
  -e TARGET_LANG=zh \
  cyberfrostfall/subtranslator:latest
```

## 环境变量

| 变量 | 说明 | 默认值 | 必需 |
|------|------|--------|------|
| `API_KEY` | API 密钥 | - | 是 |
| `PROXY_URL` | 完整 API 请求端点（无默认值） | - | 是 |
| `MODEL` | 模型名称（无默认值） | - | 是 |
| `TARGET_LANG` | 目标翻译语言 | `zh` | 否 |
| `MOVIES_DIR` | 电影目录 | `/mnt/user/media/media/movies` | 否 |
| `TV_DIR` | 电视剧目录 | `/mnt/user/media/media/tv` | 否 |
| `CUSTOM_DIRS` | 自定义监控目录（逗号分隔） | - | 否 |
| `MAX_WORKERS` | 最大并发 API 请求数（每个请求包含一批字幕） | `10` | 否 |
| `API_RETRY_COUNT` | 单个 API 请求失败后的重试次数（仅重试失败的批次或片段） | `3` | 否 |
| `LOG_LEVEL` | 日志级别 (DEBUG/INFO/WARNING/ERROR) | `INFO` | 否 |
| `WEB_PORT` | Web UI 端口 | `8095` | 否 |
| `CORS_ORIGINS` | 允许跨域访问的来源（逗号分隔；默认禁止跨域） | - | 否 |

`PROXY_URL` 和 `MODEL` 不提供默认值或候选项，必须通过环境变量或已保存的 Web UI 配置自行填写，否则服务会拒绝启动。Web UI 的管理与配置接口不再要求 Admin Token；请通过端口映射、防火墙或反向代理限制访问范围。

Web UI 保存的设置存放在 `/config/settings.json`，采用原子写入和仅属主可读写权限。重启后，文件中的设置会覆盖对应环境变量；未保存到文件的项目继续使用环境变量。API Key 留空或保持 `***` 不会覆盖已有密钥。

### 支持的目标语言

| 代码 | 语言 | 代码 | 语言 |
|------|------|------|------|
| `zh` | 中文 | `fr` | 法语 |
| `en` | 英语 | `de` | 德语 |
| `ja` | 日语 | `es` | 西班牙语 |
| `ko` | 韩语 | `it` | 意大利语 |
| `pt` | 葡萄牙语 | `ru` | 俄语 |
| `ar` | 阿拉伯语 | `th` | 泰语 |

## Web UI

访问 `http://your-server:8095` 可以：

- 实时统计：查看翻译成功/失败数量、正在处理的文件、运行时长
- 文件列表：查看最近翻译的文件及状态
- 翻译内容：实时查看翻译进度和内容
- 系统日志：实时查看服务运行日志
- 配置管理：在线修改配置，无需认证

## 工作原理

1. 服务启动后监控指定目录
2. 检测到新建或移动到监控目录的字幕文件（不含 `.{TARGET_LANG}.` 的文件）
3. 等待文件写入完成（防止翻译未下载完的文件）
4. 检查是否已是双语字幕（目标语言和其他文字各占 20% 以上则跳过）
5. 按每批 30 条把字幕发送给 AI API 翻译；SRT/VTT 按时间轴分段，ASS/SSA 只翻译 `[Events]` 中 Dialogue 行的文本，保留时间和样式标签
6. 全部片段成功后，以原子替换方式保存双语字幕文件（原文件名 + `.{TARGET_LANG}.` 后缀）

文件处理失败时不会生成“成功”输出，并会按 30、60、120 秒最多重试三次。输入编码支持 UTF-8、UTF-8 BOM、UTF-16 和 GB18030。

批次请求失败时，只会按照 `API_RETRY_COUNT` 重试该批次；模型返回的内容无法解析或缺少某些条目时，缺失的条目会逐条补译。批次在重试后仍失败时，该批字幕全部计为失败。重试耗尽后，如果整份字幕成功率达到 99%，失败片段将保留原文，文件会以“部分完成”状态保存；低于 99% 时仍按整份文件失败处理。

## CI/CD

项目使用 GitHub Actions 自动验证和发布镜像：

- Pull Request：构建 `linux/amd64` + `linux/arm64` 双平台镜像，但不推送。
- 推送到 `main`：发布 `latest` 和 `sha-<完整提交 SHA>`。
- 推送版本标签（如 `v1.2.3`）：发布 `1.2.3`、`1.2` 和 `sha-<完整提交 SHA>`。
- 发布镜像包含 OCI 元数据、构建来源证明（provenance）和 SBOM。

镜像地址：`cyberfrostfall/subtranslator`

生产环境建议使用版本标签（例如 `cyberfrostfall/subtranslator:1.1.0`）固定版本；`latest` 会跟随 `main` 持续更新。

## 故障排查

### 容器无法启动
- 检查 `API_KEY` 是否正确设置
- 检查目录映射是否正确
- 查看容器日志：`docker logs subtranslator`

### 字幕未被翻译
- 确认文件在监控目录中
- 检查文件扩展名是否支持
- 查看 Web UI 日志页面
- 确认文件名不包含 `.zh.`（已翻译文件会被跳过）

### API 调用失败
- 检查 API Key 是否有效
- 检查网络连接
- 确认自定义的 `PROXY_URL` 是完整请求地址，且 `MODEL` 为服务商支持的模型名称

## 许可证

MIT License
