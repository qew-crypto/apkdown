"""Owner-only Telegram login conversation. Secrets are never logged."""
import asyncio
import contextlib
import re
import time
from telethon import errors


def normalize_code(text):
    """Accept 4–8 ASCII digits, plain or separated by single dots."""
    text = text.strip()
    if not re.fullmatch(r'(?:[0-9]{4,8}|[0-9](?:\.[0-9]){3,7})', text):
        return None
    return text.replace('.', '')


class BotLogin:
    def __init__(self, user, owner_id):
        self.user = user
        self.owner_id = owner_id
        self.stage = None
        self.phone = None
        self.code_hash = None
        self.deadline = 0
        self.attempts = 0
        self.lock = asyncio.Lock()

    def clear(self):
        self.stage = self.phone = self.code_hash = None
        self.deadline = 0
        self.attempts = 0

    async def finish(self):
        me = await self.user.get_me()
        if not me or me.bot or me.id != self.owner_id:
            await self.user.log_out()
            await self.user.connect()
            self.clear()
            return False
        self.clear()
        return True

    async def expire(self):
        while True:
            await asyncio.sleep(10)
            async with self.lock:
                if self.stage and time.monotonic() > self.deadline:
                    self.clear()

    async def handle(self, event):
        if event.sender_id != self.owner_id or not event.is_private:
            return False
        async with self.lock:
            return await self._handle(event)

    async def _handle(self, event):
        text = (event.raw_text or '').strip()
        command = text.split('@')[0]
        if command == '/cancel':
            self.clear()
            await event.respond('Вход отменён. Для повторной попытки: /login')
            return True
        if command == '/login':
            self.clear()
            if await self.user.is_user_authorized():
                await event.respond('Аккаунт уже подключён. Можно отправлять файлы.')
                return True
            self.stage = 'phone'
            self.deadline = time.monotonic() + 300
            await event.respond(
                'Вход в ваш аккаунт. Номер, код и пароль проходят через этого бота! '
                'Используйте только своего бота на доверенном сервере. '
                'Удаление сообщений не гарантирует отсутствия копий у Telegram.\n'
                'Telegram может аннулировать код, отправленный в чат. Тогда используйте login.py.\n'
                'Пришлите номер в международном формате (+7...). Тайм-аут 5 минут. /cancel — отмена.')
            return True
        if not self.stage:
            return False
        if command.startswith('/') and self.stage != 'password':
            await event.respond('Сначала завершите вход или выполните /cancel.')
            return True
        # Delete private inputs before any network authentication request.
        with contextlib.suppress(errors.RPCError):
            await event.delete()
        if time.monotonic() > self.deadline:
            self.clear()
            await event.respond('Время входа истекло. Начните заново: /login')
            return True
        try:
            if self.stage == 'phone':
                if not text.startswith('+') or not text[1:].isdigit() or not 7 <= len(text[1:]) <= 15:
                    await event.respond('Нужен номер в формате +кодстраныномер, только цифры после +.')
                    return True
                if not self.user.is_connected():
                    await self.user.connect()
                sent = await self.user.send_code_request(text)
                self.phone, self.code_hash = text, sent.phone_code_hash
                self.stage = 'code'
                await event.respond('Код запрошен. Отправьте код через точки, например 1.2.3.5 (это 1235), или обычными цифрами. Не запрашивайте новые коды многократно.')
            elif self.stage == 'code':
                code = normalize_code(text)
                if code is None:
                    await event.respond('Введите 4–8 цифр: 1.2.3.5 или 1235. /cancel — отмена.')
                    return True
                try:
                    await self.user.sign_in(phone=self.phone, code=code, phone_code_hash=self.code_hash)
                except errors.SessionPasswordNeededError:
                    self.stage = 'password'
                    await event.respond('Включена 2FA. Отправьте облачный пароль или /cancel.')
                    return True
                await self._complete(event)
            elif self.stage == 'password':
                # Keep spaces in a password, unlike commands and phone/code input.
                await self.user.sign_in(password=event.raw_text)
                await self._complete(event)
        except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError):
            self.attempts += 1
            if self.attempts >= 3:
                self.clear()
                await event.respond('Три неверных попытки. Вход отменён. Повторите позже: /login')
            else:
                await event.respond('Неверный код или пароль. Повторите ввод или /cancel.')
        except errors.PhoneCodeExpiredError:
            self.clear()
            await event.respond('Код истёк или аннулирован Telegram. /login — заново; если повторяется, используйте login.py.')
        except errors.FloodWaitError as exc:
            self.clear()
            await event.respond(f'Telegram просит подождать {exc.seconds} с. После ожидания: /login')
        except Exception as exc:
            self.clear()
            await event.respond(f'Не удалось войти ({type(exc).__name__}). Проверьте данные. /login — повторить.')
        return True

    async def _complete(self, event):
        if await self.finish():
            await event.respond('Вход выполнен! Теперь отправьте файл. Сессия сохранена только на сервере бота.')
        else:
            await event.respond('ID аккаунта не совпадает с OWNER_ID. Доступ отозван. Проверьте конфигурацию.')
