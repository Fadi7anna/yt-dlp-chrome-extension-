"""Unit tests for the yt-dlp helper server.

Everything here is offline: no network, no downloads. The cases are the bugs
that actually bit, so a regression shows up as a red test rather than a failed
download an hour in.

Run with:  python -m unittest discover -s server
"""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


SERVER_PATH = Path(__file__).with_name("server.py")
SERVER_SPEC = importlib.util.spec_from_file_location("yt_dlp_server", SERVER_PATH)
srv = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(srv)


class ExtractorConfigTests(unittest.TestCase):
    def test_no_player_client_is_pinned(self):
        # Every pin tried here aged badly -- "mweb" needed a PO token and
        # degraded to 360p without one; "default,android_vr" served URLs that
        # 403 past ~20 MB. Plain `yt-dlp URL` pins nothing and works, so match
        # it and let each yt-dlp release choose.
        self.assertNotIn("player_client", srv.YOUTUBE_EXTRACTOR_ARGS.get("youtube", {}))
        self.assertIn("youtubepot-bgutilhttp", srv.YOUTUBE_EXTRACTOR_ARGS)

    def test_downloads_are_not_split_into_ranged_chunks(self):
        # http_chunk_size makes yt-dlp fetch the file as separate ranged
        # requests; googlevideo 403s those past the first chunk or two, so
        # downloads died at ~10-20 MB. The CLI does not set it. Neither do we.
        self.assertNotIn("http_chunk_size", srv.COMMON_OPTS)

    def test_remote_js_component_only_requested_without_a_local_engine(self):
        # Fetching the EJS solver from GitHub goes through Cloudflare, which
        # blocks it unless curl_cffi is installed; the failed fetch is retried
        # before extraction can proceed, delaying every download for nothing.
        if srv.JS_RUNTIME:
            self.assertNotIn("remote_components", srv.COMMON_OPTS)
        else:
            self.assertEqual(srv.COMMON_OPTS["remote_components"], ["ejs:github"])

    def test_local_js_runtime_detection(self):
        runtime = srv.local_js_runtime()
        self.assertIn(runtime, ("deno", "bun", "node", None))

    def test_unusable_script_provider_is_pointed_at_nothing(self):
        # An installed-but-unbuilt provider checkout makes yt-dlp spawn deno and
        # wait out a 15s probe; the TimeoutExpired then escapes and kills the
        # whole extraction. Omitting the arg is not enough -- the plugin falls
        # back to a baked-in default path -- so it must be aimed somewhere empty.
        configured = srv.YOUTUBE_EXTRACTOR_ARGS["youtubepot-bgutilscript"]["server_home"][0]
        if not srv.bgutil_script_usable(srv.YOUTUBE_POT_SERVER_HOME):
            self.assertFalse(
                os.path.isfile(os.path.join(configured, "src", "generate_once.ts")))

    def test_script_provider_needs_installed_dependencies(self):
        self.assertFalse(srv.bgutil_script_usable(None))
        self.assertFalse(srv.bgutil_script_usable(tempfile.mkdtemp()))

    def test_pot_provider_uses_ipv4_loopback_by_default(self):
        # [::1] is not reachable on every Windows box; 127.0.0.1 always is.
        if "YTDLP_POT_PROVIDER_URL" not in os.environ:
            self.assertEqual(srv.YOUTUBE_POT_PROVIDER_URL, "http://127.0.0.1:4416")

    def test_progress_strings_are_not_colourised(self):
        # ANSI escapes in _percent_str used to reach the dashboard, where
        # parseFloat() choked on them.
        self.assertEqual(srv.COMMON_OPTS["color"], "no_color")

    def test_common_options_download_fragments_concurrently(self):
        self.assertEqual(srv.COMMON_OPTS["concurrent_fragment_downloads"], 8)


