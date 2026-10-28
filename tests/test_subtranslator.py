import tempfile
import unittest
from json import dumps, loads
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace
from unittest.mock import Mock, patch

from subtranslator import (
    SubtitleEventHandler,
    SubtitleTranslator,
    is_target_language_file,
    state,
)

SRT_CONTENT = "1\n00:00:01,000 --> 00:00:02,000\nHello\n"
TWO_CUE_SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\nFirst\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\nSecond\n"
)


def chat_response(content, status_code=200):
    response = Mock(status_code=status_code, headers={})
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


def batch_items(payload):
    return loads(payload["messages"][1]["content"])


class SubtitleTranslatorTests(unittest.TestCase):
    def setUp(self):
        self.translator = SubtitleTranslator(
            api_key="test-key",
            proxy_url="https://api.example.test/v1/chat/completions",
            model="user-model",
            max_workers=2,
            target_lang="zh",
        )

    def tearDown(self):
        self.translator.close()

    def test_segment_failure_falls_back_to_original_text(self):
        initial_log_count = len(state.translation_content_logs)
        self.translator.translate_text = Mock(
            side_effect=RuntimeError("simulated API failure")
        )
        result = self.translator.translate_subtitle_content(
            SRT_CONTENT,
            file_path="example.srt",
        )
        self.assertEqual(result.failed_indices, (0,))
        self.assertEqual(result.success_rate, 0)
        self.assertIn("Hello", result.content)
        self.assertNotIn("PLACEHOLDER", result.content)
        self.assertEqual(len(state.translation_content_logs), initial_log_count)

    def test_api_failure_retries_only_the_failed_request(self):
        failed_response = Mock(status_code=400, headers={})
        failed_response.json.return_value = {
            "error": {"code": "bad_input", "message": "invalid subtitle"}
        }
        successful_response = Mock(status_code=200)
        successful_response.json.return_value = {
            "choices": [{"message": {"content": "你好"}}]
        }
        session = Mock()
        session.post.side_effect = [failed_response, successful_response]
        self.translator._get_session = Mock(return_value=session)

        with patch("subtranslator.time.sleep"):
            translated = self.translator.translate_text("Hello", retry_count=1)

        self.assertEqual(translated, "你好")
        self.assertEqual(session.post.call_count, 2)

    def test_cues_are_translated_in_one_batch_request(self):
        def post(_url, json, timeout):
            self.assertEqual(timeout, SubtitleTranslator.BATCH_TIMEOUT)
            items = batch_items(json)
            self.assertEqual([item["text"] for item in items], ["First", "Second"])
            reply = [
                {"id": item["id"], "text": f"译文 {item['text']}"} for item in items
            ]
            return chat_response(
                "```json\n" + dumps(reply, ensure_ascii=False) + "\n```"
            )

        session = Mock()
        session.post.side_effect = post
        self.translator._get_session = Mock(return_value=session)

        result = self.translator.translate_subtitle_content(TWO_CUE_SRT)

        self.assertEqual(session.post.call_count, 1)
        self.assertEqual(result.failed_indices, ())
        self.assertIn("译文 First\nFirst", result.content)
        self.assertIn("译文 Second\nSecond", result.content)

    def test_only_cues_missing_from_batch_reply_are_resent(self):
        requests_sent = []

        def post(_url, json, timeout):
            user_content = json["messages"][1]["content"]
            requests_sent.append(user_content)
            if user_content.startswith("["):
                return chat_response('[{"id": 0, "text": "译文 First"}]')
            return chat_response("译文 Second")

        session = Mock()
        session.post.side_effect = post
        self.translator._get_session = Mock(return_value=session)

        result = self.translator.translate_subtitle_content(TWO_CUE_SRT)

        self.assertEqual(len(requests_sent), 2)
        self.assertTrue(requests_sent[1].endswith("\nSecond"))
        self.assertEqual(result.failed_indices, ())
        self.assertIn("译文 Second\nSecond", result.content)

    def test_unparseable_batch_reply_falls_back_to_single_requests(self):
        def post(_url, json, timeout):
            user_content = json["messages"][1]["content"]
            if user_content.startswith("["):
                return chat_response("抱歉，我无法按格式输出")
            return chat_response(f"译文 {user_content.rsplit(chr(10), 1)[-1]}")

        session = Mock()
        session.post.side_effect = post
        self.translator._get_session = Mock(return_value=session)
        self.translator.api_retry_count = 1

        with patch("subtranslator.time.sleep"):
            result = self.translator.translate_subtitle_content(TWO_CUE_SRT)

        # 批量请求重试 1 次后，改为 2 次逐条请求
        self.assertEqual(session.post.call_count, 4)
        self.assertEqual(result.failed_indices, ())
        self.assertIn("译文 First\nFirst", result.content)

    def test_batch_api_failure_marks_whole_batch_failed(self):
        failed_response = Mock(status_code=500, headers={}, text="down")
        failed_response.json.side_effect = ValueError
        session = Mock()
        session.post.return_value = failed_response
        self.translator._get_session = Mock(return_value=session)
        self.translator.api_retry_count = 0

        result = self.translator.translate_subtitle_content(TWO_CUE_SRT)

        self.assertEqual(result.failed_indices, (0, 1))
        self.assertEqual(session.post.call_count, 1)
        self.assertIn("First", result.content)

    def test_progress_and_content_are_reported_per_batch(self):
        self.translator.batch_size = 1
        self.translator.translate_batch = Mock(
            side_effect=lambda texts: [f"译文 {text}" for text in texts]
        )
        progress = []
        content_counts = []

        def on_progress(completed, total):
            progress.append((completed, total))
            content_counts.append(
                sum(
                    log["file_path"] == "progress.srt"
                    for log in state.translation_content_logs
                )
            )

        self.translator.translate_subtitle_content(
            TWO_CUE_SRT, file_path="progress.srt", on_progress=on_progress
        )

        self.assertEqual(progress, [(0, 2), (1, 2), (2, 2)])
        # 每批完成时它的译文已经可见，而不是等整个文件结束
        self.assertEqual(content_counts, [0, 1, 2])

    def test_cues_without_text_are_not_sent(self):
        self.translator.translate_batch = Mock(return_value=["译文"])
        content = (
            "1\n00:00:01,000 --> 00:00:02,000\n♪♪\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\nHello\n"
        )

        result = self.translator.translate_subtitle_content(content)

        self.translator.translate_batch.assert_called_once_with(["Hello"])
        self.assertIn("00:00:02,000\n♪♪\n", result.content)

    def test_webvtt_cues_are_translated(self):
        self.translator.translate_batch = Mock(
            side_effect=lambda texts: [f"译文 {text}" for text in texts]
        )
        content = (
            "WEBVTT\n\n"
            "00:01.000 --> 00:02.000\nHello\n\n"
            "intro\n00:00:03.000 --> 00:00:04.000 line:0\nWorld\n"
        )

        result = self.translator.translate_subtitle_content(content)

        self.assertEqual(result.total_segments, 2)
        self.assertTrue(result.content.startswith("WEBVTT\n"))
        self.assertIn("00:01.000 --> 00:02.000\n译文 Hello\nHello", result.content)
        self.assertIn("line:0\n译文 World\nWorld", result.content)

    def test_ass_dialogue_text_is_translated_and_styles_kept(self):
        self.translator.translate_batch = Mock(
            side_effect=lambda texts: [f"译文{idx}" for idx in range(len(texts))]
        )
        content = (
            "[Script Info]\nTitle: Example\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
            "Effect, Text\n"
            r"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\an8}Hello,\Nworld"
            "\n"
            "Comment: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Note\n"
            r"Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\p1}m 0 0 l 10 0"
            "\n"
            r"Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,Bye {\i1}now{\i0}"
            "\n"
        )

        result = self.translator.translate_subtitle_content(content)

        self.translator.translate_batch.assert_called_once_with(
            ["Hello,\nworld", "Bye now"]
        )
        lines = result.content.split("\n")
        self.assertIn(
            r"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,"
            r"{\an8}译文0\NHello,\Nworld",
            lines,
        )
        self.assertIn(
            r"Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,"
            r"译文1\NBye {\i1}now{\i0}",
            lines,
        )
        self.assertIn("Comment: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Note", lines)
        self.assertIn(
            r"Dialogue: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,{\p1}m 0 0 l 10 0",
            lines,
        )

    def test_bilingual_ass_is_detected(self):
        dialogue = r"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,你好\NHello"
        content = "[Events]\n" + "\n".join([dialogue] * 4)

        self.assertTrue(self.translator._is_already_translated(content))

    def test_source_named_with_target_language_is_not_overwritten(self):
        translator = SubtitleTranslator(
            api_key="test-key",
            proxy_url="https://api.example.test/v1/chat/completions",
            model="user-model",
            max_workers=1,
            target_lang="ja",
        )
        translator.translate_subtitle_content = Mock()
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                source_path = Path(temporary_directory) / "example.ja.srt"
                source_path.write_text(SRT_CONTENT, encoding="utf-8")

                self.assertTrue(translator.translate_subtitle_file(str(source_path)))
                translator.translate_subtitle_content.assert_not_called()
                self.assertEqual(
                    source_path.read_text(encoding="utf-8"), SRT_CONTENT
                )
        finally:
            translator.close()

    def test_target_language_subtitle_does_not_overwrite_existing_output(self):
        self.translator.translate_batch = Mock()
        self.translator._wait_for_file_stable = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            existing = directory / "Movie.zh.srt"
            existing.write_text("bilingual", encoding="utf-8")
            source = directory / "Movie.zh.hi.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n[音乐] 你好\n", encoding="utf-8"
            )

            self.assertTrue(self.translator.translate_subtitle_file(str(source)))

            self.translator.translate_batch.assert_not_called()
            self.assertEqual(existing.read_text(encoding="utf-8"), "bilingual")

    def test_untagged_subtitle_already_in_target_language_is_not_written(self):
        self.translator.translate_batch = Mock()
        self.translator._wait_for_file_stable = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "Movie.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n你好\n", encoding="utf-8"
            )

            self.assertTrue(self.translator.translate_subtitle_file(str(source)))

            self.translator.translate_batch.assert_not_called()
            self.assertFalse((Path(temporary_directory) / "Movie.zh.srt").exists())

    def test_output_path_strips_all_trailing_tags(self):
        cases = {
            "/m/Movie.2024.en.srt": "/m/Movie.2024.zh.srt",
            "/m/Movie.en.hi.srt": "/m/Movie.zh.srt",
            "/m/Movie.eng.sdh.forced.ass": "/m/Movie.zh.ass",
            "/m/Movie.srt": "/m/Movie.zh.srt",
            "/m/Show.S01E01.vtt": "/m/Show.S01E01.zh.vtt",
        }
        for source, expected in cases.items():
            self.assertEqual(self.translator._generate_output_path(source), expected)

    def test_needs_translation_follows_target_language(self):
        self.translator.target_lang = "ja"
        self.assertFalse(self.translator._needs_translation("こんにちは"))
        self.assertTrue(self.translator._needs_translation("Hello"))
        self.translator.target_lang = "fr"
        self.assertTrue(self.translator._needs_translation("Hello"))

    def test_ninety_nine_percent_translation_is_saved_as_partial_success(self):
        segments = []
        for index in range(100):
            segments.append(
                f"{index + 1}\n00:00:{index % 60:02d},000 --> "
                f"00:00:{index % 60:02d},500\nLine {index + 1}"
            )
        content = "\n\n".join(segments) + "\n"

        def translate(text):
            if text == "Line 100":
                raise RuntimeError("simulated API failure")
            return f"译文 {text}"

        self.translator.translate_text = Mock(side_effect=translate)
        self.translator._wait_for_file_stable = Mock(return_value=True)
        # 每批一条，单条失败只影响自己
        self.translator.batch_size = 1

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "example.en.srt"
            source_path.write_text(content, encoding="utf-8")

            self.assertTrue(self.translator.translate_subtitle_file(str(source_path)))
            output_path = Path(temporary_directory) / "example.zh.srt"
            output = output_path.read_text(encoding="utf-8")
            self.assertIn("译文 Line 99\nLine 99", output)
            self.assertIn("Line 100", output)
            self.assertNotIn("PLACEHOLDER", output)

    def test_failed_file_is_not_written_or_reported_as_success(self):
        self.translator.translate_text = Mock(
            side_effect=RuntimeError("simulated API failure")
        )
        self.translator._wait_for_file_stable = Mock(return_value=True)

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / "example.en.srt"
            source_path.write_text(SRT_CONTENT, encoding="utf-8")

            self.assertFalse(self.translator.translate_subtitle_file(str(source_path)))
            self.assertFalse((Path(temporary_directory) / "example.zh.srt").exists())

    def test_supported_input_encodings_are_read(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = {
                "utf8-bom.srt": ("字幕", "utf-8-sig"),
                "utf16.srt": ("字幕", "utf-16"),
                "gb18030.srt": ("字幕", "gb18030"),
            }
            for filename, (content, encoding) in samples.items():
                path = directory / filename
                path.write_text(content, encoding=encoding)
                self.assertEqual(
                    self.translator._read_subtitle(path),
                    content,
                )

    def test_atomic_write_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "output.srt"
            output_path.write_text("old", encoding="utf-8")

            self.translator._atomic_write(output_path, "new")

            self.assertEqual(output_path.read_text(encoding="utf-8"), "new")
            self.assertEqual(S_IMODE(output_path.stat().st_mode), 0o644)
            self.assertEqual(list(output_path.parent.glob("*.tmp")), [])


class TargetLanguageFileTests(unittest.TestCase):
    def test_any_target_language_tag_is_detected(self):
        for name in (
            "Movie.zh.srt",
            "Movie.zh.hi.srt",
            "Movie.ZH.SDH.srt",
            "Movie.chs.srt",
            "Movie.cht.forced.ass",
            "Movie.zh-CN.srt",
            "Movie.hi.zh.srt",
        ):
            self.assertTrue(is_target_language_file(f"/m/{name}", "zh"), name)

    def test_other_languages_and_title_words_are_not_detected(self):
        for name in (
            "Movie.en.srt",
            "Movie.en.hi.srt",
            "Movie.srt",
            "zh.srt",
            "Movie.zh.2024.srt",
        ):
            self.assertFalse(is_target_language_file(f"/m/{name}", "zh"), name)


class SubtitleEventHandlerTests(unittest.TestCase):
    def test_moved_file_is_scheduled(self):
        translator = SimpleNamespace(max_workers=1)
        handler = SubtitleEventHandler(translator, [".srt"])
        handler._schedule_file = Mock()
        try:
            event = SimpleNamespace(
                is_directory=False,
                dest_path="/media/example.srt",
            )
            handler.on_moved(event)
            handler._schedule_file.assert_called_once_with("/media/example.srt")
        finally:
            handler.shutdown()

    def test_output_of_configured_target_language_is_skipped(self):
        handler = SubtitleEventHandler(
            SimpleNamespace(max_workers=1, target_lang="ja"), [".srt"]
        )
        try:
            self.assertTrue(handler._is_translated_output("/media/movie.ja.srt"))
            self.assertTrue(handler._is_translated_output("/media/movie.jpn.hi.srt"))
            self.assertFalse(handler._is_translated_output("/media/movie.zh.srt"))
            self.assertFalse(handler._is_translated_output("/media/movie.en.srt"))
        finally:
            handler.shutdown()

    def test_failed_file_is_retried_before_counting_final_failure(self):
        translator = SimpleNamespace(
            max_workers=1,
            translate_subtitle_file=Mock(return_value=False),
        )
        handler = SubtitleEventHandler(translator, [".srt"])
        handler._schedule_file = Mock()
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                source_path = str(Path(temporary_directory) / "example.srt")
                Path(source_path).write_text(SRT_CONTENT, encoding="utf-8")
                handler.pending_files[source_path] = Mock()
                initial_failed_count = state.stats["total_failed"]

                handler._process_file(source_path)

                handler._schedule_file.assert_called_once_with(
                    source_path,
                    delay=30,
                )
                self.assertNotIn(source_path, handler.processed_files)
                self.assertEqual(
                    state.stats["total_failed"],
                    initial_failed_count,
                )
        finally:
            handler.shutdown()

    def test_exhausted_retry_counts_one_final_failure(self):
        translator = SimpleNamespace(
            max_workers=1,
            translate_subtitle_file=Mock(return_value=False),
        )
        handler = SubtitleEventHandler(translator, [".srt"])
        handler.max_retries = 0
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                source_path = str(Path(temporary_directory) / "example.srt")
                Path(source_path).write_text(SRT_CONTENT, encoding="utf-8")
                handler.pending_files[source_path] = Mock()
                initial_failed_count = state.stats["total_failed"]

                handler._process_file(source_path)

                self.assertEqual(
                    state.stats["total_failed"],
                    initial_failed_count + 1,
                )
        finally:
            handler.shutdown()


if __name__ == "__main__":
    unittest.main()
