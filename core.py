"""Deterministic moderation rules and persistent per-chat configuration."""
import json
import re
import sqlite3
import time
from collections import defaultdict, deque

ROLES = {0: 'Участник', 1: 'Помощник', 2: 'Модератор', 3: 'Старший модератор',
         4: 'Администратор бота', 5: 'Создатель чата'}
DEFAULTS = {'autojoin': True, 'links': True, 'ads': True, 'spam': True,
            'warn_limit': 3, 'spam_count': 6, 'spam_window': 8,
            'spam_mute': 300, 'adwords': []}
# Deliberately conservative heuristic, not semantic advertising detection.
LINK = re.compile(r'(?i)(?:https?://|www\.|(?:t\.me|telegram\.me|telegram\.dog)/|'
                  r'(?<![\w@])[a-z0-9\u0430-\u044f][a-z0-9\u0430-\u044f-]*\.'
                  r'(?:com|net|org|ru|рф|io|me|xyz|top|online|site|app)(?:\b|/)|'
                  r'(?<!\w)@[a-z0-9_]{5,32}\b)')


class RuleError(Exception):
    pass


def duration(text):
    match = re.fullmatch(r'([1-9][0-9]{0,6})([smhd])', text.lower())
    if not match:
        raise RuleError('Время: 30s, 10m, 2h или 7d.')
    seconds = int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    if not 30 <= seconds <= 365 * 86400:
        raise RuleError('Время должно быть от 30 секунд до 365 дней.')
    return seconds


def can_assign(actor, target, wanted):
    return actor >= 4 and 0 <= wanted < actor and target < actor


def has_link(text, entities):
    return any(str(getattr(e.type, 'value', e.type)) in ('url', 'text_link', 'mention')
               for e in entities) or bool(LINK.search(text))


class SpamGuard:
    def __init__(self):
        self.messages = defaultdict(deque)
        self.last_cleanup = 0

    def hit(self, chat, user, count=6, window=8, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last_cleanup > 60:
            # Bound memory for inactive users; timestamps older than max window.
            for key in list(self.messages):
                if not self.messages[key] or now - self.messages[key][-1] > 60:
                    del self.messages[key]
            self.last_cleanup = now
        q = self.messages[(chat, user)]
        while q and now - q[0] > window:
            q.popleft()
        q.append(now)
        if len(q) >= count:
            q.clear()
            return True
        return False


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS roles(chat INTEGER,user INTEGER,rank INTEGER,
          PRIMARY KEY(chat,user));
        CREATE TABLE IF NOT EXISTS settings(chat INTEGER PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS warnings(chat INTEGER,user INTEGER,n INTEGER NOT NULL,
          PRIMARY KEY(chat,user));
        CREATE TABLE IF NOT EXISTS triggers(chat INTEGER,phrase TEXT,answer TEXT,
          PRIMARY KEY(chat,phrase));
        CREATE TABLE IF NOT EXISTS pending(chat INTEGER,user INTEGER,at INTEGER,
          PRIMARY KEY(chat,user));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,chat INTEGER,actor INTEGER,
          target INTEGER,action TEXT,reason TEXT,at INTEGER);
        ''')

    def rank(self, chat, user):
        row = self.db.execute('SELECT rank FROM roles WHERE chat=? AND user=?', (chat,user)).fetchone()
        return row[0] if row else 0

    def set_rank(self, chat, user, rank):
        if not 0 <= rank <= 4:
            raise RuleError('Назначаются только роли 0–4. Создатель определяется Telegram.')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO roles VALUES(?,?,?)', (chat,user,rank))

    def config(self, chat):
        row = self.db.execute('SELECT data FROM settings WHERE chat=?', (chat,)).fetchone()
        return {**DEFAULTS, **(json.loads(row[0]) if row else {})}

    def setting(self, chat, key, value):
        if key not in DEFAULTS:
            raise RuleError('Неизвестная настройка.')
        data = self.config(chat)
        data[key] = value
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (chat,json.dumps(data)))

    def warn(self, chat, user, delta=1):
        with self.db:
            row = self.db.execute('SELECT n FROM warnings WHERE chat=? AND user=?', (chat,user)).fetchone()
            n = max(0,(row[0] if row else 0) + delta)
            self.db.execute('INSERT OR REPLACE INTO warnings VALUES(?,?,?)', (chat,user,n))
        return n

    def warns(self, chat, user):
        row = self.db.execute('SELECT n FROM warnings WHERE chat=? AND user=?', (chat,user)).fetchone()
        return row[0] if row else 0

    def clear_warns(self, chat, user):
        with self.db:
            self.db.execute('DELETE FROM warnings WHERE chat=? AND user=?', (chat,user))

    def trigger(self, chat, phrase, answer):
        phrase = phrase.strip().casefold()
        if not phrase or len(phrase)>100 or (answer is not None and not 1 <= len(answer)<=3000):
            raise RuleError('Триггер: 1–100 символов; ответ: 1–3000 символов.')
        with self.db:
            if answer is None:
                self.db.execute('DELETE FROM triggers WHERE chat=? AND phrase=?',(chat,phrase))
            else:
                self.db.execute('INSERT OR REPLACE INTO triggers VALUES(?,?,?)',(chat,phrase,answer))

    def triggers(self, chat):
        return self.db.execute('SELECT phrase,answer FROM triggers WHERE chat=? ORDER BY phrase',(chat,)).fetchall()

    def answer(self, chat, phrase):
        row = self.db.execute('SELECT answer FROM triggers WHERE chat=? AND phrase=?',
                              (chat,phrase.strip().casefold())).fetchone()
        return row[0] if row else None

    def log(self, chat, actor, target, action, reason=''):
        with self.db:
            self.db.execute('INSERT INTO audit(chat,actor,target,action,reason,at) VALUES(?,?,?,?,?,?)',
                            (chat,actor,target,action,reason[:500],int(time.time())))

    def logs(self, chat):
        return self.db.execute('SELECT actor,target,action,reason FROM audit WHERE chat=? ORDER BY id DESC LIMIT 10', (chat,)).fetchall()

    def save_request(self, chat, user):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO pending VALUES(?,?,?)', (chat,user,int(time.time())))

    def drop_request(self, chat, user):
        with self.db:
            self.db.execute('DELETE FROM pending WHERE chat=? AND user=?',(chat,user))

    def pending(self, chat):
        return [r[0] for r in self.db.execute('SELECT user FROM pending WHERE chat=? ORDER BY at,user',(chat,))]

    def close(self):
        self.db.close()
