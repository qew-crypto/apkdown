"""ChatGuard: moderation plus optional owner-only user-session approval."""
import asyncio
import contextlib
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, Filter
from aiogram.types import (ChatPermissions, Message, ChatJoinRequest, CallbackQuery,
                           InlineKeyboardMarkup, InlineKeyboardButton)
from dotenv import load_dotenv
from telethon import errors as user_errors
from account import AccountAccess
from account_chat import AccountChatLogin

from core import Store, SpamGuard, RuleError, ROLES, duration, can_assign, has_link

BASE = Path(__file__).resolve().parent
router = Router()
LOG = logging.getLogger('chatguard')
store = None
bot = None
locks = defaultdict(asyncio.Lock)
spam = SpamGuard()
trigger_times = {}
bot_username = ''
bulk_tasks = {}
command_times = {}
account = None
account_login_ui = None
user_jobs = set()

HELP = '''ChatGuard — модерация и автоприём

5 ролей: 1 помощник, 2 модератор, 3 старший модератор,
4 администратор бота, 5 создатель чата (автоматически).
0 — обычный участник.

Наказания — ответом на сообщение или с числовым ID:
/warn [ID] [причина] — варн (роль 1+)
/unwarn [ID] — снять один варн (1+)
/warns [ID] — число варнов (1+)
/mute [ID] [10m|2h|7d] [причина] — мут (2+, по умолчанию 1h)
/unmute [ID] — снять мут (2+)
/ban [ID] [причина] — постоянный бан (3+)
/unban [ID] — разрешить вернуться (3+)
/del — удалить сообщение в ответе (2+)
/role [ID] N — назначить роль 0–4 (4+, только ниже своей)
/myrole — своя роль
/roles — список ролей
/log — последние 10 действий (2+)

Настройки (4+):
/panel — панель заявок с кнопками\n/settings — текущие настройки
/account_login — вход по номеру, коду и 2FA в личке
/account_cancel — отменить вход
/account_status — проверить аккаунт (только владелец, в личке)
/account_logout yes — отозвать личную сессию
/links on|off — удаление ссылок и @упоминаний
/ads on|off — удаление пересылок из каналов и рекламных фраз
/spam on|off — 6 сообщений за 8 секунд: мут 5 минут
/autojoin on|off — автоматический приём заявок
/warnlimit 3 — лимит варнов (1–20), затем мут 24 часа
/adword add фраза | /adword del фраза | /adword list
/trigger add фраза | ответ
/trigger del фраза
/triggers — список триггеров

Триггер совпадает со всем текстом без учёта регистра.
В личке: /panel -100ID, /autojoin -100ID on|off, /settings -100ID
(панель и автоприём: создатель, роль 4 либо Telegram-админ с правом приглашения).
/settings и остальные настройки — только роль 4+/создатель.
Бот должен быть администратором с нужными правами.
'''


def status(member):
    return getattr(member.status, 'value', member.status)


async def rank(chat, user):
    # Always consult Telegram before granting creator authority; never stored as role 5.
    member = await bot.get_chat_member(chat, user)
    if status(member)=='creator':
        return 5
    if status(member) in ('member','administrator') or (status(member)=='restricted' and getattr(member,'is_member',False)):
        return store.rank(chat,user)
    return 0


async def require(chat, user, minimum):
    value = await rank(chat,user)
    if value < minimum:
        raise RuleError(f'Нужна роль {minimum}+ ({ROLES[minimum]}).')
    return value


async def require_join_admin(chat,user):
    """Narrow join-request authority, never a grant of internal bot role 4."""
    member=await bot.get_chat_member(chat,user)
    state=status(member)
    if state=='creator':
        return
    present=state in ('administrator','member') or (
        state=='restricted' and getattr(member,'is_member',False))
    if present and store.rank(chat,user)>=4:
        return
    if state=='administrator' and getattr(member,'can_invite_users',False):
        return
    raise RuleError('Нет доступа к заявкам. Нужны права администратора Telegram '
                    '«Приглашение пользователей», роль создателя или внутренняя роль 4. '
                    'Проверьте также ID чата и отправляйте команду от личного аккаунта.')