class FfmpegDiscoveryTests(unittest.TestCase):
    def test_ffmpeg_location_is_passed_explicitly_when_found(self):
        # The original bug: the server trusted PATH inheritance, so a server
        # started without ffmpeg on PATH failed every merged download.
        if srv.FFMPEG_DIR:
            self.assertEqual(srv.COMMON_OPTS["ffmpeg_location"], srv.FFMPEG_DIR)
            self.assertTrue(os.path.isdir(srv.FFMPEG_DIR))
        else:
            self.assertNotIn("ffmpeg_location", srv.COMMON_OPTS)

    def test_candidate_search_yields_directories_only(self):
        for candidate in list(srv._ffmpeg_candidates())[:12]:
            if candidate:
                self.assertFalse(candidate.lower().endswith(".exe"), candidate)


class FormatSelectionTests(unittest.TestCase):
    def test_max_preset_prefers_bitrate_after_resolution(self):
        # Without an explicit sort, yt-dlp picks a smaller AV1 stream at the
        # same resolution -- fine, but not what "best quality" promises.
        preset = srv.QUALITY_PRESETS["max"]
        self.assertEqual(preset["format"], "bv*+ba/b")
        self.assertEqual(preset["format_sort"][:2], ["res", "fps"])
        self.assertIn("br", preset["format_sort"])

    def test_efficient_preset_defers_to_ytdlp_codec_preference(self):
        self.assertIsNone(srv.QUALITY_PRESETS["efficient"]["format_sort"])

    def test_container_preference_allows_a_fallback(self):
        # Forcing "mp4" makes VP9+Opus (YouTube's top streams) unmergeable, and
        # left the recorded filepath pointing at a file that never existed.
        opts = srv.build_ydl_opts({"quality": "max"})
        self.assertEqual(opts["merge_output_format"], "mp4/mkv")

    def test_hand_picked_video_only_format_gets_audio_merged_in(self):
        opts = srv.build_ydl_opts({"format_id": "271", "needs_audio": True})
        self.assertEqual(opts["format"], "271+bestaudio/271")

    def test_hand_picked_complete_format_is_left_alone(self):
        opts = srv.build_ydl_opts({"format_id": "18", "needs_audio": False})
        self.assertEqual(opts["format"], "18")

    def test_unknown_preset_falls_back_to_the_default(self):
        opts = srv.build_ydl_opts({"quality": "nonsense"})
        self.assertEqual(opts["format"], srv.QUALITY_PRESETS[srv.DEFAULT_QUALITY]["format"])

    def test_mp3_preset_adds_an_audio_extraction_step(self):
        opts = srv.build_ydl_opts({"quality": "mp3"})
        keys = [pp["key"] for pp in opts["postprocessors"]]
        self.assertIn("FFmpegExtractAudio", keys)

    def test_thumbnail_embedding_is_opt_in(self):
        # A post-processor failure fails the whole download, so the extras stay
        # off unless asked for.
        default = srv.build_ydl_opts({"quality": "max"})
        self.assertNotIn("EmbedThumbnail", [pp["key"] for pp in default["postprocessors"]])
        opted_in = srv.build_ydl_opts({"quality": "max", "embed_thumbnail": True})
        self.assertIn("EmbedThumbnail", [pp["key"] for pp in opted_in["postprocessors"]])

    def test_merge_detection(self):
        self.assertTrue(srv.requires_merge({"format": "bv*+ba/b"}))
        self.assertFalse(srv.requires_merge({"format": "251"}))


class ResumeAfterFailureTests(unittest.TestCase):
    def test_resumes_when_the_attempt_transferred_something(self):
        self.assertTrue(srv.should_resume_after_failure(True))

    def test_starts_over_when_nothing_transferred(self):
        # A stale .part makes the resume Range request 403 instantly, so a retry
        # that resumes too would fail the same way forever.
        self.assertFalse(srv.should_resume_after_failure(False))


