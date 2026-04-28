import html
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
import zipfile
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cut_system import ClipCandidate, format_time, load_local_config, parse_transcript, run, simplify_chinese, write_edl


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT / "projects"
LEGACY_OUTPUT_ROOT = ROOT / "outputs"
CACHE_ROOT = ROOT / "cache"
TRANSCRIPT_CACHE_ROOT = CACHE_ROOT / "transcripts"
LOG_ROOT = ROOT / "logs"
HISTORY = ROOT / "视频号动态数据明细.csv"
PORT = int(os.getenv("TRANSCRIPT_CUT_PORT", "8787"))

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".flv", ".webm"}
TEXT_EXTS = {".srt", ".txt", ".vtt"}

CLIP_DURATION_PRESETS = {
    "1min": (45, 90, "1分钟"),
    "3min": (150, 210, "3分钟"),
    "5min": (240, 330, "5分钟"),
    "over5": (300, 720, "5分钟以上"),
}

STAGES = [
    "素材准备",
    "内容分析",
    "时间线提取",
    "精彩评分",
    "标题生成",
    "合集推荐",
    "视频生成",
]


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def safe_name(filename: str, fallback: str) -> str:
    name = Path(filename or fallback).name.strip().replace("\x00", "")
    return name or fallback


def project_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]


def load_project(pid: str) -> Dict[str, object]:
    path = (PROJECT_ROOT / pid / "project.json").resolve()
    if not str(path).startswith(str(PROJECT_ROOT.resolve())) or not path.exists():
        raise FileNotFoundError(pid)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_project(pid: str, data: Dict[str, object]) -> None:
    folder = PROJECT_ROOT / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data: str) -> None:
        for stream in self.streams:
            stream.write(data)
            stream.flush()

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def setup_file_logging() -> Tuple[object, object]:
    LOG_ROOT.mkdir(exist_ok=True)
    out_file = (LOG_ROOT / "server.out.log").open("a", encoding="utf-8")
    err_file = (LOG_ROOT / "server.err.log").open("a", encoding="utf-8")
    sys.stdout = Tee(sys.stdout, out_file)
    sys.stderr = Tee(sys.stderr, err_file)
    return out_file, err_file


def list_projects() -> List[Dict[str, object]]:
    PROJECT_ROOT.mkdir(exist_ok=True)
    items: List[Dict[str, object]] = []
    for path in PROJECT_ROOT.iterdir():
        if not path.is_dir() or not (path / "project.json").exists():
            continue
        try:
            item = json.loads((path / "project.json").read_text(encoding="utf-8-sig"))
            item["id"] = path.name
            items.append(item)
        except json.JSONDecodeError:
            continue
    return sorted(items, key=lambda item: str(item.get("created_at", "")), reverse=True)


def parse_multipart(content_type: str, body: bytes) -> Dict[str, Tuple[Optional[str], bytes]]:
    marker = "boundary="
    if marker not in content_type:
        return {}
    boundary = content_type.split(marker, 1)[1].strip().strip('"').encode("utf-8")
    fields: Dict[str, Tuple[Optional[str], bytes]] = {}
    for part in body.split(b"--" + boundary):
        part = part.strip()
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].strip()
        if b"\r\n\r\n" not in part:
            continue
        header_raw, data = part.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        headers = header_raw.decode("utf-8", errors="ignore").split("\r\n")
        disposition = next((line for line in headers if line.lower().startswith("content-disposition:")), "")
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', disposition))
        name = attrs.get("name")
        if name:
            fields[name] = (attrs.get("filename") or None, data)
    return fields


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cached_transcript_path(video_hash: str) -> Path:
    return TRANSCRIPT_CACHE_ROOT / f"{video_hash}.srt"


def load_cached_transcript(video_hash: str, destination: Path) -> bool:
    cached = cached_transcript_path(video_hash)
    if not video_hash or not cached.is_file():
        return False
    shutil.copyfile(cached, destination)
    return True


def save_cached_transcript(video_hash: str, transcript_path: Path) -> None:
    if not video_hash or not transcript_path.is_file():
        return
    TRANSCRIPT_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(transcript_path, cached_transcript_path(video_hash))


def srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    millis = int(round((seconds - int(seconds)) * 1000))
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"