async def guard_target(chat, actor, target, minimum):
    if target <= 0 or target == actor:
        raise RuleError('Нельзя применять эту команду к себе или к неверному ID.')
    actor_rank = await require(chat,actor,minimum)
    member = await bot.get_chat_member(chat,target)
    target_rank = 5 if status(member)=='creator' else store.rank(chat,target)
    if target_rank >= actor_rank:
        raise RuleError('Нельзя воздействовать на равную или старшую роль.')
    if status(member) in ('creator','administrator'):
        raise RuleError('Администратора Telegram бот не наказывает. Сначала снимите его права вручную.')
    return actor_rank, target_rank


def target_args(message, args):
    if message.reply_to_message:
        reply = message.reply_to_message
        if reply.sender_chat or not reply.from_user:
            raise RuleError('Нельзя выбрать анонимного автора или канал. Укажите числовой ID пользователя.')
        return reply.from_user.id, args
    parts = args.split(maxsplit=1)
    if not parts or not parts[0].isdigit():
        raise RuleError('Ответьте на сообщение пользователя или укажите его числовой ID.')
    return int(parts[0]), parts[1] if len(parts)>1 else ''


async def mute(chat, user, seconds):
    await bot.restrict_chat_member(chat,user,permissions=ChatPermissions(can_send_messages=False),
        until_date=datetime.now(timezone.utc)+timedelta(seconds=seconds),
        use_independent_chat_permissions=True)


async def safe_delete(message):
    try:
        await message.delete()
        return True
    except TelegramAPIError:
        LOG.warning('Удаление не выполнено: chat=%s. Проверьте права и лимиты.',message.chat.id)
        return False


class AccountInputFilter(Filter):
    async def __call__(self,message: Message):
        return account_login_ui is not None and account_login_ui.expects(message)


# Registered before all commands so a password beginning with / is not treated
# as a moderation command. Only four explicit account-control commands bypass it.
@router.message(AccountInputFilter())
async def account_input(message: Message):
    await account_login_ui.receive(message)


COMMANDS = ['start','help','roles','myrole','warn','unwarn','warns','mute','unmute',
            'ban','unban','del','role','settings','links','ads','spam','autojoin',
            'warnlimit','adword','trigger','triggers','log','panel',
            'account_login','account_status','account_logout','account_cancel']


