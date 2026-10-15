import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import ApproveChatJoinRequest

import bot as app
from core import Store, RuleError


class Handlers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'test.db')
        self.bot=NS(get_chat_member=AsyncMock(return_value=NS(status='member')),
                    approve_chat_join_request=AsyncMock(return_value=True))
        self.p1=patch.object(app,'store',self.store)
        self.p2=patch.object(app,'bot',self.bot)
        self.p1.start();self.p2.start()

    def tearDown(self):
        self.p1.stop();self.p2.stop();self.store.close();self.tmp.cleanup()

    async def test_creator_automatic(self):
        self.bot.get_chat_member.return_value=NS(status='creator')
        self.assertEqual(await app.rank(-1,123),5)
        self.assertEqual(self.store.rank(-1,123),0)

    async def test_telegram_admin_not_automatically_bot_admin(self):
        self.bot.get_chat_member.return_value=NS(status='administrator')
        self.assertEqual(await app.rank(-1,123),0)

    async def test_former_member_loses_authority(self):
        self.store.set_rank(-1,123,4)
        self.bot.get_chat_member.return_value=NS(status='left')
        self.assertEqual(await app.rank(-1,123),0)

    async def test_equal_rank_cannot_ban(self):
        self.store.set_rank(-1,123,3);self.store.set_rank(-1,456,3)
        with self.assertRaises(RuleError):
            await app.guard_target(-1,123,456,3)

    async def test_cannot_mute_actual_telegram_admin(self):
        self.store.set_rank(-1,123,4)
        self.bot.get_chat_member.side_effect=[NS(status='member'),NS(status='administrator')]
        with self.assertRaises(RuleError):
            await app.guard_target(-1,123,456,2)

    async def test_autojoin_off_saves_request(self):
        self.store.setting(-1,'autojoin',False)
        await app.approve(NS(chat=NS(id=-1),from_user=NS(id=456)))
        self.bot.approve_chat_join_request.assert_not_awaited()
        self.assertEqual(self.store.pending(-1),[456])

    async def test_autojoin_on_approves(self):
        await app.approve(NS(chat=NS(id=-1),from_user=NS(id=456)))
        self.bot.approve_chat_join_request.assert_awaited_once_with(-1,456)
        self.assertEqual(self.store.pending(-1),[])

    async def test_failed_request_kept(self):
        self.store.save_request(-1,456)
        self.bot.approve_chat_join_request.side_effect=TelegramBadRequest(
            method=ApproveChatJoinRequest(chat_id=-1,user_id=456),message='not available')
        self.bot.get_chat_member.return_value=NS(status='left')
        self.assertFalse(await app.approve_saved(-1,456))
        self.assertEqual(self.store.pending(-1),[456])

    async def test_already_member_removed_from_queue(self):
        self.store.save_request(-1,456)
        self.bot.approve_chat_join_request.side_effect=TelegramBadRequest(
            method=ApproveChatJoinRequest(chat_id=-1,user_id=456),message='already member')
        self.assertTrue(await app.approve_saved(-1,456))
        self.assertEqual(self.store.pending(-1),[])

    async def test_panel_has_buttons_and_no_false_history_claim(self):
        keys=app.panel_markup(-100123)
        self.assertEqual(len(keys.inline_keyboard),3)
        self.assertIn('старых заявок',app.panel_text(-100123))
        self.assertTrue(all(len(b.callback_data.encode())<=64 for row in keys.inline_keyboard for b in row))

    async def test_non_admin_button_rejected(self):
        q=NS(data='cg:all:-1',from_user=NS(id=123),answer=AsyncMock(),message=NS())
        await app.panel_click(q)
        q.answer.assert_awaited_once()
        self.assertTrue(q.answer.await_args.kwargs['show_alert'])
        self.bot.approve_chat_join_request.assert_not_awaited()

    async def test_bulk_accepts_all_saved_requests(self):
        self.store.save_request(-1,456);self.store.save_request(-1,789)
        self.bot.send_message=AsyncMock()
        with patch.object(app,'require_join_admin',AsyncMock()),patch.object(app.asyncio,'sleep',AsyncMock()):
            await app.bulk_accept(-1,123,123)
        self.assertEqual(self.store.pending(-1),[])
        self.assertEqual(self.bot.approve_chat_join_request.await_count,2)

    def media(self,text):
        values={key:None for key in ('caption','photo','video','animation','document','sticker','voice',
                 'audio','video_note','poll','contact','location','dice','sender_chat','forward_origin')}
        return NS(**values,chat=NS(id=-1,type='supergroup'),from_user=NS(id=456,is_bot=False),
                  text=text,entities=[],caption_entities=[],answer=AsyncMock())

    async def test_link_is_deleted(self):
        message=self.media('https://example.com')
        with patch.object(app,'safe_delete',AsyncMock(return_value=True)) as delete:
            await app.moderate(message)
            delete.assert_awaited_once_with(message)

    async def test_actual_admin_not_filtered(self):
        self.bot.get_chat_member.return_value=NS(status='administrator')
        with patch.object(app,'safe_delete',AsyncMock()) as delete:
            await app.moderate(self.media('https://example.com'))
            delete.assert_not_awaited()

    async def test_trigger_responds(self):
        self.store.trigger(-1,'привет','Здравствуйте!')
        message=self.media('Привет')
        with patch.object(app,'trigger_times',{}):
            await app.moderate(message)
        message.answer.assert_awaited_once_with('Здравствуйте!')

    async def test_spam_mutes_after_threshold(self):
        from core import SpamGuard
        with patch.object(app,'spam',SpamGuard()),patch.object(app,'safe_delete',AsyncMock(return_value=True)),patch.object(app,'mute',AsyncMock()) as mute:
            for _ in range(6):
                await app.moderate(self.media('текст'))
            mute.assert_awaited_once_with(-1,456,300)

    async def test_invite_admin_gets_panel_access_without_role_assignment(self):
        self.bot.get_chat_member.return_value=NS(status='administrator',can_invite_users=True)
        await app.require_join_admin(-1,123)
        self.assertEqual(self.store.rank(-1,123),0)
        with self.assertRaises(RuleError):
            await app.require(-1,123,4)

    async def test_admin_without_invite_permission_denied(self):
        self.bot.get_chat_member.return_value=NS(status='administrator',can_invite_users=False)
        with self.assertRaises(RuleError):
            await app.require_join_admin(-1,123)

    async def test_chat_creator_has_join_authority(self):
        self.bot.get_chat_member.return_value=NS(status='creator')
        await app.require_join_admin(-1,123)

    async def test_internal_role4_still_has_join_authority(self):
        self.store.set_rank(-1,123,4)
        self.bot.get_chat_member.return_value=NS(status='member')
        await app.require_join_admin(-1,123)

    async def test_former_role4_cannot_use_panel(self):
        self.store.set_rank(-1,123,4)
        self.bot.get_chat_member.return_value=NS(status='left')
        with self.assertRaises(RuleError):
            await app.require_join_admin(-1,123)

    async def test_private_panel_for_real_invite_admin(self):
        self.bot.get_chat_member.return_value=NS(status='administrator',can_invite_users=True)
        message=NS(text='/panel -1003744604840',from_user=NS(id=123),sender_chat=None,
                   chat=NS(id=123,type='private'),answer=AsyncMock())
        with patch.object(app,'command_times',{}):
            await app.commands(message)
        message.answer.assert_awaited_once()
        self.assertIn('-1003744604840',message.answer.await_args.args[0])
        self.assertIn('reply_markup',message.answer.await_args.kwargs)
        self.assertEqual(self.store.rank(-1003744604840,123),0)

    async def test_invite_admin_can_switch_autojoin_not_other_settings(self):
        self.bot.get_chat_member.return_value=NS(status='administrator',can_invite_users=True)
        message=NS(text='/autojoin -1003744604840 off',from_user=NS(id=123),sender_chat=None,
                   chat=NS(id=123,type='private'),answer=AsyncMock())
        with patch.object(app,'command_times',{}):
            await app.commands(message)
        self.assertFalse(self.store.config(-1003744604840)['autojoin'])
        with self.assertRaises(RuleError):
            await app.require(-1003744604840,123,4)