def update_project_progress(
    pid: str,
    status: str,
    message: str,
    stage_updates: Optional[Dict[str, str]] = None,
    progress_percent: Optional[float] = None,
) -> None:
    project = load_project(pid)
    stages = dict(project.get("stage_status") or {stage: "pending" for stage in STAGES})
    if stage_updates:
        stages.update(stage_updates)
    progress_log = list(project.get("progress_log") or [])
    if progress_percent is None:
        progress_percent = float(project.get("progress_percent", 0) or 0)
    progress_percent = max(0.0, min(100.0, float(progress_percent)))
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "status": status,
        "message": message,
        "percent": round(progress_percent, 1),
    }
    if not progress_log or progress_log[-1].get("message") != message:
        progress_log.append(entry)
    progress_log = progress_log[-30:]
    project.update(
        {
            "status": status,
            "message": message,
            "stage_status": stages,
            "progress_log": progress_log,
            "progress_percent": round(progress_percent, 1),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_project(pid, project)


def start_background_processing(pid: str, use_kimi: bool) -> None:
    thread = threading.Thread(target=process_project_v2, args=(pid, use_kimi), daemon=True)
    thread.start()


def should_use_kimi(requested: bool = False) -> bool:
    if requested:
        return True
    config = load_local_config()
    return bool(
        os.getenv("MOONSHOT_API_KEY")
        or os.getenv("KIMI_API_KEY")
        or config.get("moonshot_api_key")
        or config.get("kimi_api_key")
    )


def parse_clip_duration_fields(fields: Dict[str, Tuple[Optional[str], bytes]]) -> Dict[str, object]:
    mode = fields.get("clip_duration", (None, b""))[1].decode("utf-8", errors="ignore").strip() or "3min"
    custom_raw = fields.get("custom_minutes", (None, b""))[1].decode("utf-8", errors="ignore").strip()
    if mode == "custom":
        try:
            minutes = max(0.5, min(30.0, float(custom_raw)))
        except ValueError:
            minutes = 3.0
        target = int(minutes * 60)
        spread = max(20, int(target * 0.22))
        return {
            "clip_duration": "custom",
            "custom_minutes": minutes,
            "clip_min_seconds": max(20, target - spread),
            "clip_max_seconds": max(target + spread, target + 20),
            "clip_duration_label": f"自定义 {minutes:g} 分钟",
        }
    min_seconds, max_seconds, label = CLIP_DURATION_PRESETS.get(mode, CLIP_DURATION_PRESETS["3min"])
    return {
        "clip_duration": mode,
        "custom_minutes": "",
        "clip_min_seconds": min_seconds,
        "clip_max_seconds": max_seconds,
        "clip_duration_label": label,
    }


def clip_duration_fields_html(project: Optional[Dict[str, object]] = None) -> str:
    project = project or {}
    selected = str(project.get("clip_duration") or "3min")
    custom = esc(project.get("custom_minutes", ""))
    options = [
        ("1min", "1分钟"),
        ("3min", "3分钟"),
        ("5min", "5分钟"),
        ("over5", "5分钟以上"),
        ("custom", "自定义"),
    ]
    radios = "".join(
        f'<label class="choice"><input type="radio" name="clip_duration" value="{value}" {"checked" if selected == value else ""}> {label}</label>'
        for value, label in options
    )
    return f"""<fieldset class="duration-options">
      <legend>每个片段目标时长</legend>
      <div class="choices">{radios}</div>
      <label class="custom-duration">自定义分钟数
        <input type="number" name="custom_minutes" min="0.5" max="30" step="0.5" value="{custom}" placeholder="例如 2.5">
      </label>
      <p class="small">系统会按目标时长附近智能寻找完整主题片段，尽量保留前因后果。</p>
    </fieldset>"""


def project_clip_duration(project: Dict[str, object]) -> Tuple[int, int]:
    min_seconds = int(float(project.get("clip_min_seconds") or CLIP_DURATION_PRESETS["3min"][0]))
    max_seconds = int(float(project.get("clip_max_seconds") or CLIP_DURATION_PRESETS["3min"][1]))
    return max(20, min_seconds), max(max_seconds, min_seconds + 10)


def extract_audio(video_path: Path, audio_path: Path) -> None:
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000", str(audio_path),
    ]
    subprocess.run(command, check=True)


def media_duration_seconds(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return max(0.0, float(result.stdout.strip() or 0))
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0.0


def transcribe_video_to_srt(pid: str, video_path: Path, folder: Path) -> Path:
    if not ffmpeg_available():
        raise RuntimeError("未找到 ffmpeg，无法从视频中提取音频。")

    update_project_progress(
        pid,
        "自动转写中",
        "正在从视频中提取音频。",
        {STAGES[0]: "done", STAGES[1]: "running"},
        8,
    )
    audio_path = folder / "audio_16k.wav"
    extract_audio(video_path, audio_path)
    update_project_progress(
        pid,
        "自动转写中",
        "音频提取完成，正在准备加载转写模型。",
        {STAGES[1]: "running"},
        15,
    )

    config = load_local_config()
    model_name = config.get("whisper_model", "small")
    language = config.get("transcribe_language", "zh")
    srt_path = folder / "auto_transcript.srt"
    duration = media_duration_seconds(video_path)

    update_project_progress(
        pid,
        "自动转写中",
        f"正在使用 Whisper 模型 {model_name} 转写音频，长视频可能需要等待一段时间。",
        {STAGES[1]: "running"},
        20,
    )

    segments_data = []
    try:
        from faster_whisper import WhisperModel

        update_project_progress(
            pid,
            "自动转写中",
            f"正在加载 faster-whisper 模型 {model_name}。",
            {STAGES[1]: "running"},
            24,
        )
        model = WhisperModel(model_name, device=config.get("whisper_device", "cpu"), compute_type=config.get("whisper_compute_type", "int8"))
        update_project_progress(
            pid,
            "自动转写中",
            "模型加载完成，正在识别语音并生成带时间戳字幕。",
            {STAGES[1]: "running"},
            30,
        )
        segments, _ = model.transcribe(str(audio_path), language=language, vad_filter=True)
        last_percent = 30.0
        for segment in segments:
            text = simplify_chinese(segment.text.strip())
            if text:
                segments_data.append((float(segment.start), float(segment.end), text))
            if duration > 0:
                current_percent = min(68.0, 30.0 + (float(segment.end) / duration) * 38.0)
                if current_percent - last_percent >= 3:
                    update_project_progress(
                        pid,
                        "自动转写中",
                        f"正在转写字幕：已处理到 {format_time(float(segment.end))} / {format_time(duration)}。",
                        {STAGES[1]: "running"},
                        current_percent,
                    )
                    last_percent = current_percent
    except Exception as faster_exc:
        try:
            import whisper

            update_project_progress(
                pid,
                "自动转写中",
                f"faster-whisper 不可用，正在切换 whisper 模型 {model_name}。",
                {STAGES[1]: "running"},
                24,
            )
            model = whisper.load_model(model_name)
            update_project_progress(
                pid,
                "自动转写中",
                "模型加载完成，正在识别语音并生成带时间戳字幕。",
                {STAGES[1]: "running"},
                30,
            )
            result = model.transcribe(str(audio_path), language=language, fp16=False)
            last_percent = 30.0
            for segment in result.get("segments", []):
                text = simplify_chinese(str(segment.get("text", "")).strip())
                if text:
                    segments_data.append((float(segment["start"]), float(segment["end"]), text))
                if duration > 0:
                    current_percent = min(68.0, 30.0 + (float(segment["end"]) / duration) * 38.0)
                    if current_percent - last_percent >= 3:
                        update_project_progress(
                            pid,
                            "自动转写中",
                            f"正在转写字幕：已处理到 {format_time(float(segment['end']))} / {format_time(duration)}。",
                            {STAGES[1]: "running"},
                            current_percent,
                        )
                        last_percent = current_percent
        except Exception as whisper_exc:
            raise RuntimeError(f"自动转写失败。faster-whisper: {faster_exc}; whisper: {whisper_exc}") from whisper_exc

    if not segments_data:
        raise RuntimeError("自动转写没有识别到有效字幕。")

    lines = []
    for index, (start, end, text) in enumerate(segments_data, 1):
        lines.extend([str(index), f"{srt_timestamp(start)} --> {srt_timestamp(end)}", text, ""])
    srt_path.write_text("\n".join(lines), encoding="utf-8")
    update_project_progress(
        pid,
        "自动转写中",
        f"字幕生成完成，共识别 {len(segments_data)} 条字幕，正在进入内容分析。",
        {STAGES[1]: "done", STAGES[2]: "running"},
        70,
    )
    audio_path.unlink(missing_ok=True)
    return srt_path


def generate_clip_video(video_path: Path, candidate: ClipCandidate, output_path: Path) -> bool:
    if not ffmpeg_available():
        return False
    temp_paths: List[Path] = []
    try:
        if len(candidate.segments) == 1:
            seg = candidate.segments[0]
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", format_time(seg.start), "-to", format_time(seg.end),
                "-i", str(video_path), "-c", "copy", str(output_path),
            ]
            subprocess.run(command, check=True)
            return output_path.exists()
        concat_file = output_path.with_suffix(".concat.txt")
        for index, seg in enumerate(candidate.segments, 1):
            part = output_path.with_name(f"{output_path.stem}_part_{index}.mp4")
            temp_paths.append(part)
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", format_time(seg.start), "-to", format_time(seg.end),
                "-i", str(video_path), "-c", "copy", str(part),
            ]
            subprocess.run(command, check=True)
        concat_file.write_text("\n".join(f"file '{part.as_posix()}'" for part in temp_paths), encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(output_path)],
            check=True,
        )
        concat_file.unlink(missing_ok=True)
        return output_path.exists()
    except (subprocess.CalledProcessError, OSError):
        return False
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)