class OutputNameTests(unittest.TestCase):
    """Different qualities of one video must not collide on one filename."""

    def test_video_downloads_are_tagged_with_their_height(self):
        tmpl = srv.build_ydl_opts({"quality": "max"})["outtmpl"]
        self.assertIn("%(height)sp", tmpl)

    def test_audio_downloads_are_tagged_as_audio(self):
        for quality in ("audio", "mp3"):
            tmpl = srv.build_ydl_opts({"quality": quality})["outtmpl"]
            self.assertIn("audio ", tmpl, quality)
            self.assertNotIn("%(height)sp", tmpl, quality)

    def test_qualities_of_one_video_get_distinct_names(self):
        # yt-dlp will not overwrite an existing output, so a shared name made
        # "download at 1080p" silently return the 720p file already on disk.
        self.assertNotEqual(srv.build_ydl_opts({"quality": "max"})["outtmpl"],
                            srv.build_ydl_opts({"quality": "mp3"})["outtmpl"])

    def test_same_height_different_codec_still_gets_a_distinct_name(self):
        # "max" and "efficient" are the same resolution in different codecs, so
        # the height alone cannot separate them -- the format id must be there.
        for quality in ("max", "efficient", "1080", "audio"):
            self.assertIn("%(format_id)s", srv.build_ydl_opts({"quality": quality})["outtmpl"],
                          quality)

    def test_redownload_overwrites_but_a_normal_download_does_not(self):
        self.assertTrue(srv.build_ydl_opts({"quality": "max", "overwrite": True})["overwrites"])
        self.assertNotIn("overwrites", srv.build_ydl_opts({"quality": "max"}))


