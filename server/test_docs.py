"""Checks that README.md still describes what the code actually does.

Documentation drifts silently: a preset gets renamed, an endpoint is added, a
default changes, and the README keeps confidently describing the old behaviour.
These tests fail when that happens, so the docs stay a description rather than
a wish. Everything here is offline and reads only the repository.

Run with:  python server/test_docs.py
"""

import importlib.util
import io
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = ROOT / "server" / "server.py"

SERVER_SPEC = importlib.util.spec_from_file_location("yt_dlp_server_docs", SERVER_PATH)
srv = importlib.util.module_from_spec(SERVER_SPEC)
SERVER_SPEC.loader.exec_module(srv)

README = io.open(ROOT / "README.md", encoding="utf-8").read()
SOURCE = io.open(SERVER_PATH, encoding="utf-8").read()
MANIFEST = json.loads(io.open(ROOT / "extension" / "manifest.json", encoding="utf-8").read())
BACKGROUND = io.open(ROOT / "extension" / "background.js", encoding="utf-8").read()


class ReadmeCase(unittest.TestCase):
    """assertIn against the README prints the entire file when it fails.

    That buries the one line that matters under 300 lines of prose, so state the
    claim instead of the haystack.
    """

    def assertReadme(self, needle, why=""):
        self.assertTrue(needle in README,
                        f"README does not mention {needle!r}{': ' + why if why else ''}")

    def assertNotReadme(self, needle, why=""):
        self.assertFalse(needle in README,
                         f"README still mentions {needle!r}{': ' + why if why else ''}")


class ApiTableTests(unittest.TestCase):
    def documented_paths(self):
        paths = set()
        for line in README.splitlines():
            if not line.startswith("| GET") and not line.startswith("| POST"):
                continue
            for match in re.findall(r"`(/[a-z_/<>?=]+)`", line):
                paths.add(match.split("?")[0].split("<")[0].rstrip("/"))
        return paths

    def test_every_documented_endpoint_is_routed(self):
        documented = self.documented_paths()
        self.assertTrue(documented, "no endpoints found in the README API table")
        # Never assert *against* SOURCE directly -- a failure would print the
        # entire server file. Compute the verdict, report just the paths.
        missing = [p for p in sorted(documented)
                   if f'"{p}"' not in SOURCE and f'"{p}/' not in SOURCE]
        self.assertEqual(missing, [], f"documented but not routed: {missing}")

    def test_every_route_is_documented(self):
        routed = set(re.findall(r'path (?:==|in \()\s*"(/[a-z_]+)"', SOURCE))
        routed |= set(re.findall(r'"(/[a-z_]+)"[,)]', SOURCE)) & {
            "/ping", "/health", "/formats", "/download", "/cancel", "/history",
            "/open_file", "/open_folder", "/open_downloads_folder", "/delete",
            "/clear_history", "/redownload", "/version", "/update",
        }
        documented = self.documented_paths()
        undocumented = sorted(routed - documented)
        self.assertEqual(undocumented, [], f"routed but undocumented: {undocumented}")


class PresetTableTests(ReadmeCase):
    def test_every_preset_label_appears_in_the_readme(self):
        for key, preset in srv.QUALITY_PRESETS.items():
            label = preset["label"]
            if key in ("2160", "1440", "1080", "720", "480"):
                continue  # covered by one collapsed table row
            self.assertIn(label, README, f"preset {key} ({label!r}) is undocumented")

    def test_capped_presets_are_mentioned(self):
        self.assertReadme("Up to 2160p / 1440p / 1080p / 720p / 480p")

    def test_readme_does_not_promise_a_preset_that_was_removed(self):
        for label in re.findall(r"^\| ([A-Z][^|]{3,40}?) \| ", README, re.M):
            label = label.strip()
            if not label.startswith(("Best ", "Audio only")):
                continue
            self.assertIn(label, [p["label"] for p in srv.QUALITY_PRESETS.values()],
                          f"README documents preset {label!r}, which no longer exists")


class ConfigurationTableTests(unittest.TestCase):
    def documented_vars(self):
        return set(re.findall(r"^\| `(YTDLP_[A-Z_]+)`", README, re.M))

    def read_vars(self):
        # Tolerates the call being wrapped across lines.
        return set(re.findall(r'os\.environ\.get\(\s*"(YTDLP_[A-Z_]+)"', SOURCE, re.S))

    def test_every_env_var_the_server_reads_is_documented(self):
        undocumented = sorted(self.read_vars() - self.documented_vars())
        self.assertEqual(undocumented, [], f"read but undocumented: {undocumented}")

    def test_every_documented_env_var_is_actually_read(self):
        unused = sorted(self.documented_vars() - self.read_vars())
        self.assertEqual(unused, [], f"documented but never read: {unused}")


