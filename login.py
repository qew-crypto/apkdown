"""Run interactively on your own machine, never send login codes to the bot."""
import asyncio
from getpass import getpass

from telethon import TelegramClient
from common import BASE, config


async def main():
    api_id, api_hash, _, _ = config(require_bot=False)
    client = TelegramClient(str(BASE / 'sessions' / 'user'), api_id, api_hash)
    try:
        await client.start(
            phone=lambda: input('Номер телефона (+код страны): ').strip(),
            code_callback=lambda: getpass('Код из Telegram: ').strip(),
            password=lambda: getpass('Облачный пароль 2FA: '),
        )
        me = await client.get_me()
        if me.bot:
            raise SystemExit('Нужен личный аккаунт, а не бот.')
        print(f'Аккаунт подключён. ID владельца: {me.id}. Теперь запустите python bot.py')
    finally:
        await client.disconnect()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nВход отменён.')
    except Exception as exc:
        print(f'Не удалось войти ({type(exc).__name__}). Проверьте данные и повторите вход.')
        raise SystemExit(1)