def process_project(pid: str, use_kimi: bool) -> Dict[str, object]:
    project = load_project(pid)
    folder = PROJECT_ROOT / pid
    transcript_name = str(project.get("transcript_file") or "").strip()
    transcript_path = folder / transcript_name if transcript_name else None
    video_file = project.get("video_file")
    video_path = folder / str(video_file) if video_file else None
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    if not transcript_path or not transcript_path.is_file():
        project.update(
            {
                "status": "等待字幕",
                "updated_at": now,
                "stage_status": {stage: ("done" if stage == "素材准备" else "pending") for stage in STAGES},
                "message": "视频已导入。当前版本需要字幕或带时间戳转写稿来定位高光；请在项目详情页补传字幕后开始处理。",
            }
        )
        save_project(pid, project)
        return project

    project["stage_status"] = {stage: "done" for stage in STAGES[:-1]} | {"视频生成": "running"}
    save_project(pid, project)

    try:
        use_kimi = should_use_kimi(use_kimi)
        min_seconds, max_seconds = project_clip_duration(project)
        candidates = run(transcript_path, HISTORY, folder, use_kimi, min_seconds, max_seconds)
    except Exception as exc:
        project.update(
            {
                "status": "处理失败",
                "updated_at": now,
                "stage_status": {
                    STAGES[0]: "done",
                    STAGES[1]: "error",
                    STAGES[2]: "pending",
                    STAGES[3]: "pending",
                    STAGES[4]: "pending",
                    STAGES[5]: "pending",
                    STAGES[6]: "pending",
                },
                "message": f"处理失败：{exc}",
            }
        )
        save_project(pid, project)
        return project
    kimi_records_path = folder / "candidates.kimi.json"
    if use_kimi and kimi_records_path.exists():
        try:
            records = json.loads(kimi_records_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            records = [asdict(item) for item in candidates]
    else:
        records = [asdict(item) for item in candidates]
    video_generated = False
    for index, record in enumerate(records):
        if index < len(candidates) and isinstance(record, dict) and record.get("title"):
            candidates[index].title = str(record["title"])
    if video_path and video_path.exists():
        for index, candidate in enumerate(candidates, 1):
            mp4_name = f"clip_{index:02d}_{candidate.kind}.mp4"
            edl_name = f"clip_{index:02d}_{candidate.kind}.edl"
            write_edl(candidate, folder / edl_name)
            if generate_clip_video(video_path, candidate, folder / mp4_name):
                records[index - 1]["video"] = mp4_name
                video_generated = True
            records[index - 1]["edl"] = edl_name

    (folder / "candidates.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    write_project_zip(folder)
    project.update(
        {
            "status": "已完成",
            "updated_at": now,
            "clip_count": len(records),
            "has_video_exports": video_generated,
            "ffmpeg": "available" if ffmpeg_available() else "missing",
            "stage_status": {stage: "done" for stage in STAGES},
            "message": "处理完成。已生成候选切片、EDL" + (" 和切片视频。" if video_generated else "。如需直接导出 MP4，请安装 ffmpeg。"),
        }
    )
    save_project(pid, project)
    return project


def write_project_zip(folder: Path) -> None:
    archive = folder / "project_export.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for path in folder.iterdir():
            if path.name == archive.name or path.is_dir():
                continue
            if path.suffix.lower() in {".json", ".edl", ".mp4"}:
                zipped.write(path, arcname=path.name)


def process_project_v2(pid: str, use_kimi: bool) -> Dict[str, object]:
    project = load_project(pid)
    folder = PROJECT_ROOT / pid
    transcript_name = str(project.get("transcript_file") or "").strip()
    transcript_path = folder / transcript_name if transcript_name else None
    video_file = str(project.get("video_file") or "").strip()
    video_path = folder / video_file if video_file else None
    video_hash = str(project.get("video_hash") or "").strip()
    if not video_hash and video_path and video_path.is_file():
        video_hash = file_sha256(video_path)
        project["video_hash"] = video_hash
        save_project(pid, project)

    update_project_progress(
        pid,
        "处理中",
        "素材已导入，正在准备分析。",
        {stage: ("pending" if index else "done") for index, stage in enumerate(STAGES)},
        3,
    )

    if not transcript_path or not transcript_path.is_file():
        if video_path and video_path.is_file():
            cached_target = folder / "auto_transcript.srt"
            if load_cached_transcript(video_hash, cached_target):
                update_project_progress(
                    pid,
                    "使用字幕缓存",
                    "检测到同一段视频已有自动转写字幕，已直接复用缓存，正在进入内容分析。",
                    {STAGES[0]: "done", STAGES[1]: "done", STAGES[2]: "running"},
                    70,
                )
                transcript_path = cached_target
                project = load_project(pid)
                project["transcript_file"] = transcript_path.name
                project["message"] = "已复用同一视频的字幕缓存，正在分析高光片段。"
                project["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                save_project(pid, project)
            else:
                try:
                    transcript_path = transcribe_video_to_srt(pid, video_path, folder)
                    save_cached_transcript(video_hash, transcript_path)
                    project = load_project(pid)
                    project["transcript_file"] = transcript_path.name
                    project["message"] = "自动转写完成，正在分析高光片段。"
                    project["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    save_project(pid, project)
                except Exception as exc:
                    update_project_progress(
                        pid,
                        "转写失败",
                        f"视频已导入，但自动转写失败：{exc}",
                        {
                            STAGES[0]: "done",
                            STAGES[1]: "error",
                            STAGES[2]: "pending",
                            STAGES[3]: "pending",
                            STAGES[4]: "pending",
                            STAGES[5]: "pending",
                            STAGES[6]: "pending",
                        },
                    )
                    return load_project(pid)
            if transcript_path and transcript_path.is_file():
                pass
            else:
                return load_project(pid)
        else:
            update_project_progress(
                pid,
                "等待字幕",
                "没有可分析的视频或字幕，请补传素材后重新处理。",
                {STAGES[0]: "done"},
            )
            return load_project(pid)

    update_project_progress(
        pid,
        "分析中",
        "正在提取视频大纲、识别话题时间点、评分精彩片段并生成标题。",
        {
            STAGES[0]: "done",
            STAGES[1]: "done",
            STAGES[2]: "running",
            STAGES[3]: "running",
            STAGES[4]: "running",
            STAGES[5]: "pending",
            STAGES[6]: "pending",
        },
        75,
    )

    try:
        min_seconds, max_seconds = project_clip_duration(load_project(pid))
        candidates = run(transcript_path, HISTORY, folder, use_kimi, min_seconds, max_seconds)
    except Exception as exc:
        update_project_progress(
            pid,
            "处理失败",
            f"处理失败：{exc}",
            {
                STAGES[0]: "done",
                STAGES[1]: "error",
                STAGES[2]: "pending",
                STAGES[3]: "pending",
                STAGES[4]: "pending",
                STAGES[5]: "pending",
                STAGES[6]: "pending",
            },
        )
        return load_project(pid)

    update_project_progress(
        pid,
        "生成视频中",
        "高光片段已识别，正在生成 EDL、切片视频和合集包。",
        {
            STAGES[0]: "done",
            STAGES[1]: "done",
            STAGES[2]: "done",
            STAGES[3]: "done",
            STAGES[4]: "done",
            STAGES[5]: "running",
            STAGES[6]: "running",
        },
        88,
    )

    kimi_records_path = folder / "candidates.kimi.json"
    if use_kimi and kimi_records_path.exists():
        try:
            records = json.loads(kimi_records_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            records = [asdict(item) for item in candidates]
    else:
        records = [asdict(item) for item in candidates]
    title_source = "kimi" if any(record.get("title_source") == "kimi" for record in records if isinstance(record, dict)) else "local"
    kimi_error_path = folder / "kimi_error.txt"
    if use_kimi and kimi_error_path.exists():
        update_project_progress(
            pid,
            "标题生成降级",
            "KIMI 标题生成失败，已使用本地标题兜底。详情见项目目录 kimi_error.txt。",
            {STAGES[4]: "error"},
            86,
        )
    video_generated = False
    if video_path and video_path.exists():
        for index, candidate in enumerate(candidates, 1):
            mp4_name = f"clip_{index:02d}_{candidate.kind}.mp4"
            edl_name = f"clip_{index:02d}_{candidate.kind}.edl"
            write_edl(candidate, folder / edl_name)
            if generate_clip_video(video_path, candidate, folder / mp4_name):
                records[index - 1]["video"] = mp4_name
                video_generated = True
            records[index - 1]["edl"] = edl_name

    (folder / "candidates.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    write_project_zip(folder)
    project = load_project(pid)
    project.update(
        {
            "status": "已完成",
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "clip_count": len(records),
            "has_video_exports": video_generated,
            "ffmpeg": "available" if ffmpeg_available() else "missing",
            "title_source": title_source,
            "stage_status": {stage: "done" for stage in STAGES},
            "progress_percent": 100,
            "message": "处理完成。已生成候选切片、EDL、合集包" + (" 和切片视频。" if video_generated else "。如需直接导出 MP4，请安装 ffmpeg。"),
        }
    )
    save_project(pid, project)
    return project


def base_page(title: str, content: str) -> bytes:
    body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - TranscriptCut</title>
  <style>
    :root {{
      --ink: #161713;
      --muted: #6b6f65;
      --paper: #fbfaf4;
      --panel: #ffffff;
      --line: #deded4;
      --green: #1f6f61;
      --green-dark: #155246;
      --gold: #d29b2e;
      --red: #b74d45;
      --shadow: 0 20px 50px rgba(24, 31, 23, .10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(120deg, rgba(31,111,97,.09), transparent 28%),
        radial-gradient(circle at 88% 8%, rgba(210,155,46,.18), transparent 26%),
        var(--paper);
      font-family: "Microsoft YaHei UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    }}
    a {{ color: inherit; }}
    .shell {{ max-width: 1180px; margin: 0 auto; padding: 28px 20px 56px; }}
    .topbar {{ display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 32px; }}
    .nav-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .brand {{ display: flex; align-items: center; gap: 12px; text-decoration: none; }}
    .mark {{ width: 38px; height: 38px; border-radius: 8px; background: var(--ink); color: white; display: grid; place-items: center; font-weight: 900; }}
    .brand strong {{ display: block; font-size: 18px; }}
    .brand span {{ color: var(--muted); font-size: 13px; }}
    .hero {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(300px, .9fr); gap: 26px; align-items: stretch; margin-bottom: 28px; }}
    .hero-main {{ padding: 34px; border: 1px solid var(--line); background: rgba(255,255,255,.82); box-shadow: var(--shadow); border-radius: 8px; }}
    h1 {{ margin: 0; font-size: clamp(30px, 4vw, 54px); line-height: 1.08; letter-spacing: 0; }}
    .lead {{ margin: 18px 0 0; color: var(--muted); line-height: 1.8; font-size: 16px; max-width: 760px; }}
    .panel {{ background: rgba(255,255,255,.9); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }}
    .panel.pad {{ padding: 22px; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 22px; }}
    .btn {{ border: 0; border-radius: 7px; padding: 11px 15px; background: var(--green); color: white; font-weight: 800; text-decoration: none; display: inline-flex; align-items: center; gap: 8px; cursor: pointer; }}
    .btn.secondary {{ background: #ecebe3; color: var(--ink); }}
    .btn.warn {{ background: var(--red); }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }}
    .project, .clip {{ padding: 18px; background: white; border: 1px solid var(--line); border-radius: 8px; }}
    .meta, .small {{ color: var(--muted); font-size: 13px; line-height: 1.6; }}
    .status {{ display: inline-flex; padding: 4px 8px; border-radius: 999px; background: #edf5f2; color: var(--green-dark); font-size: 12px; font-weight: 800; }}
    .drop {{ position: relative; padding: 26px; border: 2px dashed #b8beb1; border-radius: 8px; background: #fffffb; }}
    .drop input[type=file] {{ width: 100%; padding: 12px; background: #f5f5ee; border-radius: 6px; border: 1px solid var(--line); }}
    input[type=text], textarea {{ width: 100%; border: 1px solid var(--line); border-radius: 7px; padding: 12px; font: inherit; background: white; }}
    textarea {{ min-height: 180px; resize: vertical; font-family: Consolas, "Microsoft YaHei UI", monospace; }}
    label {{ display: grid; gap: 7px; font-weight: 800; }}
    form {{ display: grid; gap: 18px; }}
    fieldset {{ border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    legend {{ padding: 0 6px; font-weight: 900; }}
    .choices {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0 12px; }}
    .choice {{ display: inline-flex; grid-template-columns: auto 1fr; align-items: center; gap: 7px; padding: 9px 11px; background: #f6f6ef; border: 1px solid var(--line); border-radius: 7px; font-size: 14px; }}
    .custom-duration {{ max-width: 260px; }}
    .transcript-lines {{ display: grid; gap: 8px; }}
    .transcript-line {{ display: grid; grid-template-columns: 118px minmax(0, 1fr); gap: 12px; padding: 10px 12px; background: #f6f6ef; border: 1px solid var(--line); border-radius: 7px; }}
    .transcript-time {{ color: var(--green-dark); font-weight: 900; font-size: 13px; }}
    .steps {{ display: grid; gap: 10px; }}
    .step {{ display: flex; justify-content: space-between; gap: 12px; padding: 10px 12px; background: #f6f6ef; border-radius: 7px; }}
    .done {{ color: var(--green-dark); font-weight: 900; }}
    .error {{ color: var(--red); font-weight: 900; }}
    .running {{ color: var(--gold); font-weight: 900; }}
    .pending {{ color: var(--muted); }}
    .message {{ margin: 0 0 18px; padding: 12px 14px; background: #fff8e7; border: 1px solid #eed99f; border-radius: 8px; color: #6f5313; }}
    .progressbar {{ height: 12px; overflow: hidden; border-radius: 999px; background: #ecebe3; border: 1px solid var(--line); margin: 10px 0 8px; }}
    .progressbar span {{ display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--green), var(--gold)); transition: width .35s ease; }}
    .progress-log {{ display: grid; gap: 8px; max-height: 300px; overflow: auto; }}
    .progress-item {{ display: grid; grid-template-columns: 76px 110px minmax(0, 1fr); gap: 12px; align-items: start; padding: 10px 12px; background: #f6f6ef; border-radius: 7px; }}
    .progress-time, .progress-status {{ color: var(--muted); font-size: 13px; font-weight: 800; }}
    .progress-text {{ font-size: 14px; line-height: 1.6; }}
    .clip h3, .project h3 {{ margin: 0 0 8px; font-size: 18px; }}
    .downloads {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }}
    @media (max-width: 760px) {{
      .hero {{ grid-template-columns: 1fr; }}
      .hero-main {{ padding: 24px; }}
      .topbar {{ align-items: flex-start; flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <nav class="topbar">
      <a class="brand" href="/">
        <span class="mark">TC</span>
        <span><strong>TranscriptCut</strong><span>家庭教育直播切片工作台</span></span>
      </a>
      <span class="nav-actions">
        <a class="btn secondary" href="/settings">设置</a>
        <a class="btn secondary" href="/new">新建项目</a>
      </span>
    </nav>
    {content}
  </div>
</body>
</html>"""
    return body.encode("utf-8")


def render_home() -> bytes:
    projects = list_projects()
    items = []
    for item in projects:
        pid = str(item["id"])
        items.append(
            f"""<article class="project">
  <span class="status">{esc(item.get("status", "未知"))}</span>
  <h3>{esc(item.get("name", pid))}</h3>
  <p class="meta">视频：{esc(item.get("video_file", "未上传"))}<br>切片：{esc(item.get("clip_count", 0))} 个<br>更新：{esc(item.get("updated_at", item.get("created_at", "")))}</p>
  <a class="btn secondary" href="/projects/{esc(pid)}">查看项目</a>
</article>"""
        )
    project_grid = "\n".join(items) if items else '<p class="meta">还没有项目。点击“新建项目”导入本地直播回放。</p>'
    content = f"""<section class="hero">
  <div class="hero-main">
    <h1>从长直播回放里，整理出主题完整的高光切片。</h1>
    <p class="lead">导入本地视频和字幕，系统会按家庭教育内容逻辑识别问题背景、原因分析、解决建议和总结金句，生成连续切片、不连续合集、标题建议和达芬奇 EDL。</p>
    <div class="actions">
      <a class="btn" href="/new">新建项目</a>
      <a class="btn secondary" href="/sample">查看样例项目</a>
    </div>
  </div>
  <aside class="panel pad">
    <div class="steps">
      {''.join(f'<div class="step"><span>{esc(stage)}</span><span class="done">自动</span></div>' for stage in STAGES)}
    </div>
  </aside>
</section>
<section>
  <h2>项目</h2>
  <div class="grid">{project_grid}</div>
</section>"""
    return base_page("首页", content)


def render_new(message: str = "") -> bytes:
    notice = f'<p class="message">{esc(message)}</p>' if message else ""
    content = f"""{notice}
<section class="panel pad">
  <h1>新建项目</h1>
  <p class="lead">选择本地长视频，上传 SRT/TXT 字幕或直接粘贴带时间戳的转写稿。没有字幕也可以先导入视频，后续在项目详情页补传。</p>
  <form method="post" action="/projects" enctype="multipart/form-data">
    <label>项目名称
      <input type="text" name="name" placeholder="例如：4月28日连麦答疑直播">
    </label>
    <label class="drop">本地视频文件
      <input type="file" name="video" accept=".mp4,.mov,.mkv,.avi,.m4v,.flv,.webm" required>
      <span class="small">支持拖拽或点击选择。文件只保存在本机项目目录。</span>
    </label>
    <label class="drop">字幕文件，可选
      <input type="file" name="transcript" accept=".srt,.txt,.vtt">
      <span class="small">推荐上传 SRT；TXT 每行以 [00:00:00] 开头即可。</span>
    </label>
    <label>或粘贴字幕/转写稿，可选
      <textarea name="transcript_text" placeholder="[00:00:01] 家长问：孩子最近不想上学怎么办？"></textarea>
    </label>
    {clip_duration_fields_html()}
    <label class="small"><input type="checkbox" name="kimi" value="1"> 调用 KIMI 生成标题，需要环境变量 MOONSHOT_API_KEY</label>
    <button class="btn" type="submit">开始处理</button>
  </form>
</section>"""
    return base_page("新建项目", content)


def render_settings() -> bytes:
    config_path = ROOT / "config.json"
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else "{}"
    masked = config_text
    try:
        data = json.loads(config_text)
        key = str(data.get("moonshot_api_key", ""))
        if key:
            data["moonshot_api_key"] = key[:6] + "..." + key[-4:] if len(key) > 12 else "已填写"
        masked = json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pass
    content = f"""<section class="panel pad">
  <h1>设置</h1>
  <p class="lead">KIMI API Key 现在可以写在本地配置文件里。修改后重新点击“开始处理”即可；如果服务已经启动，通常不需要重启。</p>
  <div class="project">
    <h3>配置文件位置</h3>
    <p class="meta">{esc(config_path)}</p>
  </div>
  <div class="project">
    <h3>当前配置</h3>
    <pre style="white-space:pre-wrap;background:#f6f6ef;border:1px solid var(--line);border-radius:7px;padding:14px;">{esc(masked)}</pre>
  </div>
  <div class="project">
    <h3>填写示例</h3>
    <pre style="white-space:pre-wrap;background:#f6f6ef;border:1px solid var(--line);border-radius:7px;padding:14px;">{esc('{\n  "moonshot_api_key": "你的KIMI_API_KEY",\n  "kimi_model": "moonshot-v1-8k",\n  "whisper_model": "small",\n  "whisper_device": "cpu",\n  "whisper_compute_type": "int8",\n  "transcribe_language": "zh"\n}')}</pre>
  </div>
</section>"""
    return base_page("设置", content)


def render_project(pid: str) -> bytes:
    project = load_project(pid)
    folder = PROJECT_ROOT / pid
    candidates_path = folder / "candidates.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8")) if candidates_path.exists() else []
    message = f'<p class="message">{esc(project.get("message", ""))}</p>' if project.get("message") else ""
    stages = project.get("stage_status", {})
    stage_html = "".join(
        f'<div class="step"><span>{esc(stage)}</span><span class="{esc(stages.get(stage, "pending"))}">{esc(stages.get(stage, "pending"))}</span></div>'
        for stage in STAGES
    )
    progress_percent = max(0, min(100, int(float(project.get("progress_percent", 0) or 0))))
    progress_log = list(project.get("progress_log") or [])
    if progress_log:
        progress_items = "".join(
            f'<div class="progress-item"><span class="progress-time">{esc(item.get("time", ""))}</span><span class="progress-status">{esc(item.get("status", ""))}</span><span class="progress-text">{esc(item.get("message", ""))}</span></div>'
            for item in reversed(progress_log)
        )
    else:
        progress_items = '<p class="meta">暂无处理日志。开始处理后会显示音频提取、模型加载、字幕转写、片段分析等步骤。</p>'
    progress_html = f"""<section class="panel pad">
  <h2>处理过程</h2>
  <div class="progressbar" aria-label="处理进度"><span style="width:{progress_percent}%"></span></div>
  <p class="meta">当前进度：{progress_percent}%</p>
  <div class="progress-log">{progress_items}</div>
</section>"""
    clips = []
    for index, item in enumerate(candidates, 1):
        ranges = " + ".join(f"{format_time(seg['start'])}-{format_time(seg['end'])}" for seg in item.get("segments", []))
        downloads = [
            f'<a class="btn secondary" href="/download/{esc(pid)}/clip_{index:02d}_{esc(item.get("kind", "continuous"))}.edl">EDL</a>'
        ]
        if item.get("video"):
            downloads.append(f'<a class="btn" href="/download/{esc(pid)}/{esc(item["video"])}">下载视频</a>')
        downloads.append(f'<a class="btn secondary" href="/projects/{esc(pid)}/clips/{index}">查看字幕</a>')
        clips.append(
            f"""<article class="clip">
  <span class="status">{esc(item.get("kind", ""))}</span>
  <h3>{esc(item.get("title", ""))}</h3>
  <p class="meta">{esc(ranges)} | 评分 {esc(item.get("score", ""))}</p>
  <p>{esc(item.get("reason", ""))}</p>
  <p class="meta">概要：{esc(item.get("segments", [{}])[0].get("summary", ""))}</p>
  <p class="meta">金句：{esc(item.get("segments", [{}])[0].get("quote", ""))}</p>
  <div class="downloads">{''.join(downloads)}</div>
</article>"""
        )
    clip_html = "\n".join(clips) if clips else "<p class=\"meta\">还没有切片结果。上传字幕后点击开始处理。</p>"
    transcript_text = ""
    transcript_file = str(project.get("transcript_file") or "")
    transcript_path = folder / transcript_file if transcript_file else None
    if transcript_path and transcript_path.is_file():
        try:
            transcript_text = transcript_path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            transcript_text = transcript_path.read_text(encoding="utf-8", errors="ignore")
    editor_html = f"""<section class="panel pad">
  <h2>字幕编辑器</h2>
  <form method="post" action="/projects/{esc(pid)}/transcript" enctype="multipart/form-data">
    <label>可视化字幕编辑
      <textarea name="transcript_text">{esc(transcript_text)}</textarea>
    </label>
    {clip_duration_fields_html(project)}
    <label class="small"><input type="checkbox" name="kimi" value="1"> 保存后调用 KIMI 重新生成标题</label>
    <button class="btn" type="submit">保存字幕并重新处理</button>
  </form>
</section>""" if transcript_text else ""
    duration_panel = f"""<section class="panel pad">
  <h2>生成时长</h2>
  <form method="post" action="/projects/{esc(pid)}/regenerate" enctype="multipart/form-data">
    {clip_duration_fields_html(project)}
    <label class="small"><input type="checkbox" name="kimi" value="1"> 调用 KIMI 重新总结每个片段标题</label>
    <button class="btn" type="submit">按这个时长重新生成片段</button>
  </form>
</section>"""
    content = f"""{message}
<section class="hero">
  <div class="hero-main">
    <span class="status">{esc(project.get("status", ""))}</span>
    <h1>{esc(project.get("name", pid))}</h1>
    <p class="lead">视频：{esc(project.get("video_file", "未上传"))}<br>字幕：{esc(project.get("transcript_file", "未上传"))}<br>目标时长：{esc(project.get("clip_duration_label", "3分钟"))}<br>创建：{esc(project.get("created_at", ""))}</p>
    <p class="meta">标题来源：{esc(project.get("title_source", "处理中/本地兜底"))}</p>
    <div class="actions">
      <a class="btn secondary" href="/download/{esc(pid)}/candidates.json">下载 JSON</a>
      <a class="btn" href="/download/{esc(pid)}/project_export.zip">下载完整合集包</a>
      <a class="btn secondary" href="/new">新建项目</a>
      <form method="post" action="/projects/{esc(pid)}/regenerate" style="margin:0">
        <button class="btn secondary" type="submit">重新生成片段</button>
      </form>
      <form method="post" action="/projects/{esc(pid)}/delete" onsubmit="return confirm('确定删除这个项目文件夹吗？此操作不可恢复。')" style="margin:0">
        <button class="btn warn" type="submit">删除项目</button>
      </form>
    </div>
  </div>
  <aside class="panel pad"><div class="steps">{stage_html}</div></aside>
</section>
{progress_html}
{duration_panel}
{editor_html}
<section class="panel pad">
  <h2>补传字幕并重新处理</h2>
  <form method="post" action="/projects/{esc(pid)}/transcript" enctype="multipart/form-data">
    <label class="drop">字幕文件
      <input type="file" name="transcript" accept=".srt,.txt,.vtt">
    </label>
    <label>或粘贴字幕/转写稿
      <textarea name="transcript_text"></textarea>
    </label>
    {clip_duration_fields_html(project)}
    <label class="small"><input type="checkbox" name="kimi" value="1"> 调用 KIMI 生成标题</label>
    <button class="btn" type="submit">开始处理</button>
  </form>
</section>
<section>
  <h2>切片结果</h2>
  <div class="grid">{clip_html}</div>
</section>
<script>
  const projectId = {json.dumps(pid)};
  async function refreshProjectStatus() {{
    try {{
      const res = await fetch(`/api/projects/${{projectId}}`);
      if (!res.ok) return;
      const data = await res.json();
      const active = ["处理中", "重新生成中", "使用字幕缓存", "自动转写中", "分析中", "生成视频中"];
      if (active.includes(data.status)) {{
        setTimeout(() => location.reload(), 2500);
      }}
    }} catch (err) {{}}
  }}
  refreshProjectStatus();
</script>"""
    return base_page(str(project.get("name", pid)), content)


def render_clip_detail(pid: str, clip_number: int) -> bytes:
    project = load_project(pid)
    folder = PROJECT_ROOT / pid
    candidates_path = folder / "candidates.json"
    if not candidates_path.exists():
        raise FileNotFoundError(pid)
    candidates = json.loads(candidates_path.read_text(encoding="utf-8-sig"))
    if clip_number < 1 or clip_number > len(candidates):
        raise FileNotFoundError(f"{pid}/clips/{clip_number}")
    item = candidates[clip_number - 1]
    segments = item.get("segments", [])
    ranges = " + ".join(f"{format_time(seg['start'])}-{format_time(seg['end'])}" for seg in segments)
    transcript_file = str(project.get("transcript_file") or "").strip()
    transcript_path = folder / transcript_file if transcript_file else None
    grouped_lines: List[str] = []
    if transcript_path and transcript_path.is_file():
        lines = parse_transcript(transcript_path)
        for seg_index, seg in enumerate(segments, 1):
            start = float(seg.get("start", 0))
            end = float(seg.get("end", 0))
            matched = [
                line for line in lines
                if line.end > start and line.start < end
            ]
            body = "".join(
                f'<div class="transcript-line"><span class="transcript-time">{format_time(line.start)}-{format_time(line.end)}</span><span>{esc(line.text)}</span></div>'
                for line in matched
            ) or '<p class="meta">这个时间段没有匹配到字幕行。</p>'
            grouped_lines.append(
                f"""<section class="panel pad">
  <h2>片段 {seg_index} 字幕</h2>
  <p class="meta">{format_time(start)}-{format_time(end)}</p>
  <div class="transcript-lines">{body}</div>
</section>"""
            )
    else:
        grouped_lines.append('<section class="panel pad"><p class="meta">当前项目还没有字幕文件。</p></section>')

    downloads = [
        f'<a class="btn secondary" href="/projects/{esc(pid)}">返回项目</a>',
        f'<a class="btn secondary" href="/download/{esc(pid)}/clip_{clip_number:02d}_{esc(item.get("kind", "continuous"))}.edl">下载 EDL</a>',
    ]
    if item.get("video"):
        downloads.append(f'<a class="btn" href="/download/{esc(pid)}/{esc(item["video"])}">下载视频</a>')
    content = f"""<section class="hero">
  <div class="hero-main">
    <span class="status">{esc(item.get("kind", ""))}</span>
    <h1>{esc(item.get("title", ""))}</h1>
    <p class="lead">项目：{esc(project.get("name", pid))}<br>时间：{esc(ranges)}<br>评分：{esc(item.get("score", ""))}</p>
    <p class="meta">概要：{esc(segments[0].get("summary", "") if segments else "")}</p>
    <p class="meta">金句：{esc(segments[0].get("quote", "") if segments else "")}</p>
    <div class="actions">{''.join(downloads)}</div>
  </div>
  <aside class="panel pad">
    <h2>完整内容</h2>
    <p class="meta">{esc(segments[0].get("text", "") if segments else "")}</p>
  </aside>
</section>
{''.join(grouped_lines)}"""
    return base_page(f"片段 {clip_number}", content)


def project_status_payload(pid: str) -> bytes:
    project = load_project(pid)
    folder = PROJECT_ROOT / pid
    candidates_path = folder / "candidates.json"
    project["id"] = pid
    if candidates_path.exists():
        try:
            project["clip_count"] = len(json.loads(candidates_path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            project["clip_count"] = int(project.get("clip_count", 0) or 0)
    return json.dumps(project, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        LOG_ROOT.mkdir(exist_ok=True)
        line = "%s - - [%s] %s\n" % (
            self.client_address[0],
            self.log_date_time_string(),
            format % args,
        )
        with (LOG_ROOT / "access.log").open("a", encoding="utf-8") as handle:
            handle.write(line)
        sys.stderr.write(line)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self.send_page(render_home())
            elif path == "/new":
                self.send_page(render_new())
            elif path == "/settings":
                self.send_page(render_settings())
            elif path == "/sample":
                self.create_sample_project()
            elif path.startswith("/api/projects/"):
                pid = path.strip("/").split("/", 2)[2]
                self.send_json(project_status_payload(pid))
            elif re.match(r"^/projects/[^/]+/clips/\d+$", path):
                _, pid, _, clip_raw = path.strip("/").split("/")
                self.send_page(render_clip_detail(pid, int(clip_raw)))
            elif path.startswith("/projects/"):
                pid = path.strip("/").split("/", 1)[1]
                self.send_page(render_project(pid))
            elif path.startswith("/download/"):
                self.send_download(path)
            else:
                self.send_error(404)
        except FileNotFoundError:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "")
        fields = parse_multipart(content_type, self.rfile.read(length))
        if path == "/projects":
            self.create_project(fields)
            return
        if path.startswith("/projects/") and path.endswith("/delete"):
            pid = path.strip("/").split("/")[1]
            self.delete_project(pid)
            return
        if path.startswith("/projects/") and path.endswith("/regenerate"):
            pid = path.strip("/").split("/")[1]
            self.regenerate_project(pid, "kimi" in fields, fields)
            return
        if path.startswith("/projects/") and path.endswith("/transcript"):
            pid = path.strip("/").split("/")[1]
            self.update_transcript(pid, fields)
            return
        self.send_error(404)

    def create_sample_project(self) -> None:
        pid = project_id()
        folder = PROJECT_ROOT / pid
        folder.mkdir(parents=True, exist_ok=True)
        sample = ROOT / "sample_transcript.txt"
        transcript_name = "sample_transcript.txt"
        shutil.copyfile(sample, folder / transcript_name)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        save_project(
            pid,
            {
                "name": "样例：初一孩子不想上学",
                "created_at": now,
                "updated_at": now,
                "video_file": "",
                "transcript_file": transcript_name,
                "status": "处理中",
            },
        )
        start_background_processing(pid, False)
        self.redirect(f"/projects/{pid}")

    def create_project(self, fields: Dict[str, Tuple[Optional[str], bytes]]) -> None:
        video_filename, video_data = fields.get("video", (None, b""))
        if not video_filename or not video_data:
            self.send_page(render_new("请先选择一个本地视频文件。"))
            return
        ext = Path(video_filename).suffix.lower()
        if ext not in VIDEO_EXTS:
            self.send_page(render_new("视频格式暂不支持，请上传 mp4、mov、mkv、avi、m4v、flv 或 webm。"))
            return

        pid = project_id()
        folder = PROJECT_ROOT / pid
        folder.mkdir(parents=True, exist_ok=True)
        video_name = safe_name(video_filename, "video" + ext)
        video_path = folder / video_name
        video_path.write_bytes(video_data)
        video_hash = file_sha256(video_path)

        transcript_name = self.save_transcript_from_fields(folder, fields)
        if transcript_name:
            save_cached_transcript(video_hash, folder / transcript_name)
        name = (fields.get("name", (None, b""))[1].decode("utf-8", errors="ignore").strip() or Path(video_name).stem)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        duration_settings = parse_clip_duration_fields(fields)
        save_project(
            pid,
            {
                "name": name,
                "created_at": now,
                "updated_at": now,
                "video_file": video_name,
                "video_hash": video_hash,
                "transcript_file": transcript_name or "",
                "status": "处理中",
                **duration_settings,
            },
        )
        start_background_processing(pid, should_use_kimi("kimi" in fields))
        self.redirect(f"/projects/{pid}")

    def update_transcript(self, pid: str, fields: Dict[str, Tuple[Optional[str], bytes]]) -> None:
        project = load_project(pid)
        folder = PROJECT_ROOT / pid
        transcript_name = self.save_transcript_from_fields(folder, fields)
        if not transcript_name:
            self.redirect(f"/projects/{pid}")
            return
        project["transcript_file"] = transcript_name
        video_hash = str(project.get("video_hash") or "").strip()
        if not video_hash:
            video_file = str(project.get("video_file") or "").strip()
            video_path = folder / video_file if video_file else None
            if video_path and video_path.is_file():
                video_hash = file_sha256(video_path)
                project["video_hash"] = video_hash
        save_cached_transcript(video_hash, folder / transcript_name)
        project.update(parse_clip_duration_fields(fields))
        project["status"] = "处理中"
        project["message"] = "字幕已上传，正在重新处理。"
        project["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_project(pid, project)
        start_background_processing(pid, should_use_kimi("kimi" in fields))
        self.redirect(f"/projects/{pid}")

    def delete_project(self, pid: str) -> None:
        folder = (PROJECT_ROOT / pid).resolve()
        root = PROJECT_ROOT.resolve()
        try:
            folder.relative_to(root)
        except ValueError:
            self.send_error(404)
            return
        if folder == root or not folder.exists():
            self.send_error(404)
            return
        shutil.rmtree(folder)
        self.redirect("/")

    def regenerate_project(self, pid: str, use_kimi: bool, fields: Optional[Dict[str, Tuple[Optional[str], bytes]]] = None) -> None:
        project = load_project(pid)
        folder = PROJECT_ROOT / pid
        for pattern in ("candidates.json", "candidates.kimi.json", "clip_*.edl", "clip_*.mp4", "project_export.zip"):
            for path in folder.glob(pattern):
                if path.is_file():
                    path.unlink(missing_ok=True)
        project["status"] = "重新生成中"
        project["message"] = "正在基于当前字幕重新生成切片、标题和导出文件。"
        project["clip_count"] = 0
        project["progress_percent"] = 0
        project["progress_log"] = []
        if fields and "clip_duration" in fields:
            project.update(parse_clip_duration_fields(fields))
        project["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_project(pid, project)
        start_background_processing(pid, should_use_kimi(use_kimi))
        self.redirect(f"/projects/{pid}")

    def save_transcript_from_fields(self, folder: Path, fields: Dict[str, Tuple[Optional[str], bytes]]) -> str:
        filename, data = fields.get("transcript", (None, b""))
        if filename and data:
            ext = Path(filename).suffix.lower()
            if ext in TEXT_EXTS:
                name = safe_name(filename, "transcript" + ext)
                text = data.decode("utf-8-sig", errors="ignore")
                (folder / name).write_text(simplify_chinese(text), encoding="utf-8")
                return name
        text = fields.get("transcript_text", (None, b""))[1].decode("utf-8", errors="ignore").strip()
        if text:
            name = "transcript.txt"
            (folder / name).write_text(simplify_chinese(text), encoding="utf-8")
            return name
        return ""

    def send_download(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 3:
            self.send_error(404)
            return
        _, pid, filename = parts
        safe_filename = Path(urllib.parse.unquote(filename)).name
        file_path = (PROJECT_ROOT / pid / safe_filename).resolve()
        if not str(file_path).startswith(str((PROJECT_ROOT / pid).resolve())) or not file_path.exists():
            self.send_error(404)
            return
        data = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{urllib.parse.quote(file_path.name)}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_page(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()


if __name__ == "__main__":
    PROJECT_ROOT.mkdir(exist_ok=True)
    LEGACY_OUTPUT_ROOT.mkdir(exist_ok=True)
    CACHE_ROOT.mkdir(exist_ok=True)
    TRANSCRIPT_CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    setup_file_logging()
    server = None
    actual_port = PORT
    for port in range(PORT, PORT + 12):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            actual_port = port
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free local port found from {PORT} to {PORT + 11}.")

    url = f"http://127.0.0.1:{actual_port}"
    print(f"[TranscriptCut] Project root: {ROOT}")
    print(f"[TranscriptCut] Open: {url}")
    if os.getenv("TRANSCRIPT_CUT_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server.serve_forever()
