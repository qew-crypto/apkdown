import asyncio
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch

from telethon.tl import functions
import bot as app
from account import AccountAccess,code_digits
from core import RuleError


class AccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account=object.__new__(AccountAccess)
        a=self.account
        a.owner_id=123;a.lock=asyncio.Lock();a.confirmations={};a.disabled=False
        a.client=AsyncMock()
        a.client.is_connected=Mock(return_value=True)
        a.client.is_user_authorized.return_value=True
        a.client.get_me.return_value=NS(id=123,bot=False)
        a.client.get_input_entity.return_value='peer'
        a.client.get_permissions.return_value=NS(is_creator=False,is_admin=True,invite_users=True)
        a.client.return_value=NS(count=5)

    def test_code_format_local(self):
        self.assertEqual(code_digits('1.2.3.4.5'),'12345')
        self.assertEqual(code_digits('01234'),'01234')
        for code in ('12..34','12.34','1.2.3','１２３４'):
            with self.assertRaises(RuleError):code_digits(code)

    async def test_prepare_uses_server_requested_list(self):
        count,nonce=await self.account.prepare(-1001,123)
        self.assertEqual(count,5)
        self.assertEqual(len(nonce),8)
        request=self.account.client.await_args.args[0]
        self.assertIsInstance(request,functions.messages.GetChatInviteImportersRequest)
        self.assertTrue(request.requested)
        self.assertEqual(self.account.confirmations[-1001][0],nonce)

    async def test_non_owner_no_network(self):
        with self.assertRaises(RuleError):await self.account.prepare(-1001,999)
        with self.assertRaises(RuleError):await self.account.accept_all(-1001,999,'abcd')
        self.account.client.assert_not_awaited()

    async def test_session_id_must_match(self):
        self.account.client.get_me.return_value=NS(id=999,bot=False)
        with self.assertRaises(RuleError):await self.account.prepare(-1001,123)
        self.account.client.assert_not_awaited()

    async def test_account_must_have_invite_permission(self):
        self.account.client.get_permissions.return_value=NS(is_creator=False,is_admin=True,invite_users=False)
        with self.assertRaises(RuleError):await self.account.prepare(-1001,123)
        self.account.client.assert_not_awaited()

    async def test_confirmation_expires(self):
        self.account.confirmations[-1001]=('abcd',time.monotonic()-1)
        with self.assertRaises(RuleError):await self.account.accept_all(-1001,123,'abcd')
        self.account.client.assert_not_awaited()

    async def test_bad_nonce_does_not_consume_valid_confirmation(self):
        self.account.confirmations[-1001]=('abcd',time.monotonic()+60)
        with self.assertRaises(RuleError):await self.account.accept_all(-1001,123,'other')
        self.assertIn(-1001,self.account.confirmations)
        self.account.client.assert_not_awaited()

    async def test_explicit_approval_and_nonce_single_use(self):
        self.account.confirmations[-1001]=('abcd',time.monotonic()+60)
        self.account.client.side_effect=[NS(),NS(count=0)]
        remaining=await self.account.accept_all(-1001,123,'abcd')
        self.assertEqual(remaining,0)
        request=self.account.client.await_args_list[0].args[0]
        self.assertIsInstance(request,functions.messages.HideAllChatJoinRequestsRequest)
        self.assertIs(request.approved,True)
        self.assertEqual(request.peer,'peer')
        with self.assertRaises(RuleError):await self.account.accept_all(-1001,123,'abcd')
        self.assertEqual(self.account.client.await_count,2)

    async def test_permissions_rechecked_on_confirmation(self):
        _,nonce=await self.account.prepare(-1001,123)
        self.account.client.get_permissions.return_value=NS(is_creator=False,is_admin=False,invite_users=False)
        with self.assertRaises(RuleError):await self.account.accept_all(-1001,123,nonce)
        self.assertEqual(self.account.client.await_count,1)  # only earlier count, no approval

    async def test_no_authorized_session(self):
        self.account.client.is_user_authorized.return_value=False
        with self.assertRaises(RuleError):await self.account.prepare(-1001,123)

    async def test_logged_out_session_not_reused(self):
        self.account.client.log_out.return_value=True
        await self.account.logout(123)
        self.assertTrue(self.account.disabled)
        with self.assertRaises(RuleError):await self.account.verified()

    async def test_other_moderator_cannot_use_account_button(self):
        q=NS(from_user=NS(id=999),data='ua:approve:-1001:abcd',answer=AsyncMock(),
             message=NS(chat=NS(type='private')))
        with patch.object(app,'account',self.account):
            await app.account_click(q)
        self.account.client.assert_not_awaited()
        self.assertTrue(q.answer.await_args.kwargs['show_alert'])

    async def test_confirmation_not_allowed_in_group(self):
        q=NS(from_user=NS(id=123),data='ua:approve:-1001:abcd',answer=AsyncMock(),
             message=NS(chat=NS(type='supergroup')))
        with patch.object(app,'account',self.account):
            await app.account_click(q)
        self.account.client.assert_not_awaited()
        self.assertTrue(q.answer.await_args.kwargs['show_alert'])

    def test_optional_panel_button(self):
        with patch.object(app,'account',self.account),patch.object(app,'store',NS(config=lambda chat:{'autojoin':True})):
            markup=app.panel_markup(-1001)
        self.assertEqual(len(markup.inline_keyboard),4)
        self.assertEqual(markup.inline_keyboard[-1][0].callback_data,'cg:old:-1001')
