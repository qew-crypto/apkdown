"""Private Telegram-account downloader -> Filebin. Python 3.11+."""
import asyncio
import contextlib
import hashlib
import logging
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import aiohttp
from telethon import TelegramClient, events, errors
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

from common import BASE, config, human_bytes, safe_name
from auth import BotLogin

LOG = logging.getLogger('apkdown')
FILEBIN = 'https://filebin.net'


class TransferError(Exception):
    pass


@dataclass
class Progress:
    stage: str = 'Скачивание из Telegram'
    done: int = 0
    total: int = 0
    previous: int = 0
    tick: float = field(default_factory=time.monotonic)

    def reset(self, stage, total):
        self.stage, self.total = stage, total
        self.done = self.previous = 0
        self.tick = time.monotonic()

    def text(self, name):
        now = time.monotonic()
        speed = max(0, self.done - self.previous) / max(now - self.tick, 0.001)
        self.previous, self.tick = self.done, now
        percent = f'{min(100, self.done / self.total * 100):.1f}%' if self.total else 'размер неизвестен'
        suffix = '\nОтправлено; ожидаю ответ сервера.' if self.stage.startswith('Загрузка') and self.done == self.total else ''
        return (f'{self.stage}\n{name}\n'
                f'{human_bytes(self.done)} / {human_bytes(self.total)} ({percent})\n'
                f'Скорость: {human_bytes(speed)}/с{suffix}')


async def edit_status(message, text):
    try:
        await message.edit(text, parse_mode=None, link_preview=False)
    except errors.MessageNotModifiedError:
        pass
    except errors.FloodWaitError as exc:
        # Respect Telegram's requested delay; final message is also sent separately.
        await asyncio.sleep(exc.seconds)
    except errors.RPCError:
        LOG.warning('Не удалось обновить сообщение о прогрессе.')


async def report(message, progress, name):
    while True:
        await asyncio.sleep(5)
        await edit_status(message, progress.text(name))


async def upload_file(path, progress, base_url=FILEBIN):
    """Stream without loading the whole file into RAM; redirects intentionally disabled."""
    size = path.stat().st_size
    bin_id = secrets.token_hex(16)
    url = f'{base_url}/{bin_id}/{quote(path.name, safe="")}'
    digest = hashlib.sha256()
    progress.reset('Загрузка на Filebin', size)

    async def chunks():
        with path.open('rb') as source:
            while True:
                chunk = await asyncio.to_thread(source.read, 512 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                yield chunk
                progress.done += len(chunk)

    timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_read=300)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url, data=chunks(), allow_redirects=False,
            headers={'Content-Type': 'application/octet-stream',
                     'Content-Length': str(size), 'Accept': 'application/json',
                     'User-Agent': 'apkdown/1.0'},
        ) as response:
            if response.status != 201:
                reasons = {400: 'Некорректный файл или имя.', 403: 'Filebin отклонил файл или его расширение.',
                           405: 'Хранилище недоступно для записи.', 411: 'Сервер не принял размер файла.',
                           413: 'Превышен лимит размера Filebin.', 429: 'Слишком много запросов.',
                           507: 'На Filebin закончилось место.'}
                raise TransferError(f'Filebin HTTP {response.status}. ' + reasons.get(response.status, 'Попробуйте позже.'))
            try:
                payload = await response.json(content_type=None)
            except (ValueError, aiohttp.ClientError):
                raise TransferError('Filebin не вернул корректный JSON; загрузка не подтверждена.') from None
    if not isinstance(payload, dict) or not isinstance(payload.get('file'), dict):
        raise TransferError('Неожиданный ответ Filebin; загрузка не подтверждена.')
    info = payload['file']
    if info.get('bytes') is not None and info['bytes'] != size:
        raise TransferError('Размер файла в ответе Filebin не совпал.')
    if info.get('sha256') and info['sha256'].lower() != digest.hexdigest():
        raise TransferError('Контрольная сумма Filebin не совпала.')
    expires = (payload.get('bin') or {}).get('expired_at', 'см. на странице Filebin')
    return f'{base_url}/{bin_id}', url, expires


