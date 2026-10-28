import json
import logging
import os
import re
import signal
import stat
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# 导入 Web UI
from config_manager import ConfigError, get_config_path, load_config
from web_ui import start_web_server, state

logger = logging.getLogger("SubtitleTranslator")


# 任意文字（含 CJK 等），不含数字和下划线
LETTER_RE = re.compile(r"[^\W\d_]")
# SRT 用逗号、WebVTT 用点分隔毫秒，WebVTT 还可以省略小时
SUBTITLE_TIMING_RE = re.compile(r"(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s*-->")
ASS_EVENTS_RE = re.compile(r"^\s*\[events\]\s*$", re.IGNORECASE | re.MULTILINE)
ASS_TAG_RE = re.compile(r"\{[^}]*\}")
ASS_LEADING_TAGS_RE = re.compile(r"^(?:\{[^}]*\})*")
ASS_DRAWING_RE = re.compile(r"\\p[1-9]")


# 文件名中各目标语言的常见标签写法，例如 movie.chs.srt、movie.zh-CN.srt
LANGUAGE_TAG_ALIASES = {
    "zh": {"zh", "chs", "cht", "chi", "zho", "sc", "tc", "cn", "gb", "big5",
           "zh-cn", "zh-hans", "zh-sg", "zh-tw", "zh-hant", "zh-hk"},
    "en": {"en", "eng", "en-us", "en-gb"},
    "ja": {"ja", "jp", "jpn"},
    "ko": {"ko", "kor"},
    "fr": {"fr", "fre", "fra"},
    "de": {"de", "ger", "deu"},
    "es": {"es", "spa", "es-419"},
    "it": {"it", "ita"},
    "pt": {"pt", "por", "pt-br", "pt-pt"},
    "ru": {"ru", "rus"},
    "ar": {"ar", "ara"},
    "th": {"th", "tha"},
}
# 不作为目标语言、但会出现在文件名里的语言
OTHER_LANGUAGE_TAGS = {"vi"}
# 语言之外的附加标签：听障（hi/sdh/cc）、强制字幕等
SUBTITLE_FLAG_TAGS = {"hi", "sdh", "cc", "forced", "default", "full"}
KNOWN_SUBTITLE_TAGS = SUBTITLE_FLAG_TAGS.union(
    OTHER_LANGUAGE_TAGS, *LANGUAGE_TAG_ALIASES.values()
)


def split_subtitle_name(file_path):
    """拆出 (去掉标签后的路径, 文件名末尾的标签列表, 扩展名)。

    movie.zh.hi.srt -> ("movie", ["zh", "hi"], ".srt")
    """
    base, ext = os.path.splitext(file_path)
    tags = []
    directory, name = os.path.split(base)
    parts = name.split(".")
    while len(parts) > 1 and parts[-1].lower() in KNOWN_SUBTITLE_TAGS:
        tags.insert(0, parts.pop().lower())
    return os.path.join(directory, ".".join(parts)), tags, ext


def is_target_language_file(file_path, target_lang):
    """文件名任一语言标签属于目标语言（含 .zh.hi、.chs 等写法）"""
    target_lang = target_lang.lower()
    aliases = LANGUAGE_TAG_ALIASES.get(target_lang, {target_lang})
    _, tags, _ = split_subtitle_name(file_path)
    return any(tag in aliases for tag in tags)


class SubtitleTranslationError(RuntimeError):
    pass


class InvalidResponseError(SubtitleTranslationError):
    """API 返回了内容，但无法解析成译文"""


@dataclass(frozen=True)
class SubtitleCue:
    position: int  # 在输出行列表中的位置
    text: str  # 发送给模型的纯文本
    original: str  # 无需翻译或翻译失败时原样写回的内容
    ass_prefix: str = ""  # ASS: Text 字段之前的部分及前导样式标签
    ass_body: str = ""  # ASS: 前导样式标签之后的原文
    is_ass: bool = False

    def render(self, translated):
        """生成双语内容：译文在上，原文在下"""
        if translated is None:
            return self.original
        if self.is_ass:
            translated_line = "\\N".join(translated.split("\n"))
            return f"{self.ass_prefix}{translated_line}\\N{self.ass_body}"
        return f"{translated}\n{self.original}"


