import argparse
import csv
import json
import os
import re
import textwrap
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


TIME_RE = re.compile(r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?:[,.](\d{1,3}))?")

PAIN_WORDS = [
    "厌学", "躺平", "没干劲", "不想上学", "沉默", "封闭", "疏远", "叛逆",
    "锁门", "玩手机", "打架", "孤立", "偏科", "情绪失控", "退缩", "自卑",
    "不愿意交流", "亲子", "冲突", "焦虑", "拖拉", "假装努力",
]

SOLUTION_WORDS = [
    "怎么办", "建议", "方法", "核心", "关键", "首先", "第二", "第三", "不要",
    "要做", "父母", "家长", "沟通", "理解", "目标", "边界", "支持", "引导",
]

STRUCTURE_WORDS = [
    "为什么", "原因", "因为", "所以", "如果", "但是", "真正", "本质", "最后",
    "结果", "问题", "解决", "举个例子", "你会发现",
]

DEFAULT_TAGS = "#王金海 #家庭教育 #心理学 #育儿 #父母必看 #亲子"
CONFIG_PATH = Path(__file__).resolve().with_name("config.json")

FALLBACK_TRADITIONAL_TO_SIMPLIFIED = str.maketrans(
    {
        "學": "学",
        "習": "习",
        "與": "与",
        "專": "专",
        "業": "业",
        "個": "个",
        "們": "们",
        "會": "会",
        "來": "来",
        "這": "这",
        "那": "那",
        "裡": "里",
        "裏": "里",
        "還": "还",
        "對": "对",
        "為": "为",
        "爲": "为",
        "麼": "么",
        "麽": "么",
        "時": "时",
        "間": "间",
        "點": "点",
        "話": "话",
        "題": "题",
        "問": "问",
        "題": "题",
        "說": "说",
        "聽": "听",
        "講": "讲",
        "讓": "让",
        "給": "给",
        "帶": "带",
        "親": "亲",
        "關": "关",
        "係": "系",
        "無": "无",
        "沒": "没",
        "幹": "干",
        "動": "动",
        "衝": "冲",
        "突": "突",
        "難": "难",
        "壓": "压",
        "慮": "虑",
        "緒": "绪",
        "應": "应",
        "該": "该",
        "辦": "办",
        "麼": "么",
        "師": "师",
        "長": "长",
        "歲": "岁",
        "國": "国",
        "語": "语",
        "數": "数",
        "種": "种",
        "樣": "样",
        "現": "现",
        "實": "实",
        "認": "认",
        "識": "识",
        "變": "变",
        "經": "经",
        "歷": "历",
        "總": "总",
        "結": "结",
        "構": "构",
        "選": "选",
        "擇": "择",
        "標": "标",
        "準": "准",
        "確": "确",
        "隻": "只",
        "併": "并",
        "從": "从",
        "開": "开",
        "閉": "闭",
        "鬆": "松",
        "復": "复",
        "雜": "杂",
        "較": "较",
        "邊": "边",
        "導": "导",
        "幫": "帮",
        "獨": "独",
        "處": "处",
        "理": "理",
        "內": "内",
        "驅": "驱",
        "體": "体",
        "驗": "验",
        "觀": "观",
        "念": "念",
        "斷": "断",
        "續": "续",
        "線": "线",
        "區": "区",
    }
)


def simplify_chinese(text: str) -> str:
    """Prefer OpenCC when installed; keep a local fallback for common transcript text."""
    if not text:
        return text
    try:
        from opencc import OpenCC  # type: ignore

        return OpenCC("t2s").convert(text)
    except Exception:
        return text.translate(FALLBACK_TRADITIONAL_TO_SIMPLIFIED).replace("視頻", "视频")


@dataclass
class TranscriptLine:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    summary: str
    quote: str
    text: str = ""


@dataclass
class ClipCandidate:
    kind: str
    title: str
    score: float
    segments: List[Segment]
    reason: str
    tags: str = DEFAULT_TAGS