@router.message(Command(*COMMANDS))
async def commands(message: Message):
    if not message.from_user or message.sender_chat:
        await message.answer('Отправьте команду от личного аккаунта, не анонимно.')
        return
    now = asyncio.get_running_loop().time()
    throttle = (message.chat.id,message.from_user.id)
    if now-command_times.get(throttle,0)<1 and not (message.text or '').startswith('/account_cancel'):
        return
    command_times[throttle]=now
    token, _, args = (message.text or '').partition(' ')
    command = token.split('@')[0][1:].lower()
    args = args.strip()
    try:
        if command in ('start','help'):
            await message.answer(HELP)
            return
        if command=='roles':
            await message.answer('\n'.join(f'{i}: {name}' for i,name in ROLES.items()))
            return
        if command.startswith('account_'):
            if message.chat.type!='private':
                raise RuleError('Команды аккаунта доступны только в личке с ботом.')
            if account is None:
                raise RuleError('Заполните API_ID, API_HASH, ACCOUNT_OWNER_ID в .env и перезапустите бот.')
            account.owner(message.from_user.id)
            if command=='account_login':
                await account_login_ui.begin(message)
            elif command=='account_cancel':
                await account_login_ui.cancel(message)
            elif command=='account_status':
                async with account.lock:
                    await account.verified()
                await message.answer('Личная сессия подключена. ID совпадает с ACCOUNT_OWNER_ID.')
            elif args=='yes':
                await account.logout(message.from_user.id)
                account_login_ui.clear()
                await message.answer('Личная сессия отозвана. Обычная модерация и Bot API автоприём продолжают работать.')
            else:
                await message.answer('Подтвердите отзыв сессии: /account_logout yes')
            return
        chat = message.chat.id
        actor = message.from_user.id
        if message.chat.type == 'private':
            if command not in ('autojoin','settings','panel'):
                raise RuleError('Эта команда работает в группе/супергруппе. См. /help.')
            pieces = args.split(maxsplit=1)
            if not pieces or not pieces[0].lstrip('-').isdigit():
                raise RuleError('В личке: /panel -100ID, /autojoin -100ID on|off или /settings -100ID.')
            chat = int(pieces[0])
            args = pieces[1] if len(pieces)>1 else ''
            if chat >= 0:
                raise RuleError('Нужен отрицательный ID группы или канала.')
        async with locks[chat]:
            if command=='panel':
                await require_join_admin(chat,actor)
                await message.answer(panel_text(chat),reply_markup=panel_markup(chat))
            elif command=='myrole':
                r = await rank(chat,actor)
                await message.answer(f'Ваша роль: {r} — {ROLES[r]}.')
            elif command=='settings':
                await require(chat,actor,4)
                cfg = store.config(chat)
                await message.answer(f'Чат {chat}\n'+'\n'.join(f'{k}: {v}' for k,v in cfg.items()))
            elif command in ('links','ads','spam','autojoin'):
                if command=='autojoin':
                    await require_join_admin(chat,actor)
                else:
                    await require(chat,actor,4)
                if args.lower() not in ('on','off'):
                    raise RuleError(f'Используйте /{command} on|off.')
                if command=='autojoin' and args.lower()=='on':
                    me = await bot.get_me()
                    member = await bot.get_chat_member(chat,me.id)
                    if not getattr(member,'can_invite_users',False):
                        raise RuleError('Боту нужны права администратора «Приглашение пользователей».')
                store.setting(chat,command,args.lower()=='on')
                store.log(chat,actor,0,command,args)
                await message.answer(f'{command}: {args.lower()} (чат {chat}).')
            elif command=='warnlimit':
                await require(chat,actor,4)
                if not args.isdigit() or not 1 <= int(args) <= 20:
                    raise RuleError('Лимит варнов: число от 1 до 20.')
                store.setting(chat,'warn_limit',int(args))
                store.log(chat,actor,0,command,args)
                await message.answer('Лимит варнов обновлён.')
            elif command=='adword':
                await require(chat,actor,4)
                op, _, phrase = args.partition(' ')
                words = list(store.config(chat)['adwords'])
                phrase = phrase.strip().casefold()
                if op=='list':
                    await message.answer(('Рекламные фразы:\n'+'\n'.join(words))[:4000] if words else 'Список фраз пуст.')
                    return
                if op not in ('add','del') or not 2 <= len(phrase) <= 100:
                    raise RuleError('/adword add фраза или /adword del фраза (2–100 символов).')
                if op=='add' and phrase not in words:
                    if len(words)>=100:
                        raise RuleError('Максимум 100 рекламных фраз.')
                    words.append(phrase)
                elif op=='del' and phrase in words:
                    words.remove(phrase)
                store.setting(chat,'adwords',words)
                store.log(chat,actor,0,command,op)
                await message.answer('Список рекламных фраз обновлён.')
            elif command in ('trigger','triggers'):
                await require(chat,actor,4)
                if command=='triggers':
                    items = store.triggers(chat)
                    await message.answer(('Триггеры:\n'+'\n'.join(p for p,_ in items))[:4000] if items else 'Триггеров нет.')
                    return
                op, _, rest = args.partition(' ')
                if op=='add' and '|' in rest:
                    phrase,answer = (v.strip() for v in rest.split('|',1))
                    if len(store.triggers(chat))>=100 and store.answer(chat,phrase) is None:
                        raise RuleError('Максимум 100 триггеров.')
                    store.trigger(chat,phrase,answer)
                elif op=='del':
                    store.trigger(chat,rest,None)
                else:
                    raise RuleError('/trigger add фраза | ответ или /trigger del фраза')
                store.log(chat,actor,0,'trigger',op)
                await message.answer('Триггер обновлён.')
            elif command=='log':
                await require(chat,actor,2)
                rows = store.logs(chat)
                await message.answer(('Последние действия:\n'+'\n'.join(f'{a} → {t}: {c} {r}' for a,t,c,r in rows))[:4000] if rows else 'Журнал пуст.')
            elif command=='del':
                target, _ = target_args(message,args)
                await guard_target(chat,actor,target,2)
                if not message.reply_to_message:
                    raise RuleError('/del применяется ответом на сообщение.')
                if not await safe_delete(message.reply_to_message):
                    raise RuleError('Не удалось удалить сообщение. Проверьте права бота.')
                store.log(chat,actor,target,'delete')
                await message.answer('Сообщение удалено.')
            elif command=='role':
                target, rest = target_args(message,args)
                actor_rank = await require(chat,actor,4)
                target_rank = await rank(chat,target)
                if not rest.isdigit() or not can_assign(actor_rank,target_rank,int(rest)) or actor==target:
                    raise RuleError('Роль: 0–4. Менять можно только младшие роли, назначать — ниже своей. Себе нельзя.')
                store.set_rank(chat,target,int(rest))
                store.log(chat,actor,target,'role',rest)
                await message.answer(f'{target}: роль {rest} — {ROLES[int(rest)]}.')
            elif command=='warns':
                target,_ = target_args(message,args)
                await require(chat,actor,1)
                await message.answer(f'{target}: {store.warns(chat,target)} варнов.')
            else:
                minimum = 3 if command in ('ban','unban') else 1 if command in ('warn','unwarn') else 2
                target,rest = target_args(message,args)
                await guard_target(chat,actor,target,minimum)
                if command=='ban':
                    await bot.ban_chat_member(chat,target)
                elif command=='unban':
                    await bot.unban_chat_member(chat,target,only_if_banned=True)
                elif command=='mute':
                    pieces = rest.split(maxsplit=1)
                    seconds = duration(pieces[0]) if pieces else 3600
                    await mute(chat,target,seconds)
                elif command=='unmute':
                    current = await bot.get_chat(chat)
                    if not current.permissions:
                        raise RuleError('Не удалось узнать обычные разрешения чата.')
                    await bot.restrict_chat_member(chat,target,permissions=current.permissions,
                                                  use_independent_chat_permissions=True)
                elif command=='warn':
                    n = store.warn(chat,target)
                    store.log(chat,actor,target,'warn',rest)
                    limit = store.config(chat)['warn_limit']
                    if n>=limit:
                        try:
                            await mute(chat,target,86400)
                        except TelegramAPIError:
                            await message.answer(f'Варн сохранён ({n}/{limit}), но мут не выполнен. Проверьте права бота.')
                            return
                        store.clear_warns(chat,target)
                        store.log(chat,actor,target,'warn-limit-mute','24h')
                        await message.answer(f'{target}: лимит варнов достигнут, мут 24 часа. Варны сброшены.')
                    else:
                        await message.answer(f'{target}: варн {n}/{limit}. {rest[:500]}')
                    return
                elif command=='unwarn':
                    n = store.warn(chat,target,-1)
                    store.log(chat,actor,target,'unwarn')
                    await message.answer(f'{target}: осталось варнов — {n}.')
                    return
                store.log(chat,actor,target,command,rest)
                await message.answer(f'Выполнено: {command}, пользователь {target}.')
    except RuleError as exc:
        await message.answer(str(exc))
    except user_errors.FloodWaitError as exc:
        await message.answer(f'Telegram просит подождать {exc.seconds} с. Не повторяйте запросы раньше.')
    except (user_errors.RPCError,OSError,asyncio.TimeoutError) as exc:
        LOG.warning('Ошибка личного аккаунта: %s',type(exc).__name__)
        await message.answer('Операция аккаунта не выполнена. Проверьте сессию, права и сеть.')
    except TelegramRetryAfter as exc:
        await message.answer(f'Telegram ограничил запросы. Повторите через {exc.retry_after} с.')
    except TelegramAPIError as exc:
        LOG.warning('Команда не выполнена: %s',type(exc).__name__)
        await message.answer('Telegram отклонил действие. Проверьте права бота, тип чата, ID и доступность участника.')


