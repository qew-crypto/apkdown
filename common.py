import os
import re
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent


def config(require_bot=True):
    # Session files grant account access: keep files private on Unix.
    os.umask(0o077)
    load_dotenv(BASE / '.env')
    try:
        api_id = int(os.environ['API_ID'])
        api_hash = os.environ['API_HASH'].strip()
        token = os.environ.get('BOT_TOKEN', '').strip()
        max_mb = int(os.environ.get('MAX_FILE_MB', '4096'))
        if api_id <= 0 or not re.fullmatch(r'[a-fA-F0-9]{32}', api_hash):
            raise ValueError('Invalid API credentials')
        if require_bot and (':' not in token or token.startswith('replace_')):
            raise ValueError('Invalid bot token')
        if max_mb <= 0:
            raise ValueError('Invalid MAX_FILE_MB')
    except (KeyError, ValueError):
        raise SystemExit('Заполните API_ID, API_HASH, BOT_TOKEN и MAX_FILE_MB в .env (см. .env.example).')
    (BASE / 'sessions').mkdir(mode=0o700, exist_ok=True)
    return api_id, api_hash, token, max_mb * 1024 * 1024


def safe_name(name):
    name = str(name or 'file.bin').replace('\\', '/').split('/')[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '_', name).strip(' .')
    if not name:
        name = 'file.bin'
    # Conservative UTF-8 limit for common filesystems; retain extension.
    stem, ext = os.path.splitext(name)
    ext = ext[:20]
    while len((stem + ext).encode('utf-8')) > 180:
        stem = stem[:-1]
    return stem + ext


def human_bytes(value):
    value = float(value)
    for unit in ('Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'):
        if value < 1024 or unit == 'ТиБ':
            return f'{value:.1f} {unit}'
        value /= 1024