def parse_time(value: str) -> float:
    match = TIME_RE.search(value.strip())
    if not match:
        raise ValueError(f"无法解析时间: {value}")
    hour, minute, second, millis = match.groups()
    total = int(second) + int(minute) * 60 + (int(hour) if hour else 0) * 3600
    if millis:
        total += int(millis.ljust(3, "0")[:3]) / 1000
    return float(total)


def format_time(seconds: float, sep: str = ":") -> str:
    seconds = max(0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}{sep}{m:02d}{sep}{s:02d}"


def parse_transcript(path: Path) -> List[TranscriptLine]:
    text = path.read_text(encoding="utf-8-sig")
    if "-->" in text:
        return parse_srt(text)
    return parse_plain_transcript(text)


def parse_srt(text: str) -> List[TranscriptLine]:
    lines: List[TranscriptLine] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        raw = [line.strip() for line in block.splitlines() if line.strip()]
        if not raw:
            continue
        time_line = next((line for line in raw if "-->" in line), "")
        if not time_line:
            continue
        start_raw, end_raw = [part.strip() for part in time_line.split("-->", 1)]
        content = simplify_chinese(" ".join(line for line in raw if line != time_line and not line.isdigit()))
        if content:
            lines.append(TranscriptLine(parse_time(start_raw), parse_time(end_raw), content))
    return lines


def parse_plain_transcript(text: str) -> List[TranscriptLine]:
    parsed: List[TranscriptLine] = []
    pending: List[Tuple[float, str]] = []
    line_re = re.compile(
        r"^\s*(?:\[)?(?P<start>\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?)(?:\])?"
        r"(?:\s*(?:-|~|-->|到)\s*(?P<end>\d{1,2}:\d{2}(?::\d{2})?(?:[,.]\d{1,3})?))?\s*(?P<text>.+)$"
    )
    for raw in text.splitlines():
        match = line_re.match(raw)
        if not match:
            continue
        start = parse_time(match.group("start"))
        content = simplify_chinese(match.group("text").strip())
        if match.group("end"):
            parsed.append(TranscriptLine(start, parse_time(match.group("end")), content))
        else:
            pending.append((start, content))
    if pending:
        for index, (start, content) in enumerate(pending):
            end = pending[index + 1][0] if index + 1 < len(pending) else start + 4
            parsed.append(TranscriptLine(start, max(start + 1, end), content))
    return sorted(parsed, key=lambda item: item.start)


def load_title_examples(csv_path: Optional[Path]) -> List[str]:
    if not csv_path or not csv_path.exists():
        return []
    examples: List[Tuple[float, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            title = (row.get("视频描述") or "").strip().replace("\n", "")
            if not title:
                continue
            plays = float(row.get("播放量") or 0)
            complete = float((row.get("完播率") or "0").replace("%", "") or 0)
            shares = float(row.get("分享量") or 0)
            score = plays * 0.5 + complete * 200 + shares * 8
            examples.append((score, title[:90]))
    return [title for _, title in sorted(examples, reverse=True)[:12]]


def load_local_config() -> Dict[str, str]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value).strip() for key, value in data.items() if value is not None}


def window_lines(lines: Sequence[TranscriptLine], min_seconds: int, max_seconds: int) -> Iterable[List[TranscriptLine]]:
    for start_index in range(len(lines)):
        end_index = start_index
        while end_index < len(lines) and lines[end_index].end - lines[start_index].start <= max_seconds:
            duration = lines[end_index].end - lines[start_index].start
            if duration >= min_seconds:
                yield list(lines[start_index:end_index + 1])
            end_index += 1


def score_text(text: str, duration: float) -> Tuple[float, Dict[str, int]]:
    counts = {
        "pain": sum(word in text for word in PAIN_WORDS),
        "solution": sum(word in text for word in SOLUTION_WORDS),
        "structure": sum(word in text for word in STRUCTURE_WORDS),
    }
    completeness = min(counts["pain"], 2) * 2 + min(counts["solution"], 4) * 1.5 + min(counts["structure"], 4)
    duration_bonus = 4 if 45 <= duration <= 180 else 2 if 25 <= duration <= 240 else -2
    density = min(len(text) / max(duration, 1), 7)
    return completeness + duration_bonus + density * 0.3, counts


