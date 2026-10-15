"""Owner-only in-bot login UI. Never persist or log phone/code/password messages."""
import asyncio
import re
import time

from aiogram.exceptions import TelegramAPIError
from telethon import errors
from account import code_digits
from core import RuleError


class AccountChatLogin:
    CONTROL = {'/account_login','/account_cancel','/account_status','/account_logout'}

    def __init__(self,account):
        self.account=account
        self.clear()

    def clear(self):
        self.stage=self.phone=self.code_hash=None
        self.deadline=0
        self.attempts=0

    def expects(self,message):
        if not self.stage or not message.from_user or message.from_user.id!=self.account.owner_id:
            return False
        if message.chat.type!='private' or message.sender_chat:
            return False
        raw=message.text or ''
        command=raw.split(maxsplit=1)[0].split('@')[0] if raw else ''
        return command not in self.CONTROL

    async def begin(self,message):
        self.account.owner(message.from_user.id)
        if message.chat.type!='private':
            raise RuleError('Вход доступен только в личке владельца.')
        async with self.account.lock:
            self.clear()
            await self.account.connected()
            if await self.account.client.is_user_authorized():
                await self.account.verified()
                await message.answer('Аккаунт уже подключён. Откройте /panel -100ID.')
                return
            self.account.confirmations.clear()
            self.stage='phone'
            self.deadline=time.monotonic()+300
            await message.answer(
                'Вход в ВАШ личный Telegram-аккаунт. Номер, код и 2FA-пароль пройдут через этого бота '
                'и ваш сервер. Используйте только свой экземпляр. Сообщения с данными будут удаляться '
                'по возможности, но отсутствие копий у Telegram не гарантируется. '
                'Telegram может аннулировать код, отправленный в чат; точки этого не гарантируют.\n\n'
                'Пришлите номер в формате +кодстраныномер. На вход 5 минут. '
                '/account_cancel — отмена. Не отправляйте данные в группу.')

    async def cancel(self,message):
        self.account.owner(message.from_user.id)
        async with self.account.lock:
            self.clear()
        await message.answer('Вход отменён. Начать снова: /account_login')

    async def finish(self,message):
        me=await self.account.client.get_me()
        if not me or me.bot or me.id!=self.account.owner_id:
            try:
                await self.account.client.log_out()
            finally:
                await self.account.reset_client()
                self.clear()
            raise RuleError('ID введённого аккаунта не совпал с ACCOUNT_OWNER_ID. Новая сессия отозвана. Проверьте настройки.')
        self.clear()
        await message.answer('Вход выполнен! Откройте /panel -100ID_ЧАТА → «Старые заявки (личный аккаунт)».')

    async def receive(self,message):
        if not self.expects(message):
            return
        async with self.account.lock:
            # Re-check after waiting for another owner operation to finish.
            if not self.expects(message):
                return
            try:
                await message.delete()
            except TelegramAPIError:
                # Don't echo content and don't claim successful deletion.
                pass
            try:
                if time.monotonic()>self.deadline:
                    self.clear()
                    raise RuleError('Время входа истекло. Начните заново: /account_login')
                if message.text is None:
                    raise RuleError('Нужен текст, не файл/фото/контакт. /account_cancel — отмена.')
                raw=message.text
                if self.stage=='phone':
                    phone=raw.strip()
                    if not re.fullmatch(r'\+[0-9]{7,15}',phone):
                        raise RuleError('Пришлите номер: +кодстраныномер, без пробелов.')
                    result=await self.account.client.send_code_request(phone)
                    self.phone,self.code_hash=phone,result.phone_code_hash
                    self.stage='code'
                    await message.answer('Пришлите полученный код целиком: 12345 или 1.2.3.4.5. /account_cancel — отмена.')
                elif self.stage=='code':
                    code=code_digits(raw)
                    try:
                        await self.account.client.sign_in(phone=self.phone,code=code,phone_code_hash=self.code_hash)
                    except errors.SessionPasswordNeededError:
                        self.stage='password'
                        await message.answer('Включён облачный пароль 2FA. Пришлите его или /account_cancel. Пароль не будет изменяться.')
                        return
                    await self.finish(message)
                elif self.stage=='password':
                    await self.account.client.sign_in(password=raw)
                    await self.finish(message)
            except (errors.PhoneCodeInvalidError,errors.PasswordHashInvalidError):
                self.attempts+=1
                if self.attempts>=3:
                    self.clear()
                    await message.answer('Три неверных попытки. Вход отменён. Повторите позже: /account_login')
                else:
                    await message.answer('Неверный код или пароль. Повторите ввод или /account_cancel.')
            except errors.PhoneCodeExpiredError:
                self.clear()
                await message.answer('Код истёк или аннулирован Telegram. /account_login — заново. '
                    'Если повторяется, этот способ входа недоступен; через точки тоже не гарантируется.')
            except errors.FloodWaitError as exc:
                self.clear()
                await message.answer(f'Telegram просит подождать {exc.seconds} с. Не запрашивайте новый код до истечения срока.')
            except RuleError as exc:
                await message.answer(str(exc))
            except Exception as exc:
                self.clear()
                # No raw exceptions: requests may contain private authentication data.
                await message.answer(f'Вход не выполнен ({type(exc).__name__}). Проверьте данные, настройки и сеть.')

    async def expire(self):
        while True:
            await asyncio.sleep(10)
            async with self.account.lock:
                if self.stage and time.monotonic()>self.deadline:
                    self.clear()
