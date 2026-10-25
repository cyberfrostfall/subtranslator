import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from struct import unpack_from
from unittest.mock import patch

import web_ui


class IconLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and attributes.get("rel") in ("icon", "apple-touch-icon"):
            self.links.append(attributes)


class WebUiTests(unittest.TestCase):
    def setUp(self):
        self.client = web_ui.app.test_client()
        web_ui.state.set_health_checker(None)

    def tearDown(self):
        web_ui.state.set_health_checker(None)

    def test_health_reflects_monitor_state(self):
        self.assertEqual(self.client.get("/health").status_code, 503)
        web_ui.state.set_health_checker(lambda: True)
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_management_apis_do_not_require_admin_token(self):
        self.assertEqual(self.client.get("/api/logs").status_code, 200)

    def test_configuration_is_saved_and_secret_is_not_rendered(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "settings.json"
            environment = {
                "CONFIG_FILE": str(config_path),
                "API_KEY": "environment-key",
            }
            with patch.dict(os.environ, environment, clear=False):
                response = self.client.post(
                    "/api/config",
                    json={"MODEL": "saved-model"},
                )
                self.assertEqual(response.status_code, 200)

                page = self.client.get("/")
                self.assertIn(b"static/dashboard.js", page.data)
                self.assertIn(b"static/style.css", page.data)

                dashboard_script = self.client.get("/static/dashboard.js")
                try:
                    self.assertEqual(dashboard_script.status_code, 200)
                    self.assertIn(b"escapeHtml", dashboard_script.data)
                finally:
                    dashboard_script.close()

    def test_dashboard_has_modern_accessible_controls(self):
        page = self.client.get("/")

        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="theme-toggle"', page.data)
        self.assertIn(b'rel="icon"', page.data)
        self.assertIn(b'role="dialog"', page.data)
        self.assertIn(b'aria-live="polite"', page.data)
        self.assertIn(b'<input type="text" name="MODEL"', page.data)
        self.assertIn(b'name="API_RETRY_COUNT"', page.data)
        self.assertIn(b'<select name="TARGET_LANG"', page.data)
        self.assertIn(b'name="WEB_PORT"', page.data)
        self.assertNotIn(b'<select name="MODEL"', page.data)
        self.assertNotIn(b'class="glass"', page.data)

    def test_favicon_serves_real_multisize_ico(self):
        with self.client.get("/favicon.ico") as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn(response.mimetype, ("image/vnd.microsoft.icon", "image/x-icon"))
            reserved, image_type, count = unpack_from("<HHH", response.data)
            self.assertEqual((reserved, image_type), (0, 1))
            sizes = set()
            for index in range(count):
                offset = 6 + 16 * index
                width, height = unpack_from("BB", response.data, offset)
                size, position = unpack_from("<II", response.data, offset + 8)
                self.assertGreater(size, 0)
                self.assertLessEqual(position + size, len(response.data))
                sizes.add((width, height))
            self.assertTrue({(16, 16), (32, 32), (48, 48)} <= sizes)

    def test_page_provides_working_raster_icons_for_safari(self):
        parser = IconLinks()
        parser.feed(self.client.get("/").get_data(as_text=True))
        png_sizes = set()
        for link in parser.links:
            with self.client.get(link["href"]) as response:
                self.assertEqual(response.status_code, 200, link["href"])
                if response.mimetype == "image/png":
                    self.assertEqual(response.data[:8], b"\x89PNG\r\n\x1a\n")
                    dimensions = unpack_from(">II", response.data, 16)
                    png_sizes.add((link["rel"], dimensions))
        self.assertIn(("icon", (32, 32)), png_sizes)
        self.assertIn(("apple-touch-icon", (180, 180)), png_sizes)

    def test_dashboard_returns_data_only_when_version_changes(self):
        first = self.client.get("/api/dashboard").get_json()
        self.assertTrue(first["changed"])
        self.assertIn("translation_content_logs", first)

        unchanged = self.client.get(f"/api/dashboard?since={first['version']}")
        self.assertEqual(
            unchanged.get_json(), {"version": first["version"], "changed": False}
        )

        web_ui.state.add_log("INFO", "new entry")
        changed = self.client.get(
            f"/api/dashboard?since={first['version']}"
        ).get_json()
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["logs"][-1]["message"], "new entry")
        self.assertEqual(changed["logs"][-1]["id"], changed["version"])

    def test_cors_is_not_open_by_default(self):
        response = self.client.get(
            "/api/stats",
            headers={"Origin": "https://attacker.example"},
        )
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)


if __name__ == "__main__":
    unittest.main()