def summarize(text: str) -> Tuple[str, str]:
    sentences = re.split(r"[。！？!?]\s*", text)
    sentences = [item.strip() for item in sentences if item.strip()]
    if not sentences:
        return text[:70], text[:90]

    def sentence_score(sentence: str, words: Sequence[str]) -> int:
        return sum(word in sentence for word in words)

    problem_words = PAIN_WORDS + ["问题", "为什么", "原因", "难受", "学校", "同学", "手机"]
    method_words = SOLUTION_WORDS + ["首先", "第二", "第三", "步骤", "不要", "可以", "关键", "核心"]
    problem = max(sentences, key=lambda item: sentence_score(item, problem_words))
    method = max(sentences, key=lambda item: sentence_score(item, method_words))
    if sentence_score(problem, problem_words) == 0:
        problem = sentences[0]
    if method != problem and sentence_score(method, method_words) > 0:
        summary = f"{problem}；{method}"
    else:
        summary = problem
    quote = max(
        sentences,
        key=lambda item: sentence_score(item, ["核心", "关键", "真正", "不要", "父母", "孩子"]) * 10 + min(len(item), 80),
    )
    summary = summary[:110]
    quote = quote[:90]
    return summary, quote


def clean_reference_title(title: str) -> str:
    title = title.split("#", 1)[0].strip(" ，。")
    title = re.sub(r"^(王老师|王金海老师|王金海)(连麦)?(答疑|讲干货)?[:：]?", "", title).strip()
    title = re.sub(r"^(连麦答疑|讲干货)[:：]?", "", title).strip()
    return title[:42]


def title_angle(text: str) -> str:
    if any(word in text for word in ["为什么", "原因", "因为", "本质", "真正"]):
        return "看懂原因"
    if any(word in text for word in ["不要", "千万", "最怕", "火上浇油", "做错"]):
        return "避开误区"
    if any(word in text for word in ["首先", "第二", "第三", "步骤", "方法", "建议", "可以先"]):
        return "具体方法"
    if any(word in text for word in ["目标", "动力", "内驱力", "成就感", "掌控感"]):
        return "找回动力"
    if any(word in text for word in ["沟通", "交流", "关系", "理解", "支持", "愿意"]):
        return "修复关系"
    if any(word in text for word in ["情绪", "焦虑", "难受", "自卑"]):
        return "稳定情绪"
    return "完整分析"


def clean_focus_phrase(phrase: str) -> str:
    phrase = re.sub(r"^(家长问|妈妈问|爸爸问|王老师说|老师说)[，,：:\s]*", "", phrase)
    phrase = re.sub(r"(应该)?怎么办.*$", "怎么办", phrase)
    phrase = phrase.strip(" ，。！？!?：:")
    phrase = re.sub(r"\s+", "", phrase)
    if len(phrase) > 18:
        phrase = phrase[:18]
    return phrase