@dataclass(frozen=True)
class SubtitleTranslationResult:
    content: str
    total_segments: int
    failed_indices: tuple[int, ...]

    @property
    def successful_segments(self):
        return self.total_segments - len(self.failed_indices)

    @property
    def success_rate(self):
        if self.total_segments == 0:
            return 1.0
        return self.successful_segments / self.total_segments


def configure_logging(log_level):
    handlers = [logging.StreamHandler(sys.stdout)]
    file_error = None
    log_path = get_config_path().parent / "subtranslator.log"

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.insert(0, logging.FileHandler(log_path, encoding="utf-8"))
    except OSError as exc:
        file_error = exc

    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )

    if file_error:
        logger.warning(
            "无法写入日志文件 %s，仅使用控制台日志: %s", log_path, file_error
        )


class SubtitleTranslator:
    MIN_PARTIAL_SUCCESS_RATE: ClassVar[float] = 0.99
    LANG_NAMES: ClassVar[dict[str, str]] = {
        "zh": "中文",
        "en": "English",
        "ja": "日本語",
        "ko": "한국어",
        "fr": "Français",
        "de": "Deutsch",
        "es": "Español",
        "it": "Italiano",
        "pt": "Português",
        "ru": "Русский",
        "ar": "العربية",
        "th": "ไทย",
    }
    # 目标语言特有的文字，用于判断是否需要翻译、是否已是双语
    TARGET_SCRIPT_PATTERNS: ClassVar[dict[str, re.Pattern]] = {
        "zh": re.compile(r"[\u4e00-\u9fff]"),
        "ja": re.compile(r"[\u3040-\u30ff]"),
        "ko": re.compile(r"[\uac00-\ud7af]"),
        "ru": re.compile(r"[\u0400-\u04ff]"),
        "ar": re.compile(r"[\u0600-\u06ff]"),
        "th": re.compile(r"[\u0e00-\u0e7f]"),
    }
    BATCH_TIMEOUT: ClassVar[int] = 120

    def __init__(
        self,
        api_key,
        proxy_url,
        model,
        max_workers=10,
        target_lang="zh",
        api_retry_count=3,
        batch_size=30,
    ):
        self.api_key = api_key
        self.proxy_url = proxy_url
        self.model = model
        self.max_workers = max_workers
        self.target_lang = target_lang
        self.api_retry_count = api_retry_count
        self.batch_size = max(1, batch_size)
        self.target_lang_name = self.LANG_NAMES.get(target_lang, target_lang)
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        self.executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="subtitle-api",
        )
        self.thread_local = threading.local()
        logger.info(
            "初始化翻译器: 模型=%s, 代理URL=%s, 最大并发数=%s, API失败重试次数=%s, 每批字幕数=%s",
            model,
            "已设置" if proxy_url else "未设置",
            max_workers,
            api_retry_count,
            self.batch_size,
        )
        state.add_log("INFO", f"翻译器初始化完成: {model}")

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _get_session(self):
        session = getattr(self.thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.headers)
            self.thread_local.session = session
        return session

    def _target_script(self):
        """返回目标语言特有文字的正则；拉丁字母语言无法按文字区分，返回 None"""
        return self.TARGET_SCRIPT_PATTERNS.get(self.target_lang)

    def _is_already_translated(self, content):
        """检测字幕是否已经是双语格式（同时含目标语言文字和其他文字）"""
        target_script = self._target_script()
        if target_script is None:
            return False

        target_count = 0
        other_count = 0
        cues, _ = self._parse_subtitle(content)
        for cue in cues:
            for line in cue.text.split("\n"):
                if target_script.search(line):
                    target_count += 1
                elif LETTER_RE.search(line):
                    other_count += 1

        total = target_count + other_count
        if total < 4:
            return False
        return target_count >= total * 0.2 and other_count >= total * 0.2

    def _wait_for_file_stable(self, file_path, timeout=10):
        """等待文件写入完成"""
        last_size = -1
        stable_count = 0
        start_time = time.time()

        while time.time() - start_time < timeout:
            try:
                current_size = os.path.getsize(file_path)
                if current_size == last_size:
                    stable_count += 1
                    if stable_count >= 3:  # 连续3次大小不变
                        return True
                else:
                    stable_count = 0
                    last_size = current_size
                time.sleep(0.5)
            except OSError:
                time.sleep(0.5)

        return stable_count >= 2

    @staticmethod
    def _parse_subtitle(content):
        """拆分出输出行和待翻译片段；片段所在行最终由 SubtitleCue.render 填充"""
        content = content.replace("\r\n", "\n")
        if ASS_EVENTS_RE.search(content):
            return SubtitleTranslator._parse_ass(content)
        return SubtitleTranslator._parse_timed_text(content)

    @staticmethod
    def _parse_timed_text(content):
        """解析 SRT / WebVTT：时间轴行之后到空行之前的内容为一个片段"""
        lines = content.strip().split("\n")
        result_lines = []
        cues = []

        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip().isdigit():
                result_lines.append(line)
                i += 1
                continue

            if SUBTITLE_TIMING_RE.search(line):
                result_lines.append(line)

                text_lines = []
                i += 1

                while (
                    i < len(lines)
                    and lines[i].strip()
                    and not lines[i].strip().isdigit()
                    and "-->" not in lines[i]
                ):
                    text_lines.append(lines[i])
                    i += 1

                if text_lines:
                    text = "\n".join(text_lines)
                    cues.append(
                        SubtitleCue(position=len(result_lines), text=text, original=text)
                    )
                    result_lines.append(text)

                if i < len(lines) and not lines[i].strip():
                    result_lines.append("")
                    i += 1
            else:
                result_lines.append(line)
                i += 1

        return cues, result_lines

    @staticmethod
    def _parse_ass(content):
        """解析 ASS / SSA：[Events] 段中的 Dialogue 行，按 Format 定位 Text 字段"""
        result_lines = []
        cues = []
        in_events = False
        text_index = 9

        for line in content.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_events = stripped.lower() == "[events]"
            elif in_events and stripped.lower().startswith("format:"):
                fields = [
                    field.strip().lower()
                    for field in stripped.split(":", 1)[1].split(",")
                ]
                if "text" in fields:
                    text_index = fields.index("text")
            elif in_events and stripped.startswith("Dialogue:"):
                cue = SubtitleTranslator._parse_ass_dialogue(
                    line, text_index, len(result_lines)
                )
                if cue:
                    cues.append(cue)
            result_lines.append(line)

        return cues, result_lines

    @staticmethod
    def _parse_ass_dialogue(line, text_index, position):
        fields = line.split(":", 1)[1].split(",", text_index)
        if len(fields) <= text_index:
            return None

        text_field = fields[text_index]
        # 绘图指令（\p1 等）不是台词
        if ASS_DRAWING_RE.search(text_field):
            return None

        leading_tags = ASS_LEADING_TAGS_RE.match(text_field).group(0)
        body = text_field[len(leading_tags) :]
        text = (
            ASS_TAG_RE.sub("", body)
            .replace("\\N", "\n")
            .replace("\\n", "\n")
            .replace("\\h", " ")
        )
        text = "\n".join(part.strip() for part in text.split("\n")).strip()
        if not text:
            return None

        prefix = line[: len(line) - len(text_field)] + leading_tags
        return SubtitleCue(
            position=position,
            text=text,
            original=line,
            ass_prefix=prefix,
            ass_body=body,
            is_ass=True,
        )

    def translate_subtitle_content(
        self, content, file_path="unknown", on_progress=None
    ):
        """按批翻译字幕内容，保留时间轴和样式，译文在上、原文在下。

        每完成一批就记录该批译文，并以 on_progress(已完成批数, 总批数) 报告进度。
        """
        cues, result_lines = self._parse_subtitle(content)
        translations = [None] * len(cues)
        failures = []

        pending = [
            idx for idx, cue in enumerate(cues) if self._needs_translation(cue.text)
        ]
        batches = [
            pending[start : start + self.batch_size]
            for start in range(0, len(pending), self.batch_size)
        ]
        future_to_batch = {
            self.executor.submit(
                self.translate_batch, [cues[idx].text for idx in batch]
            ): batch
            for batch in batches
        }

        if on_progress:
            on_progress(0, len(batches))
        for completed, future in enumerate(as_completed(future_to_batch), start=1):
            batch = future_to_batch[future]
            try:
                batch_translations = future.result()
            except Exception as exc:  # noqa: BLE001 - future may raise provider errors
                logger.error("翻译批次失败（%s 个片段）: %s", len(batch), exc)
                batch_translations = [None] * len(batch)
            for idx, translated_text in zip(batch, batch_translations):
                if translated_text is None:
                    failures.append(idx)
                else:
                    translations[idx] = translated_text
            self._log_batch_content(file_path, cues, batch, translations)
            if on_progress:
                on_progress(completed, len(batches))

        failures.sort()
        for idx in failures:
            logger.warning(
                "片段 %s 重试耗尽，保留原文: %s",
                idx + 1,
                " ".join(cues[idx].text.split())[:120],
            )

        for cue, translated_text in zip(cues, translations):
            result_lines[cue.position] = cue.render(translated_text)

        return SubtitleTranslationResult(
            content="\n".join(result_lines),
            total_segments=len(cues),
            failed_indices=tuple(failures),
        )

    @staticmethod
    def _log_batch_content(file_path, cues, batch, translations):
        """记录一批译文；日志按最新优先排列，倒序写入让同一批保持字幕顺序"""
        for idx in reversed(batch):
            if translations[idx] is not None:
                state.add_translation_content_log(
                    file_path=file_path,
                    original_text=cues[idx].text[:200],
                    translated_text=translations[idx][:200],
                )

    @staticmethod
    def _api_error_detail(response):
        try:
            body = response.json()
        except (ValueError, TypeError):
            detail = response.text
        else:
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or error
                code = error.get("code")
                detail = f"{code}: {message}" if code and code != message else message
            else:
                detail = error or body

        normalized = " ".join(str(detail or "无响应详情").split())
        request_id = response.headers.get("x-request-id", "")
        if request_id:
            normalized = f"{normalized} (request_id={request_id})"
        return normalized[:500]

    def _chat(self, messages, parse, timeout, retries):
        """发送聊天请求并解析回复；HTTP 错误和响应格式错误都按指数退避重试"""
        total_attempts = retries + 1
        for attempt in range(total_attempts):
            try:
                payload = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": 0.3,
                }
                response = self._get_session().post(
                    self.proxy_url, json=payload, timeout=timeout
                )

                if response.status_code != 200:
                    detail = self._api_error_detail(response)
                    raise SubtitleTranslationError(
                        f"API 错误 {response.status_code}: {detail}"
                    )

                try:
                    reply = response.json()["choices"][0]["message"]["content"]
                    reply = reply.strip()
                except (
                    KeyError,
                    IndexError,
                    TypeError,
                    AttributeError,
                    ValueError,
                ) as exc:
                    raise InvalidResponseError("API 响应格式不正确") from exc
                return parse(reply)
            except Exception as exc:
                if attempt == total_attempts - 1:
                    raise
                logger.warning(
                    "API 请求失败，尝试 %s/%s: %s",
                    attempt + 1,
                    total_attempts,
                    exc,
                )
                time.sleep(2**attempt)  # 指数退避

        raise SubtitleTranslationError("API 请求未执行")

    def translate_batch(self, texts):
        """一次请求翻译多条连续字幕，返回与 texts 等长的列表，None 表示该条失败。

        整批请求失败时抛出异常；响应无法解析或缺少条目时，缺失的条目逐条补译。
        """
        if len(texts) == 1:
            return [self.translate_text(texts[0])]

        items = [{"id": idx, "text": text} for idx, text in enumerate(texts)]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个专业的字幕翻译专家。用户会发送一个 JSON 数组，"
                    "每个元素包含 id 和 text，是同一部影片中按时间顺序排列的连续字幕。"
                    f"请把每条 text 翻译成{self.target_lang_name}，"
                    "结合上下文让译文自然流畅，人名、称谓和术语前后一致。"
                    '只返回 JSON 数组，格式为 [{"id": 0, "text": "译文"}]：'
                    "每个 id 对应一条译文，不要合并、拆分或遗漏条目，"
                    "text 中的换行保留为换行，不要添加任何解释或额外内容。"
                ),
            },
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ]

        try:
            translated = self._chat(
                messages,
                parse=lambda reply: self._parse_batch_reply(reply, len(texts)),
                timeout=self.BATCH_TIMEOUT,
                retries=self.api_retry_count,
            )
        except InvalidResponseError as exc:
            logger.warning("批量翻译响应无法解析，改为逐条翻译: %s", exc)
            translated = {}

        missing = [idx for idx in range(len(texts)) if idx not in translated]
        if missing and translated:
            logger.warning(
                "批量翻译响应缺少 %s/%s 条，逐条补译", len(missing), len(texts)
            )
        for idx in missing:
            try:
                translated[idx] = self.translate_text(texts[idx])
            except Exception as exc:  # noqa: BLE001 - keep other cues of the batch
                logger.error("逐条补译失败: %s", exc)
                translated[idx] = None

        return [translated[idx] for idx in range(len(texts))]

    @staticmethod
    def _parse_batch_reply(reply, count):
        """解析模型返回的 [{"id": n, "text": "..."}]，容忍代码块包裹和多余文字"""
        start, end = reply.find("["), reply.rfind("]")
        if start == -1 or end < start:
            raise InvalidResponseError("响应中没有 JSON 数组")
        try:
            items = json.loads(reply[start : end + 1])
        except json.JSONDecodeError as exc:
            raise InvalidResponseError(f"JSON 解析失败: {exc}") from exc
        if not isinstance(items, list):
            raise InvalidResponseError("响应不是 JSON 数组")

        translated = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            idx, text = item.get("id"), item.get("text")
            if isinstance(idx, str) and idx.isdigit():
                idx = int(idx)
            if (
                isinstance(idx, int)
                and 0 <= idx < count
                and isinstance(text, str)
                and text.strip()
            ):
                translated[idx] = text.strip()

        if not translated:
            raise InvalidResponseError("响应中没有有效的译文条目")
        return translated

    def translate_text(self, text, retry_count=None):
        """单条翻译：用于只有一条的批次和批量响应中缺失条目的补译"""
        if not self._needs_translation(text):
            return text

        retries = self.api_retry_count if retry_count is None else retry_count
        messages = [
            {
                "role": "system",
                "content": f"你是一个专业的字幕翻译专家。请将以下字幕翻译成{self.target_lang_name}，保持原意的同时使翻译自然流畅。不要添加任何解释或额外内容。",
            },
            {
                "role": "user",
                "content": f"翻译以下字幕内容为{self.target_lang_name}:\n{text}",
            },
        ]
        return self._chat(messages, parse=lambda reply: reply, timeout=30, retries=retries)

    def _has_translatable_cues(self, content):
        cues, _ = self._parse_subtitle(content)
        return any(self._needs_translation(cue.text) for cue in cues)

    def _needs_translation(self, text):
        """检查文本是否需要翻译"""
        # 纯数字、符号或音符等没有文字的片段原样保留
        if not LETTER_RE.search(text):
            return False
        target_script = self._target_script()
        if target_script is None:
            return True
        return len(target_script.findall(text)) < len(text) * 0.3

    def translate_subtitle_file(self, file_path):
        """翻译字幕文件"""
        state.increment_stat("in_progress")
        try:
            # 目标语言字幕（如 .zh.hi.srt）的输出路径会与已有译文重名，翻译会覆盖它
            output_path = self._generate_output_path(file_path)
            if is_target_language_file(file_path, self.target_lang):
                logger.info("文件名已带目标语言标识，跳过: %s", file_path)
                state.add_file(file_path, "success", "已是目标语言字幕")
                return True

            logger.info(f"开始处理字幕文件: {file_path}")
            state.add_log("INFO", f"开始翻译: {file_path}")
            state.add_translation_log(file_path, "starting", "等待文件稳定...", 0)
            state.add_file(file_path, "progress", "正在翻译...")

            # 等待文件稳定
            if not self._wait_for_file_stable(file_path):
                logger.warning(f"文件可能未完全写入: {file_path}")
                state.add_translation_log(
                    file_path, "translating", "文件可能未完全写入，继续处理...", 10
                )

            state.add_translation_log(file_path, "translating", "读取文件内容...", 20)
            content = self._read_subtitle(file_path)

            # 检查是否已翻译
            if self._is_already_translated(content):
                logger.info(f"文件已是双语字幕，跳过: {file_path}")
                state.add_log("INFO", f"跳过已翻译文件: {file_path}")
                state.add_translation_log(
                    file_path, "skipped", "已是双语字幕，跳过翻译", 100
                )
                state.add_file(file_path, "success", "已是双语字幕")
                return True

            # 文件名没有语言标签但内容已是目标语言时，不调用 API，也不写出副本
            if not self._has_translatable_cues(content):
                logger.info(f"字幕已是目标语言，跳过: {file_path}")
                state.add_log("INFO", f"跳过目标语言字幕: {file_path}")
                state.add_translation_log(
                    file_path, "skipped", "已是目标语言字幕，跳过翻译", 100
                )
                state.add_file(file_path, "success", "已是目标语言字幕")
                return True

            state.add_translation_log(
                file_path, "translating", "正在调用 API 翻译...", 30
            )
            def report_progress(completed, total):
                # API 翻译阶段占进度条的 30%–80%
                progress = 30 + (50 * completed // total if total else 50)
                state.add_translation_log(
                    file_path,
                    "translating",
                    f"正在翻译：已完成 {completed}/{total} 批",
                    progress,
                )

            translation_result = self.translate_subtitle_content(
                content, file_path=file_path, on_progress=report_progress
            )

            if (
                translation_result.failed_indices
                and translation_result.success_rate < self.MIN_PARTIAL_SUCCESS_RATE
            ):
                raise SubtitleTranslationError(
                    f"仅完成 {translation_result.successful_segments}/"
                    f"{translation_result.total_segments} 个字幕片段 "
                    f"({translation_result.success_rate:.2%})，低于 99% 保存阈值"
                )

            state.add_translation_log(
                file_path, "translating", "翻译完成，保存文件...", 80
            )
            source_mode = stat.S_IMODE(Path(file_path).stat().st_mode)
            self._atomic_write(
                output_path,
                translation_result.content,
                mode=source_mode,
            )

            if translation_result.failed_indices:
                message = (
                    f"部分完成: {translation_result.successful_segments}/"
                    f"{translation_result.total_segments} "
                    f"({translation_result.success_rate:.2%})，"
                    f"{len(translation_result.failed_indices)} 个片段保留原文"
                )
                logger.warning("%s: %s", message, output_path)
                state.add_log("WARNING", f"{message}: {output_path}")
                state.add_translation_log(file_path, "partial", message, 100)
                state.add_file(file_path, "partial", f"{message}: {output_path}")
            else:
                logger.info(f"翻译完成: {output_path}")
                state.add_log("INFO", f"翻译完成: {output_path}")
                state.add_translation_log(
                    file_path, "success", f"已保存到 {output_path}", 100
                )
                state.add_file(file_path, "success", f"已保存到 {output_path}")
            state.increment_stat("total_translated")
            return True

        except Exception as e:  # noqa: BLE001 - file task boundary records all failures
            logger.error(f"翻译失败 {file_path}: {e!s}")
            state.add_log("ERROR", f"翻译失败 {file_path}: {e!s}")
            state.add_translation_log(file_path, "failed", f"错误: {e!s}", 0)
            state.add_file(file_path, "error", str(e))
            return False
        finally:
            state.decrement_stat("in_progress")

    @staticmethod
    def _read_subtitle(file_path):
        data = Path(file_path).read_bytes()
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return data.decode("utf-16")
        if data.startswith(b"\xef\xbb\xbf"):
            return data.decode("utf-8-sig")

        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return data.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise SubtitleTranslationError(
                    "字幕编码不受支持，请转换为 UTF-8、UTF-16 或 GB18030"
                ) from exc

    @staticmethod
    def _atomic_write(output_path, content, mode=0o644):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                os.fchmod(output_file.fileno(), mode)
                output_file.write(content)
                output_file.flush()
                os.fsync(output_file.fileno())
            os.replace(temporary_path, output_path)
        finally:
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()

    def _generate_output_path(self, file_path):
        """生成输出文件路径：去掉文件名末尾的语言和附加标签，加上目标语言"""
        base, _, ext = split_subtitle_name(file_path)
        return f"{base}.{self.target_lang}{ext}"


class SubtitleEventHandler(FileSystemEventHandler):
    def __init__(self, translator, subtitle_extensions):
        self.translator = translator
        self.subtitle_extensions = subtitle_extensions
        self.processed_files = {}  # {path: timestamp}
        self.processing_lock = threading.Lock()
        self.pending_files = {}
        self.retry_counts = {}
        self.max_age = 3600  # 1 hour TTL
        self.max_retries = 3
        self.shutting_down = False
        self.file_executor = ThreadPoolExecutor(
            max_workers=max(1, min(4, translator.max_workers)),
            thread_name_prefix="subtitle-file",
        )

    def on_created(self, event):
        if event.is_directory:
            return
        self._schedule_file(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        self._schedule_file(event.dest_path)

    def shutdown(self):
        with self.processing_lock:
            self.shutting_down = True
            timers = list(self.pending_files.values())
            self.pending_files.clear()
        for timer in timers:
            timer.cancel()
        self.file_executor.shutdown(wait=True, cancel_futures=True)

    def _schedule_file(self, file_path, delay=2):
        if not any(file_path.lower().endswith(ext) for ext in self.subtitle_extensions):
            return
        if self._is_translated_output(file_path):
            logger.debug("跳过已翻译文件: %s", file_path)
            return

        with self.processing_lock:
            if self.shutting_down:
                return
            self._cleanup_processed_locked()
            if file_path in self.pending_files or file_path in self.processed_files:
                return

            timer = threading.Timer(delay, self._submit_file, args=[file_path])
            timer.daemon = True
            self.pending_files[file_path] = timer
            timer.start()

    def _submit_file(self, file_path):
        with self.processing_lock:
            if self.shutting_down or file_path not in self.pending_files:
                return
        try:
            self.file_executor.submit(self._process_file, file_path)
        except RuntimeError:
            logger.debug("文件处理队列已关闭: %s", file_path)

    def _process_file(self, file_path):
        with self.processing_lock:
            if self.shutting_down:
                return

            # 跳过已翻译的文件
            if self._is_translated_output(file_path):
                self.pending_files.pop(file_path, None)
                return

            if not os.path.exists(file_path):
                logger.warning(f"文件不存在: {file_path}")
                self.pending_files.pop(file_path, None)
                return

        success = self.translator.translate_subtitle_file(file_path)
        retry_delay = None

        with self.processing_lock:
            self.pending_files.pop(file_path, None)
            if success:
                self.processed_files[file_path] = time.time()
                self.retry_counts.pop(file_path, None)
            elif not self.shutting_down:
                attempt = self.retry_counts.get(file_path, 0) + 1
                self.retry_counts[file_path] = attempt
                if attempt <= self.max_retries:
                    retry_delay = 30 * (2 ** (attempt - 1))
                else:
                    state.increment_stat("total_failed")
                    state.add_log("ERROR", f"文件重试次数已耗尽: {file_path}")

        if retry_delay is not None:
            logger.warning(
                "将在 %s 秒后重试文件（%s/%s）: %s",
                retry_delay,
                self.retry_counts[file_path],
                self.max_retries,
                file_path,
            )
            self._schedule_file(file_path, delay=retry_delay)

    def _is_translated_output(self, file_path):
        """本服务的输出及其他目标语言字幕（如 .zh.srt、.zh.hi.srt）都不再翻译"""
        target_lang = getattr(self.translator, "target_lang", "zh")
        return is_target_language_file(file_path, target_lang)

    def _cleanup_processed_locked(self):
        now = time.time()
        expired = [k for k, v in self.processed_files.items() if now - v > self.max_age]
        for k in expired:
            del self.processed_files[k]


def main():
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    configure_logging(config["LOG_LEVEL"])

    api_key = config["API_KEY"]
    proxy_url = config["PROXY_URL"]
    model = config["MODEL"]

    watch_dirs = [
        config["MOVIES_DIR"],
        config["TV_DIR"],
    ]

    custom_dirs = config["CUSTOM_DIRS"]
    if custom_dirs:
        custom_dir_list = [d.strip() for d in custom_dirs.split(",") if d.strip()]
        watch_dirs.extend(custom_dir_list)
    watch_dirs = list(dict.fromkeys(watch_dirs))

    max_workers = int(config["MAX_WORKERS"])
    api_retry_count = int(config["API_RETRY_COUNT"])
    web_port = int(config["WEB_PORT"])
    target_lang = config["TARGET_LANG"]

    missing_config = [
        key for key in ("API_KEY", "PROXY_URL", "MODEL") if not config[key]
    ]
    if missing_config:
        message = f"未设置必需配置: {', '.join(missing_config)}"
        logger.error(message)
        state.add_log("ERROR", message)
        return 1

    subtitle_extensions = [".srt", ".ass", ".ssa", ".vtt"]

    translator = SubtitleTranslator(
        api_key,
        proxy_url,
        model,
        max_workers,
        target_lang,
        api_retry_count,
    )

    event_handler = SubtitleEventHandler(translator, subtitle_extensions)
    observer = Observer()
    shutdown_event = threading.Event()

    def request_shutdown(signum, _frame):
        logger.info("收到退出信号 %s，正在关闭服务...", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    scheduled_directories = 0
    for watch_dir in watch_dirs:
        if os.path.exists(watch_dir):
            observer.schedule(event_handler, watch_dir, recursive=True)
            scheduled_directories += 1
            logger.info(f"开始监控目录: {watch_dir}")
            state.add_log("INFO", f"开始监控目录: {watch_dir}")
        else:
            logger.warning(f"目录不存在，跳过监控: {watch_dir}")
            state.add_log("WARNING", f"目录不存在: {watch_dir}")

    if scheduled_directories == 0:
        logger.error("没有可用的监控目录，服务无法启动")
        event_handler.shutdown()
        translator.close()
        return 1

    observer.start()
    state.set_health_checker(observer.is_alive)

    # 启动 Web UI
    web_thread = threading.Thread(target=start_web_server, args=[web_port], daemon=True)
    web_thread.start()

    logger.info(f"字幕翻译服务已启动，监控 {len(watch_dirs)} 个目录")
    logger.info(f"Web UI 访问地址: http://localhost:{web_port}")
    state.add_log("INFO", f"服务启动成功，Web UI: http://localhost:{web_port}")

    try:
        while not shutdown_event.wait(1):
            if not observer.is_alive():
                raise RuntimeError("文件监控线程意外退出")
    finally:
        state.add_log("INFO", "服务正在关闭...")
        observer.stop()
        observer.join(timeout=10)
        event_handler.shutdown()
        translator.close()
        state.set_health_checker(None)

    return 0


if __name__ == "__main__":
    sys.exit(main())