def panel_text(chat):
    cfg = store.config(chat)
    return (f'Заявки: чат {chat}\n'
            f'Автоприём: {"включён" if cfg["autojoin"] else "выключен"}\n'
            f'Сохранённых заявок: {len(store.pending(chat))}\n'
            'Кнопка принимает только заявки, которые бот получил и сохранил. '
            'Список старых заявок Telegram Bot API не предоставляет.'
            + ('\nДля старых заявок доступен отдельный режим личного аккаунта.' if account is not None else ''))


def panel_markup(chat):
    enabled = store.config(chat)['autojoin']
    rows = [
        [InlineKeyboardButton(text='✅ Принять все сохранённые заявки',callback_data=f'cg:all:{chat}')],
        [InlineKeyboardButton(text='⏸ Выключить автоприём' if enabled else '▶️ Включить автоприём',
                              callback_data=f'cg:{"off" if enabled else "on"}:{chat}')],
        [InlineKeyboardButton(text='🔄 Обновить',callback_data=f'cg:refresh:{chat}'),
         InlineKeyboardButton(text='⛔ Остановить массовый приём',callback_data=f'cg:stop:{chat}')]]
    if account is not None:
        rows.append([InlineKeyboardButton(text='🗂 Старые заявки (личный аккаунт)',callback_data=f'cg:old:{chat}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def approve_saved(chat,user):
    # Return True on accepted/already member; preserve failed requests for retry.
    for attempt in range(2):
        try:
            await bot.approve_chat_join_request(chat,user)
            store.drop_request(chat,user)
            store.log(chat,0,user,'approve')
            return True
        except TelegramRetryAfter as exc:
            if attempt == 0:
                await asyncio.sleep(exc.retry_after)
                continue
            return False
        except TelegramBadRequest:
            with contextlib.suppress(TelegramAPIError):
                member = await bot.get_chat_member(chat,user)
                if status(member) in ('member','administrator','creator','kicked') or (
                        status(member)=='restricted' and getattr(member,'is_member',False)):
                    store.drop_request(chat,user)
                    return True
            return False
        except TelegramAPIError:
            return False
    return False


async def bulk_accept(chat,actor,destination):
    accepted = failed = 0
    try:
        # Fixed snapshot: incoming requests while this job runs are a future batch.
        users = store.pending(chat)
        await bot.send_message(destination,f'Принимаю сохранённые заявки: {len(users)}. Остановка — кнопкой в панели.')
        for i,user in enumerate(users):
            if i%10==0:
                await require_join_admin(chat,actor)
            if await approve_saved(chat,user):
                accepted += 1
            else:
                failed += 1
            await asyncio.sleep(0.15)
        await bot.send_message(destination,
            f'Обработка завершена. Принято/уже в чате: {accepted}. Не обработано: {failed}. '
            'Необработанные заявки сохранены для повторной попытки.')
    except asyncio.CancelledError:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(destination,f'Массовый приём остановлен. Обработано: {accepted}. Остальные заявки сохранены.')
        raise
    except (TelegramAPIError,RuleError) as exc:
        LOG.warning('Массовый приём остановлен: %s',type(exc).__name__)
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(destination,'Массовый приём остановлен: проверьте доступ к чату и права администратора.')
    finally:
        bulk_tasks.pop(chat,None)


@router.callback_query(F.data.startswith('cg:'))
async def panel_click(query: CallbackQuery):
    try:
        _,action,value = query.data.split(':')
        chat = int(value)
        await require_join_admin(chat,query.from_user.id)
        if not query.message:
            raise RuleError('Откройте новую панель: /panel.')
        # Acknowledge first: bulk work never blocks Telegram's callback deadline.
        await query.answer()
        async with locks[chat]:
            if action in ('on','off'):
                if action=='on':
                    me = await bot.get_me()
                    member = await bot.get_chat_member(chat,me.id)
                    if not getattr(member,'can_invite_users',False):
                        raise RuleError('Боту нужно право «Приглашение пользователей».')
                store.setting(chat,'autojoin',action=='on')
                store.log(chat,query.from_user.id,0,'autojoin',action)
            elif action=='old':
                if account is None:
                    raise RuleError('Режим аккаунта не настроен в .env.')
                account.owner(query.from_user.id)
                if chat in bulk_tasks:
                    raise RuleError('В этом чате уже идёт приём заявок.')
                count,nonce = await account.prepare(chat,query.from_user.id)
                keys=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='✅ Подтверждаю приём ВСЕХ заявок',callback_data=f'ua:approve:{chat}:{nonce}')],
                    [InlineKeyboardButton(text='Отмена',callback_data=f'ua:cancel:{chat}:{nonce}')]])
                await bot.send_message(account.owner_id,
                    f'Личный аккаунт: чат {chat}. Сейчас ожидают: {count}.\n'
                    'Принять ВСЕ ожидающие заявки, включая старые? Будут приняты и заявки, '
                    'поступившие до выполнения операции. Отменить уже отправленный серверу приём нельзя. '
                    'Подтверждение действует 60 секунд.',reply_markup=keys)
            elif action=='all':
                if chat in bulk_tasks:
                    raise RuleError('Массовый приём уже выполняется.')
                if not store.pending(chat):
                    raise RuleError('Сохранённых заявок нет. Старые заявки до подключения бота недоступны через Bot API.')
                bulk_tasks[chat]=asyncio.create_task(bulk_accept(chat,query.from_user.id,query.message.chat.id))
            elif action=='stop':
                if chat in user_jobs:
                    if account is None:
                        raise RuleError('Нет активного аккаунта.')
                    account.owner(query.from_user.id)
                    raise RuleError('Приём через аккаунт выполняется на сервере Telegram. Отправленную операцию нельзя отменить.')
                if chat in bulk_tasks:
                    bulk_tasks[chat].cancel()
                else:
                    raise RuleError('Массовый приём не запущен.')
            elif action!='refresh':
                raise RuleError('Неизвестная кнопка.')
            try:
                await query.message.edit_text(panel_text(chat),reply_markup=panel_markup(chat))
            except TelegramBadRequest:
                pass
    except (RuleError,ValueError) as exc:
        with contextlib.suppress(TelegramAPIError):
            await query.answer(str(exc)[:190],show_alert=True)
    except user_errors.FloodWaitError as exc:
        with contextlib.suppress(TelegramAPIError):
            await query.message.answer(f'Telegram просит подождать {exc.seconds} с. Повторите позже.')
    except (user_errors.RPCError,OSError,asyncio.TimeoutError) as exc:
        LOG.warning('Ошибка запроса аккаунта: %s',type(exc).__name__)
        with contextlib.suppress(TelegramAPIError):
            await query.message.answer('Не удалось прочитать заявки аккаунта. Проверьте сессию и права.')
    except TelegramAPIError:
        with contextlib.suppress(TelegramAPIError):
            await query.answer('Не удалось проверить права или выполнить действие.',show_alert=True)


