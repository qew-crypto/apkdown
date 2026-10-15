"""Enter credentials locally; the moderation bot never asks for OTP or password."""
import asyncio
import os
from getpass import getpass
from pathlib import Path

from dotenv import load_dotenv
from account import AccountAccess,code_digits
from core import RuleError


async def main():
    os.umask(0o077)
    base=Path(__file__).resolve().parent
    load_dotenv(base/'.env')
    account=AccountAccess.from_env(base)
    if account is None:
        raise SystemExit('Заполните API_ID, API_HASH, ACCOUNT_OWNER_ID в .env.')
    try:
        print('Остановите bot.py перед входом. Входите только в свой Telegram-аккаунт.')
        await account.client.start(phone=lambda:input('Номер (+код страны): ').strip(),
            code_callback=lambda:code_digits(getpass('Код из Telegram: ')),
            password=lambda:getpass('Облачный пароль 2FA: '))
        me=await account.client.get_me()
        if not me or me.bot or me.id!=account.owner_id:
            await account.client.log_out()
            raise RuleError('ID не совпал с ACCOUNT_OWNER_ID. Новая сессия отозвана; проверьте настройки.')
        print('Личный аккаунт подключён. Теперь запустите python bot.py.')
    finally:
        await account.close()


if __name__=='__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nВход отменён.')
    except RuleError as exc:
        print(str(exc));raise SystemExit(1)
    except Exception as exc:
        print(f'Вход не выполнен ({type(exc).__name__}). Проверьте настройки, номер, код и пароль.')
        raise SystemExit(1)