class StatedDefaultsTests(ReadmeCase):
    def test_port(self):
        self.assertReadme("`4599`")
        self.assertEqual(srv.PORT, 4599)

    def test_concurrency(self):
        self.assertEqual(srv.MAX_CONCURRENT, 2)
        self.assertReadme("Simultaneous downloads")

    def test_history_cap(self):
        self.assertEqual(srv.HISTORY_LIMIT, 500)
        self.assertReadme("500 most recent")

    def test_formats_cache_ttl(self):
        self.assertEqual(srv.FORMATS_CACHE_TTL, 300)
        self.assertReadme("cached 5 min")

    def test_auto_update_interval(self):
        self.assertEqual(srv.AUTO_UPDATE_INTERVAL_SECONDS, 6 * 60 * 60)
        self.assertReadme("every 6 hours")

    def test_loopback_only(self):
        self.assertIn('Server(("127.0.0.1"', SOURCE)
        self.assertReadme("binds `127.0.0.1`")

    def test_download_directory(self):
        self.assertReadme("~/Downloads/yt-dlp-extension")


class DocumentedBehaviourTests(ReadmeCase):
    def test_readme_describes_the_quality_tagged_filenames(self):
        self.assertIn("%(format_id)s", srv.build_ydl_opts({"quality": "max"})["outtmpl"])
        self.assertReadme("File naming")

    def test_readme_states_ffmpeg_is_required_for_merges(self):
        self.assertReadme("ffmpeg is not optional")

    def test_readme_states_the_pot_provider_is_optional(self):
        self.assertReadme("not required")
        self.assertNotIn("player_client", srv.YOUTUBE_EXTRACTOR_ARGS.get("youtube", {}))

    def test_readme_explains_why_nothing_is_pinned(self):
        self.assertReadme("http_chunk_size")
        self.assertNotIn("http_chunk_size", srv.COMMON_OPTS)

    def test_readme_documents_the_encoder_preference(self):
        for encoder in srv.REQUIRED_ENCODERS:
            self.assertIn(encoder, README, f"{encoder} requirement is undocumented")


class ExtensionTests(unittest.TestCase):
    def test_manifest_port_matches_the_server(self):
        self.assertTrue(any(str(srv.PORT) in host for host in MANIFEST["host_permissions"]))

    def test_referenced_files_exist(self):
        referenced = [MANIFEST["action"]["default_popup"], *MANIFEST["icons"].values()]
        for name in referenced:
            self.assertTrue((ROOT / "extension" / name).exists(), name)

    def test_extension_pages_reference_existing_assets(self):
        for page in ("popup.html", "dashboard.html"):
            html = io.open(ROOT / "extension" / page, encoding="utf-8").read()
            for ref in re.findall(r'(?:href|src)="([^"]+)"', html):
                if ref.startswith("http"):
                    continue
                self.assertTrue((ROOT / "extension" / ref).exists(), f"{page} -> {ref}")

    def test_the_service_worker_is_declared_and_present(self):
        # Without it there is no auto-reload, no badge, and no context menu --
        # the extension quietly loses everything that works with nothing open.
        worker = MANIFEST.get("background", {}).get("service_worker")
        self.assertTrue(worker, "manifest declares no service worker")
        self.assertTrue((ROOT / "extension" / worker).exists(), worker)

    def test_the_worker_only_uses_apis_the_manifest_asks_for(self):
        needed = {
            "chrome.alarms": "alarms",
            "chrome.notifications": "notifications",
            "chrome.contextMenus": "contextMenus",
            "chrome.storage": "storage",
        }
        missing = [perm for api, perm in needed.items()
                   if api in BACKGROUND and perm not in MANIFEST["permissions"]]
        self.assertEqual(missing, [], f"used by background.js but not requested: {missing}")

    def test_the_worker_reloads_at_most_once_per_version(self):
        # The guard against reload-looping the browser when new files don't load.
        self.assertIn("reloadedFor", BACKGROUND)
        self.assertIn("chrome.runtime.reload", BACKGROUND)

    def test_the_worker_asks_the_endpoint_the_server_actually_serves(self):
        for path in re.findall(r'api\("(/[a-z_]+)"', BACKGROUND):
            self.assertIn(f'"{path}"', SOURCE, f"background.js calls unrouted {path}")

    def test_getcontexts_is_available_at_the_declared_minimum_chrome(self):
        # getContexts landed in Chrome 116; below that the reload guard would
        # throw and every update would close whatever page was open.
        if "getContexts" in BACKGROUND:
            self.assertGreaterEqual(int(MANIFEST["minimum_chrome_version"]), 116)

    def test_permissions_match_what_the_code_uses(self):
        with io.open(ROOT / "extension" / "popup.js", encoding="utf-8") as f:
            popup = f.read()
        if "chrome.storage" in popup:
            self.assertIn("storage", MANIFEST["permissions"])
        if "chrome.tabs.query" in popup:
            self.assertIn("activeTab", MANIFEST["permissions"])


class TestCountTests(ReadmeCase):
    """The README quotes test counts; they were wrong within one commit.

    A stated count is a promise about coverage, and a stale one quietly says
    "we test less than we do" -- or, worse, more.
    """

    @staticmethod
    def count(path):
        # Anchored to the start of a line, so the pattern this method quotes
        # does not count itself.
        with io.open(path, encoding="utf-8") as f:
            return len(re.findall(r"^    def test_", f.read(), re.M))

    def test_the_behaviour_test_count_is_right(self):
        self.assertReadme(f"**{self.count(ROOT / 'server' / 'test_server.py')} behaviour tests**")

    def test_the_documentation_test_count_is_right(self):
        self.assertReadme(f"**{self.count(Path(__file__))} documentation tests**")


if __name__ == "__main__":
    unittest.main(verbosity=2)