def extract_title_subject(text: str, topic: str) -> str:
    if "青春期" in text and "尊重" in text:
        return "青春期孩子需要被尊重"
    if "青春期" in text and "选择" in text:
        return "青春期孩子要学会做选择"
    if "人生" in text and ("目标" in text or "选择" in text):
        return "孩子的人生目标"
    if "父母" in text and "感受" in text:
        return "父母要看见孩子的感受"
    focus_patterns = [
        "不想上学", "不想去学校", "锁门玩手机", "被孤立", "和同学闹矛盾",
        "不愿意交流", "沉默封闭", "亲子疏远", "没有学习动力", "没干劲",
        "三分钟热度", "一遇到困难就退缩", "情绪失控", "偏科", "打架",
        "厌学", "躺平", "自卑", "焦虑", "玩手机",
    ]
    for pattern in focus_patterns:
        if pattern in text:
            if pattern.startswith("孩子") or pattern.startswith("亲子"):
                return pattern
            return f"孩子{pattern}"
    if topic:
        return f"孩子{topic}"
    sentences = re.split(r"[。！？!?]\s*", text)
    scored: List[Tuple[int, str]] = []
    for sentence in sentences:
        sentence = clean_focus_phrase(sentence)
        if not sentence:
            continue
        score = 0
        if topic and topic in sentence:
            score += 5
        score += sum(word in sentence for word in PAIN_WORDS) * 3
        score += sum(word in sentence for word in ["怎么办", "为什么", "不要", "核心", "关键", "真正", "父母", "孩子"])
        if 5 <= len(sentence) <= 18:
            score += 2
        scored.append((score, sentence))
    scored.sort(key=lambda item: item[0], reverse=True)
    if scored and scored[0][0] > 0:
        subject = scored[0][1]
        if subject.startswith("孩子") or subject.startswith("父母") or subject.startswith("亲子"):
            return subject
        return f"孩子{subject}" if topic and topic not in subject else subject
    return f"孩子{topic}" if topic else ""


def make_title(text: str, examples: Sequence[str], variant: int = 0) -> str:
    topic = next((word for word in PAIN_WORDS if word in text), "")
    subject = extract_title_subject(text, topic)
    has_solution = "怎么办" in text or any(sol in text for sol in SOLUTION_WORDS)
    angle = title_angle(text)
    if subject:
        if subject in {"青春期孩子需要被尊重", "青春期孩子要学会做选择", "孩子的人生目标", "父母要看见孩子的感受"}:
            special_templates = [
                f"{subject}，父母越早明白越少冲突",
                f"{subject}：青春期亲子关系的关键一步",
                f"{subject}，不是降低父母身份",
            ]
            return special_templates[variant % len(special_templates)]
        if angle == "看懂原因":
            templates = [
                f"{subject}背后，真正要先看懂的是原因",
                f"为什么{subject}？很多父母只看到了表面",
                f"{subject}不是突然发生，背后常有信号",
            ]
        elif angle == "避开误区":
            templates = [
                f"{subject}时，父母最怕把事情做反",
                f"面对{subject}，别急着用硬管解决",
                f"{subject}越管越反？先停下这个动作",
            ]
        elif angle == "具体方法" or has_solution:
            templates = [
                f"{subject}，父母先稳住关系再谈方法",
                f"{subject}别只讲道理，这几步更关键",
                f"处理{subject}，可以先从这一步开始",
            ]
        elif angle == "找回动力":
            templates = [
                f"{subject}，可能缺的不是管教而是目标感",
                f"{subject}没动力？先帮他找回掌控感",
                f"{subject}背后，常常藏着长期失败感",
            ]
        elif angle == "修复关系":
            templates = [
                f"{subject}时，先修复关系比讲道理重要",
                f"亲子关系卡住了，{subject}只是表面信号",
                f"{subject}不愿沟通，父母要先换一种问法",
            ]
        else:
            templates = [
                f"{subject}背后，先处理的不是表面问题",
                f"{subject}不是故意对抗，父母要看懂原因",
                f"很多亲子冲突，都是从误解{subject}开始",
            ]
        return templates[variant % len(templates)]
    cleaned = [clean_reference_title(item) for item in examples]
    cleaned = [item for item in cleaned if item and "王老师" not in item and "连麦答疑" not in item]
    if cleaned:
        return cleaned[variant % len(cleaned)]
    return "这段家庭教育干货，很多父母听完才知道问题在哪"