async def main():
    api_id, api_hash, token, max_bytes = config()
    try:
        owner_id = int(os.environ['OWNER_ID'])
        if owner_id <= 0:
            raise ValueError()
    except (KeyError, ValueError):
        raise SystemExit('Укажите свой числовой Telegram ID в OWNER_ID в .env.')
    user = TelegramClient(str(BASE / 'sessions' / 'user'), api_id, api_hash)
    bot = TelegramClient(str(BASE / 'sessions' / 'bot'), api_id, api_hash)
    queue = asyncio.Queue(maxsize=3)
    worker_task = None
    expiry_task = None
    try:
        await user.connect()
        if await user.is_user_authorized():
            me = await user.get_me()
            if me.bot or me.id != owner_id:
                raise SystemExit('Сессия user не совпадает с OWNER_ID. Остановите бот и исправьте настройки.')
        await bot.start(bot_token=token)
        bot_me = await bot.get_me()
        auth = BotLogin(user, owner_id)
        expiry_task = asyncio.create_task(auth.expire())

        @bot.on(events.NewMessage(incoming=True))
        async def commands(event):
            if not event.is_private or event.sender_id != owner_id:
                return
            if await auth.handle(event):
                return
            command = (event.raw_text or '').split('@')[0].strip()
            if command in ('/start', '/help'):
                await event.respond(
                    '/login — вход по номеру, коду и 2FA. /cancel — отмена входа.\n'
                    'После входа отправьте или перешлите файл, видео, аудио или фото.\n'
                    'Я скачаю его через ваш аккаунт и загружу на Filebin.\n'
                    'Прогресс обновляется каждые 5 секунд. Очередь: до 3 ожидающих файлов.\n'
                    'Файлы доступны всем, у кого есть ссылка! Не отправляйте секретные данные.\n'
                    'Не передавайте данные входа чужим ботам. Локальная альтернатива: login.py.\n'
                    '/status — состояние очереди.', parse_mode=None)
            elif command == '/status':
                connected = await user.is_user_authorized()
                await event.respond(f'Аккаунт подключён: {connected}. Ожидают в очереди: {queue.qsize()}.')
            elif event.message.file and not await user.is_user_authorized():
                await event.respond('Сначала войдите в аккаунт: /login')

        # User-session IDs differ from bot-session IDs. Use the outgoing event's
        # original user message directly, rather than looking it up by the bot ID.
        @user.on(events.NewMessage(outgoing=True))
        async def receive(event):
            if not event.is_private or event.chat_id != bot_me.id:
                return
            if not isinstance(event.message.media, (MessageMediaPhoto, MessageMediaDocument)):
                return
            size = event.message.file.size or 0
            if size > max_bytes:
                await bot.send_message(owner_id, f'Файл превышает настроенный лимит {human_bytes(max_bytes)}.')
                return
            if queue.full():
                await bot.send_message(owner_id, 'Очередь заполнена. Повторите отправку позже.')
                return
            status = await bot.send_message(owner_id, 'Файл принят. Ожидание в очереди…')
            try:
                queue.put_nowait((event.message, status))
            except asyncio.QueueFull:
                await edit_status(status, 'Очередь заполнена. Повторите отправку позже.')

        async def worker():
            while True:
                message, status = await queue.get()
                folder = None
                reporter = None
                name = safe_name(message.file.name or f'telegram_{message.id}{message.file.ext or ".bin"}')
                progress = Progress(total=message.file.size or 0)
                try:
                    folder = Path(tempfile.mkdtemp(prefix='job-', dir=BASE / 'downloads'))
                    required = progress.total or max_bytes
                    if shutil.disk_usage(folder).free < required + 100 * 1024 * 1024:
                        raise TransferError('Недостаточно свободного места на диске.')
                    await edit_status(status, progress.text(name))
                    reporter = asyncio.create_task(report(status, progress, name))

                    def downloaded(done, total):
                        progress.done, progress.total = done, total
                        if done > max_bytes:
                            raise TransferError('Превышен настроенный лимит размера.')

                    result = await user.download_media(message, file=str(folder / name), progress_callback=downloaded)
                    if not result:
                        raise TransferError('Telegram не вернул файл.')
                    path = Path(result)
                    if message.file.size and path.stat().st_size != message.file.size:
                        raise TransferError('Файл из Telegram скачан не полностью.')
                    progress.reset('Загрузка на Filebin', path.stat().st_size)
                    await edit_status(status, progress.text(name))
                    bin_url, file_url, expires = await upload_file(path, progress)
                    text = (f'Готово: {name}\nРазмер: {human_bytes(path.stat().st_size)}\n'
                            f'Страница: {bin_url}\nФайл: {file_url}\n'
                            f'Срок хранения (ответ сервера): {expires}\n'
                            'Ссылка не защищена паролем; хранение временное.')
                except asyncio.CancelledError:
                    raise
                except TransferError as exc:
                    text = f'Не удалось обработать {name}: {exc}'
                except Exception as exc:
                    # Don't print API credentials, session details or raw server responses.
                    LOG.error('Transfer failed (%s)', type(exc).__name__)
                    text = f'Ошибка обработки {name} ({type(exc).__name__}). Повторите отправку позже.'
                finally:
                    if reporter:
                        reporter.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await reporter
                    if folder:
                        await asyncio.to_thread(shutil.rmtree, folder, True)
                    queue.task_done()
                await edit_status(status, text)
                # Separate final message also provides a notification after long jobs.
                try:
                    await bot.send_message(owner_id, text, parse_mode=None, link_preview=False)
                except errors.RPCError:
                    LOG.warning('Не удалось отправить итоговое уведомление.')

        (BASE / 'downloads').mkdir(mode=0o700, exist_ok=True)
        worker_task = asyncio.create_task(worker())
        print(f'Запущен @{bot_me.username}. Владелец: {owner_id}. Ctrl+C — остановка.')
        # Stop rather than silently continue if either Telegram connection is lost.
        done, pending = await asyncio.wait(
            [asyncio.ensure_future(user.disconnected),
             asyncio.create_task(bot.run_until_disconnected()), worker_task],
            return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()
    finally:
        if expiry_task:
            expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await expiry_task
        if worker_task:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task
        await bot.disconnect()
        await user.disconnect()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nОстановлен.')
    except Exception as exc:
        print(f'Ошибка запуска ({type(exc).__name__}). Проверьте .env, сеть и сессию Telegram.')
        raise SystemExit(1)