async def accept_old_requests(chat,actor,nonce,destination):
    try:
        await require_join_admin(chat,actor)
        remaining=await account.accept_all(chat,actor,nonce)
        store.log(chat,actor,0,'account-approve-all')
        suffix=f'Сейчас ожидают: {remaining}.' if remaining is not None else 'Не удалось прочитать остаток заявок.'
        await bot.send_message(destination,'Telegram подтвердил массовый приём через личный аккаунт. '+suffix+
            ' Новые заявки могут появляться во время обработки. Локальную очередь Bot API при необходимости обновите отдельной кнопкой.')
    except RuleError as exc:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(destination,str(exc))
    except user_errors.FloodWaitError as exc:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(destination,f'Telegram просит подождать {exc.seconds} с. Запрос автоматически не повторяется.')
    except (user_errors.RPCError,OSError,asyncio.TimeoutError) as exc:
        LOG.warning('Приём через аккаунт: %s',type(exc).__name__)
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(destination,'Не удалось подтвердить результат операции. Проверьте заявки в Telegram перед повтором: сервер мог уже принять их.')
    except TelegramAPIError:
        LOG.warning('Не удалось отправить результат приёма через аккаунт.')
    finally:
        user_jobs.discard(chat)
        bulk_tasks.pop(chat,None)