class FfmpegCapabilityTests(unittest.TestCase):
    def test_mp3_encoder_is_required_of_the_chosen_build(self):
        # conda-forge ffmpeg ships without libmp3lame, so the MP3 preset died
        # after downloading everything, with "Encoder not found".
        self.assertIn("libmp3lame", srv.REQUIRED_ENCODERS)

    def test_chosen_build_can_encode_mp3_when_one_is_available(self):
        if not srv.FFMPEG_DIR:
            self.skipTest("no ffmpeg installed")
        exe = os.path.join(srv.FFMPEG_DIR, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        probed = srv._probe_ffmpeg(exe)
        self.assertIsNotNone(probed)
        _version, encoders = probed
        self.assertIn("libmp3lame", encoders)


class ErrorMessageTests(unittest.TestCase):
    def test_missing_ffmpeg_gets_an_actionable_message(self):
        raw = ("ERROR: You have requested merging of multiple formats but ffmpeg "
               "is not installed. Aborting due to --abort-on-error")
        message = srv.clean_error(raw)
        self.assertIn("ffmpeg", message)
        self.assertIn("winget install ffmpeg", message)
        self.assertNotIn("--abort-on-error", message)

    def test_ansi_escapes_are_stripped(self):
        self.assertEqual(srv.clean_error("\x1b[0;31mERROR:\x1b[0m boom"), "boom")

    def test_unrecognised_errors_pass_through_trimmed(self):
        self.assertEqual(srv.clean_error("ERROR: something odd"), "something odd")

    def test_empty_error_still_says_something(self):
        self.assertTrue(srv.clean_error(""))


class OriginGuardTests(unittest.TestCase):
    def test_extension_origins_are_allowed(self):
        self.assertTrue(srv.origin_allowed("chrome-extension://abcdef"))
        self.assertTrue(srv.origin_allowed("moz-extension://abcdef"))

    def test_non_browser_clients_are_allowed(self):
        self.assertTrue(srv.origin_allowed(None))

    def test_web_pages_cannot_drive_the_server(self):
        # Any page you visit could otherwise POST /delete to 127.0.0.1:4599.
        self.assertFalse(srv.origin_allowed("https://example.com"))
        self.assertFalse(srv.origin_allowed("http://localhost:3000"))


class PathSafetyTests(unittest.TestCase):
    def test_files_in_the_download_dir_are_accepted(self):
        self.assertTrue(srv.inside_download_dir(os.path.join(srv.DOWNLOAD_DIR, "a.mkv")))

    def test_paths_outside_the_download_dir_are_rejected(self):
        self.assertFalse(srv.inside_download_dir(os.path.join(srv.DOWNLOAD_DIR, "..", "a.mkv")))
        if os.name == "nt":
            self.assertFalse(srv.inside_download_dir(r"C:\Windows\System32\config\SAM"))
        else:
            self.assertFalse(srv.inside_download_dir("/etc/passwd"))


class FormatDescriptionTests(unittest.TestCase):
    def test_video_only_stream_is_flagged_as_needing_audio(self):
        described = srv.describe_format({
            "format_id": "271", "ext": "webm", "vcodec": "vp9",
            "acodec": "none", "height": 1216, "fps": 30, "tbr": 3355,
        })
        self.assertEqual(described["kind"], "video only")
        self.assertTrue(described["needs_audio"])
        self.assertEqual(described["resolution"], "1216p")

    def test_complete_stream_needs_no_extra_audio(self):
        described = srv.describe_format({
            "format_id": "18", "ext": "mp4", "vcodec": "avc1.42001E",
            "acodec": "mp4a.40.2", "height": 360, "fps": 30,
        })
        self.assertEqual(described["kind"], "video+audio")
        self.assertFalse(described["needs_audio"])

    def test_high_frame_rate_is_shown(self):
        described = srv.describe_format({
            "format_id": "303", "ext": "webm", "vcodec": "vp9",
            "acodec": "none", "height": 1080, "fps": 60,
        })
        self.assertEqual(described["resolution"], "1080p60")

    def test_audio_only_stream(self):
        described = srv.describe_format({
            "format_id": "251", "ext": "webm", "vcodec": "none",
            "acodec": "opus", "abr": 140,
        })
        self.assertEqual(described["kind"], "audio only")
        self.assertFalse(described["needs_audio"])


class HumanSizeTests(unittest.TestCase):
    def test_scales_units(self):
        self.assertEqual(srv.human_size(512), "512.0B")
        self.assertEqual(srv.human_size(1536), "1.5KB")
        self.assertEqual(srv.human_size(1024 ** 3), "1.0GB")

    def test_zero_and_none_are_not_reported_as_a_size(self):
        self.assertIsNone(srv.human_size(0))
        self.assertIsNone(srv.human_size(None))


class ThumbnailPickingTests(unittest.TestCase):
    def test_explicit_thumbnail_wins(self):
        self.assertEqual(srv.pick_thumbnail({"thumbnail": "a.jpg"}), "a.jpg")

    def test_largest_thumbnail_is_chosen_from_the_list(self):
        # Unprocessed info dicts have no single "thumbnail" key.
        info = {"thumbnails": [
            {"url": "small.jpg", "width": 120, "height": 90},
            {"url": "big.jpg", "width": 1920, "height": 1080},
        ]}
        self.assertEqual(srv.pick_thumbnail(info), "big.jpg")

    def test_missing_thumbnails_are_tolerated(self):
        self.assertIsNone(srv.pick_thumbnail({}))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self._history = srv.HISTORY[:]
        self._file = srv.HISTORY_FILE
        self._tmp = tempfile.mkdtemp()
        srv.HISTORY_FILE = os.path.join(self._tmp, "history.json")

    def tearDown(self):
        srv.HISTORY[:] = self._history
        srv.HISTORY_FILE = self._file

    def test_history_is_trimmed_to_the_limit(self):
        srv.HISTORY[:] = [{"id": str(i)} for i in range(srv.HISTORY_LIMIT + 25)]
        with srv.HISTORY_LOCK:
            srv.save_history()
        self.assertEqual(len(srv.HISTORY), srv.HISTORY_LIMIT)
        # The newest records survive, the oldest are dropped.
        self.assertEqual(srv.HISTORY[-1]["id"], str(srv.HISTORY_LIMIT + 24))

    def test_find_record_tolerates_malformed_rows(self):
        srv.HISTORY[:] = [{}, {"id": "keep"}]
        with srv.HISTORY_LOCK:
            self.assertIsNotNone(srv.find_record("keep"))
            self.assertIsNone(srv.find_record("missing"))
            self.assertIsNone(srv.find_record(None))

    def test_update_record_can_drop_fields(self):
        srv.HISTORY[:] = [{"id": "x", "percent": "50%", "status": "downloading"}]
        srv.update_record("x", status="done", percent=srv._DROP)
        record = srv.HISTORY[0]
        self.assertEqual(record["status"], "done")
        self.assertNotIn("percent", record)

    def test_interrupted_downloads_are_marked_failed_on_load(self):
        import json
        with open(srv.HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump([{"id": "a", "status": "downloading", "percent": "42%"}], f)
        srv.load_history()
        self.assertEqual(srv.HISTORY[0]["status"], "error")
        self.assertIn("Interrupted", srv.HISTORY[0]["error"])
        self.assertNotIn("percent", srv.HISTORY[0])


class OutputPathTests(unittest.TestCase):
    class _FakeYdl:
        def __init__(self, guess):
            self._guess = guess

        def prepare_filename(self, info):
            return self._guess

    def test_real_path_comes_from_requested_downloads(self):
        # Reconstructing the path by swapping in the merge extension broke
        # whenever the container fell back to mkv.
        info = {"requested_downloads": [{"filepath": "C:/dl/video.mkv"}]}
        path = srv.resolve_output_path(self._FakeYdl("C:/dl/video.mp4"), info)
        self.assertEqual(path, "C:/dl/video.mkv")

    def test_falls_back_to_the_top_level_filepath(self):
        info = {"filepath": "C:/dl/video.mkv"}
        self.assertEqual(
            srv.resolve_output_path(self._FakeYdl("C:/dl/video.mp4"), info),
            "C:/dl/video.mkv")


class PartialCleanupTests(unittest.TestCase):
    """Cancelling must remove only the cancelled job's own temp files."""

    def setUp(self):
        self._dir = srv.DOWNLOAD_DIR
        self._tmp = tempfile.mkdtemp()
        srv.DOWNLOAD_DIR = self._tmp

    def tearDown(self):
        srv.DOWNLOAD_DIR = self._dir

    def _touch(self, name):
        path = os.path.join(self._tmp, name)
        with open(path, "wb") as f:
            f.write(b"x")
        return path

    def test_removes_the_reported_temp_files(self):
        mine = self._touch("mine.f271.webm.part")
        srv.cleanup_partials({mine})
        self.assertFalse(os.path.exists(mine))

    def test_removes_part_and_ytdl_siblings(self):
        base = os.path.join(self._tmp, "mine.f271.webm")
        part = self._touch("mine.f271.webm.part")
        ytdl = self._touch("mine.f271.webm.ytdl")
        srv.cleanup_partials({base})
        self.assertFalse(os.path.exists(part))
        self.assertFalse(os.path.exists(ytdl))

    def test_leaves_other_downloads_alone(self):
        # The earlier implementation scanned for *.part and deleted by mtime,
        # which wiped whatever a concurrent download was writing.
        mine = self._touch("mine.f271.webm.part")
        theirs = self._touch("theirs.f400.mp4.part")
        srv.cleanup_partials({mine})
        self.assertFalse(os.path.exists(mine))
        self.assertTrue(os.path.exists(theirs))

    def test_refuses_paths_outside_the_download_dir(self):
        outside = tempfile.mkdtemp()
        victim = os.path.join(outside, "important.txt")
        with open(victim, "wb") as f:
            f.write(b"keep me")
        srv.cleanup_partials({victim})
        self.assertTrue(os.path.exists(victim))

    def test_empty_and_none_are_no_ops(self):
        srv.cleanup_partials(None)
        srv.cleanup_partials(set())


class PresetCatalogueTests(unittest.TestCase):
    def test_every_preset_has_a_label_and_format(self):
        for key, preset in srv.QUALITY_PRESETS.items():
            self.assertTrue(preset.get("label"), key)
            self.assertTrue(preset.get("format"), key)

    def test_default_preset_exists(self):
        self.assertIn(srv.DEFAULT_QUALITY, srv.QUALITY_PRESETS)

    def test_audio_presets_select_audio_only(self):
        for key in ("audio", "mp3"):
            self.assertEqual(srv.QUALITY_PRESETS[key]["format"], "ba/b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
