import io
import unittest
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from transcripts import export_ticket


class TranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_and_attachment_saved_in_zip(self):
        attachment = SimpleNamespace(id=3, filename='proof.txt', size=5)
        async def save(path):
            path.write_bytes(b'proof')
        attachment.save = save
        message = SimpleNamespace(id=4, author=SimpleNamespace(id=5), created_at=datetime.now(timezone.utc),
            edited_at=None, reference=None, content='<script>alert(1)</script>', embeds=[], attachments=[attachment], stickers=[])
        async def history(**kwargs):
            yield message
        channel = SimpleNamespace(id=1, name='test', guild=SimpleNamespace(filesize_limit=100000), history=history)
        async def send(*args, **kwargs):
            data = kwargs['file'].fp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                self.assertEqual(archive.read('attachments/3-proof.txt'), b'proof')
                content = archive.read('transcript.html').decode('utf-8')
                self.assertIn('&lt;script&gt;', content)
                self.assertNotIn('<script>', content)
            return SimpleNamespace(id=9)
        destination = SimpleNamespace(send=AsyncMock(side_effect=send))
        result = await export_ticket(channel, destination, 5, SimpleNamespace(id=6))
        self.assertEqual(result.id, 9)

    async def test_oversized_archive_is_not_uploaded(self):
        async def history(**kwargs):
            if False:
                yield None
        channel = SimpleNamespace(id=1, name='test', guild=SimpleNamespace(filesize_limit=1), history=history)
        destination = SimpleNamespace(send=AsyncMock())
        with self.assertRaises(ValueError):
            await export_ticket(channel, destination, 5, SimpleNamespace(id=6))
        destination.send.assert_not_awaited()