@router.callback_query(F.data.startswith('ua:'))
async def account_click(query: CallbackQuery):
    try:
        if account is None:
            raise RuleError('Режим аккаунта не настроен.')
        account.owner(query.from_user.id)
        if not query.message or query.message.chat.type!='private':
            raise RuleError('Подтверждение работает только в личке владельца.')
        _,action,chat_id,nonce=query.data.split(':')
        chat=int(chat_id)
        await require_join_admin(chat,query.from_user.id)
        async with locks[chat]:
            if action=='cancel':
                async with account.lock:
                    pending=account.confirmations.get(chat)
                    if pending and pending[0]==nonce:
                        account.confirmations.pop(chat,None)
                await query.answer('Подтверждение отменено.')
            elif action=='approve':
                if chat in bulk_tasks:
                    raise RuleError('Для этого чата уже идёт обработка.')
                # The job revalidates the nonce and account/chat permissions.
                await query.answer('Проверяю подтверждение и отправляю запрос…')
                user_jobs.add(chat)
                bulk_tasks[chat]=asyncio.create_task(accept_old_requests(chat,query.from_user.id,nonce,query.from_user.id))
            else:
                raise RuleError('Неизвестная кнопка.')
            with contextlib.suppress(TelegramAPIError):
                await query.message.edit_reply_markup(reply_markup=None)
    except (RuleError,ValueError) as exc:
        with contextlib.suppress(TelegramAPIError):
            await query.answer(str(exc)[:190],show_alert=True)
    except TelegramAPIError:
        with contextlib.suppress(TelegramAPIError):
            await query.answer('Не удалось проверить права. Повторите позже.',show_alert=True)


