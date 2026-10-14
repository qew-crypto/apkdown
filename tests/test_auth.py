import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from telethon import errors
from auth import BotLogin


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(
            is_user_authorized=AsyncMock(return_value=False),
            is_connected=Mock(return_value=True),
            connect=AsyncMock(), log_out=AsyncMock(),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash='test-hash')),
            sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(id=123, bot=False)))
        self.login = BotLogin(self.user, 123)

    def event(self, text, owner=123):
        return SimpleNamespace(sender_id=owner, is_private=True, raw_text=text,
                               delete=AsyncMock(), respond=AsyncMock())

    async def begin(self):
        await self.login.handle(self.event('/login'))
        number = self.event('+79990000000')
        await self.login.handle(number)
        number.delete.assert_awaited_once()
        self.assertEqual(self.login.stage, 'code')

    async def test_login(self):
        await self.begin()
        code = self.event('12345')
        await self.login.handle(code)
        code.delete.assert_awaited_once()
        self.assertIsNone(self.login.stage)
        self.user.sign_in.assert_awaited_once_with(phone='+79990000000', code='12345', phone_code_hash='test-hash')

    async def test_two_factor(self):
        await self.begin()
        self.user.sign_in.side_effect = [errors.SessionPasswordNeededError(request=None), None]
        await self.login.handle(self.event('12345'))
        self.assertEqual(self.login.stage, 'password')
        password = self.event('  /pass with spaces  ')
        await self.login.handle(password)
        password.delete.assert_awaited_once()
        self.assertEqual(self.user.sign_in.await_args.kwargs['password'], '  /pass with spaces  ')
        self.assertIsNone(self.login.stage)

    async def test_other_user_ignored(self):
        event = self.event('/login', owner=999)
        self.assertFalse(await self.login.handle(event))
        event.respond.assert_not_awaited()
        self.assertIsNone(self.login.stage)

    async def test_timeout(self):
        await self.begin()
        self.login.deadline = time.monotonic() - 1
        await self.login.handle(self.event('12345'))
        self.user.sign_in.assert_not_awaited()
        self.assertIsNone(self.login.stage)

    async def test_invalid_attempt_limit(self):
        await self.begin()
        self.user.sign_in.side_effect = errors.PhoneCodeInvalidError(request=None)
        for _ in range(3):
            await self.login.handle(self.event('12345'))
        self.assertIsNone(self.login.stage)

    async def test_wrong_account_revoked(self):
        await self.begin()
        self.user.get_me.return_value.id = 999
        await self.login.handle(self.event('12345'))
        self.user.log_out.assert_awaited_once()
        self.assertIsNone(self.login.stage)

    async def test_cancel(self):
        await self.begin()
        await self.login.handle(self.event('/cancel'))
        self.assertIsNone(self.login.stage)
        self.assertIsNone(self.login.phone)
        self.assertIsNone(self.login.code_hash)
