import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,Mock,patch
import time

from telethon import errors
import account as module
from account import AccountAccess
import bot as app


class ErrorHandling(unittest.IsolatedAsyncioTestCase):
    def test_factory_preserves_original_rpc_without_automatic_retries(self):
        a=object.__new__(AccountAccess)
        a.path='unused-test-session';a.api_id=1;a.api_hash='a'*32
        with patch.object(module,'TelegramClient',Mock()) as client:
            a.make_client()
        self.assertTrue(client.call_args.kwargs['raise_last_call_error'])
        self.assertEqual(client.call_args.kwargs['request_retries'],0)
        self.assertEqual(client.call_args.kwargs['flood_sleep_threshold'],0)

    async def job(self,exception):
        account=NS(accept_all=AsyncMock(side_effect=exception))
        sender=NS(send_message=AsyncMock())
        jobs={-1:NS()};user_jobs={-1}
        with patch.object(app,'account',account),patch.object(app,'bot',sender),\
             patch.object(app,'require_join_admin',AsyncMock()),\
             patch.object(app,'bulk_tasks',jobs),patch.object(app,'user_jobs',user_jobs):
            await app.accept_old_requests(-1,123,'test',123)
        self.assertEqual(jobs,{})
        self.assertEqual(user_jobs,set())
        account.accept_all.assert_awaited_once()
        sender.send_message.assert_awaited_once()
        return sender.send_message.await_args.args[1]

    async def test_generic_telethon_value_error_not_unhandled(self):
        text=await self.job(ValueError('Request was unsuccessful 1 time(s)'))
        self.assertIn('ValueError',text)
        self.assertIn('Автоповтора нет',text)

    async def test_original_rpc_error_shows_safe_diagnostic(self):
        text=await self.job(errors.RpcCallFailError(request=None))
        self.assertIn('RpcCallFailError',text)
        self.assertIn('RPC 500',text)
        self.assertNotIn('request=',text)

    async def test_timeout_not_retried(self):
        import asyncio
        text=await self.job(asyncio.TimeoutError())
        self.assertIn('Автоповтора нет',text)

    async def test_count_failure_after_success_is_not_failed_approval(self):
        import asyncio
        a=object.__new__(AccountAccess)
        a.owner_id=123;a.lock=asyncio.Lock()
        a.confirmations={-1:('test',time.monotonic()+60)}
        a.peer=AsyncMock(return_value='peer')
        a.client=AsyncMock(return_value=NS())
        a.count=AsyncMock(side_effect=ValueError('Request was unsuccessful 1 time(s)'))
        result=await a.accept_all(-1,123,'test')
        self.assertIsNone(result)
        a.client.assert_awaited_once()
        self.assertNotIn(-1,a.confirmations)