def ensure_unique_titles(candidates: Sequence[ClipCandidate], examples: Sequence[str]) -> List[ClipCandidate]:
    seen: Dict[str, int] = {}
    suffixes = ["看懂原因", "避开误区", "具体方法", "修复关系", "找回动力", "完整分析"]
    for candidate in candidates:
        source_text = "".join((segment.text or segment.summary + segment.quote) for segment in candidate.segments)
        count = seen.get(candidate.title, 0)
        if count:
            candidate.title = make_title(source_text, examples, count)
            if candidate.title in seen:
                candidate.title = f"{candidate.title}：{suffixes[count % len(suffixes)]}"
        seen[candidate.title] = seen.get(candidate.title, 0) + 1
    return list(candidates)


def build_candidates(
    lines: Sequence[TranscriptLine],
    title_examples: Sequence[str],
    min_seconds: int = 45,
    max_seconds: int = 210,
    limit: int = 8,
) -> List[ClipCandidate]:
    ranked: List[ClipCandidate] = []
    for group in window_lines(lines, min_seconds, max_seconds):
        text = "".join(item.text for item in group)
        duration = group[-1].end - group[0].start
        score, counts = score_text(text, duration)
        if counts["pain"] == 0 and counts["solution"] < 2:
            continue
        summary, quote = summarize(text)
        ranked.append(
            ClipCandidate(
                kind="continuous",
                title=make_title(text, title_examples, len(ranked)),
                score=round(score, 2),
                segments=[Segment(group[0].start, group[-1].end, summary, quote, text)],
                reason=f"痛点词{counts['pain']}个，方法词{counts['solution']}个，结构词{counts['structure']}个，时长{int(duration)}秒",
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    deduped = dedupe_candidates(ranked)
    continuous = ensure_unique_titles(deduped[:limit], title_examples)
    discontinuous = build_discontinuous_candidates(continuous, limit=3, title_examples=title_examples)
    return ensure_unique_titles((continuous + discontinuous)[: limit + 3], title_examples)


def dedupe_candidates(candidates: Sequence[ClipCandidate]) -> List[ClipCandidate]:
    chosen: List[ClipCandidate] = []
    for candidate in candidates:
        seg = candidate.segments[0]
        if all(seg.end <= other.segments[0].start or seg.start >= other.segments[0].end for other in chosen):
            chosen.append(candidate)
        if len(chosen) >= 12:
            break
    return chosen


def build_discontinuous_candidates(candidates: Sequence[ClipCandidate], limit: int, title_examples: Sequence[str]) -> List[ClipCandidate]:
    results: List[ClipCandidate] = []
    seen_combos = set()
    for first in candidates:
        first_text = first.segments[0].summary + first.segments[0].quote
        topic = next((word for word in PAIN_WORDS if word in first_text), "")
        if not topic:
            continue
        matches = [first]
        for other in candidates:
            if other is first:
                continue
            other_text = other.segments[0].summary + other.segments[0].quote
            if topic in other_text or any(word in other_text for word in SOLUTION_WORDS):
                matches.append(other)
            if len(matches) == 2:
                break
        if len(matches) < 2:
            continue
        segments = [match.segments[0] for match in sorted(matches, key=lambda item: item.segments[0].start)]
        combo_key = tuple((round(segment.start, 2), round(segment.end, 2)) for segment in segments)
        if combo_key in seen_combos:
            continue
        seen_combos.add(combo_key)
        combined_text = "".join((segment.text or segment.summary + segment.quote) for segment in segments)
        results.append(
            ClipCandidate(
                kind="discontinuous",
                title=make_title(combined_text, title_examples, len(results) + 1),
                score=round(sum(match.score for match in matches) / len(matches) + 1.5, 2),
                segments=segments,
                reason="由同一痛点的背景片段和解决片段组合，适合做不连续精剪",
            )
        )
        if len(results) >= limit:
            break
    return results


def kimi_titles(
    candidates: Sequence[ClipCandidate],
    title_examples: Sequence[str],
    api_key: Optional[str] = None,
    model: str = "moonshot-v1-8k",
) -> List[Dict[str, object]]:
    config = load_local_config()
    api_key = (
        api_key
        or os.getenv("MOONSHOT_API_KEY")
        or os.getenv("KIMI_API_KEY")
        or config.get("moonshot_api_key")
        or config.get("kimi_api_key")
    )
    model = config.get("kimi_model") or model
    if not api_key:
        fallback = [asdict(candidate) for candidate in candidates]
        for item in fallback:
            item["title_source"] = "local"
            item["kimi_error"] = "missing_api_key"
        return fallback
    payload_candidates = []
    for index, candidate in enumerate(candidates, 1):
        item = asdict(candidate)
        item["index"] = index
        item["full_text"] = "\n".join(segment.text or (segment.summary + segment.quote) for segment in candidate.segments)
        payload_candidates.append(item)
    prompt = {
        "role": "user",
        "content": "你是家庭教育、亲子沟通、育儿心理短视频标题策划。请逐条阅读每个切片的 full_text，先总结这个切片真正讲的完整内容，再生成一个吸引人的短视频标题。标题必须基于该片段的核心问题、原因、方法或反转，不要直接照抄第一句话。历史标题只用于学习痛点表达、问题意识和行动指向，不要把人名、老师称呼、连麦答疑、讲干货当成固定格式。除非切片内容明确要求，否则不要在标题里写“王老师”“连麦答疑”。返回 JSON 数组，每项包含 index、主标题、备选标题、内容总结。"
        + "\n历史标题（仅参考表达风格）:\n"
        + "\n".join(clean_reference_title(item) for item in title_examples[:8])
        + "\n候选切片:\n"
        + json.dumps(payload_candidates, ensure_ascii=False),
    }
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": "只输出 JSON 数组，不要输出解释。"}, prompt],
        "temperature": 0.7,
    }
    request = urllib.request.Request(
        "https://api.moonshot.cn/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    def parse_kimi_json(content: str):
        content = content.strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content).strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            array_start = content.find("[")
            array_end = content.rfind("]")
            object_start = content.find("{")
            object_end = content.rfind("}")
            if array_start != -1 and array_end != -1 and array_end > array_start:
                parsed = json.loads(content[array_start : array_end + 1])
            elif object_start != -1 and object_end != -1 and object_end > object_start:
                parsed = json.loads(content[object_start : object_end + 1])
            else:
                raise
        return parsed if isinstance(parsed, list) else [parsed]

    raw_content = ""
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        raw_content = content
        generated = parse_kimi_json(content)
    except Exception as exc:
        fallback = [asdict(candidate) for candidate in candidates]
        error = f"{type(exc).__name__}: {exc}"
        for item in fallback:
            item["title_source"] = "local"
            item["kimi_error"] = error[:500]
            if raw_content:
                item["kimi_raw_response"] = raw_content[:4000]
        return fallback
    merged = [asdict(candidate) for candidate in candidates]
    for item, ai_item in zip(merged, generated):
        if isinstance(ai_item, dict):
            item["kimi_titles"] = ai_item
            main_title = (
                ai_item.get("主标题")
                or ai_item.get("main_title")
                or ai_item.get("title")
                or ai_item.get("标题")
                or ai_item.get("video_title")
            )
            if isinstance(main_title, str) and main_title.strip():
                item["title"] = main_title.strip()
                item["title_source"] = "kimi"
            content_summary = ai_item.get("内容总结") or ai_item.get("summary")
            if isinstance(content_summary, str) and content_summary.strip() and item.get("segments"):
                item["segments"][0]["summary"] = content_summary.strip()[:140]
        else:
            item["title_source"] = "local"
    seen: Dict[str, int] = {}
    suffixes = ["看懂原因", "避开误区", "具体方法", "修复关系", "找回动力", "完整分析"]
    for item in merged:
        title = str(item.get("title") or "").strip()
        count = seen.get(title, 0)
        if count:
            item["title"] = f"{title}：{suffixes[count % len(suffixes)]}"
        seen[str(item.get("title") or "")] = seen.get(str(item.get("title") or ""), 0) + 1
    return merged


def frames(seconds: float, fps: int = 25) -> str:
    frame = int(round((seconds - int(seconds)) * fps))
    return f"{format_time(seconds)}:{frame:02d}"


def write_edl(candidate: ClipCandidate, output: Path, reel: str = "AX") -> None:
    timeline = 0.0
    lines = ["TITLE: " + candidate.title[:60], "FCM: NON-DROP FRAME", ""]
    for index, segment in enumerate(candidate.segments, 1):
        duration = segment.end - segment.start
        lines.append(
            f"{index:03d}  {reel:<8} V     C        "
            f"{frames(segment.start)} {frames(segment.end)} {frames(timeline)} {frames(timeline + duration)}"
        )
        lines.append(f"* FROM CLIP NAME: segment_{index}_{format_time(segment.start, '-')}_{format_time(segment.end, '-')}")
        lines.append("")
        timeline += duration
    output.write_text("\n".join(lines), encoding="utf-8")


def export_outputs(candidates: Sequence[ClipCandidate], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "candidates.json").write_text(
        json.dumps([asdict(item) for item in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for index, candidate in enumerate(candidates, 1):
        write_edl(candidate, out_dir / f"clip_{index:02d}_{candidate.kind}.edl")


def run(
    transcript: Path,
    csv_path: Optional[Path],
    out_dir: Path,
    use_kimi: bool,
    min_seconds: int = 45,
    max_seconds: int = 210,
) -> List[ClipCandidate]:
    lines = parse_transcript(transcript)
    if not lines:
        raise ValueError("没有解析到带时间戳的字幕。支持 SRT，或每行以 [00:00:00] 开头的文本。")
    examples = load_title_examples(csv_path)
    candidates = build_candidates(lines, examples, min_seconds=min_seconds, max_seconds=max_seconds)
    export_outputs(candidates, out_dir)
    if use_kimi:
        titled = kimi_titles(candidates, examples)
        (out_dir / "candidates.kimi.json").write_text(json.dumps(titled, ensure_ascii=False, indent=2), encoding="utf-8")
        errors = sorted({str(item.get("kimi_error", "")) for item in titled if item.get("kimi_error")})
        if errors:
            (out_dir / "kimi_error.txt").write_text("\n".join(errors), encoding="utf-8")
            raw = "\n\n---\n\n".join(str(item.get("kimi_raw_response", "")) for item in titled if item.get("kimi_raw_response"))
            if raw:
                (out_dir / "kimi_raw_response.txt").write_text(raw, encoding="utf-8")
        else:
            error_path = out_dir / "kimi_error.txt"
            if error_path.exists():
                error_path.unlink()
            raw_path = out_dir / "kimi_raw_response.txt"
            if raw_path.exists():
                raw_path.unlink()
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="家庭教育直播回放切片候选生成器")
    parser.add_argument("transcript", type=Path, help="带时间戳的字幕/转写稿，支持 srt/txt")
    parser.add_argument("--history", type=Path, default=Path("视频号动态数据明细.csv"), help="历史视频数据 CSV")
    parser.add_argument("--out", type=Path, default=Path("outputs"), help="输出目录")
    parser.add_argument("--kimi", action="store_true", help="调用 KIMI/Moonshot 生成标题，需要 MOONSHOT_API_KEY")
    parser.add_argument("--min-seconds", type=int, default=45, help="候选片段最短秒数")
    parser.add_argument("--max-seconds", type=int, default=210, help="候选片段最长秒数")
    args = parser.parse_args()
    candidates = run(args.transcript, args.history, args.out, args.kimi, args.min_seconds, args.max_seconds)
    for index, candidate in enumerate(candidates, 1):
        ranges = " + ".join(f"{format_time(seg.start)}-{format_time(seg.end)}" for seg in candidate.segments)
        print(textwrap.shorten(f"{index}. [{candidate.kind}] {ranges} {candidate.title} | {candidate.reason}", width=160))


if __name__ == "__main__":
    main()
