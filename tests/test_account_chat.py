import asyncio
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

from telethon import errors
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage
from account_chat import AccountChatLogin
from core import RuleError
import bot as app


class ChatLogin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        def owner(actor):
            if actor!=123:raise RuleError('owner only')
        self.account=NS(owner_id=123,owner=owner,lock=asyncio.Lock(),confirmations={},
                        connected=AsyncMock(),verified=AsyncMock(),reset_client=AsyncMock(),client=AsyncMock())
        self.account.client.is_user_authorized.return_value=False
        self.account.client.send_code_request.return_value=NS(phone_code_hash='test-code-hash')
        self.account.client.get_me.return_value=NS(id=123,bot=False)
        self.ui=AccountChatLogin(self.account)

    def message(self,text,owner=123,private=True):
        return NS(text=text,from_user=NS(id=owner),chat=NS(type='private' if private else 'supergroup'),
                  sender_chat=None,answer=AsyncMock(),delete=AsyncMock())

    async def start_code(self):
        await self.ui.begin(self.message('/account_login'))
        phone=self.message('+79990000000')
        await self.ui.receive(phone)
        phone.delete.assert_awaited_once()
        self.assertEqual(self.ui.stage,'code')

    async def test_phone_code_login(self):
        await self.start_code()
        code=self.message('0.1.2.3.4')
        await self.ui.receive(code)
        code.delete.assert_awaited_once()
        self.account.client.sign_in.assert_awaited_once_with(
            phone='+79990000000',code='01234',phone_code_hash='test-code-hash')
        self.assertIsNone(self.ui.stage)
        self.assertIsNone(self.ui.phone)
        self.assertIsNone(self.ui.code_hash)

    async def test_two_factor_password_unchanged(self):
        await self.start_code()
        self.account.client.sign_in.side_effect=[errors.SessionPasswordNeededError(request=None),None]
        await self.ui.receive(self.message('12345'))
        self.assertEqual(self.ui.stage,'password')
        password=self.message(' /secret.with.dots and spaces ')
        await self.ui.receive(password)
        password.delete.assert_awaited_once()
        self.assertEqual(self.account.client.sign_in.await_args.kwargs,{'password':' /secret.with.dots and spaces '})
        self.assertIsNone(self.ui.stage)

    async def test_other_user_ignored(self):
        await self.start_code()
        message=self.message('12345',owner=999)
        self.assertFalse(self.ui.expects(message))
        await self.ui.receive(message)
        message.delete.assert_not_awaited()
        self.account.client.sign_in.assert_not_awaited()
        with self.assertRaises(RuleError):await self.ui.begin(self.message('/account_login',owner=999))

    async def test_no_group_login(self):
        with self.assertRaises(RuleError):await self.ui.begin(self.message('/account_login',private=False))
        await self.start_code()
        self.assertFalse(self.ui.expects(self.message('12345',private=False)))

    async def test_three_failures_clear(self):
        await self.start_code()
        self.account.client.sign_in.side_effect=errors.PhoneCodeInvalidError(request=None)
        for _ in range(3):await self.ui.receive(self.message('12345'))
        self.assertIsNone(self.ui.stage)
        self.assertIsNone(self.ui.phone)

    async def test_cancel(self):
        await self.start_code()
        await self.ui.cancel(self.message('/account_cancel'))
        self.assertIsNone(self.ui.stage)
        self.account.client.sign_in.assert_not_awaited()

    async def test_timeout(self):
        await self.start_code()
        self.ui.deadline=time.monotonic()-1
        message=self.message('12345')
        await self.ui.receive(message)
        message.delete.assert_awaited_once()
        self.account.client.sign_in.assert_not_awaited()
        self.assertIsNone(self.ui.stage)

    async def test_wrong_account_revoked(self):
        await self.start_code()
        self.account.client.get_me.return_value=NS(id=999,bot=False)
        await self.ui.receive(self.message('12345'))
        self.account.client.log_out.assert_awaited_once()
        self.account.reset_client.assert_awaited_once()
        self.assertIsNone(self.ui.stage)

    async def test_expired_code_clears(self):
        await self.start_code()
        self.account.client.sign_in.side_effect=errors.PhoneCodeExpiredError(request=None)
        await self.ui.receive(self.message('12345'))
        self.assertIsNone(self.ui.stage)

    async def test_flood_wait_clears(self):
        await self.start_code()
        self.account.client.sign_in.side_effect=errors.FloodWaitError(request=None,capture=60)
        await self.ui.receive(self.message('12345'))
        self.assertIsNone(self.ui.stage)

    async def test_failed_delete_does_not_echo_secret(self):
        await self.start_code()
        message=self.message('12345')
        message.delete.side_effect=TelegramBadRequest(method=DeleteMessage(chat_id=123,message_id=1),message='denied')
        await self.ui.receive(message)
        self.account.client.sign_in.assert_awaited_once()
        self.assertTrue(all('12345' not in c.args[0] for c in message.answer.await_args_list))

    async def test_control_commands_not_consumed_as_secrets(self):
        await self.start_code()
        for command in AccountChatLogin.CONTROL:
            self.assertFalse(self.ui.expects(self.message(command)))

    async def test_password_command_intercepted_before_moderation(self):
        await self.start_code()
        self.ui.stage='password'
        message=self.message('/ban')
        with patch.object(app,'account_login_ui',self.ui):
            self.assertTrue(await app.AccountInputFilter()(message))
        await self.ui.receive(message)
        self.account.client.sign_in.assert_awaited_once_with(password='/ban')
