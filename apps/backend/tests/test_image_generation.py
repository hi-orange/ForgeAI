from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from app.core.exceptions import BusinessException
from app.core.image_generation import generate_image
from app.core.settings import settings
from app.tools.images import generate_project_image

PNG = b"\x89PNG\r\n\x1a\n" + b"generated-image"
JPEG = b"\xff\xd8\xff" + b"generated-image"


class ImageGenerationTests(TestCase):
    def _client(self, *, payload: dict, image: bytes = PNG, content_type: str = "image/png"):
        post = MagicMock()
        post.raise_for_status.return_value = None
        post.json.return_value = payload
        download = MagicMock()
        download.raise_for_status.return_value = None
        download.content = image
        download.headers = {"content-type": content_type}
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.post.return_value = post
        client.get.return_value = download
        return client

    def test_calls_ark_seedream_with_requested_protocol_and_downloads_url(self):
        client = self._client(payload={"data": [{"url": "https://example.com/a.png"}]})
        with (
            patch.object(settings, "ark_api_key", "secret"),
            patch.object(settings, "ark_image_model", "doubao-seedream-5-0-pro-260628"),
            patch("app.core.image_generation.httpx.Client", return_value=client),
        ):
            result = generate_image("招聘网站的现代职业插画", size="2K", watermark=True)

        self.assertEqual(result.data, PNG)
        self.assertEqual(result.media_type, "image/png")
        body = client.post.call_args.kwargs["json"]
        self.assertEqual(body["model"], "doubao-seedream-5-0-pro-260628")
        self.assertEqual(body["response_format"], "url")
        self.assertEqual(body["size"], "2K")
        self.assertTrue(body["watermark"])

    def test_accepts_base64_response_and_writes_local_asset_with_actual_extension(self):
        client = self._client(payload={"data": [{"b64_json": base64.b64encode(JPEG).decode()}]})
        with (
            TemporaryDirectory() as directory,
            patch.object(settings, "ark_api_key", "secret"),
            patch("app.core.image_generation.httpx.Client", return_value=client),
        ):
            observed_paths: list[str] = []
            result = generate_project_image(
                Path(directory),
                path="frontend/public/assets/hero.png",
                prompt="招聘网站 hero 图",
                before_write=observed_paths.append,
            )
            actual = Path(directory) / "frontend/public/assets/hero.jpg"
            self.assertTrue(result.ok)
            self.assertEqual(result.data["path"], "frontend/public/assets/hero.jpg")
            self.assertEqual(observed_paths, ["frontend/public/assets/hero.jpg"])
            self.assertEqual(actual.read_bytes(), JPEG)

    def test_rejects_workspace_escape_and_disabled_provider(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(BusinessException, "frontend/public"):
                generate_project_image(Path(directory), path="../hero.png", prompt="hero")
        with patch.object(settings, "ark_api_key", None):
            with self.assertRaisesRegex(BusinessException, "ARK_API_KEY"):
                generate_image("hero")
