# ClipFlow

智能剪辑长视频的高光片段，并生成可以导入到 DaVinci Resolve 的 EDL 文件。

ClipFlow 是一个本地运行的长视频/直播回放切片工作台。导入本地视频和字幕/转写稿后，系统会自动生成连续切片、片段标题、DaVinci Resolve EDL，以及可下载的项目合集包。没有字幕时，也可以尝试使用本机 ffmpeg + Whisper/faster-whisper 自动转写。

## 一键启动

双击：

```text
start.bat
```

或在 PowerShell 中运行：

```powershell
.\start.ps1
```

如果 PowerShell 拦截脚本：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

启动后浏览器会自动打开。如果端口 `8787` 被占用，服务会自动尝试后续端口，并在启动窗口打印实际地址。

## 页面流程

1. 首页点击“新建项目”。
2. 选择本地视频文件。
3. 选择生成时长：1 分钟、3 分钟、5 分钟、5 分钟以上，或自定义。
4. 上传字幕文件，或粘贴带时间戳的转写稿。也可以不传字幕，系统会自动尝试从视频转文字。
5. 点击“开始处理”。
6. 在项目详情页查看切片、片段字幕、下载 EDL、下载单条 MP4 或完整合集包。
7. 想重新生成切片时，点击“按这个时长重新生成片段”。

## 智能处理步骤

- 素材准备：保存本地视频和字幕到项目目录。
- 自动转写：没有字幕时，使用本机 ffmpeg + Whisper/faster-whisper 生成 `auto_transcript.srt`。
- 字幕处理：转写和上传字幕会尽量统一为中文简体。
- 内容分析：从转写稿识别主题、问题背景、原因、方法和金句。
- 时间线提取：按目标时长输出候选片段。
- 精彩评分：按痛点密度、方法完整度、前因后果和时长评分。
- 标题生成：可选调用 KIMI，为每个片段总结内容并生成标题。
- 视频生成：本机安装 ffmpeg 时自动导出 MP4；否则仍会生成 EDL。

## KIMI 标题

推荐写入本地配置文件：

```text
E:\project\cut\config.json
```

内容示例：

```json
{
  "moonshot_api_key": "你的KIMI_API_KEY",
  "kimi_model": "moonshot-v1-8k",
  "whisper_model": "small",
  "whisper_device": "cpu",
  "whisper_compute_type": "int8",
  "transcribe_language": "zh"
}
```

保存后，页面会自动检测配置并使用 KIMI。也可以打开页面右上角“设置”查看配置文件位置。

## 输出位置

每个项目保存在：

```text
projects/<项目ID>/
```

常见文件：

- `project.json`：项目状态。
- `auto_transcript.srt`：自动转写字幕，如果没有手动上传字幕。
- `candidates.json`：切片结果。
- `candidates.kimi.json`：KIMI 标题结果。
- `clip_*.edl`：达芬奇 EDL。
- `clip_*.mp4`：自动生成的视频切片，需要 ffmpeg。
- `project_export.zip`：完整合集下载包。

## 字幕缓存

同一段视频重复上传时，系统会按视频内容计算 SHA256 指纹。只要视频文件内容完全相同，就会复用已有自动转写字幕，不会重新跑 Whisper。

缓存位置：

```text
cache/transcripts/
```

## 日志

运行日志统一保存在：

```text
logs/
```

常见文件：

- `server.out.log`：启动和普通输出。
- `server.err.log`：错误输出。
- `access.log`：浏览器请求访问日志。

## 命令行模式

也可以只用字幕跑分析：

```powershell
python cut_system.py .\transcript.txt --out .\outputs --min-seconds 45 --max-seconds 210
```
