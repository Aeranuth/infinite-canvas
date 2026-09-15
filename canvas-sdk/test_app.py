import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app as adapter


class AdapterBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_multipart_limit_returns_413(self):
        async def body():
            yield (b'--boundary\r\nContent-Disposition: form-data; name="image[]"; '
                   b'filename="reference.png"\r\nContent-Type: image/png\r\n\r\n')
            yield b'x' * 2048
            yield b'\r\n--boundary--\r\n'

        # A small limit exercises the same streamed parser failure without a large upload.
        with patch.object(adapter, 'MAX_GENERATION_REQUEST_BYTES', 1024):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=adapter.app),
                base_url='http://localhost',
            ) as client:
                response = await client.post(
                    '/v1/videos', content=body(),
                    headers={'X-Canvas-SDK': '1', 'Content-Type': 'multipart/form-data; boundary=boundary'},
                )
        self.assertEqual(response.status_code, 413)

    async def test_download_cleanup_before_first_response_byte(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'download'
            directory.mkdir()
            video = directory / 'video.mp4'
            video.write_bytes(b'video fixture')
            response = adapter._CleanupFileResponse(video, directory, media_type='video/mp4')

            async def receive():
                return {'type': 'http.disconnect'}

            async def send(_):
                raise OSError('client disconnected')

            with self.assertRaises(OSError):
                await response(
                    {'type': 'http', 'method': 'GET', 'path': '/content', 'headers': [],
                     'asgi': {'version': '3.0', 'spec_version': '2.4'}},
                    receive, send,
                )
            self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
