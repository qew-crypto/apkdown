import hashlib
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from bot import Progress, TransferError, upload_file
from common import safe_name


class Names(unittest.TestCase):
    def test_path_safety(self):
        self.assertEqual(safe_name('../../private.apk'), 'private.apk')
        self.assertEqual(safe_name('C:\\temp\\demo.apk'), 'demo.apk')
        self.assertEqual(safe_name('..'), 'file.bin')
        self.assertNotIn('\n', safe_name('a\nb.apk'))

    def test_unicode_length(self):
        result = safe_name('я' * 300 + '.apk')
        self.assertLessEqual(len(result.encode()), 180)
        self.assertTrue(result.endswith('.apk'))

    def test_progress(self):
        p = Progress(total=1024)
        p.done = 512
        text = p.text('test.apk')
        self.assertIn('50.0%', text)
        self.assertIn('Скорость:', text)
        p.reset('Загрузка на Filebin', 4096)
        self.assertEqual(p.done, 0)
        self.assertEqual(p.total, 4096)


class Upload(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'тест файл.apk'
        self.data = b'apk-test\0' * 100000
        self.path.write_bytes(self.data)
        self.status = 201
        self.bad_hash = False
        self.bad_size = False
        app = web.Application()
        app.router.add_post('/{bin}/{filename}', self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        self.url = f'http://127.0.0.1:{self.site._server.sockets[0].getsockname()[1]}'

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self.temp.cleanup()

    async def handler(self, request):
        body = await request.read()
        self.assertEqual(body, self.data)
        self.assertEqual(int(request.headers['Content-Length']), len(self.data))
        self.assertEqual(request.match_info['filename'], self.path.name)
        if self.status != 201:
            return web.Response(status=self.status)
        return web.json_response({'file': {
            'bytes': len(body) + int(self.bad_size),
            'sha256': 'bad' if self.bad_hash else hashlib.sha256(body).hexdigest()},
            'bin': {'expired_at': '2026-10-21T00:00:00Z'}}, status=201)

    async def test_success(self):
        p = Progress()
        page, direct, expires = await upload_file(self.path, p, self.url)
        self.assertTrue(direct.startswith(page + '/'))
        self.assertIn('%20', direct)
        self.assertEqual(p.done, len(self.data))
        self.assertEqual(expires, '2026-10-21T00:00:00Z')

    async def test_blocked_file(self):
        self.status = 403
        with self.assertRaisesRegex(TransferError, '403'):
            await upload_file(self.path, Progress(), self.url)

    async def test_bad_hash(self):
        self.bad_hash = True
        with self.assertRaisesRegex(TransferError, 'сумма'):
            await upload_file(self.path, Progress(), self.url)

    async def test_bad_size(self):
        self.bad_size = True
        with self.assertRaisesRegex(TransferError, 'Размер'):
            await upload_file(self.path, Progress(), self.url)
