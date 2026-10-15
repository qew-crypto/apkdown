"""Optional local user session, used only for owner-approved join-request operations."""
import asyncio
import os
import re
import secrets
import time
from datetime import datetime, timezone

from telethon import TelegramClient, errors
from telethon.tl import functions, types
from core import RuleError


def code_digits(text):
    value=text.strip()
    if not re.fullmatch(r'(?:[0-9]{4,8}|[0-9](?:\.[0-9]){3,7})',value):
        raise RuleError('Код: 4–8 цифр, например 12345 или 1.2.3.4.5.')
    return value.replace('.','')


class AccountAccess:
    def __init__(self,base,api_id,api_hash,owner_id):
        self.owner_id=owner_id
        directory=base/'sessions'
        directory.mkdir(mode=0o700,exist_ok=True)
        self.api_id,self.api_hash=api_id,api_hash
        self.path=str(directory/'join_account')
        self.client=self.make_client()
        self.lock=asyncio.Lock()
        self.confirmations={}
        self.disabled=False

    def make_client(self):
        return TelegramClient(self.path,self.api_id,self.api_hash,
            flood_sleep_threshold=0,receive_updates=False,request_retries=0,
            raise_last_call_error=True)

    async def reset_client(self):
        await self.client.disconnect()
        self.client=self.make_client()
        self.disabled=False

    @classmethod
    def from_env(cls,base):
        values=[os.getenv(k,'').strip() for k in ('API_ID','API_HASH','ACCOUNT_OWNER_ID')]
        if not any(values):
            return None
        try:
            api_id,owner_id=int(values[0]),int(values[2])
            if api_id<=0 or owner_id<=0 or not re.fullmatch(r'[a-fA-F0-9]{32}',values[1]):
                raise ValueError()
        except ValueError:
            raise SystemExit('Заполните API_ID, API_HASH, ACCOUNT_OWNER_ID или оставьте все три пустыми.')
        return cls(base,api_id,values[1],owner_id)

    def owner(self,actor):
        if actor!=self.owner_id:
            raise RuleError('Личную сессию может использовать только ACCOUNT_OWNER_ID.')

    async def connected(self):
        if self.disabled:
            raise RuleError('Сессия отозвана. Выполните /account_login в личке.')
        if not self.client.is_connected():
            await self.client.connect()

    async def verified(self):
        await self.connected()
        if not await self.client.is_user_authorized():
            raise RuleError('Аккаунт не подключён. Выполните /account_login в личке бота.')
        me=await self.client.get_me()
        if not me or me.bot or me.id!=self.owner_id:
            raise RuleError('ID личной сессии не совпадает с ACCOUNT_OWNER_ID. Операция запрещена.')
        return me

    async def peer(self,chat):
        await self.verified()
        try:
            peer=await self.client.get_input_entity(chat)
        except ValueError:
            peer=None
            async for dialog in self.client.iter_dialogs():
                if dialog.id==chat:
                    peer=dialog.input_entity
                    break
            if peer is None:
                raise RuleError('Аккаунт не видит чат. Добавьте его администратором.')
        rights=await self.client.get_permissions(peer,self.owner_id)
        if not (rights.is_creator or (rights.is_admin and rights.invite_users)):
            raise RuleError('Личному аккаунту нужны права администратора «Приглашение пользователей».')
        return peer

    async def count(self,peer):
        result=await self.client(functions.messages.GetChatInviteImportersRequest(
            peer=peer,requested=True,offset_date=datetime(1970,1,1,tzinfo=timezone.utc),
            offset_user=types.InputUserEmpty(),limit=1))
        return result.count

    async def prepare(self,chat,actor):
        self.owner(actor)
        async with self.lock:
            peer=await self.peer(chat)
            count=await self.count(peer)
            if not count:
                raise RuleError('По данным аккаунта ожидающих заявок сейчас нет.')
            nonce=secrets.token_hex(4)
            now=time.monotonic()
            self.confirmations={c:v for c,v in self.confirmations.items() if v[1]>now}
            self.confirmations[chat]=(nonce,now+60)
            return count,nonce

    async def accept_all(self,chat,actor,nonce):
        self.owner(actor)
        async with self.lock:
            expected=self.confirmations.get(chat)
            if not expected or not secrets.compare_digest(expected[0],nonce) or time.monotonic()>expected[1]:
                raise RuleError('Подтверждение истекло или уже использовано. Откройте панель заново.')
            del self.confirmations[chat]
            # Revalidate the account ID and live chat permissions at confirmation time.
            peer=await self.peer(chat)
            # Explicit approved=True is critical: omitted flag means rejection.
            await self.client(functions.messages.HideAllChatJoinRequestsRequest(peer=peer,approved=True))
            try:
                return await self.count(peer)
            except (errors.RPCError,OSError,asyncio.TimeoutError,ValueError):
                return None  # Approval succeeded, but the following count is unavailable.

    async def logout(self,actor):
        self.owner(actor)
        async with self.lock:
            await self.verified()
            self.confirmations.clear()
            result=await self.client.log_out()
            self.disabled=True
            if not result:
                raise RuleError('Telegram не подтвердил отзыв. Завершите сессию вручную в разделе «Устройства».')
            await self.reset_client()

    async def close(self):
        self.confirmations.clear()
        await self.client.disconnect()