@router.chat_join_request()
async def approve(request: ChatJoinRequest):
    store.save_request(request.chat.id,request.from_user.id)
    if store.config(request.chat.id)['autojoin']:
        if not await approve_saved(request.chat.id,request.from_user.id):
            LOG.warning('Заявка сохранена, но не принята: chat=%s. Проверьте права.',request.chat.id)


@router.message()
async def moderate(message: Message):
    if message.chat.type not in ('group','supergroup') or not message.from_user or message.sender_chat:
        return
    if message.from_user.is_bot:
        return
    # Ignore Telegram service messages, but count photos/stickers/etc. for spam.
    if not (message.text or message.caption or message.photo or message.video or message.animation
            or message.document or message.sticker or message.voice or message.audio
            or message.video_note or message.poll or message.contact or message.location or message.dice):
        return
    chat,user = message.chat.id,message.from_user.id
    async with locks[chat]:
        try:
            trusted = store.rank(chat,user)>0
            cfg = store.config(chat)
            text = message.text or message.caption or ''
            is_spam = not trusted and cfg['spam'] and spam.hit(chat,user,cfg['spam_count'],cfg['spam_window'])
            entities = list(message.entities or [])+list(message.caption_entities or [])
            link = not trusted and cfg['links'] and has_link(text,entities)
            origin = message.forward_origin
            ads = not trusted and cfg['ads'] and (getattr(origin,'type',None)=='channel' or
                    any(word in text.casefold() for word in cfg['adwords']))
            if is_spam or link or ads:
                member = await bot.get_chat_member(chat,user)
                if status(member) in ('creator','administrator'):
                    return
                deleted = await safe_delete(message)
                action = 'spam' if is_spam else 'link' if link else 'ad'
                store.log(chat,0,user,action,'deleted' if deleted else 'delete-failed')
                if is_spam:
                    try:
                        await mute(chat,user,cfg['spam_mute'])
                        store.log(chat,0,user,'spam-mute','5m')
                    except TelegramAPIError:
                        LOG.warning('Антиспам: мут не выполнен, chat=%s',chat)
                return
            answer = store.answer(chat,text)
            if answer:
                # Per-chat cooldown prevents a trigger from becoming a flood source.
                now = asyncio.get_running_loop().time()
                if now-trigger_times.get(chat,0)>=3:
                    trigger_times[chat]=now
                    await message.answer(answer)
        except TelegramAPIError as exc:
            LOG.warning('Ошибка автофильтра: %s',type(exc).__name__)


async def main():
    global bot,store,bot_username,account,account_login_ui
    os.umask(0o077)
    load_dotenv(BASE/'.env')
    token = os.getenv('BOT_TOKEN','').strip()
    if not token or token.startswith('replace_'):
        raise SystemExit('Укажите BOT_TOKEN в .env.')
    data = BASE/'data'
    data.mkdir(mode=0o700,exist_ok=True)
    account=AccountAccess.from_env(BASE)
    account_login_ui=AccountChatLogin(account) if account is not None else None
    expiry_task=None
    store = Store(data/'chatguard.db')
    bot = Bot(token)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        if account_login_ui is not None:
            expiry_task=asyncio.create_task(account_login_ui.expire())
        me = await bot.get_me()
        bot_username=me.username
        print(f'@{me.username} запущен. Ctrl+C — остановка.')
        await dp.start_polling(bot,allowed_updates=dp.resolve_used_update_types())
    finally:
        if expiry_task is not None:
            expiry_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await expiry_task
        if account_login_ui is not None:
            account_login_ui.clear()
        tasks = list(bulk_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if account is not None:
            await account.close()
        await bot.session.close()
        store.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.WARNING,format='%(levelname)s: %(message)s')
    # Avoid HTTP logs that could include Bot API URLs containing the token.
    logging.getLogger('aiohttp').setLevel(logging.ERROR)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f'Ошибка запуска ({type(exc).__name__}). Проверьте токен, сеть и отсутствие второго экземпляра.')
        raise SystemExit(1)
