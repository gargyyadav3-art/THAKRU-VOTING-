#!/usr/bin/env python3
"""
Telegram bot — single-file build.

All original modules (config, database, services/*, handlers/*) are bundled
below and executed as in-memory modules so the original `import` statements
keep working unchanged.

Storage: this build uses a local SQLite database file (bot.db in the current
directory) via an aiosqlite-backed shim that emulates the small subset of
asyncpg the original code relied on. NO external DATABASE_URL is required.

Run:
    pip install aiogram==3.16.0 pyrofork TgCrypto-pyrofork aiohttp httpx aiosqlite pillow uvloop
    python main.py
"""
import asyncio
import base64
import logging
import os
import re
import sys
import types
import subprocess
import importlib

# ────────────────────────────────────────────────────────────────────────────
#  Auto-install missing dependencies
# ────────────────────────────────────────────────────────────────────────────
# Map: import name -> list of pip package specs to try (in order).
# TgCrypto speeds up Pyrogram dramatically. We prefer TgCrypto-pyrofork (has
# prebuilt wheels for modern Python incl. 3.12/3.13) and fall back to the
# legacy `tgcrypto` package if needed.
_REQUIRED = {
    "aiosqlite":  ["aiosqlite"],
    "aiogram":    ["aiogram==3.16.0"],
    "pyrogram":   ["pyrofork"],
    "pyrogram.crypto.aes":  ["TgCrypto-pyrofork", "tgcrypto"],
    "aiohttp":    ["aiohttp"],
    "httpx":      ["httpx"],
    "PIL":        ["pillow"],
}

# Optional speed-ups (best-effort; ignore failure).
_OPTIONAL = {
    "uvloop": ["uvloop"],   # ~2-4x faster asyncio event loop on Linux/macOS
}

def _pip_install(specs):
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install",
         "--disable-pip-version-check", "--no-input",
         "--root-user-action=ignore", "--quiet", *specs]
    )

def _try_import(mod):
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False

def _purge_and_reload_pyrogram():
    """Remove stale pyrogram from sys.modules and reimport pyrofork cleanly."""
    to_remove = [k for k in sys.modules if k == "pyrogram" or k.startswith("pyrogram.")]
    for k in to_remove:
        sys.modules.pop(k, None)
    importlib.invalidate_caches()
    try:
        importlib.import_module("pyrogram")
    except Exception:
        pass

def _ensure_pyrofork():
    """
    Guarantee pyrofork is installed and pyrogram.crypto_executor is accessible.

    Problem: if the original `pyrogram` package is already cached in sys.modules
    (e.g. from a system install), installing pyrofork on disk won't update the
    in-memory module — so crypto_executor stays missing.  Fix: detect the stale
    module, purge it from sys.modules, and reimport from the newly installed
    pyrofork wheel.
    """
    # Step 1 — if pyrogram not importable at all, install pyrofork fresh
    if not _try_import("pyrogram"):
        print("[bootstrap] Installing pyrofork (for pyrogram) …", flush=True)
        _pip_install(["pyrofork"])
        _purge_and_reload_pyrogram()

    # Step 2 — if pyrogram is importable but missing crypto_executor,
    # force-reinstall pyrofork and purge the stale in-memory module
    try:
        import pyrogram as _chk
        if not hasattr(_chk, "crypto_executor"):
            print("[bootstrap] Original pyrogram detected (no crypto_executor) — "
                  "reinstalling pyrofork …", flush=True)
            _pip_install(["pyrofork", "--force-reinstall"])
            _purge_and_reload_pyrogram()
    except Exception:
        pass

    # Step 3 — belt-and-suspenders: if crypto_executor still missing, patch it
    try:
        import pyrogram as _p
        if not hasattr(_p, "crypto_executor"):
            import concurrent.futures as _cf
            _p.crypto_executor = _cf.ThreadPoolExecutor(1, thread_name_prefix="TgCrypto")
            print("[compat] Patched pyrogram.crypto_executor", flush=True)
    except Exception as _pe:
        print(f"[compat] crypto_executor patch skipped: {_pe}", file=sys.stderr)

def _ensure_deps():
    # Required (skip pyrogram — handled separately by _ensure_pyrofork)
    for mod, specs in _REQUIRED.items():
        if mod in ("pyrogram", "pyrogram.crypto.aes"):
            continue
        if _try_import(mod):
            continue
        installed = False
        for spec in specs:
            try:
                print(f"[bootstrap] Installing {spec} (for {mod}) …", flush=True)
                _pip_install([spec])
                importlib.invalidate_caches()
                if _try_import(mod):
                    installed = True
                    break
            except Exception as e:
                print(f"[bootstrap]   {spec} failed: {e}", file=sys.stderr)
        if not installed:
            print(f"[bootstrap] Could not satisfy '{mod}'. Tried: {specs}\n"
                  f"Please run manually:  pip install {' '.join(specs)}",
                  file=sys.stderr)
            raise SystemExit(1)

    # Optional (silent failures)
    for mod, specs in _OPTIONAL.items():
        if _try_import(mod):
            continue
        for spec in specs:
            try:
                _pip_install([spec])
                importlib.invalidate_caches()
                if _try_import(mod):
                    break
            except Exception:
                pass

    # Install / validate pyrogram (pyrofork) last, after everything else
    _ensure_pyrofork()

_ensure_deps()

# ────────────────────────────────────────────────────────────────────────────
#  Speed-ups: uvloop (faster event loop) + verify TgCrypto is active
# ────────────────────────────────────────────────────────────────────────────
try:
    import uvloop  # type: ignore
    uvloop.install()
    print("[speedup] uvloop enabled", flush=True)
except Exception:
    pass

try:
    # Pyrogram lazily picks TgCrypto if importable. Force-import to confirm
    # and print a clear status line so the "TgCrypto missing" warning is gone.
    import pyrogram.crypto.aes as _aes  # noqa: F401
    _has_tg = False
    for _name in ("tgcrypto", "TgCrypto"):
        try:
            importlib.import_module(_name)
            _has_tg = True
            break
        except Exception:
            continue
    print(f"[speedup] TgCrypto active: {_has_tg}", flush=True)
except Exception as _e:
    print(f"[speedup] TgCrypto check failed: {_e}", file=sys.stderr)

# ── Pyrogram crypto_executor compatibility patch ──────────────────────────────
# pyrofork 2.3.65+ removed the module-level `crypto_executor` that the session
# code still references when TgCrypto is not active. Restore it so OTP login
# and session connect work correctly on any pyrofork version.
try:
    import pyrogram as _pyro_mod
    if not hasattr(_pyro_mod, "crypto_executor"):
        import concurrent.futures as _cf
        _pyro_mod.crypto_executor = _cf.ThreadPoolExecutor(1, thread_name_prefix="TgCrypto")
        print("[compat] Patched pyrogram.crypto_executor (pyrofork compat)", flush=True)
except Exception as _pe:
    print(f"[compat] crypto_executor patch skipped: {_pe}", file=sys.stderr)


# ────────────────────────────────────────────────────────────────────────────
#  asyncpg → aiosqlite shim
# ────────────────────────────────────────────────────────────────────────────
import sqlite3
import aiosqlite

_SQLITE_PATH = os.environ.get("BOT_DB_PATH", "bot.db")

_TYPE_MAP = [
    (re.compile(r'\bSERIAL\s+PRIMARY\s+KEY\b', re.I), 'INTEGER PRIMARY KEY AUTOINCREMENT'),
    (re.compile(r'\bBIGSERIAL\b', re.I), 'INTEGER'),
    (re.compile(r'\bBIGINT\b', re.I), 'INTEGER'),
    (re.compile(r'\bBOOLEAN\b', re.I), 'INTEGER'),
    (re.compile(r'\bTIMESTAMP\b', re.I), 'TEXT'),
    (re.compile(r'\bNOW\(\)', re.I), "CURRENT_TIMESTAMP"),
    (re.compile(r'(?<![A-Za-z0-9_])TRUE(?![A-Za-z0-9_])', re.I), '1'),
    (re.compile(r'(?<![A-Za-z0-9_])FALSE(?![A-Za-z0-9_])', re.I), '0'),
]

_PARAM_RE = re.compile(r'\$(\d+)')
_ADD_COL_IFNX = re.compile(r'ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+', re.I)

def _translate(sql: str) -> str:
    for rx, rep in _TYPE_MAP:
        sql = rx.sub(rep, sql)
    sql = _PARAM_RE.sub('?', sql)
    return sql

def _coerce_params(params):
    out = []
    for p in params:
        if isinstance(p, bool):
            out.append(1 if p else 0)
        else:
            out.append(p)
    return out


class _Conn:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    async def _maybe_alter(self, sql: str):
        # SQLite doesn't support "ADD COLUMN IF NOT EXISTS" — strip & swallow dup errors.
        cleaned = _ADD_COL_IFNX.sub('ADD COLUMN ', sql)
        try:
            await self._db.execute(cleaned)
            await self._db.commit()
        except Exception as e:
            if 'duplicate column' in str(e).lower():
                return
            raise

    async def execute(self, sql: str, *params):
        sql_t = _translate(sql)
        if _ADD_COL_IFNX.search(sql):
            return await self._maybe_alter(sql_t.replace('ADD COLUMN IF NOT EXISTS', 'ADD COLUMN'))
        await self._db.execute(sql_t, _coerce_params(params))
        await self._db.commit()

    async def fetch(self, sql: str, *params):
        sql_t = _translate(sql)
        cur = await self._db.execute(sql_t, _coerce_params(params))
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def fetchrow(self, sql: str, *params):
        sql_t = _translate(sql)
        cur = await self._db.execute(sql_t, _coerce_params(params))
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchval(self, sql: str, *params):
        row = await self.fetchrow(sql, *params)
        if row is None:
            return None
        return row[0]


class _PoolAcquireCtx:
    def __init__(self, pool): self._pool = pool
    async def __aenter__(self): return self._pool._conn
    async def __aexit__(self, et, ev, tb): return False


class _Pool:
    def __init__(self):
        self._db = None
        self._conn = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        self._db = await aiosqlite.connect(_SQLITE_PATH)
        self._db.row_factory = sqlite3.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        self._conn = _Conn(self._db)

    def acquire(self):
        return _PoolAcquireCtx(self)


async def _create_pool(dsn=None, **kw):
    p = _Pool()
    await p._connect()
    return p


# Register the shim as the `asyncpg` module BEFORE we exec database.py.
_asyncpg_mod = types.ModuleType("asyncpg")
_asyncpg_mod.create_pool = _create_pool
_asyncpg_mod.Pool = _Pool
_asyncpg_mod.Record = sqlite3.Row
sys.modules["asyncpg"] = _asyncpg_mod


# ────────────────────────────────────────────────────────────────────────────
#  Bundled module sources (base64 encoded)
# ────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────
#  Bundled module sources (plain Python, in original layout)
# ──────────────────────────────────────────────────────────────────────────
_MODULES = {
    'config': r'''# =============================================================================
#  CONFIGURE YOUR SECRETS HERE — edit these values then run: python main.py
# =============================================================================

BOT_TOKEN             = "8704534001:AAEnEvhhJA2a4uE0CxaHaEbx6OyczAEOrKk"          # From @BotFather on Telegram
DATABASE_URL          = "sqlite://bot.db"  # local SQLite — no setup required
GEMINI_API_KEY        = "AIzaSyAZ9zv1iY07FUNX1b_NxNqo2CHefcdWEPU"     # https://aistudio.google.com/app/apikey
API_ID                = 30173657                 # https://my.telegram.org → App api_id
API_HASH              = "8cdaa1ab7078ebf0a2dc0bce3358b5de"           # https://my.telegram.org → App api_hash
ADMIN_IDS             = [7734153365]                    # Your Telegram user ID(s) as a list

# ── Optional ────────────────────────────────────────────────────────────────
KEEP_ALIVE_SESSION_STRING = ""   # Leave empty if not using keep-alive
BOT_USERNAME              = "THAKUR_VOTE_XD_BOT"   # e.g. "Vespyrmass_bot" (without @), or leave empty

# ── Internal (do not change) ─────────────────────────────────────────────────
WEBHOOK_HOST = "https://admin.win91bot.online"
WEBHOOK_PATH = "/telegram-bot/webhook"
WEBHOOK_URL  = ""
PORT         = 8080
''',
    'database': r'''import asyncpg
import os
import config
from typing import Optional, List

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_accounts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                session_string TEXT NOT NULL,
                phone TEXT,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                telegram_account_id BIGINT,
                is_active BOOLEAN DEFAULT TRUE,
                added_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                post_url TEXT,
                action_type TEXT NOT NULL,
                reaction TEXT,
                button_index INTEGER,
                channel_link TEXT,
                channel_list TEXT,
                ai_assisted BOOLEAN DEFAULT FALSE,
                status TEXT DEFAULT 'pending',
                total_accounts INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW(),
                completed_at TIMESTAMP
            )
        """)
        # Safe migrations for campaigns table
        await conn.execute("""
            ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS channel_link TEXT
        """)
        await conn.execute("""
            ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS channel_list TEXT
        """)
        await conn.execute("""
            ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS ai_assisted BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS delay_seconds INTEGER DEFAULT 0
        """)
        await conn.execute("""
            ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS mixed_reactions BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS source_campaign_id INTEGER
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS campaign_logs (
                id SERIAL PRIMARY KEY,
                campaign_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                account_phone TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                error_message TEXT,
                timestamp TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS forward_tasks (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_id INTEGER NOT NULL,
                source_chat TEXT NOT NULL,
                dest_chat TEXT NOT NULL,
                keyword_filter TEXT,
                copy_media BOOLEAN DEFAULT TRUE,
                mode TEXT DEFAULT 'account',
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Safe migration: add mode column to forward_tasks for existing DBs
        await conn.execute("""
            ALTER TABLE forward_tasks ADD COLUMN IF NOT EXISTS mode TEXT DEFAULT 'account'
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_reply_rules (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_id INTEGER NOT NULL,
                trigger_keyword TEXT,
                reply_text TEXT NOT NULL,
                target_type TEXT DEFAULT 'all',
                delay_seconds INTEGER DEFAULT 0,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_listings (
                id SERIAL PRIMARY KEY,
                seller_user_id BIGINT NOT NULL,
                account_id INTEGER NOT NULL,
                account_name TEXT,
                account_phone TEXT,
                account_username TEXT,
                session_string TEXT NOT NULL,
                two_fa_password TEXT,
                price TEXT NOT NULL,
                currency TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                payment_address TEXT,
                payment_qr_file_id TEXT,
                stars_amount INTEGER,
                description TEXT,
                status TEXT DEFAULT 'available',
                created_at TIMESTAMP DEFAULT NOW(),
                sold_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS marketplace_orders (
                id SERIAL PRIMARY KEY,
                listing_id INTEGER NOT NULL,
                buyer_user_id BIGINT NOT NULL,
                buyer_username TEXT,
                buyer_name TEXT,
                status TEXT DEFAULT 'pending',
                payment_note TEXT,
                seller_message_id BIGINT,
                admin_message_id BIGINT,
                created_at TIMESTAMP DEFAULT NOW(),
                resolved_at TIMESTAMP
            )
        """)


# ── Bot Users ─────────────────────────────────────────────────────────────────

async def upsert_user(telegram_id: int, username: str = None, first_name: str = None, last_name: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO bot_users (telegram_id, username, first_name, last_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name
        """, telegram_id, username, first_name, last_name)


async def get_user(telegram_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM bot_users WHERE telegram_id = $1", telegram_id)


async def list_users():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT u.*,
                   (SELECT COUNT(*) FROM telegram_accounts a WHERE a.user_id = u.telegram_id) AS account_count,
                   (SELECT COUNT(*) FROM campaigns c WHERE c.user_id = u.telegram_id) AS campaign_count
            FROM bot_users u
            ORDER BY u.created_at DESC
        """)


# ── Telegram Accounts ─────────────────────────────────────────────────────────

async def add_account(user_id: int, session_string: str, phone: str = None,
                      first_name: str = None, last_name: str = None,
                      username: str = None, telegram_account_id: int = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO telegram_accounts
                (user_id, session_string, phone, first_name, last_name, username, telegram_account_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING id
        """, user_id, session_string, phone, first_name, last_name, username, telegram_account_id)
        return row["id"]


async def get_user_accounts(user_id: int) -> List:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            "SELECT * FROM telegram_accounts WHERE user_id = $1 ORDER BY added_at DESC", user_id
        )


async def get_account(account_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM telegram_accounts WHERE id = $1", account_id)


async def delete_account(account_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM telegram_accounts WHERE id = $1", account_id)


async def list_all_accounts():
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM telegram_accounts ORDER BY added_at DESC")


async def set_account_active(account_id: int, active: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE telegram_accounts SET is_active = $1 WHERE id = $2", active, account_id)


# ── Campaigns ─────────────────────────────────────────────────────────────────

async def create_campaign(user_id: int, post_url: str = None, action_type: str = "react",
                           reaction: str = None, button_index: int = None,
                           channel_link: str = None, channel_list: str = None,
                           ai_assisted: bool = False, total_accounts: int = 0,
                           delay_seconds: int = 0, mixed_reactions: bool = False) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO campaigns (user_id, post_url, action_type, reaction, button_index,
                                   channel_link, channel_list, ai_assisted, total_accounts,
                                   delay_seconds, mixed_reactions)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING id
        """, user_id, post_url, action_type, reaction, button_index,
             channel_link, channel_list, ai_assisted, total_accounts,
             delay_seconds, mixed_reactions)
        return row["id"]


async def get_campaign(campaign_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM campaigns WHERE id = $1", campaign_id)


async def list_campaigns(user_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user_id:
            return await conn.fetch(
                "SELECT * FROM campaigns WHERE user_id = $1 ORDER BY created_at DESC", user_id
            )
        return await conn.fetch("SELECT * FROM campaigns ORDER BY created_at DESC")


async def get_accounts_from_campaign(campaign_id: int) -> list:
    """Return active accounts that had at least one 'success' log in the given campaign."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ta.*
            FROM telegram_accounts ta
            JOIN campaign_logs cl ON cl.account_id = ta.id
            WHERE cl.campaign_id = $1 AND cl.status = 'success' AND ta.is_active = TRUE
        """, campaign_id)
        return [dict(r) for r in rows]


async def get_campaign_status(campaign_id: int) -> str | None:
    """Lightweight status-only fetch used for stop-signal checks inside campaign loops."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM campaigns WHERE id = $1", campaign_id)
        return row["status"] if row else None


async def update_campaign_status(campaign_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status in ("completed", "stopped", "failed"):
            await conn.execute("""
                UPDATE campaigns SET status = $1, completed_at = NOW() WHERE id = $2
            """, status, campaign_id)
        else:
            await conn.execute("UPDATE campaigns SET status = $1 WHERE id = $2", status, campaign_id)


async def increment_campaign_counts(campaign_id: int, success: int = 0, fail: int = 0):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE campaigns
            SET success_count = success_count + $1, fail_count = fail_count + $2
            WHERE id = $3
        """, success, fail, campaign_id)


# ── Campaign Logs ─────────────────────────────────────────────────────────────

async def add_campaign_log(campaign_id: int, account_id: int, account_phone: str,
                            action: str, status: str, error_message: str = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO campaign_logs (campaign_id, account_id, account_phone, action, status, error_message)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, campaign_id, account_id, account_phone, action, status, error_message)


async def get_campaign_logs(campaign_id: int = None, limit: int = 50):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if campaign_id:
            return await conn.fetch("""
                SELECT * FROM campaign_logs WHERE campaign_id = $1
                ORDER BY timestamp DESC LIMIT $2
            """, campaign_id, limit)
        return await conn.fetch("""
            SELECT * FROM campaign_logs ORDER BY timestamp DESC LIMIT $1
        """, limit)


# ── Forward Tasks ─────────────────────────────────────────────────────────────

async def create_forward_task(user_id: int, account_id: int, source_chat: str,
                               dest_chat: str, keyword_filter: str = None,
                               copy_media: bool = True, mode: str = "account") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO forward_tasks (user_id, account_id, source_chat, dest_chat, keyword_filter, copy_media, mode)
            VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id
        """, user_id, account_id, source_chat, dest_chat, keyword_filter, copy_media, mode)
        return row["id"]


async def get_bot_forward_tasks(enabled_only: bool = True) -> List:
    """Return all forward tasks where mode = 'bot'."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = "WHERE mode = 'bot'" + (" AND enabled = TRUE" if enabled_only else "")
        return await conn.fetch(f"SELECT * FROM forward_tasks {where} ORDER BY created_at DESC")


async def get_forward_tasks(user_id: int = None, account_id: int = None, enabled_only: bool = False) -> List:
    pool = await get_pool()
    async with pool.acquire() as conn:
        wheres, params = [], []
        if user_id is not None:
            params.append(user_id)
            wheres.append(f"user_id = ${len(params)}")
        if account_id is not None:
            params.append(account_id)
            wheres.append(f"account_id = ${len(params)}")
        if enabled_only:
            wheres.append("enabled = TRUE")
        where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        return await conn.fetch(f"SELECT * FROM forward_tasks {where} ORDER BY created_at DESC", *params)


async def get_forward_task(task_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM forward_tasks WHERE id = $1", task_id)


async def delete_forward_task(task_id: int, user_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user_id:
            await conn.execute("DELETE FROM forward_tasks WHERE id = $1 AND user_id = $2", task_id, user_id)
        else:
            await conn.execute("DELETE FROM forward_tasks WHERE id = $1", task_id)


async def toggle_forward_task(task_id: int, enabled: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE forward_tasks SET enabled = $1 WHERE id = $2", enabled, task_id)


# ── Auto Reply Rules ───────────────────────────────────────────────────────────

async def create_auto_reply_rule(user_id: int, account_id: int, trigger_keyword: str = None,
                                  reply_text: str = "", target_type: str = "all",
                                  delay_seconds: int = 0) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO auto_reply_rules
                (user_id, account_id, trigger_keyword, reply_text, target_type, delay_seconds)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING id
        """, user_id, account_id, trigger_keyword, reply_text, target_type, delay_seconds)
        return row["id"]


async def get_auto_reply_rules(user_id: int = None, account_id: int = None, enabled_only: bool = False) -> List:
    pool = await get_pool()
    async with pool.acquire() as conn:
        wheres, params = [], []
        if user_id is not None:
            params.append(user_id)
            wheres.append(f"user_id = ${len(params)}")
        if account_id is not None:
            params.append(account_id)
            wheres.append(f"account_id = ${len(params)}")
        if enabled_only:
            wheres.append("enabled = TRUE")
        where = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        return await conn.fetch(f"SELECT * FROM auto_reply_rules {where} ORDER BY created_at DESC", *params)


async def get_auto_reply_rule(rule_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM auto_reply_rules WHERE id = $1", rule_id)


async def delete_auto_reply_rule(rule_id: int, user_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if user_id:
            await conn.execute("DELETE FROM auto_reply_rules WHERE id = $1 AND user_id = $2", rule_id, user_id)
        else:
            await conn.execute("DELETE FROM auto_reply_rules WHERE id = $1", rule_id)


async def toggle_auto_reply_rule(rule_id: int, enabled: bool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE auto_reply_rules SET enabled = $1 WHERE id = $2", enabled, rule_id)


# ── Marketplace ───────────────────────────────────────────────────────────────

async def create_marketplace_listing(seller_user_id: int, account_id: int,
                                      account_name: str, account_phone: str,
                                      account_username: str, session_string: str,
                                      two_fa_password: str, price: str, currency: str,
                                      payment_method: str, payment_address: str = None,
                                      payment_qr_file_id: str = None,
                                      stars_amount: int = None,
                                      description: str = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO marketplace_listings
                (seller_user_id, account_id, account_name, account_phone, account_username,
                 session_string, two_fa_password, price, currency, payment_method,
                 payment_address, payment_qr_file_id, stars_amount, description)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING id
        """, seller_user_id, account_id, account_name, account_phone, account_username,
             session_string, two_fa_password, price, currency, payment_method,
             payment_address, payment_qr_file_id, stars_amount, description)
        return row["id"]


async def get_marketplace_listing(listing_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM marketplace_listings WHERE id = $1", listing_id)


async def get_marketplace_listings(status: str = "available", limit: int = 20, offset: int = 0) -> List:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM marketplace_listings WHERE status = $1
            ORDER BY created_at DESC LIMIT $2 OFFSET $3
        """, status, limit, offset)


async def get_user_marketplace_listings(seller_user_id: int) -> List:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM marketplace_listings WHERE seller_user_id = $1
            ORDER BY created_at DESC
        """, seller_user_id)


async def update_listing_status(listing_id: int, status: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status == "sold":
            await conn.execute("""
                UPDATE marketplace_listings SET status = $1, sold_at = NOW() WHERE id = $2
            """, status, listing_id)
        else:
            await conn.execute("UPDATE marketplace_listings SET status = $1 WHERE id = $2", status, listing_id)


async def delete_marketplace_listing(listing_id: int, seller_user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM marketplace_listings WHERE id = $1 AND seller_user_id = $2 AND status = 'available'
        """, listing_id, seller_user_id)


async def create_marketplace_order(listing_id: int, buyer_user_id: int,
                                    buyer_username: str = None, buyer_name: str = None,
                                    payment_note: str = None) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO marketplace_orders
                (listing_id, buyer_user_id, buyer_username, buyer_name, payment_note)
            VALUES ($1, $2, $3, $4, $5) RETURNING id
        """, listing_id, buyer_user_id, buyer_username, buyer_name, payment_note)
        return row["id"]


async def get_marketplace_order(order_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM marketplace_orders WHERE id = $1", order_id)


async def get_pending_order_for_listing(listing_id: int, buyer_user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT * FROM marketplace_orders
            WHERE listing_id = $1 AND buyer_user_id = $2 AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
        """, listing_id, buyer_user_id)


async def update_order_status(order_id: int, status: str,
                               seller_message_id: int = None, admin_message_id: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status in ("approved", "rejected"):
            await conn.execute("""
                UPDATE marketplace_orders SET status = $1, resolved_at = NOW(),
                seller_message_id = COALESCE($3, seller_message_id),
                admin_message_id  = COALESCE($4, admin_message_id)
                WHERE id = $2
            """, status, order_id, seller_message_id, admin_message_id)
        else:
            await conn.execute("""
                UPDATE marketplace_orders SET status = $1,
                seller_message_id = COALESCE($3, seller_message_id),
                admin_message_id  = COALESCE($4, admin_message_id)
                WHERE id = $2
            """, status, order_id, seller_message_id, admin_message_id)


async def get_accounts_with_tasks() -> List[int]:
    """Return distinct account IDs that have at least one enabled account/hybrid task or reply rule."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT account_id FROM forward_tasks
            WHERE enabled = TRUE AND mode IN ('account', 'hybrid') AND account_id != 0
            UNION
            SELECT DISTINCT account_id FROM auto_reply_rules WHERE enabled = TRUE
        """)
        return [r["account_id"] for r in rows]


# ── Stats ─────────────────────────────────────────────────────────────────────

async def get_overview_stats():
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM bot_users")
        total_accounts = await conn.fetchval("SELECT COUNT(*) FROM telegram_accounts")
        active_accounts = await conn.fetchval("SELECT COUNT(*) FROM telegram_accounts WHERE is_active = TRUE")
        total_campaigns = await conn.fetchval("SELECT COUNT(*) FROM campaigns")
        running_campaigns = await conn.fetchval("SELECT COUNT(*) FROM campaigns WHERE status = 'running'")
        total_actions = await conn.fetchval("SELECT COUNT(*) FROM campaign_logs WHERE status = 'success'")
        return {
            "total_users": total_users,
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "total_campaigns": total_campaigns,
            "running_campaigns": running_campaigns,
            "total_actions": total_actions,
        }
''',
    'services.ai_helper': r'''"""
AI helper — uses Google Gemini (primary) or OpenAI (fallback) to assist with
campaign decisions:
  - Analysing channel verification messages and deciding which button to click
  - Determining the correct action to complete a referral/join flow
  - Building campaign configurations from natural-language descriptions
"""
import logging
import os
import json
import config
from typing import Optional, List

import httpx

logger = logging.getLogger(__name__)

# ── Gemini (primary) ──────────────────────────────────────────────────────────
_GEMINI_KEY = config.GEMINI_API_KEY
_GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash-latest", "gemini-flash-latest"]
_GEMINI_URL_TMPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Track last failure reason for better user messages
_last_ai_failure: str = ""

# ── OpenAI (fallback) ─────────────────────────────────────────────────────────
_OAI_BASE = "https://api.openai.com/v1"
_OAI_KEY = ""


async def _ask_gemini(system_prompt: str, user_message: str) -> Optional[str]:
    """Call Gemini, trying each model in _GEMINI_MODELS until one succeeds."""
    if not _GEMINI_KEY:
        return None
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"parts": [{"text": user_message}]}],
        "generationConfig": {"maxOutputTokens": 512, "temperature": 0.2},
    }
    last_err = None
    quota_exceeded = False
    async with httpx.AsyncClient(timeout=30) as client:
        for model in _GEMINI_MODELS:
            try:
                resp = await client.post(
                    _GEMINI_URL_TMPL.format(model=model),
                    params={"key": _GEMINI_KEY},
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
                if resp.status_code == 404:
                    last_err = f"{model} -> 404 (model not found)"
                    continue
                if resp.status_code == 429:
                    quota_exceeded = True
                    last_err = f"{model} -> 429 (quota exceeded)"
                    continue
                resp.raise_for_status()
                data = resp.json()
                # Walk parts and pick the first non-thought text part
                parts = data["candidates"][0]["content"].get("parts", [])
                for p in parts:
                    if p.get("text") and not p.get("thought"):
                        return p["text"].strip()
                # Fallback: return any text we can find
                for p in parts:
                    if p.get("text"):
                        return p["text"].strip()
            except Exception as e:
                last_err = f"{model} -> {e}"
                continue
    if quota_exceeded:
        logger.warning("Gemini quota exceeded — all models returned 429")
    else:
        logger.warning(f"All Gemini models failed: {last_err}")
    return None


async def _ask_openai(system_prompt: str, user_message: str) -> Optional[str]:
    """Call OpenAI-compatible endpoint and return the response text, or None."""
    if not _OAI_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_OAI_BASE}/chat/completions",
                headers={
                    "Authorization": f"Bearer {_OAI_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    "max_completion_tokens": 512,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"OpenAI call failed: {e}")
        return None


async def ask_ai(system_prompt: str, user_message: str, model: str = "") -> Optional[str]:
    """Try Gemini first, fall back to OpenAI, return None if both unavailable."""
    global _last_ai_failure
    result = await _ask_gemini(system_prompt, user_message)
    if result:
        _last_ai_failure = ""
        return result
    result = await _ask_openai(system_prompt, user_message)
    if result:
        _last_ai_failure = ""
        return result
    _last_ai_failure = "quota_exceeded" if _GEMINI_KEY else "no_key"
    logger.warning("No AI backend available (set GEMINI_API_KEY or OpenAI vars)")
    return None


# ── Public helpers ────────────────────────────────────────────────────────────

async def ai_pick_verification_button(buttons: List[str], message_text: str) -> Optional[int]:
    """
    Given a list of button labels and the channel's verification message,
    decide which button index to click to pass verification.
    Returns the 0-based button index, or None if AI can't decide.
    """
    if not buttons:
        return None

    numbered = "\n".join(f"{i}: {b}" for i, b in enumerate(buttons))
    system = (
        "You are a Telegram bot verification assistant. "
        "Your job is to analyse verification/captcha button options in Telegram channels "
        "and pick the correct button to click to pass verification and join the channel. "
        "Respond ONLY with the integer index (0-based) of the correct button. "
        "If unsure, pick 0."
    )
    user = (
        f"Verification message from channel:\n{message_text}\n\n"
        f"Available buttons:\n{numbered}\n\n"
        "Which button index should I click to pass verification? Reply with just the number."
    )
    answer = await ask_ai(system, user)
    if answer is None:
        return 0
    try:
        idx = int(answer.strip().split()[0])
        if 0 <= idx < len(buttons):
            return idx
    except (ValueError, IndexError):
        pass
    return 0


async def ai_analyze_join_challenge(page_html_or_text: str, _extra=None) -> str:
    """
    Analyse a web page, referral URL, or message to determine what steps to take.
    Returns a natural language description of the action to take.
    """
    system = (
        "You are an AI assistant that analyses Telegram referral/invite links and verification challenges. "
        "Given a URL or page content, explain: the channel type, whether it requires verification, "
        "what button to click (if any), and whether bot-assisted auto-join will work. "
        "Keep it to 3-5 bullet points."
    )
    answer = await ask_ai(system, page_html_or_text[:2000])
    return answer or "Complete the verification steps shown on the page."


async def ai_build_campaign_from_description(description: str) -> dict:
    """
    Build a campaign configuration from a natural language description.
    Returns a dict with campaign parameters matching the handler's expected keys.
    """
    system = (
        "You are an AI assistant that builds Telegram promotion campaign configurations. "
        "Given a description of what the user wants to do, output a JSON object with these EXACT fields:\n"
        "- action_type: one of 'react', 'vote', 'both', 'referral', 'leave_channels'\n"
        "- emoji: emoji string to react with (only for react/both, e.g. '👍')\n"
        "- button_index: integer 0-based poll/vote button index (only for vote/both)\n"
        "- post_url: full Telegram post URL (e.g. https://t.me/channel/123) if mentioned\n"
        "- channels: JSON array of Telegram channel links (for referral/leave_channels)\n"
        "- ai_assisted: boolean true if the task needs AI help with captcha/verification\n"
        "- notes: brief explanation in the same language as the user's description\n"
        "Respond ONLY with a single valid JSON object. No markdown, no backticks."
    )
    answer = await ask_ai(system, description)
    if not answer:
        if _last_ai_failure == "quota_exceeded":
            note = "⚠️ Gemini API quota exceeded — resets daily. Try again later or use a different API key."
        elif _last_ai_failure == "no_key":
            note = "⚠️ AI unavailable — GEMINI_API_KEY is not set."
        else:
            note = "⚠️ AI unavailable — could not reach any AI model."
        return {"action_type": "react", "notes": note}
    try:
        clean = answer.strip().strip("`")
        if clean.startswith("json"):
            clean = clean[4:].strip()
        start = clean.find("{")
        end = clean.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(clean[start:end])
            if "reaction" in parsed and "emoji" not in parsed:
                parsed["emoji"] = parsed.pop("reaction")
            if "channel_list" in parsed and "channels" not in parsed:
                raw = parsed.pop("channel_list")
                if isinstance(raw, str):
                    parsed["channels"] = [c.strip() for c in raw.split(",") if c.strip()]
                elif isinstance(raw, list):
                    parsed["channels"] = raw
            return parsed
    except Exception:
        pass
    return {"action_type": "react", "notes": answer}
''',
    'services.keep_alive': r'''"""
Keep-Alive Virtual User Service

Sends /ping to the bot every 30 seconds using a real Telegram user account
(configured via KEEP_ALIVE_SESSION_STRING env variable) so the bot stays
active on Replit even when no one is chatting with it.

Setup:
  1. Get a Pyrogram session string for any Telegram account (can be your own).
  2. Set KEEP_ALIVE_SESSION_STRING=<session> in Replit secrets.
  3. Set BOT_USERNAME=<your_bot_username> in Replit secrets (without @).
  The bot will auto-discover its username if BOT_USERNAME is not set.
"""
import asyncio
import logging
import config
from pyrogram import Client
from pyrogram.errors import FloodWait, AuthKeyUnregistered, UserDeactivated

logger = logging.getLogger(__name__)

PING_INTERVAL = 30  # seconds between pings

_bot_username: str = ""


def set_bot_username(username: str):
    global _bot_username
    _bot_username = username.lstrip("@")


async def start_keep_alive():
    session_str = config.KEEP_ALIVE_SESSION_STRING
    if not session_str:
        logger.info("⏭️  Keep-alive: KEEP_ALIVE_SESSION_STRING not set — virtual user disabled")
        return

    if not config.API_ID or not config.API_HASH:
        logger.warning("⏭️  Keep-alive: API_ID/API_HASH not configured — virtual user disabled")
        return

    logger.info("🤖 Keep-alive virtual user started — will ping bot every 30s")

    await asyncio.sleep(15)  # Short delay before first ping

    while True:
        target = _bot_username or config.BOT_USERNAME
        if not target:
            logger.warning("Keep-alive: BOT_USERNAME not known yet — waiting...")
            await asyncio.sleep(PING_INTERVAL)
            continue

        try:
            client = Client(
                name="keepalive_virtual_user",
                api_id=config.API_ID,
                api_hash=config.API_HASH,
                session_string=session_str,
                in_memory=True,
                no_updates=True,
            )
            async with client:
                await client.send_message(f"@{target}", "/ping")
                logger.debug(f"🤖 Keep-alive: sent /ping to @{target}")

        except FloodWait as e:
            logger.warning(f"Keep-alive FloodWait: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            continue
        except (AuthKeyUnregistered, UserDeactivated):
            logger.error("Keep-alive: virtual user session invalid or deactivated — stopping keep-alive")
            return
        except Exception as e:
            logger.warning(f"Keep-alive ping error: {e}")

        await asyncio.sleep(PING_INTERVAL)
''',
    'services.pyrogram_manager': r'''import asyncio
import logging
import concurrent.futures
import pyrogram as _pyro_chk
from typing import Dict, Optional
# Ensure crypto_executor exists (guards against stale original-pyrogram installs)
if not hasattr(_pyro_chk, "crypto_executor"):
    _pyro_chk.crypto_executor = concurrent.futures.ThreadPoolExecutor(
        1, thread_name_prefix="TgCrypto"
    )
del _pyro_chk
from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired,
    FloodWait, AuthKeyUnregistered, UserDeactivated,
    SessionRevoked, SessionExpired, AuthKeyInvalid,
)
import config

logger = logging.getLogger(__name__)

# Temp clients used during OTP login flow (keyed by telegram_user_id)
_pending_logins: Dict[int, dict] = {}
# Active user clients (keyed by account db id)
_active_clients: Dict[int, Client] = {}


async def start_login(user_id: int, phone: str) -> dict:
    """Start OTP login for a phone number."""
    if not config.API_ID or not config.API_HASH:
        return {"status": "error", "message": "API credentials are not configured. Contact admin."}

    # Cancel any previous pending login for this user
    await cancel_login(user_id)

    try:
        client = Client(
            name=f"temp_{user_id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            in_memory=True
        )
        await client.connect()
        sent = await client.send_code(phone)
        _pending_logins[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "awaiting_password": False,
            "signed_in": False,
        }
        return {"status": "code_sent", "phone_code_hash": sent.phone_code_hash}
    except FloodWait as e:
        return {"status": "flood_wait", "seconds": e.value}
    except Exception as e:
        logger.error(f"start_login error: {e}")
        return {"status": "error", "message": str(e)}


async def complete_login(user_id: int, code: str, password: str = None) -> dict:
    """
    Complete OTP login.
    - First call: pass code, returns 'success' or 'need_password'
    - Second call (2FA): pass password only, returns 'success' or 'error'
    """
    pending = _pending_logins.get(user_id)
    if not pending:
        return {"status": "error", "message": "No pending login. Please start again with /addaccount"}

    client: Client = pending["client"]
    phone = pending["phone"]
    phone_code_hash = pending["phone_code_hash"]

    # --- 2FA password step (skip sign_in entirely) ---
    if pending.get("awaiting_password"):
        if not password:
            return {"status": "need_password"}
        try:
            await client.check_password(password)
        except Exception as e:
            error_msg = str(e).lower()
            if "password" in error_msg or "hash" in error_msg or "invalid" in error_msg:
                return {"status": "error", "message": "Wrong 2FA password. Please try again."}
            return {"status": "error", "message": f"Password error: {e}"}
        # Fall through to export session
    else:
        # --- OTP code step ---
        try:
            await client.sign_in(phone, phone_code_hash, code)
        except SessionPasswordNeeded:
            # Mark that we need the password on next call — do NOT disconnect
            pending["awaiting_password"] = True
            return {"status": "need_password"}
        except PhoneCodeInvalid:
            return {"status": "error", "message": "Invalid code. Please try again."}
        except PhoneCodeExpired:
            _pending_logins.pop(user_id, None)
            try:
                await client.disconnect()
            except Exception:
                pass
            return {"status": "error", "message": "Code expired. Start again with /addaccount"}
        except Exception as e:
            logger.error(f"complete_login sign_in error: {e}")
            return {"status": "error", "message": str(e)}

    # --- Export session after successful auth ---
    try:
        me = await client.get_me()
        session_string = await client.export_session_string()
        await client.disconnect()
        _pending_logins.pop(user_id, None)
        return {
            "status": "success",
            "session_string": session_string,
            "phone": phone,
            "first_name": me.first_name or "",
            "last_name": me.last_name or "",
            "username": me.username or "",
            "telegram_account_id": me.id,
        }
    except Exception as e:
        logger.error(f"complete_login export error: {e}")
        return {"status": "error", "message": str(e)}


async def cancel_login(user_id: int):
    pending = _pending_logins.pop(user_id, None)
    if pending:
        try:
            await pending["client"].disconnect()
        except Exception:
            pass


def has_pending_login(user_id: int) -> bool:
    return user_id in _pending_logins


def get_pending_phone(user_id: int) -> Optional[str]:
    p = _pending_logins.get(user_id)
    return p["phone"] if p else None


def is_awaiting_password(user_id: int) -> bool:
    return _pending_logins.get(user_id, {}).get("awaiting_password", False)


async def get_client_for_session(account_id: int, session_string: str) -> Optional[Client]:
    """Get or create a pyrogram client for a session string."""
    if account_id in _active_clients:
        client = _active_clients[account_id]
        if client.is_connected:
            return client
        else:
            _active_clients.pop(account_id, None)

    if not config.API_ID or not config.API_HASH:
        return None

    try:
        client = Client(
            name=f"account_{account_id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session_string,
            in_memory=True,
            no_updates=True,
        )
        await client.start()
        _active_clients[account_id] = client
        return client
    except (AuthKeyUnregistered, UserDeactivated, SessionRevoked, SessionExpired, AuthKeyInvalid) as e:
        logger.warning(f"Account {account_id} dead session ({type(e).__name__}) — will be auto-disabled")
        return None
    except Exception as e:
        logger.error(f"get_client_for_session error for {account_id}: {e}")
        return None


async def disconnect_client(account_id: int):
    client = _active_clients.pop(account_id, None)
    if client:
        try:
            await client.stop()
        except Exception:
            pass


async def validate_session_string(session_string: str) -> dict:
    """Validate a session string and return account info."""
    if not config.API_ID or not config.API_HASH:
        return {"status": "error", "message": "API credentials not configured"}
    try:
        client = Client(
            name="validate_temp",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session_string,
            in_memory=True,
            no_updates=True,
        )
        await client.start()
        me = await client.get_me()
        await client.stop()
        return {
            "status": "success",
            "phone": me.phone_number or "",
            "first_name": me.first_name or "",
            "last_name": me.last_name or "",
            "username": me.username or "",
            "telegram_account_id": me.id,
        }
    except (AuthKeyUnregistered, UserDeactivated):
        return {"status": "error", "message": "Session is invalid or account was deactivated."}
    except Exception as e:
        return {"status": "error", "message": str(e)}
''',
    'services.session_keeper': r'''"""
Session Keeper — runs as a background task.
Every 30 minutes it touches every active session in the DB
(calls get_me()) so Telegram doesn't expire them from inactivity.
"""
import asyncio
import logging
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyUnregistered, UserDeactivated, SessionRevoked,
    SessionExpired, AuthKeyInvalid, FloodWait,
)
import database as db
import config

logger = logging.getLogger(__name__)

KEEP_ALIVE_INTERVAL = 30 * 60   # 30 minutes


async def ping_session(account: dict) -> bool:
    """Try to connect and call get_me() to keep the session alive."""
    if not config.API_ID or not config.API_HASH:
        return True  # Skip if not configured yet

    account_id = account["id"]
    session_string = account["session_string"]

    try:
        client = Client(
            name=f"keepalive_{account_id}",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session_string,
            in_memory=True,
            no_updates=True,
        )
        await client.start()
        me = await client.get_me()
        await client.stop()
        logger.debug(f"✅ Session alive: {me.first_name} (account #{account_id})")
        return True
    except FloodWait as e:
        logger.warning(f"FloodWait {e.value}s for account #{account_id} — skipping")
        return True
    except (AuthKeyUnregistered, UserDeactivated, SessionRevoked, SessionExpired, AuthKeyInvalid) as e:
        logger.warning(f"⚠️  Account #{account_id} permanently dead ({type(e).__name__}) — marking inactive")
        await db.set_account_active(account_id, False)
        return False
    except Exception as e:
        logger.warning(f"Session ping error for account #{account_id}: {e}")
        return True  # Don't mark inactive on transient network errors


async def start_session_keeper():
    """Background loop: ping all active sessions every 30 minutes."""
    logger.info("🔄 Session keeper started — pings every 30 min")
    await asyncio.sleep(60)   # Wait 1 min after startup before first run

    while True:
        try:
            accounts = await db.list_all_accounts()
            active = [a for a in accounts if a["is_active"]]

            if active:
                logger.info(f"🔄 Session keeper: pinging {len(active)} active session(s)...")
                for account in active:
                    await ping_session(account)
                    await asyncio.sleep(2)   # Small delay between pings
                logger.info("✅ Session keeper: all sessions pinged")
            else:
                logger.debug("Session keeper: no active sessions to ping")

        except Exception as e:
            logger.error(f"Session keeper error: {e}")

        await asyncio.sleep(KEEP_ALIVE_INTERVAL)
''',
    'services.account_monitor': r'''"""
Persistent account monitoring service.

Keeps Pyrogram clients connected 24/7 for accounts that have:
  - Auto-forward tasks  (copy messages from source → destination)
  - Auto-reply rules    (reply to incoming messages by keyword)

Three forwarding modes
----------------------
  account  — Pyrogram account joins source AND destination, copies via account.
  hybrid   — Pyrogram account joins source (reads), aiogram bot sends to destination.
  bot      — aiogram bot is member of both groups; handled in forwarding.py, NOT here.
"""
import asyncio
import logging
import re
from typing import Dict, List, Optional

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    UserAlreadyParticipant,
    InviteRequestSent,
    ChannelPrivate,
    UsernameNotOccupied,
    PeerIdInvalid,
)
from pyrogram.handlers import MessageHandler

import database as db
from config import API_ID, API_HASH

logger = logging.getLogger(__name__)


# ── Chat-link helpers ──────────────────────────────────────────────────────────

def normalize_chat_link(raw: str) -> str:
    """
    Convert any Telegram chat reference to a form Pyrogram accepts.

    https://t.me/foo         →  @foo
    t.me/foo                 →  @foo
    https://t.me/+XXX        →  https://t.me/+XXX   (private invite — kept as-is)
    @foo                     →  @foo
    -1001234567890           →  -1001234567890       (numeric — kept as-is)
    """
    raw = raw.strip()

    # Numeric chat ID — return as string so Pyrogram can accept it
    try:
        int(raw)
        return raw
    except ValueError:
        pass

    # Private invite link (contains /+)
    m = re.search(r"(t\.me/\+[A-Za-z0-9_-]+)", raw)
    if m:
        return f"https://{m.group(1)}"

    # Public t.me link  →  @username
    m = re.search(r"t\.me/([A-Za-z0-9_]+)", raw)
    if m:
        return f"@{m.group(1)}"

    # Already @username
    if raw.startswith("@"):
        return raw

    # Plain username without @
    if re.match(r"^[A-Za-z0-9_]+$", raw):
        return f"@{raw}"

    return raw


async def resolve_and_join(client: Client, raw_link: str, label: str) -> Optional[int]:
    """
    Normalize the link, join the channel if needed, and return the numeric chat ID.
    Returns None on unrecoverable failure.
    """
    link = normalize_chat_link(raw_link)
    is_invite = "/+" in link

    logger.info(f"[{label}] Resolving {raw_link!r} → normalized: {link!r}")

    if is_invite:
        # Private invite link — must join to receive updates
        try:
            await client.join_chat(link)
            logger.info(f"[{label}] Joined private channel ✅")
        except UserAlreadyParticipant:
            logger.info(f"[{label}] Already member of private channel")
        except InviteRequestSent:
            logger.warning(f"[{label}] Join request sent — waiting 15s for approval…")
            await asyncio.sleep(15)
        except Exception as e:
            logger.error(f"[{label}] Cannot join {link!r}: {type(e).__name__}: {e}")
            return None

        try:
            chat = await client.get_chat(link)
            logger.info(f"[{label}] Resolved: {chat.title!r} (id={chat.id})")
            return chat.id
        except Exception as e:
            logger.error(f"[{label}] get_chat failed after join: {e}")
            return None

    else:
        # Public channel / group / username
        try:
            chat = await client.get_chat(link)
        except (ChannelPrivate, UsernameNotOccupied, PeerIdInvalid) as e:
            logger.error(f"[{label}] Cannot resolve {link!r}: {e}")
            return None
        except Exception as e:
            logger.error(f"[{label}] get_chat({link!r}) failed: {type(e).__name__}: {e}")
            return None

        # Try to join (needed to receive updates)
        try:
            await client.join_chat(link)
            logger.info(f"[{label}] Joined {chat.title!r} ✅")
        except UserAlreadyParticipant:
            logger.info(f"[{label}] Already member of {chat.title!r}")
        except Exception as je:
            logger.warning(f"[{label}] Could not join {link!r}: {type(je).__name__}: {je} "
                           "(continuing — updates may not arrive if account not member)")

        logger.info(f"[{label}] Source ready: {chat.title!r} (id={chat.id})")
        return chat.id


# ── Monitor class ──────────────────────────────────────────────────────────────

class AccountMonitor:
    def __init__(self):
        self._clients: Dict[int, Client] = {}
        self._tasks:   Dict[int, asyncio.Task] = {}
        self._bot = None          # set by main.py after bot is created

    def set_bot(self, bot):
        """Inject the aiogram Bot instance (needed for hybrid mode)."""
        self._bot = bot

    # ── Public API ─────────────────────────────────────────────────────────────

    async def reload_account(self, account_id: int):
        """Stop then restart a client after tasks/rules change."""
        await self.stop_account(account_id)

        account = await db.get_account(account_id)
        if not account or not account["is_active"]:
            return

        fwd_tasks = await db.get_forward_tasks(account_id=account_id, enabled_only=True)
        reply_rules = await db.get_auto_reply_rules(account_id=account_id, enabled_only=True)

        # Only account/hybrid tasks are monitored here; bot tasks go to forwarding.py
        acct_tasks = [t for t in fwd_tasks if t.get("mode", "account") in ("account", "hybrid")]

        if not acct_tasks and not reply_rules:
            return

        phone = account.get("phone") or f"id:{account_id}"
        await self._launch(account_id, account["session_string"], phone, acct_tasks, reply_rules)

    async def stop_account(self, account_id: int):
        """Cancel and clean up everything for one account."""
        task = self._tasks.pop(account_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        client = self._clients.pop(account_id, None)
        if client:
            try:
                if client.is_connected:
                    await client.stop()
            except Exception:
                pass
        logger.info(f"Monitor: stopped account {account_id}")

    async def start_all(self):
        """On bot startup — resume monitoring for all accounts with active tasks."""
        account_ids = await db.get_accounts_with_tasks()
        started = 0
        for account_id in account_ids:
            account = await db.get_account(account_id)
            if not account or not account["is_active"]:
                continue
            fwd_tasks = await db.get_forward_tasks(account_id=account_id, enabled_only=True)
            reply_rules = await db.get_auto_reply_rules(account_id=account_id, enabled_only=True)
            acct_tasks = [t for t in fwd_tasks if t.get("mode", "account") in ("account", "hybrid")]
            if acct_tasks or reply_rules:
                phone = account.get("phone") or f"id:{account_id}"
                await self._launch(account_id, account["session_string"], phone, acct_tasks, reply_rules)
                started += 1
        logger.info(f"Monitor: started {started} account client(s)")

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _launch(self, account_id: int, session_string: str, phone: str,
                      fwd_tasks: list, reply_rules: list):
        task = asyncio.create_task(
            self._run_client(account_id, session_string, phone, fwd_tasks, reply_rules),
            name=f"monitor_{account_id}",
        )
        self._tasks[account_id] = task
        logger.info(f"Monitor: launched account {account_id} ({phone}) "
                    f"— {len(fwd_tasks)} fwd tasks, {len(reply_rules)} reply rules")

    async def _run_client(self, account_id: int, session_string: str, phone: str,
                          fwd_tasks: list, reply_rules: list):
        """Infinite reconnect loop."""
        retry_delay = 5
        while True:
            client = Client(
                f"monitor_{account_id}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=session_string,
                in_memory=True,
            )
            self._clients[account_id] = client
            try:
                await client.start()
                logger.info(f"[{phone}] Pyrogram connected ✅")
                retry_delay = 5

                # ── Resolve + join every source chat ──────────────────────────
                # resolved_fwd maps numeric_source_id → list of tasks
                resolved_fwd: Dict[int, List[dict]] = {}
                for task in fwd_tasks:
                    src_raw = task["source_chat"]
                    chat_id = await resolve_and_join(client, src_raw, phone)
                    if chat_id is not None:
                        resolved_fwd.setdefault(chat_id, []).append(task)
                        logger.info(f"[{phone}] ✅ Watching chat {chat_id} "
                                    f"for task #{task['id']} (mode={task.get('mode','account')})")
                    else:
                        logger.error(f"[{phone}] ❌ Could not resolve source "
                                     f"{src_raw!r} — task #{task['id']} DISABLED")

                if not resolved_fwd and not reply_rules:
                    logger.warning(f"[{phone}] Nothing to watch — stopping client")
                    await client.stop()
                    return

                logger.info(f"[{phone}] Monitoring {len(resolved_fwd)} source chat(s), "
                            f"{len(reply_rules)} reply rule(s)")

                # ── Register message handler ───────────────────────────────────
                # Capture current values in closure to avoid late-binding
                _resolved = dict(resolved_fwd)
                _rules    = list(reply_rules)

                async def on_message(c, message, _r=_resolved, _rl=_rules):
                    try:
                        await self._handle_message(c, message, phone, _r, _rl)
                    except Exception as exc:
                        logger.error(f"[{phone}] handler exception: {exc}")

                client.add_handler(MessageHandler(on_message))
                logger.info(f"[{phone}] Message handler registered — waiting for messages…")

                # ── Idle forever ───────────────────────────────────────────────
                await asyncio.Event().wait()

            except asyncio.CancelledError:
                logger.info(f"[{phone}] Monitor cancelled")
                break
            except Exception as e:
                logger.warning(
                    f"[{phone}] Client error ({type(e).__name__}): {e} "
                    f"— retry in {retry_delay}s"
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 120)
            finally:
                try:
                    if client.is_connected:
                        await client.stop()
                except Exception:
                    pass

    # ── Message handling ───────────────────────────────────────────────────────

    async def _handle_message(self, client: Client, message, phone: str,
                              resolved_fwd: Dict[int, list], reply_rules: list):
        chat_id = message.chat.id if message.chat else None
        msg_type = "text" if message.text else (
            "photo" if message.photo else (
                "video" if message.video else (
                    "document" if message.document else "other"
                )
            )
        )
        logger.info(f"[{phone}] 📨 msg from chat {chat_id} (type={msg_type}, id={message.id})")

        # ── Forwarding ────────────────────────────────────────────────────────
        if chat_id and chat_id in resolved_fwd:
            for task in resolved_fwd[chat_id]:
                logger.info(f"[{phone}] → will forward to {task['dest_chat']} "
                            f"(task #{task['id']}, mode={task.get('mode','account')})")
                asyncio.create_task(
                    self._forward_message(client, message, task, phone),
                    name=f"fwd_{task['id']}_{message.id}",
                )
        else:
            logger.debug(f"[{phone}] chat {chat_id} not in watched sources "
                         f"(watching: {list(resolved_fwd.keys())})")

        # ── Auto-reply ────────────────────────────────────────────────────────
        for rule in reply_rules:
            if self._matches_rule(message, rule):
                asyncio.create_task(
                    self._send_auto_reply(client, message, rule, phone),
                    name=f"reply_{rule['id']}_{message.id}",
                )
                break

    # ── Forwarding logic ───────────────────────────────────────────────────────

    def _matches_rule(self, message, rule: dict) -> bool:
        if message.outgoing:
            return False
        target = rule.get("target_type", "all")
        chat_type = message.chat.type.value if message.chat else ""
        if target == "private" and chat_type != "private":
            return False
        if target == "group" and chat_type not in ("group", "supergroup"):
            return False
        trigger = rule.get("trigger_keyword")
        if trigger:
            text = message.text or message.caption or ""
            if trigger.lower() not in text.lower():
                return False
        return True

    async def _forward_message(self, client: Client, message, task: dict, phone: str):
        """Copy a message to destination — handles all media types."""
        keyword = task.get("keyword_filter")
        if keyword:
            text = message.text or message.caption or ""
            if keyword.lower() not in text.lower():
                logger.debug(f"[{phone}] msg #{message.id} skipped — keyword {keyword!r} not found")
                return

        mode = task.get("mode", "account")
        dest_raw = task["dest_chat"]
        dest = normalize_chat_link(dest_raw)
        src_id = message.chat.id
        msg_id = message.id

        if mode == "hybrid":
            if self._bot:
                await self._hybrid_send(message, dest, phone)
            else:
                logger.error(f"[{phone}] Hybrid mode but no bot instance set!")
            return

        # Account mode — copy via Pyrogram (handles ALL media types automatically)
        for attempt in range(3):
            try:
                await client.copy_message(dest, src_id, msg_id)
                logger.info(f"[{phone}] ✅ Copied msg #{msg_id} from {src_id} → {dest}")
                return
            except FloodWait as e:
                logger.warning(f"[{phone}] FloodWait {e.value}s (forward attempt {attempt+1})")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"[{phone}] Forward error attempt {attempt+1}: "
                             f"{type(e).__name__}: {e}")
                if attempt < 2:
                    await asyncio.sleep(5)

    async def _hybrid_send(self, message, dest: str, phone: str):
        """
        Hybrid mode: account reads the message, bot sends it to destination.
        This makes Telegram see the bot as the sender, not the account.
        """
        bot = self._bot
        try:
            if message.text:
                await bot.send_message(dest, message.text)
            elif message.photo:
                fid = message.photo.file_id
                await bot.send_photo(dest, fid, caption=message.caption or "")
            elif message.video:
                fid = message.video.file_id
                await bot.send_video(dest, fid, caption=message.caption or "")
            elif message.document:
                fid = message.document.file_id
                await bot.send_document(dest, fid, caption=message.caption or "")
            elif message.audio:
                fid = message.audio.file_id
                await bot.send_audio(dest, fid, caption=message.caption or "")
            elif message.voice:
                fid = message.voice.file_id
                await bot.send_voice(dest, fid)
            elif message.video_note:
                fid = message.video_note.file_id
                await bot.send_video_note(dest, fid)
            elif message.sticker:
                fid = message.sticker.file_id
                await bot.send_sticker(dest, fid)
            elif message.animation:
                fid = message.animation.file_id
                await bot.send_animation(dest, fid, caption=message.caption or "")
            else:
                logger.info(f"[{phone}] Hybrid: unsupported message type — skipping")
                return
            logger.info(f"[{phone}] ✅ Hybrid sent to {dest}")
        except FloodWait as e:
            await asyncio.sleep(e.value)
            await self._hybrid_send(message, dest, phone)
        except Exception as e:
            logger.error(f"[{phone}] Hybrid send error: {type(e).__name__}: {e}")

    # ── Auto-reply ─────────────────────────────────────────────────────────────

    async def _send_auto_reply(self, client: Client, message, rule: dict, phone: str):
        delay = rule.get("delay_seconds") or 0
        if delay:
            await asyncio.sleep(delay)
        reply_text = rule["reply_text"]
        for attempt in range(3):
            try:
                await client.send_message(
                    message.chat.id, reply_text,
                    reply_to_message_id=message.id,
                )
                logger.info(f"[{phone}] ✅ Auto-replied in chat {message.chat.id}")
                return
            except FloodWait as e:
                logger.warning(f"[{phone}] FloodWait {e.value}s (auto-reply)")
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.error(f"[{phone}] Auto-reply error attempt {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(3)


# ── Singleton ──────────────────────────────────────────────────────────────────

monitor = AccountMonitor()


async def start_monitor():
    """Call from main.py on_startup."""
    await monitor.start_all()
''',
    'services.campaign_runner': r'''import asyncio
import logging
import random
import re
from typing import Optional, List
from pyrogram import Client
from pyrogram.errors import (
    FloodWait, UserDeactivated, AuthKeyUnregistered,
    SessionRevoked, SessionExpired, AuthKeyInvalid,
    ChannelPrivate, ChatWriteForbidden, UserNotParticipant,
    PeerIdInvalid, UserBannedInChannel,
)

_DEAD_SESSION_ERRORS = (UserDeactivated, AuthKeyUnregistered, SessionRevoked, SessionExpired, AuthKeyInvalid)


def _is_dead_session_error(error_str: str) -> bool:
    """Return True if an error message indicates the session is permanently dead."""
    if not error_str:
        return False
    upper = error_str.upper()
    return any(k in upper for k in (
        "SESSION_REVOKED", "SESSION_EXPIRED",
        "AUTH_KEY_UNREGISTERED", "USER_DEACTIVATED", "AUTH_KEY_INVALID",
    ))


from pyrogram.raw.functions.messages import GetStickerSet
from pyrogram.raw.functions.messages import SendReaction as RawSendReaction
from pyrogram.raw.functions.messages import GetMessagesViews
from pyrogram.raw.functions.channels import GetFullChannel
from pyrogram.raw.types import InputStickerSetShortName
from pyrogram.raw.types import ReactionCustomEmoji as RawReactionCustomEmoji
from pyrogram.raw.types import InputChannel, ChatReactionsSome

import database as db
from services.pyrogram_manager import get_client_for_session, disconnect_client

logger = logging.getLogger(__name__)

def parse_bot_ref_link(link: str):
    """
    Parse a bot referral / web-app link.
    Returns (bot_username, start_param_or_None, app_name_or_None).
    app_name is set only for Mini App links: https://t.me/Bot/AppName?startapp=XXX
    Supports:
      https://t.me/BotName?start=refXXX           → (BotName, refXXX, None)
      https://t.me/BotName/AppName?startapp=refXXX → (BotName, refXXX, AppName)
      https://t.me/BotName                         → (BotName, None, None)
      @BotName                                     → (BotName, None, None)
    """
    link = link.strip()
    m = re.match(r"https?://t\.me/(?P<bot>[a-zA-Z0-9_]+)(?:/(?P<app>[a-zA-Z0-9_]+))?(?:\?(?P<query>[^#\s]+))?$", link)
    if m:
        bot = m.group("bot")
        app = m.group("app")  # e.g. "AppName" for Mini App links
        query = m.group("query") or ""
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        if "startapp" in params:
            return bot, params["startapp"], app  # Mini App link
        if "start" in params:
            return bot, params["start"], None
        return bot, None, app
    m = re.match(r"@([a-zA-Z0-9_]+)", link)
    if m:
        return m.group(1), None, None
    return None, None, None

_running: dict = {}


def parse_post_url(url: str) -> Optional[tuple]:
    """
    Parse a Telegram post URL and return (chat_identifier, message_id).
    Supports:
      https://t.me/channelname/123
      https://t.me/c/1234567890/123  (private channels)
    """
    url = url.strip().rstrip("/")
    # Private channel: t.me/c/CHANNEL_ID/MSG_ID — must be matched FIRST
    m = re.match(r"https?://t\.me/c/(\d+)/(\d+)", url)
    if m:
        return (int(f"-100{m.group(1)}"), int(m.group(2)))
    # Public channel: t.me/username/MSG_ID
    m = re.match(r"https?://t\.me/([a-zA-Z0-9_]+)/(\d+)", url)
    if m:
        return (m.group(1), int(m.group(2)))
    return None


def _get_channel_link(post_url: str) -> str:
    """
    Extract a usable channel join link from a post URL.
      Public:  https://t.me/channelname/123  →  https://t.me/channelname
      Private: https://t.me/c/ID/123         →  "" (no public link exists)
    """
    url = post_url.strip().rstrip("/")
    # Private channel URL — check FIRST to prevent matching 'c' as a username
    if re.match(r"https?://t\.me/c/\d+/\d+", url):
        return ""
    # Public channel
    m = re.match(r"(https?://t\.me/[a-zA-Z0-9_]+)/\d+", url)
    if m:
        return m.group(1)
    return ""


def _normalize_tg_link(link: str) -> str:
    """
    Normalize Telegram deep-links to https://t.me/ format so they pass
    the join filter and can be used with join_chat().

    Handles:
      tg://resolve?domain=USERNAME    → https://t.me/USERNAME
      tg://join?invite=HASH           → https://t.me/+HASH
      tg://privatepost?channel=ID     → (ignored, private post)
      https://t.me/USERNAME           → unchanged
    """
    if not link:
        return link
    link = link.strip()
    # tg://resolve?domain=NAME  (most common for colourful URL buttons)
    m = re.match(r"tg://resolve\?(?:.*&)?domain=([A-Za-z0-9_]+)", link, re.IGNORECASE)
    if m:
        return f"https://t.me/{m.group(1)}"
    # tg://join?invite=HASH
    m = re.match(r"tg://join\?invite=([A-Za-z0-9_\-]+)", link, re.IGNORECASE)
    if m:
        return f"https://t.me/+{m.group(1)}"
    return link


def _is_valid_join_link(link: str) -> bool:
    """
    Return True only for genuine Telegram join links:
      https://t.me/channelname        (public channel)
      https://t.me/+InviteCode        (private invite)
      @channelname                    (public username)
      tg://resolve?domain=NAME        (premium colourful button deep-link)
      tg://join?invite=HASH           (private invite deep-link)
    Rejects garbage like 'https://t.me/c' or full post URLs.
    """
    if not link:
        return False
    link = link.strip()
    # Normalise tg:// deep-links first so later checks work uniformly
    link = _normalize_tg_link(link)
    # Bare https://t.me/c (the classic extraction bug)
    if re.match(r"https?://t\.me/c/?$", link):
        return False
    # Full private post URL — not a join link
    if re.match(r"https?://t\.me/c/\d+", link):
        return False
    # Bot deep-links (t.me/BotName?start=... or ?startapp=...) are referral
    # launch URLs, NOT channels to join — skip them entirely.
    if re.search(r"\?(start|startapp)=", link):
        return False
    return "t.me/" in link or link.startswith("@")


def _extract_channel_links_from_message(msg) -> list:
    """
    Robustly extract all Telegram channel/invite links from a Pyrogram message.

    Covers:
      1. Plain text regex (catches most t.me links in the body)
      2. Message entities: MessageEntityUrl (bare URLs), MessageEntityTextUrl
         (text hyperlinks — used by premium bots for "colourful" buttons and links)
      3. Caption entities (same as above but for photo/video captions)
      4. Inline keyboard buttons — ALL button attributes are inspected:
           .url           — standard URL button
           .login_url.url — login-redirect button
           .web_app.url   — web-app launch button (extracts bot-ref links too)
         Filters for t.me links only to avoid noise.
    Returns a deduplicated list of valid join links.
    """
    seen: set = set()
    links: list = []

    def _add(raw: str):
        if not raw:
            return
        raw = raw.strip().strip(".,)\"'")
        # Normalise tg:// deep-links (premium colourful buttons use these)
        raw = _normalize_tg_link(raw)
        if _is_valid_join_link(raw) and raw not in seen:
            seen.add(raw)
            links.append(raw)

    def _add_from_url(url_str: str):
        """Add the URL itself, and also scan its query params for embedded t.me links.
        Handles tracker/redirect URLs like:
          https://tracker.com/track?redirect_url=https%3A%2F%2Ft.me%2F%2BHASH
        which contain a real invite link in a query parameter.
        """
        if not url_str:
            return
        _add(url_str)
        # Try to extract t.me links from any query parameter value
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            parsed = urlparse(url_str)
            if parsed.query:
                for param_values in parse_qs(parsed.query, keep_blank_values=False).values():
                    for val in param_values:
                        decoded = unquote(val)
                        # Direct t.me link in a param value
                        if "t.me/" in decoded or decoded.startswith("tg://"):
                            _add(decoded)
                        # Also scan inside the decoded value with regex in case
                        # it contains multiple or partial URLs
                        for m2 in re.finditer(
                            r"(?:https?://t\.me/|tg://(?:resolve|join))\S+", decoded
                        ):
                            _add(m2.group(0))
        except Exception:
            pass

    # 1. Regex over full text / caption — covers both https:// and tg:// links
    for field in (msg.text or "", msg.caption or ""):
        for m in re.finditer(r"(?:https?://t\.me/|tg://(?:resolve|join))\S+", field):
            _add(m.group(0))

    # 2+3. Message entities — covers text links / hidden hyperlinks
    for entity_list in (
        getattr(msg, "entities", None) or [],
        getattr(msg, "caption_entities", None) or [],
    ):
        for ent in entity_list:
            url = getattr(ent, "url", None)
            if url:
                _add_from_url(url)

    # 4. Inline keyboard — inspect every button attribute that might hold a URL.
    #    Premium / colourful buttons use tg://resolve?domain=NAME.
    #    Tracking buttons (e.g. api-tgrass.space/track?redirect_url=t.me/+HASH)
    #    hide real invite links inside their query parameters — _add_from_url unpacks those.
    markup = getattr(msg, "reply_markup", None)
    if markup:
        rows = getattr(markup, "inline_keyboard", None) or []
        for row in rows:
            for btn in row:
                _add_from_url(getattr(btn, "url", None) or "")
                login_url_obj = getattr(btn, "login_url", None)
                if login_url_obj:
                    _add_from_url(getattr(login_url_obj, "url", None) or "")
                web_app_obj = getattr(btn, "web_app", None)
                if web_app_obj:
                    _add_from_url(getattr(web_app_obj, "url", None) or "")

    return links


def _parse_flood_wait_seconds(error_str: str) -> Optional[int]:
    """
    Try to extract the wait duration from a FLOOD_WAIT error string.
    Pyrogram encodes it as e.g. 'FLOOD_WAIT_30' or 'A wait of 30 seconds is required'.
    Returns the number of seconds, or None if unparseable.
    """
    m = re.search(r"FLOOD_WAIT_(\d+)", error_str, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"wait of (\d+)", error_str, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*second", error_str, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


# Keywords that identify "I have subscribed / I joined" confirmation buttons.
# Order matters — more specific first.
_SUBSCRIBED_KEYWORDS = [
    "подписал",   # Russian: "подписался" / "подписан"
    "subscrib",   # English: "subscribed" / "subscribe"
    "я подписа",  # Russian: "Я подписался"
    "joined",
    "join",
    "готово",     # Russian: "Ready / Done"
    "done",
    "confirm",
    "continue",
    "продолжить", # Russian: "Continue"
    "check",
    "проверить",  # Russian: "Check"
    "claim",      # English: "🔒 Claim" style buttons
    "получить",   # Russian: "Get / Receive"
    "забрать",    # Russian: "Take / Claim"
    "🔒",
    "✅",
]


async def _click_confirmation_button(
    client,
    bot_username: str,
    phone: str,
    campaign_id: int,
    account_id: int,
) -> bool:
    """
    Find and click the "I subscribed / I joined" confirmation button in the most
    recent message from the referral bot.

    Strategy:
      1. Look for a callback button whose text contains any known confirmation keyword.
      2. If nothing matches by keyword, fall back to AI to pick the best button.
      3. Returns True if a button was clicked, False otherwise.
    """
    from services.ai_helper import ai_pick_verification_button

    try:
        msgs = []
        async for msg in client.get_chat_history(bot_username, limit=5):
            msgs.append(msg)

        for msg in msgs:
            markup = getattr(msg, "reply_markup", None)
            if not markup:
                continue
            rows = getattr(markup, "inline_keyboard", None) or []
            flat = [btn for row in rows for btn in row]
            # Only callback buttons can confirm subscription (URL buttons open links)
            cb_buttons = [b for b in flat if getattr(b, "callback_data", None)]
            if not cb_buttons:
                continue

            # 1. Keyword match
            chosen = None
            for kw in _SUBSCRIBED_KEYWORDS:
                for btn in cb_buttons:
                    if kw.lower() in (btn.text or "").lower():
                        chosen = btn
                        break
                if chosen:
                    break

            # 2. AI fallback
            if not chosen:
                msg_text = msg.text or msg.caption or ""
                labels = [b.text for b in flat]
                idx = await ai_pick_verification_button(labels, msg_text)
                idx = idx or 0
                # Prefer the AI pick if it's a cb button
                target = flat[idx] if idx < len(flat) else None
                chosen = target if (target and getattr(target, "callback_data", None)) else cb_buttons[0]

            # Click it
            try:
                await client.request_callback_answer(
                    chat_id=msg.chat.id,
                    message_id=msg.id,
                    callback_data=chosen.callback_data,
                )
                logger.info(f"Account {phone}: clicked confirmation button '{chosen.text}'")
                return True
            except Exception as ce:
                logger.debug(f"Account {phone}: confirmation click failed: {ce}")

    except Exception as e:
        logger.debug(f"Account {phone}: _click_confirmation_button error: {e}")

    return False


async def _is_member_of_channel(client: Client, channel: str) -> bool:
    try:
        chat = await client.get_chat(channel)
        return getattr(chat, "is_member", False) or getattr(chat, "status", "") in {"member", "administrator", "creator"}
    except Exception:
        return False


async def run_campaign(campaign_id: int, bot_notify_callback=None):
    """Execute a campaign: react and/or vote with all user's accounts."""
    campaign = await db.get_campaign(campaign_id)
    if not campaign:
        logger.error(f"Campaign {campaign_id} not found")
        return

    await db.update_campaign_status(campaign_id, "running")
    accounts = await db.get_user_accounts(campaign["user_id"])
    active_accounts = [a for a in accounts if a["is_active"]]

    if not active_accounts:
        await db.update_campaign_status(campaign_id, "failed")
        if bot_notify_callback:
            await bot_notify_callback(
                campaign["user_id"],
                f"❌ Campaign #{campaign_id} failed: No active accounts found."
            )
        return

    parsed = parse_post_url(campaign["post_url"])
    if not parsed:
        await db.update_campaign_status(campaign_id, "failed")
        if bot_notify_callback:
            await bot_notify_callback(
                campaign["user_id"],
                f"❌ Campaign #{campaign_id} failed: Invalid post URL."
            )
        return

    chat_id, message_id = parsed
    action_type = campaign["action_type"]
    reaction = campaign["reaction"]
    button_index = campaign["button_index"]
    post_url = campaign["post_url"]
    is_private = isinstance(chat_id, int)

    # Prefer DB-stored link (user-provided); fall back to extracted link
    raw_link = campaign.get("channel_link") or _get_channel_link(post_url)
    channel_link = raw_link if _is_valid_join_link(raw_link) else ""

    logger.info(
        f"Campaign {campaign_id}: chat_id={chat_id}, is_private={is_private}, "
        f"channel_link={channel_link!r}, action={action_type}, accounts={len(active_accounts)}"
    )

    delay_seconds = float(campaign.get("delay_seconds") or 1.5)
    mixed_reactions = bool(campaign.get("mixed_reactions") or False)

    await _run_with_accounts(
        campaign_id=campaign_id,
        notify_user_id=campaign["user_id"],
        active_accounts=active_accounts,
        chat_id=chat_id,
        message_id=message_id,
        action_type=action_type,
        reaction=reaction,
        button_index=button_index,
        post_url=post_url,
        is_private=is_private,
        channel_link=channel_link,
        bot_notify_callback=bot_notify_callback,
        delay_seconds=delay_seconds,
        mixed_reactions=mixed_reactions,
    )


async def run_admin_campaign(
    campaign_id: int,
    accounts: List[dict],
    chat_id,
    message_id: int,
    action_type: str,
    reaction: Optional[str],
    button_index: Optional[int],
    post_url: str,
    channel_link: str,
    notify_user_id: int,
    bot_notify_callback=None,
    delay_seconds: float = 1.5,
    mixed_reactions: bool = False,
):
    """Execute a campaign with an explicit list of accounts (admin use)."""
    await db.update_campaign_status(campaign_id, "running")
    is_private = isinstance(chat_id, int)
    valid_link = channel_link if _is_valid_join_link(channel_link) else ""

    await _run_with_accounts(
        campaign_id=campaign_id,
        notify_user_id=notify_user_id,
        active_accounts=accounts,
        chat_id=chat_id,
        message_id=message_id,
        action_type=action_type,
        reaction=reaction,
        button_index=button_index,
        post_url=post_url,
        is_private=is_private,
        channel_link=valid_link,
        bot_notify_callback=bot_notify_callback,
        delay_seconds=delay_seconds,
        mixed_reactions=mixed_reactions,
    )


_MIXED_REACTION_POOL = [
    "👍","❤️","🔥","🥰","👏","😁","🤩","🎉","🙏","💯",
    "😎","🤣","😢","👎","💩","🫡","❤️‍🔥","🤝","🕊","🏆",
]


async def _run_with_accounts(
    campaign_id: int,
    notify_user_id: int,
    active_accounts: List[dict],
    chat_id,
    message_id: int,
    action_type: str,
    reaction: Optional[str],
    button_index: Optional[int],
    post_url: str,
    is_private: bool,
    channel_link: str,
    bot_notify_callback=None,
    delay_seconds: float = 1.5,
    mixed_reactions: bool = False,
):
    """Core campaign execution logic shared by regular and admin campaigns."""
    if is_private and not channel_link:
        logger.warning(
            f"Campaign {campaign_id}: private channel with NO valid join link. "
            "Peer resolution depends entirely on session cache."
        )

    total = len(active_accounts)
    success = 0
    fail = 0

    task = asyncio.current_task()
    _running[campaign_id] = task

    def back_to_menu_keyboard():
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 New Campaign", callback_data="menu:newcampaign")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")],
        ])

    try:
        for account in active_accounts:
            if _running.get(campaign_id) is None:
                break
            # Check DB status — allows "Stop All" from the web panel to work
            _db_status = await db.get_campaign_status(campaign_id)
            if _db_status == "stopped":
                logger.info(f"Campaign {campaign_id}: stop signal received from DB — halting")
                _running.pop(campaign_id, None)
                break

            account_id = account["id"]
            session_str = account["session_string"]
            phone = account.get("phone") or f"ID:{account_id}"
            acc_name = account.get("first_name") or phone

            client = await get_client_for_session(account_id, session_str)
            if not client:
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "connect", "failed", "Could not connect"
                )
                await db.set_account_active(account_id, False)
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
                continue

            # ── Channel access: private channels AND public channels with a join link ──
            if is_private:
                if channel_link:
                    # Path A — private + invite link provided
                    access = await _ensure_channel_access(client, channel_link, phone)
                    if not access["ok"]:
                        err_detail = access.get("error", "unknown error")
                        if access.get("revoked"):
                            logger.warning(f"Account {phone}: 💀 session revoked — auto-disabling")
                            await db.set_account_active(account_id, False)
                            await db.add_campaign_log(
                                campaign_id, account_id, phone, "join", "failed",
                                "Session revoked — auto-disabled"
                            )
                            fail += 1
                            await db.increment_campaign_counts(campaign_id, fail=1)
                            await asyncio.sleep(delay_seconds)
                            continue
                        if access.get("frozen"):
                            logger.warning(f"Account {phone}: ❄️ frozen — auto-disabling account")
                            await db.set_account_active(account_id, False)
                            await db.add_campaign_log(
                                campaign_id, account_id, phone, "join", "frozen",
                                "Account frozen by Telegram — auto-disabled"
                            )
                            fail += 1
                            await db.increment_campaign_counts(campaign_id, fail=1)
                            await asyncio.sleep(delay_seconds)
                            continue
                        logger.warning(f"Account {phone}: channel access failed — {err_detail}")
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "join", "failed", err_detail
                        )
                        fail += 1
                        await db.increment_campaign_counts(campaign_id, fail=1)
                        await _send_not_member_alert(
                            bot_notify_callback, notify_user_id,
                            acc_name, channel_link, post_url, campaign_id
                        )
                        await asyncio.sleep(delay_seconds)
                        continue
                    if access.get("joined"):
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "join", "success",
                            access.get("note", "Auto-joined channel")
                        )
                        logger.info(f"Account {phone}: joined channel ✅ ({access.get('note', '')})")
                        # After a fresh join via invite link, Pyrogram cached the peer
                        # by the invite hash, NOT by the numeric chat_id from the post URL.
                        # We must wait for Telegram to propagate membership, then force-resolve
                        # the numeric peer — otherwise send_reaction(chat_id=int) gets CHANNEL_INVALID.
                        await asyncio.sleep(3)
                        try:
                            await client.get_chat(chat_id)
                            logger.debug(f"Account {phone}: numeric peer cached for {chat_id} ✅")
                        except Exception as _e:
                            logger.debug(f"Account {phone}: peer cache warning after join (non-fatal): {_e}")
                else:
                    # Path B — no invite link: resolve peer via account's dialogs
                    found = await _resolve_peer_via_dialogs(client, chat_id, phone)
                    if not found:
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "access", "failed",
                            "Not a channel member and no join link provided"
                        )
                        fail += 1
                        await db.increment_campaign_counts(campaign_id, fail=1)
                        if bot_notify_callback:
                            await bot_notify_callback(
                                notify_user_id,
                                f"⚠️ <b>Account not in channel!</b>\n\n"
                                f"👤 <b>{acc_name}</b> is not a member of this channel.\n\n"
                                f"💡 Create a new campaign and paste the channel invite link "
                                f"(<code>https://t.me/+...</code>) so accounts can be auto-joined.\n\n"
                                f"🗳 <a href='{post_url}'>View Post</a>",
                                parse_mode="HTML"
                            )
                        await asyncio.sleep(delay_seconds)
                        continue
            elif channel_link:
                # Public channel with a join link provided — auto-join AND resolve peer
                # Many polls/votes require channel membership even on public channels
                join_res = await _join_and_cache(client, channel_link, phone)
                if join_res.get("joined"):
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "join", "success",
                        join_res.get("note", "Auto-joined public channel")
                    )
                    logger.info(f"Account {phone}: joined public channel ✅")
                    await asyncio.sleep(3)  # allow Telegram to propagate membership
                    try:
                        await client.get_chat(chat_id)  # cache numeric peer from post URL
                    except Exception:
                        pass
                elif not join_res["ok"]:
                    if join_res.get("frozen"):
                        logger.warning(f"Account {phone}: ❄️ frozen — auto-disabling account")
                        await db.set_account_active(account_id, False)
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "join", "frozen",
                            "Account frozen by Telegram — auto-disabled"
                        )
                        fail += 1
                        await db.increment_campaign_counts(campaign_id, fail=1)
                        await asyncio.sleep(delay_seconds)
                        continue
                    logger.debug(f"Account {phone}: public channel join failed ({join_res.get('error')}) — trying anyway")
            else:
                # Public channel, no join link — try to cache peer so resolve_peer works
                try:
                    await client.get_chat(chat_id)
                except Exception:
                    pass

            not_member_notified = False

            # ── Determine per-account reaction emoji ──────────────────────────
            account_reaction = reaction
            if mixed_reactions or reaction == "mixed":
                account_reaction = random.choice(_MIXED_REACTION_POOL)

            # ── View post (makes engagement look organic) ─────────────────────
            if action_type in ("react", "vote", "unvote", "both"):
                await _increment_post_views(client, chat_id, message_id)

            # ── React action ──────────────────────────────────────────────────
            if action_type in ("react", "both") and account_reaction:
                result = await _do_react(client, chat_id, message_id, account_reaction)
                logger.info(
                    f"Account {phone}: react {account_reaction} → {'✅' if result['ok'] else '❌'} "
                    f"{result.get('error', '')}"
                )
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "react",
                    "success" if result["ok"] else "failed",
                    result.get("error")
                )
                if result["ok"]:
                    success += 1
                    await db.increment_campaign_counts(campaign_id, success=1)
                else:
                    fail += 1
                    await db.increment_campaign_counts(campaign_id, fail=1)
                    err_msg = result.get("error", "")
                    if _is_dead_session_error(err_msg):
                        logger.warning(f"Account {phone}: 💀 dead session on react — auto-disabling")
                        await db.set_account_active(account_id, False)
                        await asyncio.sleep(delay_seconds)
                        continue
                    if err_msg == "NOT_MEMBER" and not not_member_notified:
                        not_member_notified = True
                        await _send_not_member_alert(
                            bot_notify_callback, notify_user_id,
                            acc_name, channel_link, post_url, campaign_id
                        )

            # ── Vote / Unvote action ──────────────────────────────────────────
            if action_type in ("vote", "unvote", "both") and button_index is not None:
                result = await _do_vote(client, chat_id, message_id, button_index)
                _vote_note = result.get("note") or result.get("error") or ""
                _action_label = "unvote" if action_type == "unvote" else "vote"
                if result["ok"]:
                    logger.info(f"Account {phone}: {_action_label} → ✅ {_vote_note}")
                elif result.get("already_voted"):
                    logger.info(f"Account {phone}: {_action_label} → ⚠️ already voted ({_vote_note})")
                else:
                    logger.info(f"Account {phone}: {_action_label} → ❌ {_vote_note}")
                _vote_status = "success" if result["ok"] else ("already_voted" if result.get("already_voted") else "failed")
                await db.add_campaign_log(
                    campaign_id, account_id, phone, _action_label,
                    _vote_status,
                    _vote_note or None
                )
                if result["ok"] and action_type in ("vote", "unvote"):
                    success += 1
                    await db.increment_campaign_counts(campaign_id, success=1)
                elif not result["ok"] and action_type in ("vote", "unvote"):
                    if not result.get("already_voted"):
                        fail += 1
                        await db.increment_campaign_counts(campaign_id, fail=1)
                    if _is_dead_session_error(_vote_note):
                        logger.warning(f"Account {phone}: 💀 dead session on vote — auto-disabling")
                        await db.set_account_active(account_id, False)
                        await asyncio.sleep(delay_seconds)
                        continue
                    if result.get("error") == "NOT_MEMBER" and not not_member_notified:
                        not_member_notified = True
                        await _send_not_member_alert(
                            bot_notify_callback, notify_user_id,
                            acc_name, channel_link, post_url, campaign_id
                        )

            await asyncio.sleep(delay_seconds)

    except asyncio.CancelledError:
        await db.update_campaign_status(campaign_id, "stopped")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"⏹ Campaign #{campaign_id} stopped.\n✅ Success: {success} | ❌ Failed: {fail}"
            )
        return
    finally:
        _running.pop(campaign_id, None)

    final_status = "completed" if fail == 0 else ("failed" if success == 0 else "completed")
    await db.update_campaign_status(campaign_id, final_status)

    if bot_notify_callback:
        emoji = "✅" if final_status == "completed" else "⚠️"
        speed_note = f"\n⏱ Delay: {delay_seconds}s/account" if delay_seconds > 2 else ""

        # ── Surface actual failure reasons so users can see WHY it failed ──
        err_breakdown = ""
        if fail > 0:
            try:
                logs = await db.get_campaign_logs(campaign_id, limit=200)
                err_counts: dict = {}
                samples: dict = {}
                for log in logs:
                    if log["status"] not in ("failed", "frozen"):
                        continue
                    raw = (log["error_message"] or "Unknown error").strip()
                    # Bucket on the first 80 chars so similar errors collapse
                    bucket = raw[:80]
                    err_counts[bucket] = err_counts.get(bucket, 0) + 1
                    samples.setdefault(bucket, raw)
                if err_counts:
                    top = sorted(err_counts.items(), key=lambda x: -x[1])[:5]
                    lines = []
                    for b, n in top:
                        msg = samples[b].replace("<", "&lt;").replace(">", "&gt;")
                        if len(msg) > 180:
                            msg = msg[:177] + "…"
                        lines.append(f"• <code>{msg}</code> ×{n}")
                    err_breakdown = "\n\n<b>❗ Failure reasons:</b>\n" + "\n".join(lines)
            except Exception as _e:
                logger.debug(f"campaign summary err-breakdown failed: {_e}")

        await bot_notify_callback(
            notify_user_id,
            f"{emoji} Campaign #{campaign_id} finished!\n"
            f"📊 Total accounts: {total}\n"
            f"✅ Success: {success}\n"
            f"❌ Failed: {fail}"
            + speed_note
            + err_breakdown,
            parse_mode="HTML",
        )



async def _send_not_member_alert(
    bot_notify_callback,
    user_id: int,
    acc_name: str,
    channel_link: str,
    post_url: str,
    campaign_id: int,
):
    """Send an inline-button alert when an account cannot join the channel."""
    if not bot_notify_callback:
        return
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    text = (
        f"⚠️ <b>Account not in channel!</b>\n\n"
        f"👤 Account: <b>{acc_name}</b>\n"
        f"❌ This account is <b>not a member</b> of the channel.\n\n"
        f"📢 <b>Only channel subscribers can vote.</b>\n\n"
        f"👇 Share these links so the account can join and then vote:"
    )

    buttons = []
    if _is_valid_join_link(channel_link):
        buttons.append(InlineKeyboardButton(text="📢 Join Channel", url=channel_link))
    buttons.append(InlineKeyboardButton(text="🗳 Vote Link", url=post_url))

    kb = InlineKeyboardMarkup(inline_keyboard=[buttons])
    await bot_notify_callback(user_id, text, keyboard=kb)


async def stop_campaign(campaign_id: int) -> bool:
    task = _running.pop(campaign_id, None)
    if task:
        task.cancel()
        await db.update_campaign_status(campaign_id, "stopped")
        return True
    return False


def is_running(campaign_id: int) -> bool:
    return campaign_id in _running


async def _resolve_peer_via_dialogs(client: Client, target_chat_id: int, phone: str) -> bool:
    """
    For private channels with no invite link, resolve the channel peer by iterating
    the account's dialog list (async generator in Pyrogram 2.x).
    Pyrogram caches access_hash for every chat yielded by get_dialogs(), so after
    this call the numeric chat_id becomes usable in API calls.

    Returns True if the channel was found (account is a member), False otherwise.
    """
    logger.info(f"Account {phone}: resolving private channel peer via dialogs...")
    try:
        async for dialog in client.get_dialogs():
            if dialog.chat and dialog.chat.id == target_chat_id:
                logger.info(f"Account {phone}: found channel in dialogs ✅")
                return True
        logger.warning(
            f"Account {phone}: channel {target_chat_id} not found in dialogs — not a member"
        )
        return False
    except Exception as e:
        logger.warning(f"Account {phone}: get_dialogs failed ({type(e).__name__}): {e}")
        return False


async def _ensure_channel_access(client: Client, channel_link: str, phone: str) -> dict:
    """
    Ensure the account is a member of the channel and can interact with it.

    Strategy:
    - Private invite links (t.me/+...): ALWAYS call join_chat() first.
      join_chat() gracefully handles already-joined accounts (USER_ALREADY_PARTICIPANT).
      This is correct because get_chat() with an invite link succeeds even for
      non-members (it just fetches channel info), so we cannot use it to check membership.
    - Public channel links (t.me/channelname or @username): get_chat() is enough
      to resolve the peer; non-members will get a membership error on reaction/vote.

    After join_chat(), always re-resolve the peer via get_chat() so the numeric
    chat_id is in Pyrogram's access_hash cache for the subsequent API calls.

    Returns:
      {"ok": True,  "joined": False}                  — already a member, peer cached
      {"ok": True,  "joined": True,  "note": "..."}   — joined now, peer cached
      {"ok": False, "error": "..."}                   — could not access/join
    """
    # t.me/+CODE is a private invite link; public links have no '+' in the path
    is_private_invite = "t.me/+" in channel_link

    if is_private_invite:
        # ── Private invite link: always join (handles already-member case too) ──
        return await _join_and_cache(client, channel_link, phone)
    else:
        # ── Public channel link: get_chat to resolve peer, join if not member ──
        try:
            await client.get_chat(channel_link)
            logger.info(f"Account {phone}: public channel peer resolved ✅")
            return {"ok": True, "joined": False}
        except FloodWait as e:
            logger.warning(f"Account {phone}: FloodWait {e.value}s during get_chat")
            await asyncio.sleep(e.value)
            try:
                await client.get_chat(channel_link)
                return {"ok": True, "joined": False}
            except Exception:
                pass
        except Exception as e:
            err_upper = str(e).upper()
            is_not_member = (
                "INVITE_HASH" in err_upper or
                "CHANNEL_PRIVATE" in err_upper or
                "NOT_PARTICIPANT" in err_upper or
                "NEED_MEMBER_APPROVED" in err_upper or
                _is_not_member_error(e)
            )
            if not is_not_member:
                logger.warning(
                    f"Account {phone}: get_chat unexpected error ({type(e).__name__}): {e}"
                    " — proceeding optimistically"
                )
                return {"ok": True, "joined": False}
            logger.info(f"Account {phone}: not a member of public channel — will join")

        return await _join_and_cache(client, channel_link, phone)


async def _auto_leave_oldest_channels(client: Client, phone: str, count: int = 10) -> int:
    """
    Leave `count` channels/supergroups for the given account (oldest activity first).
    This is called automatically when Telegram returns CHANNELS_TOO_MUCH.
    Returns the number of channels actually left.
    """
    left = 0
    try:
        # get_dialogs returns most-recently-active first; collect all channels then
        # take the LAST `count` (least recently active = "oldest")
        channel_dialogs = []
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            if chat and chat.type.name in ("CHANNEL", "SUPERGROUP", "GROUP"):
                channel_dialogs.append(chat)

        # Leave the least-recently-active ones (end of list)
        targets = channel_dialogs[-count:] if len(channel_dialogs) > count else channel_dialogs
        for chat in targets:
            try:
                await client.leave_chat(chat.id)
                left += 1
                logger.info(f"Account {phone}: auto-left '{getattr(chat, 'title', chat.id)}' to free up channel slot")
                await asyncio.sleep(1.5)
            except Exception as le:
                logger.debug(f"Account {phone}: could not leave {chat.id}: {le}")
    except Exception as e:
        logger.warning(f"Account {phone}: _auto_leave_oldest_channels error: {e}")
    return left


async def _join_and_cache(client: Client, channel_link: str, phone: str) -> dict:
    """
    Join a channel (or confirm membership) and cache the peer for numeric access.

    Handles:
    - Direct join success
    - USER_ALREADY_PARTICIPANT (already a member)
    - CHANNELS_TOO_MUCH (auto-leaves 10 oldest channels then retries once)
    - INVITE_REQUEST_SENT (approval required — waits 10 s then checks again)
    - FloodWait
    """
    logger.info(f"Account {phone}: joining channel via {channel_link!r}")
    try:
        await client.join_chat(channel_link)
        logger.info(f"Account {phone}: joined channel directly ✅")
        # Re-cache the peer by numeric ID after join
        await asyncio.sleep(1)
        try:
            await client.get_chat(channel_link)
        except Exception:
            pass
        return {"ok": True, "joined": True, "note": "Joined channel directly"}

    except FloodWait as e:
        wait_secs = e.value
        # Hard cap: if Telegram wants us to wait more than 5 min, the account
        # is already heavily rate-limited — skip this channel immediately rather
        # than blocking the whole campaign for 20+ minutes.
        if wait_secs > 300:
            logger.warning(
                f"Account {phone}: FloodWait {wait_secs}s > 300s cap — skipping channel, "
                f"account is rate-limited"
            )
            return {"ok": False, "error": f"FLOOD_WAIT_{wait_secs}", "rate_limited": True}
        logger.warning(f"Account {phone}: FloodWait {wait_secs}s during join_chat — waiting…")
        await asyncio.sleep(wait_secs + 5)
        try:
            await client.join_chat(channel_link)
            await asyncio.sleep(1)
            try:
                await client.get_chat(channel_link)
            except Exception:
                pass
            return {"ok": True, "joined": True, "note": "Joined after FloodWait"}
        except FloodWait as ex2:
            # Second consecutive FloodWait — account is rate-limited.
            # Return rate_limited=True so the caller skips remaining channels
            # instead of waiting another 20 minutes.
            logger.warning(
                f"Account {phone}: second FloodWait {ex2.value}s — account rate-limited, "
                f"skipping channel"
            )
            return {"ok": False, "error": f"FLOOD_WAIT_{ex2.value}", "rate_limited": True}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    except Exception as e:
        err = str(e)
        err_upper = err.upper()

        # ── Too many channels — auto-leave 10 oldest and retry ────────────────
        if "CHANNELS_TOO_MUCH" in err_upper or "channels_too_much" in err.lower():
            logger.warning(
                f"Account {phone}: CHANNELS_TOO_MUCH — auto-leaving 10 oldest channels and retrying"
            )
            left_count = await _auto_leave_oldest_channels(client, phone, count=10)
            logger.info(f"Account {phone}: auto-left {left_count} channel(s) — retrying join")
            try:
                await client.join_chat(channel_link)
                logger.info(f"Account {phone}: joined after auto-leave ✅")
                await asyncio.sleep(1)
                try:
                    await client.get_chat(channel_link)
                except Exception:
                    pass
                return {
                    "ok": True, "joined": True,
                    "note": f"Joined after auto-leaving {left_count} channels (CHANNELS_TOO_MUCH)"
                }
            except Exception as retry_e:
                retry_err = str(retry_e)
                logger.warning(f"Account {phone}: retry after auto-leave also failed: {retry_err}")
                return {"ok": False, "error": f"CHANNELS_TOO_MUCH — auto-left {left_count} channels but retry failed: {retry_err}"}

        # ── Already a member ──────────────────────────────────────────────────
        if "USER_ALREADY_PARTICIPANT" in err_upper or "already" in err.lower():
            logger.info(f"Account {phone}: already a member — caching peer")
            # Still need to cache the peer so numeric chat_id works
            try:
                await client.get_chat(channel_link)
            except Exception:
                pass
            return {"ok": True, "joined": False}

        # ── Approval-required channel ─────────────────────────────────────────
        if (
            "INVITE_REQUEST_SENT" in err_upper or
            "invite_request_sent" in err.lower() or
            "request_sent" in err.lower()
        ):
            logger.info(
                f"Account {phone}: join request sent — will retry up to 3 times..."
            )
            # Retry every 5 seconds, up to 3 attempts (15 seconds total)
            for attempt in range(3):
                await asyncio.sleep(5)
                try:
                    await client.get_chat(channel_link)
                    logger.info(f"Account {phone}: approved after attempt {attempt+1} ✅")
                    return {"ok": True, "joined": True, "note": f"Approved after join request (attempt {attempt+1})"}
                except Exception as ex2:
                    ex2_upper = str(ex2).upper()
                    logger.info(
                        f"Account {phone}: attempt {attempt+1}/3 — still waiting "
                        f"({type(ex2).__name__})"
                    )
                    if attempt < 2:
                        continue
                    # Final attempt failed
                    if (
                        "CHANNEL_PRIVATE" in ex2_upper or
                        "NOT_PARTICIPANT" in ex2_upper or
                        "PEER_ID_INVALID" in ex2_upper
                    ):
                        return {
                            "ok": False,
                            "error": "Join request pending — not yet approved by channel admin"
                        }
                    return {"ok": True, "joined": True, "note": "Join request may have been approved"}

        # ── Session revoked / dead session ────────────────────────────────────
        if isinstance(e, _DEAD_SESSION_ERRORS) or "SESSION_REVOKED" in err.upper() or "SESSION_EXPIRED" in err.upper():
            logger.warning(f"Account {phone}: 💀 session revoked/dead — will be auto-disabled")
            return {"ok": False, "error": err, "revoked": True}

        # ── Account frozen by Telegram ────────────────────────────────────────
        if "FROZEN_METHOD_INVALID" in err.upper() or "frozen_method_invalid" in err.lower():
            logger.warning(f"Account {phone}: ❄️ account is FROZEN by Telegram — will be auto-disabled")
            return {"ok": False, "error": err, "frozen": True}

        logger.warning(f"Account {phone}: join failed ({type(e).__name__}): {err}")
        return {"ok": False, "error": err}


def _is_not_member_error(e: Exception) -> bool:
    """Return True if the exception means the account is not a channel member."""
    if isinstance(e, (ChannelPrivate, UserNotParticipant, ChatWriteForbidden,
                      PeerIdInvalid, UserBannedInChannel)):
        return True
    err_upper = str(e).upper()
    keywords = (
        "PEER ID INVALID",
        "PEER_ID_INVALID",
        "CHANNEL_PRIVATE",
        "CHANNEL PRIVATE",
        "NOT_PARTICIPANT",
        "NOT PARTICIPANT",
        "CHAT_WRITE_FORBIDDEN",
        "CHAT WRITE FORBIDDEN",
        "USER_BANNED_IN_CHANNEL",
        "USER BANNED IN CHANNEL",
        "YOU ARE NOT A MEMBER",
    )
    return any(kw in err_upper for kw in keywords)


async def _ai_verify_channel(
    client: Client,
    channel: str,
    phone: str,
    campaign_id: int,
    account_id: int,
):
    """
    After joining a channel, look for verification button messages in:
    1. The channel itself (pinned / recent messages with inline buttons)
    2. Bot DMs — a verification bot may have messaged the account directly
    Clicks the AI-selected button to complete verification.
    """
    from services.ai_helper import ai_pick_verification_button

    async def _try_click_verification(msgs, source_label: str):
        """Iterate messages, find ones with inline buttons, let AI pick and click."""
        for msg in msgs:
            if not (msg.reply_markup and msg.reply_markup.inline_keyboard):
                continue
            flat_buttons = []
            for row in msg.reply_markup.inline_keyboard:
                flat_buttons.extend(row)
            # Only process buttons that have callback_data (not URL buttons)
            cb_buttons = [b for b in flat_buttons if b.callback_data]
            if not cb_buttons:
                continue
            btn_labels = [b.text for b in flat_buttons]
            msg_text = msg.text or msg.caption or ""
            btn_idx = await ai_pick_verification_button(btn_labels, msg_text)
            if btn_idx is None:
                btn_idx = 0
            # Pick from cb_buttons if possible
            target = flat_buttons[btn_idx] if btn_idx < len(flat_buttons) else cb_buttons[0]
            if not target.callback_data:
                target = cb_buttons[0]
            try:
                await client.request_callback_answer(
                    chat_id=msg.chat.id,
                    message_id=msg.id,
                    callback_data=target.callback_data,
                )
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "ai_verify", "success",
                    f"AI clicked '{target.text}' ({source_label})"
                )
                logger.info(
                    f"Account {phone}: AI verified via {source_label} "
                    f"by clicking '{target.text}'"
                )
                return True
            except Exception as ce:
                logger.debug(f"Account {phone}: click failed ({source_label}): {ce}")
        return False


async def _ai_verify_bot_dialog(
    client: Client,
    bot_username: str,
    phone: str,
    campaign_id: int,
    account_id: int,
) -> bool:
    try:
        async for msg in client.get_chat_history(bot_username, limit=15):
            if getattr(msg, "outgoing", False):
                continue
            text = (msg.text or msg.caption or "").strip().lower()
            if any(k in text for k in ("verify", "verification", "subscribe", "subscribed", "confirm", "continue", "done", "start", "join")):
                if await _click_confirmation_button(client, bot_username, phone, campaign_id, account_id):
                    return True
                if await _ai_verify_channel(client, bot_username, phone, campaign_id, account_id):
                    return True
        return await _ai_verify_channel(client, bot_username, phone, campaign_id, account_id)
    except Exception as e:
        logger.warning(f"Account {phone}: bot dialog AI verify failed: {e}")
        return False

    # 1. Check channel history for verification messages
    try:
        channel_msgs = []
        async for msg in client.get_chat_history(channel, limit=10):
            channel_msgs.append(msg)
        if await _try_click_verification(channel_msgs, "channel"):
            return
    except Exception as e:
        logger.debug(f"Account {phone}: channel history check failed: {e}")

    # 2. Check recent bot DMs — look for messages from bots received after joining
    try:
        async for dialog in client.get_dialogs(limit=15):
            if not dialog.chat:
                continue
            # Only check bot chats that have new unread messages
            if not (hasattr(dialog.chat, 'type') and str(dialog.chat.type) in ('bot', 'ChatType.BOT', 'private')):
                continue
            if dialog.unread_messages_count == 0:
                continue
            bot_msgs = []
            async for msg in client.get_chat_history(dialog.chat.id, limit=5):
                bot_msgs.append(msg)
            if await _try_click_verification(bot_msgs, f"bot DM ({dialog.chat.id})"):
                return
    except Exception as e:
        logger.debug(f"Account {phone}: bot DM check failed: {e}")


async def _increment_post_views(client: Client, chat_id, message_id: int):
    """Increment the view counter on a channel post (makes engagement look organic)."""
    try:
        peer = await client.resolve_peer(chat_id)
        await client.invoke(
            GetMessagesViews(peer=peer, id=[message_id], increment=True)
        )
    except Exception:
        pass


async def _do_react(client: Client, chat_id, message_id: int, emoji: str) -> dict:
    try:
        await client.send_reaction(
            chat_id=chat_id,
            message_id=message_id,
            emoji=emoji,
        )
        return {"ok": True}
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await client.send_reaction(chat_id=chat_id, message_id=message_id, emoji=emoji)
            return {"ok": True}
        except Exception as ex:
            if _is_not_member_error(ex):
                return {"ok": False, "error": "NOT_MEMBER"}
            return {"ok": False, "error": str(ex)}
    except (UserDeactivated, AuthKeyUnregistered):
        return {"ok": False, "error": "Account deactivated or session expired"}
    except Exception as e:
        if _is_not_member_error(e):
            return {"ok": False, "error": "NOT_MEMBER"}
        err = str(e)
        if "REACTION" in err.upper() or "EMOJI" in err.upper():
            return {"ok": False, "error": f"Reaction '{emoji}' not supported on this post"}
        logger.warning(f"react error ({type(e).__name__}): {err}")
        return {"ok": False, "error": err}


async def _find_vote_message(client: Client, chat_id, message_id: int, button_index: int = 0):
    """
    Return (actual_msg_id, button) where button is an InlineKeyboardButton
    with callback_data set, or (None, None) if not found.

    Uses pyrofork's high-level get_chat_history (most reliable) then
    get_messages as a fallback.

    Button selection priority:
      1. Button whose text contains a vote keyword at the requested index
      2. All callback buttons (including image-only/empty-text buttons) — pick by button_index
      3. First callback button as last resort
    """
    _VOTE_KEYWORDS = ("vote", "голос", "проголос", "vot", "نظر")

    def _flat_buttons(msg):
        if not msg or not getattr(msg, "reply_markup", None):
            return []
        rows = getattr(msg.reply_markup, "inline_keyboard", None) or []
        return [btn for row in rows for btn in row]

    def _pick_button(btns, idx):
        """
        Pick the right button from a list:
        - All buttons with callback_data (includes image-only buttons with empty text)
        - Among those, select by idx; fall back to idx=0
        Text-keyword matching is tried first; if no keyword matches we use idx directly
        so that image-only button rows are handled correctly.
        """
        cb_btns = [b for b in btns if b.callback_data]
        if not cb_btns:
            return None

        # Check if ANY button has vote-keyword text
        keyword_btns = [
            b for b in cb_btns
            if any(kw in (b.text or "").lower() for kw in _VOTE_KEYWORDS)
        ]
        if keyword_btns:
            # There are labelled vote buttons — pick by index within them
            chosen = keyword_btns[idx] if idx < len(keyword_btns) else keyword_btns[0]
            return chosen

        # No text labels (image buttons or unlabelled) — pick by index from ALL cb buttons
        chosen = cb_btns[idx] if idx < len(cb_btns) else cb_btns[0]
        return chosen

    # ── Stage 1: check the EXACT target message first ───────────────────────────
    # If the exact message exists but has no buttons, stop immediately.
    # Do NOT spill to adjacent messages — spillover causes the bot to click
    # unrelated buttons on nearby messages (e.g. a ❤️ like-counter on msg+1).
    # Only search neighbors if the exact message was not found at all
    # (handles VTH threading where the vote message ID may be off by 1-2).
    exact_msg_found = False
    try:
        exact = await client.get_messages(chat_id, message_id)
        if exact and not getattr(exact, "empty", False):
            exact_msg_found = True
            flat = _flat_buttons(exact)
            btn = _pick_button(flat, button_index)
            if btn:
                btn_label = btn.text or f"<image btn #{button_index}>"
                logger.info(f"_find_vote_message: ✅ found '{btn_label}' (idx={button_index}) on exact msg {message_id}")
                return message_id, btn
            else:
                # Message exists but has no inline buttons — do not spill over
                logger.warning(
                    f"_find_vote_message: msg {message_id} exists but has no inline buttons — "
                    f"not searching neighbors (use 'react' campaign for reaction-based votes)"
                )
                return None, None
    except Exception as e:
        logger.debug(f"_find_vote_message exact get_messages({message_id}): {e}")

    # ── Stage 2: exact message not found — search nearby (VTH threading) ────────
    # This only runs when the exact message was inaccessible (e.g. non-member,
    # or VTH-style bot posted the vote at a slightly different message ID).
    logger.warning(f"_find_vote_message: exact msg {message_id} not found — searching nearby (chat={chat_id})")

    for pass_offset, pass_limit in [
        (message_id + 11, 20),   # covers message_id+10 down to message_id-9
        (message_id + 21, 10),   # covers message_id+20 down to message_id+11
    ]:
        try:
            async for m in client.get_chat_history(chat_id, limit=pass_limit, offset_id=pass_offset):
                if m.id < message_id - 5:
                    break
                flat = _flat_buttons(m)
                btn = _pick_button(flat, button_index)
                if btn:
                    btn_label = btn.text or f"<image btn #{button_index}>"
                    logger.info(f"_find_vote_message: ✅ found '{btn_label}' (idx={button_index}) on nearby msg {m.id}")
                    return m.id, btn
        except Exception as e:
            logger.warning(f"_find_vote_message history (offset={pass_offset}): {type(e).__name__}: {e}")

    # ── Stage 3: get_messages fallback on nearby range ────────────────────────
    for mid in range(message_id - 2, message_id + 6):
        if mid == message_id:
            continue  # already checked above
        try:
            m = await client.get_messages(chat_id, mid)
            if not m or getattr(m, "empty", False):
                continue
            flat = _flat_buttons(m)
            btn = _pick_button(flat, button_index)
            if btn:
                btn_label = btn.text or f"<image btn #{button_index}>"
                logger.info(f"_find_vote_message: ✅ get_messages found '{btn_label}' (idx={button_index}) on msg {m.id}")
                return m.id, btn
        except Exception as e:
            logger.debug(f"_find_vote_message get_messages({mid}): {e}")

    logger.warning(f"_find_vote_message: ❌ no button found for chat={chat_id} msg={message_id}")
    return None, None


async def _do_vote(client: Client, chat_id, message_id: int, button_index: int) -> dict:
    # ── Step 0: Check if this is a Telegram native poll ─────────────────────────
    # Native polls have no inline keyboard — they must be voted with vote_poll(),
    # not request_callback_answer(). Without this check the code would spill over
    # to the next message (which may have a ❤️ reaction button) and click that
    # instead, giving fake "vote ✅" results while the actual poll is untouched.
    async def _try_native_poll():
        try:
            msg = await client.get_messages(chat_id, message_id)
            if not msg or getattr(msg, "empty", False):
                return None
            poll = getattr(msg, "poll", None)
            if not poll:
                return None  # not a native poll — fall through to button path
            answers = getattr(poll, "options", None) or []
            if not answers:
                return {"ok": False, "error": "Native poll has no options"}
            # Pick option by button_index (clamp to valid range)
            idx = min(button_index, len(answers) - 1)
            option_text = getattr(answers[idx], "text", str(idx))
            logger.info(
                f"vote: native Telegram poll on msg {message_id}, "
                f"voting option [{idx}]: {option_text!r}"
            )
            await client.vote_poll(chat_id, message_id, options=[idx])
            return {"ok": True, "note": f"native poll: {option_text!r}"}
        except Exception as e:
            err = str(e)
            if "VOTE_ALREADY_USED" in err or "already" in err.lower():
                return {"ok": False, "error": f"Already voted: {err}", "already_voted": True}
            if _is_not_member_error(e):
                return {"ok": False, "error": "NOT_MEMBER"}
            logger.debug(f"vote: native poll error: {err}")
            return None  # fall through to button path on unknown errors

    poll_result = await _try_native_poll()
    if poll_result is not None:
        return poll_result

    # ── Step 1: Inline-button vote (bot polls / @VoteBot-style) ─────────────────
    async def _attempt():
        actual_msg_id, btn = await _find_vote_message(client, chat_id, message_id, button_index)

        if actual_msg_id is None or btn is None:
            return {"ok": False, "error": "No inline buttons on this message"}

        btn_label = btn.text or f"<image btn #{button_index}>"
        if actual_msg_id != message_id:
            logger.info(f"vote: using msg {actual_msg_id} instead of {message_id} (btn='{btn_label}' idx={button_index})")

        answer = await client.request_callback_answer(
            chat_id=chat_id,
            message_id=actual_msg_id,
            callback_data=btn.callback_data,
        )

        # Log the bot's response text so we can see what actually happened
        answer_text = getattr(answer, "text", None) or ""
        if answer_text:
            logger.info(f"vote callback response: {answer_text!r}")

        # Detect "already voted" — bot returned an alert rather than counting the vote
        _ALREADY_VOTED = (
            "already voted", "already cast",
            "вы уже", "уже проголосовали", "уже голосовали",
            "already participated", "vote again",
        )
        answer_lower = answer_text.lower()
        if any(phrase in answer_lower for phrase in _ALREADY_VOTED):
            logger.info(f"vote: account already voted — bot said: {answer_text!r}")
            return {"ok": False, "error": f"Already voted: {answer_text}", "already_voted": True}

        return {"ok": True, "note": answer_text}

    try:
        return await _attempt()
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            return await _attempt()
        except Exception as ex:
            if _is_not_member_error(ex):
                return {"ok": False, "error": "NOT_MEMBER"}
            return {"ok": False, "error": str(ex)}
    except (UserDeactivated, AuthKeyUnregistered):
        return {"ok": False, "error": "Account deactivated or session expired"}
    except Exception as e:
        if _is_not_member_error(e):
            return {"ok": False, "error": "NOT_MEMBER"}
        logger.warning(f"vote error ({type(e).__name__}): {str(e)}")
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Custom / Premium Emoji Pack Reactions
# ─────────────────────────────────────────────────────────────────────────────

async def _get_emoji_pack_ids(client: Client, short_name: str) -> list:
    """
    Fetch all custom-emoji document IDs from a Telegram emoji/sticker set.
    short_name is the set's short name, e.g. 'Callmejija_by_fStikBot'.
    Returns a list of int document IDs, or [] on failure.
    """
    try:
        result = await client.invoke(
            GetStickerSet(
                stickerset=InputStickerSetShortName(short_name=short_name),
                hash=0,
            )
        )
        ids = [doc.id for doc in result.documents]
        logger.info(f"Emoji pack '{short_name}': loaded {len(ids)} emoji IDs")
        return ids
    except Exception as e:
        logger.warning(f"_get_emoji_pack_ids({short_name!r}): {e}")
        return []


async def _do_react_with_custom_emoji(
    client: Client, chat_id, message_id: int, custom_emoji_id: int
) -> dict:
    """
    Send a custom-emoji reaction (premium/pack emoji) using the raw Telegram API.
    Non-premium accounts CAN use these if the channel has enabled them.
    """
    async def _send():
        peer = await client.resolve_peer(chat_id)
        await client.invoke(
            RawSendReaction(
                peer=peer,
                msg_id=message_id,
                big=False,
                reaction=[RawReactionCustomEmoji(document_id=custom_emoji_id)],
            )
        )

    try:
        await _send()
        return {"ok": True}
    except FloodWait as e:
        await asyncio.sleep(e.value)
        try:
            await _send()
            return {"ok": True}
        except Exception as ex:
            if _is_not_member_error(ex):
                return {"ok": False, "error": "NOT_MEMBER"}
            return {"ok": False, "error": str(ex)}
    except (UserDeactivated, AuthKeyUnregistered):
        return {"ok": False, "error": "Account deactivated or session expired"}
    except Exception as e:
        if _is_not_member_error(e):
            return {"ok": False, "error": "NOT_MEMBER"}
        err = str(e)
        logger.warning(f"custom_emoji_react error ({type(e).__name__}): {err}")
        return {"ok": False, "error": err}


async def _get_channel_allowed_custom_emoji_ids(client: Client, numeric_chat_id: int) -> list:
    """
    Return the list of custom-emoji document IDs the channel explicitly allows as reactions.
    Returns [] if the channel allows ALL reactions (ChatReactionsAll) or on any error,
    so callers can fall back to the full pack list.
    Returns None if reactions are fully disabled (ChatReactionsNone).
    """
    try:
        peer = await client.resolve_peer(numeric_chat_id)
        if not hasattr(peer, "channel_id"):
            return []
        full = await client.invoke(
            GetFullChannel(channel=InputChannel(
                channel_id=peer.channel_id,
                access_hash=peer.access_hash,
            ))
        )
        avail = full.full_chat.available_reactions
        if isinstance(avail, ChatReactionsSome):
            ids = [r.document_id for r in avail.reactions
                   if isinstance(r, RawReactionCustomEmoji)]
            logger.debug(f"Channel allowed custom emoji reactions: {ids}")
            return ids
        # ChatReactionsAll → any emoji OK; ChatReactionsNone → no reactions
        return []
    except Exception as e:
        logger.debug(f"_get_channel_allowed_custom_emoji_ids failed: {e}")
        return []


async def run_emoji_pack_campaign(
    campaign_id: int,
    accounts: List[dict],
    chat_id,
    message_id: int,
    post_url: str,
    emoji_pack_short_name: str,
    notify_user_id: int,
    bot_notify_callback=None,
    delay_seconds: float = 1.5,
):
    """
    React to a post using random emojis from a Telegram emoji pack.
    Each account picks a random emoji from the pack and reacts with it.
    Non-premium accounts are supported as long as the channel allows the pack.
    """
    await db.update_campaign_status(campaign_id, "running")
    task = asyncio.current_task()
    _running[campaign_id] = task

    total = len(accounts)
    success = 0
    fail = 0
    emoji_ids: list = []          # all doc IDs from the pack (fetched once)
    allowed_ids: list = []        # channel's permitted reaction doc IDs (fetched once)
    allowed_ids_fetched = False   # whether we've attempted the channel fetch
    resolved_numeric_id = None    # numeric channel ID reused across accounts
    channel_link = _get_channel_link(post_url)
    is_private = isinstance(chat_id, int)
    error_tally: dict = {}        # {error_str: count} for the summary report

    try:
        for account in accounts:
            if _running.get(campaign_id) is None:
                break
            _db_status = await db.get_campaign_status(campaign_id)
            if _db_status == "stopped":
                logger.info(f"Campaign {campaign_id}: stop signal received from DB — halting")
                _running.pop(campaign_id, None)
                break

            account_id = account["id"]
            session_str = account["session_string"]
            phone = account.get("phone") or f"ID:{account_id}"

            client = await get_client_for_session(account_id, session_str)
            if not client:
                await db.set_account_active(account_id, False)
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "connect", "failed", "Could not connect"
                )
                continue

            # Step 1: fetch emoji pack IDs (only once; reuse across accounts)
            if not emoji_ids:
                emoji_ids = await _get_emoji_pack_ids(client, emoji_pack_short_name)
                if not emoji_ids:
                    err_msg = f"Could not load emoji pack '{emoji_pack_short_name}'"
                    logger.error(f"Campaign {campaign_id}: {err_msg}")
                    await db.update_campaign_status(campaign_id, "failed")
                    if bot_notify_callback:
                        await bot_notify_callback(
                            notify_user_id,
                            f"❌ Emoji Pack Campaign #{campaign_id} failed: {err_msg}"
                        )
                    return

            # Step 2: resolve peer + auto-join the channel
            # Always call get_chat to ensure Pyrogram's peer cache is populated.
            # resolve_peer() fails with PeerIdInvalid if the account has never
            # interacted with the channel — get_chat() forces the lookup.
            # Once we have the numeric ID from any account, reuse it for the rest.
            actual_chat_id = resolved_numeric_id or chat_id
            try:
                chat_info = await client.get_chat(chat_id)
                actual_chat_id = chat_info.id  # numeric ID is more reliable than username
                if resolved_numeric_id is None:
                    resolved_numeric_id = actual_chat_id
                    logger.info(f"Campaign {campaign_id}: resolved channel {chat_id} → {resolved_numeric_id}")
            except Exception as ge:
                logger.debug(f"Account {phone}: get_chat failed ({ge}); using {'numeric' if resolved_numeric_id else 'original'} ID")

            if _is_valid_join_link(channel_link):
                try:
                    join_res = await _join_and_cache(client, channel_link, phone)
                    if not join_res["ok"]:
                        logger.warning(
                            f"Account {phone}: join failed ({join_res.get('error')}) — "
                            "attempting reaction anyway"
                        )
                    elif join_res.get("joined"):
                        # Just joined — short pause so Telegram registers membership
                        await asyncio.sleep(2)
                except Exception as je:
                    logger.debug(f"Account {phone}: join exception (non-fatal): {je}")

            # Step 2b: fetch the channel's allowed reaction IDs (once, after we have numeric ID)
            if not allowed_ids_fetched and resolved_numeric_id:
                allowed_ids = await _get_channel_allowed_custom_emoji_ids(client, resolved_numeric_id)
                allowed_ids_fetched = True
                if allowed_ids:
                    # Intersect with pack IDs — only use what the channel explicitly allows
                    pack_set = set(emoji_ids)
                    valid_ids = [i for i in allowed_ids if i in pack_set]
                    if valid_ids:
                        logger.info(
                            f"Campaign {campaign_id}: channel allows {len(allowed_ids)} custom emoji; "
                            f"{len(valid_ids)} match the pack → using those"
                        )
                        emoji_ids = valid_ids
                    else:
                        # Channel has some allowed reactions but none match our pack —
                        # use the full allowed list anyway so something will work
                        logger.warning(
                            f"Campaign {campaign_id}: channel allowed reactions don't overlap with pack; "
                            f"using all {len(allowed_ids)} channel-allowed IDs"
                        )
                        emoji_ids = allowed_ids
                else:
                    logger.info(f"Campaign {campaign_id}: channel allows all reactions — using full pack")

            # Step 3: pick a random emoji and react
            custom_emoji_id = random.choice(emoji_ids)
            result = await _do_react_with_custom_emoji(client, actual_chat_id, message_id, custom_emoji_id)

            if result["ok"]:
                success += 1
                await db.increment_campaign_counts(campaign_id, success=1)
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "emoji_pack_react", "success",
                    f"Reacted with custom emoji ID {custom_emoji_id} from pack '{emoji_pack_short_name}'"
                )
                logger.info(f"Account {phone}: emoji pack react ✅ (id={custom_emoji_id})")
            else:
                err = result.get("error", "Unknown error")
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "emoji_pack_react", "failed", err
                )
                logger.warning(f"Account {phone}: emoji pack react ❌ ({err})")
                # Tally unique errors for the summary report
                short_err = err[:80]
                error_tally[short_err] = error_tally.get(short_err, 0) + 1

            await asyncio.sleep(max(2.0, delay_seconds))

    except asyncio.CancelledError:
        await db.update_campaign_status(campaign_id, "stopped")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"⏹ Emoji Pack Campaign #{campaign_id} stopped.\n✅ {success} · ❌ {fail}"
            )
        return
    finally:
        _running.pop(campaign_id, None)

    final_status = "completed" if fail == 0 else ("failed" if success == 0 else "completed")
    await db.update_campaign_status(campaign_id, final_status)

    # Build error breakdown for the report
    error_lines = ""
    if error_tally:
        top = sorted(error_tally.items(), key=lambda x: -x[1])[:3]
        error_lines = "\n\n⚠️ <b>Top errors:</b>\n" + "\n".join(
            f"  • [{cnt}x] <code>{e}</code>" for e, cnt in top
        )

    if bot_notify_callback:
        await bot_notify_callback(
            notify_user_id,
            f"✅ Emoji Pack Campaign #{campaign_id} finished!\n"
            f"📊 Total accounts: {total}\n"
            f"✅ Success: {success}\n"
            f"❌ Failed: {fail}\n"
            f"🎭 Pack: <code>{emoji_pack_short_name}</code>"
            + error_lines,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Referral Campaign — join a list of channels, AI-assisted verification
# ─────────────────────────────────────────────────────────────────────────────

async def run_referral_campaign(
    campaign_id: int,
    accounts: List[dict],
    channel_list: List[str],
    ai_assisted: bool = True,
    notify_user_id: int = 0,
    bot_notify_callback=None,
    delay_seconds: float = 1.5,
):
    """
    Join all specified channels with every account.
    If a channel sends a verification message with buttons, AI picks the right button.
    """
    from services.ai_helper import ai_pick_verification_button

    await db.update_campaign_status(campaign_id, "running")
    task = asyncio.current_task()
    _running[campaign_id] = task

    total = len(accounts) * len(channel_list)
    success = 0
    fail = 0

    try:
        for account in accounts:
            if _running.get(campaign_id) is None:
                break
            _db_status = await db.get_campaign_status(campaign_id)
            if _db_status == "stopped":
                logger.info(f"Campaign {campaign_id}: stop signal received from DB — halting")
                _running.pop(campaign_id, None)
                break

            account_id = account["id"]
            session_str = account["session_string"]
            phone = account.get("phone") or f"ID:{account_id}"

            client = await get_client_for_session(account_id, session_str)
            if not client:
                await db.set_account_active(account_id, False)
                fail += len(channel_list)
                await db.increment_campaign_counts(campaign_id, fail=len(channel_list))
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "connect", "failed", "Could not connect"
                )
                continue

            for channel in channel_list:
                channel = channel.strip()
                if not channel:
                    continue
                try:
                    join_result = await _join_and_cache(client, channel, phone)
                    if join_result["ok"]:
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "referral_join", "success",
                            join_result.get("note", "Joined")
                        )
                        success += 1
                        await db.increment_campaign_counts(campaign_id, success=1)

                        # AI-assisted: look for verification messages in channel + bot DMs
                        if ai_assisted:
                            await asyncio.sleep(2)
                            await _ai_verify_channel(
                                client, channel, phone,
                                campaign_id, account_id
                            )
                    else:
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "referral_join", "failed",
                            join_result.get("error", "Join failed")
                        )
                        fail += 1
                        await db.increment_campaign_counts(campaign_id, fail=1)
                except Exception as e:
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "referral_join", "failed", str(e)
                    )
                    fail += 1
                    await db.increment_campaign_counts(campaign_id, fail=1)
                await asyncio.sleep(2)

            # Delay between accounts (configurable slow mode)
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

    except asyncio.CancelledError:
        await db.update_campaign_status(campaign_id, "stopped")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"⏹ Referral Campaign #{campaign_id} stopped.\n✅ {success} · ❌ {fail}"
            )
        return
    finally:
        _running.pop(campaign_id, None)

    final_status = "completed" if fail == 0 else ("failed" if success == 0 else "completed")
    await db.update_campaign_status(campaign_id, final_status)

    if bot_notify_callback:
        emoji = "✅" if final_status == "completed" else "⚠️"
        await bot_notify_callback(
            notify_user_id,
            f"{emoji} Referral Campaign #{campaign_id} finished!\n"
            f"📊 Accounts × Channels: {total}\n"
            f"✅ Joined: {success}  ❌ Failed: {fail}"
        )


async def _solve_captcha_with_ai(client, captcha_msg, phone: str) -> Optional[str]:
    """Download a captcha photo from a Telegram message and solve it with Gemini Vision AI."""
    try:
        import config
        from google import genai
        from google.genai import types as genai_types

        api_key = config.GEMINI_API_KEY
        if not api_key:
            logger.error(f"Account {phone}: GEMINI_API_KEY not set — cannot solve captcha")
            return None

        img_buf = await client.download_media(captcha_msg, in_memory=True)
        if not img_buf:
            logger.warning(f"Account {phone}: captcha image download returned empty")
            return None
        img_bytes = bytes(img_buf.getvalue()) if hasattr(img_buf, "getvalue") else bytes(img_buf)
        if not img_bytes:
            logger.warning(f"Account {phone}: captcha image bytes empty")
            return None

        mime_type = "image/jpeg"
        doc = getattr(captcha_msg, "document", None)
        if doc and getattr(doc, "mime_type", None):
            mime_type = doc.mime_type

        genai_client = genai.Client(api_key=api_key)
        response = genai_client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                genai_types.Part.from_text(
                    "This is a captcha image from a Telegram bot. "
                    "Read the characters shown in the image very carefully — they may be distorted or have noise. "
                    "Reply with ONLY the exact characters, no spaces, no punctuation, no explanation."
                ),
                genai_types.Part.from_bytes(data=img_bytes, mime_type=mime_type),
            ],
        )

        solved = response.text.strip().replace(" ", "").replace("\n", "")
        logger.info(f"Account {phone}: Gemini solved captcha → '{solved}'")
        return solved if solved else None
    except Exception as e:
        logger.error(f"Account {phone}: captcha AI error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Bot Referral Campaign — open bot with referral link, join found channels, AI-verify
# ─────────────────────────────────────────────────────────────────────────────

async def run_bot_referral_campaign(
    campaign_id: int,
    accounts: List[dict],
    bot_ref_link: str,
    notify_user_id: int = 0,
    bot_notify_callback=None,
    delay_seconds: float = 1.5,
):
    """
    Bot Referral Campaign:
    Each account sends /start <param> to the bot from the referral link,
    reads the bot response to extract channel links, joins them, and AI-verifies.
    """
    await db.update_campaign_status(campaign_id, "running")
    task = asyncio.current_task()
    _running[campaign_id] = task

    bot_username, start_param, app_name = parse_bot_ref_link(bot_ref_link)
    is_webapp_link = bool(app_name)  # True for https://t.me/Bot/App?startapp=XXX
    if not bot_username:
        logger.error(f"run_bot_referral_campaign: cannot parse bot from {bot_ref_link!r}")
        await db.update_campaign_status(campaign_id, "failed")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"❌ Cannot parse bot username from: <code>{bot_ref_link}</code>"
            )
        _running.pop(campaign_id, None)
        return

    start_command = f"/start {start_param}" if start_param else "/start"
    total = len(accounts)
    success = 0
    fail = 0

    # Semaphore: run up to 5 accounts concurrently to speed up the campaign
    # while avoiding flooding Telegram with too many simultaneous connections.
    sem = asyncio.Semaphore(5)

    async def _run_one_account(account):
        nonlocal success, fail
        account_id = account["id"]
        session_str = account["session_string"]
        phone = account.get("phone") or f"ID:{account_id}"

        if _running.get(campaign_id) is None:
            return

        async with sem:
            if _running.get(campaign_id) is None:
                return

            client = await get_client_for_session(account_id, session_str)
            if not client:
                await db.set_account_active(account_id, False)
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "connect", "failed", "Could not connect"
                )
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
                return

            try:
                # Step 0: Resolve the bot peer — Pyrogram raises PeerIdInvalid
                # if the account has NEVER interacted with this bot before.
                try:
                    await client.get_users(bot_username)
                    logger.debug(f"Account {phone}: peer resolved for @{bot_username}")
                except Exception as pe:
                    logger.debug(f"Account {phone}: peer pre-resolve warning (non-fatal): {pe}")

                # Step 1: Open the bot — either via RequestAppWebView (Mini App links)
                # or via the /start command (classic deep-links).
                # IMPORTANT: Do NOT send a plain /start before this — doing so
                # registers the user without the referral param and the bot won't
                # count the referral even if /start PARAM is sent right after.
                try:
                    await client.get_chat(bot_username)
                except Exception:
                    pass  # non-fatal; send_message will resolve the peer itself

                # Capture the latest existing message ID BEFORE we interact with the
                # bot, so we can filter out old history when polling for replies.
                _baseline_msg_id = 0
                try:
                    async for _bm in client.get_chat_history(bot_username, limit=1):
                        _baseline_msg_id = _bm.id
                        break
                except Exception:
                    pass

                webapp_opened = False
                sent_msg_id = 0  # will be updated after the start message

                if is_webapp_link and app_name:
                    # Mini App link: use RequestAppWebView so the bot receives the
                    # web_app_data event — this is what actually registers the referral.
                    try:
                        from pyrogram.raw.functions.messages import RequestAppWebView
                        from pyrogram.raw.types import InputBotAppShortName as _InputBotAppShortName
                        _peer = await client.resolve_peer(bot_username)
                        await client.invoke(
                            RequestAppWebView(
                                peer=_peer,
                                app=_InputBotAppShortName(bot_id=_peer, short_name=app_name),
                                platform="android",
                                write_allowed=True,
                                start_param=start_param or "",
                            )
                        )
                        webapp_opened = True
                        # Use the baseline msg ID so the polling only looks at new messages
                        sent_msg_id = _baseline_msg_id
                        logger.info(
                            f"Account {phone}: opened Mini App '{app_name}' of @{bot_username}"
                            f" (startapp={start_param!r})"
                        )
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "bot_start", "success",
                            f"Opened Mini App '{app_name}' via RequestAppWebView (startapp={start_param})"
                        )
                    except Exception as _wv_err:
                        logger.warning(
                            f"Account {phone}: RequestAppWebView failed ({_wv_err}), "
                            f"falling back to /start command"
                        )

                if not webapp_opened:
                    # Classic deep-link: send /start <param> (or just /start)
                    sent_msg = await client.send_message(bot_username, start_command)
                    sent_msg_id = sent_msg.id if sent_msg else _baseline_msg_id
                    logger.info(
                        f"Account {phone}: sent {start_command!r} to @{bot_username}"
                        f" (msg_id={sent_msg_id})"
                    )
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "bot_start", "success",
                        f"Sent '{start_command}' to @{bot_username}"
                    )

                # Poll for the bot's first response — check every 2 s for up to 30 s.
                # This handles bots that respond in 1 s as well as slow bots (15-20 s).
                _bot_replied = False
                for _poll in range(15):          # 15 × 2 s = 30 s max
                    await asyncio.sleep(2)
                    async for _pm in client.get_chat_history(bot_username, limit=5):
                        if sent_msg_id and _pm.id <= sent_msg_id:
                            break
                        if not getattr(_pm, "outgoing", False):
                            _bot_replied = True
                            break
                    if _bot_replied:
                        logger.info(f"Account {phone}: bot replied after ~{(_poll+1)*2}s")
                        break
                if not _bot_replied:
                    logger.warning(f"Account {phone}: bot @{bot_username} did not reply within 30s")

                # ── CAPTCHA DETECTION & AI SOLVING ───────────────────────────
                # Some bots (e.g. TgShark_Bot) respond to /start with an image
                # captcha that must be solved before the referral is registered.
                # Flow: detect photo + "Security Verification" text → download
                # image → Gemini Vision reads the characters → send answer back
                # → wait for "Captcha verified" → continue to channel joining.
                _used_captcha_photo_ids: set = set()
                for _cap_round in range(3):
                    _cap_photo_msg = None
                    _cap_is_captcha = False
                    async for _cm in client.get_chat_history(bot_username, limit=10):
                        if sent_msg_id and _cm.id < sent_msg_id:
                            break
                        if getattr(_cm, "outgoing", False):
                            continue
                        _cm_text = (
                            getattr(_cm, "text", "") or getattr(_cm, "caption", "") or ""
                        ).lower()
                        if "security verification" in _cm_text or "type the characters" in _cm_text:
                            _cap_is_captcha = True
                        if (
                            getattr(_cm, "photo", None)
                            and _cm.id not in _used_captcha_photo_ids
                            and _cap_photo_msg is None
                        ):
                            _cap_photo_msg = _cm

                    if not _cap_is_captcha:
                        break  # No captcha detected — proceed normally

                    if not _cap_photo_msg:
                        logger.warning(
                            f"Account {phone}: captcha text detected but no new photo found — stopping"
                        )
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "captcha_solve", "failed",
                            "Captcha text detected but no image to solve"
                        )
                        break

                    _used_captcha_photo_ids.add(_cap_photo_msg.id)
                    logger.info(
                        f"Account {phone}: captcha detected (round {_cap_round + 1}/3) — solving with Gemini Vision"
                    )
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "captcha_detect", "info",
                        f"Captcha detected (round {_cap_round + 1}/3) — solving with AI"
                    )

                    solved_text = await _solve_captcha_with_ai(client, _cap_photo_msg, phone)
                    if not solved_text:
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "captcha_solve", "failed",
                            f"Round {_cap_round + 1}: Gemini could not read captcha image"
                        )
                        break

                    _ans_baseline_id = _cap_photo_msg.id
                    await client.send_message(bot_username, solved_text)
                    logger.info(f"Account {phone}: sent captcha answer '{solved_text}' to @{bot_username}")

                    # Wait up to 16 s for bot to verify or reject the answer
                    _cap_verified = False
                    _cap_rejected = False
                    for _vp in range(8):
                        await asyncio.sleep(2)
                        async for _vrm in client.get_chat_history(bot_username, limit=5):
                            if _vrm.id <= _ans_baseline_id:
                                break
                            if getattr(_vrm, "outgoing", False):
                                continue
                            _vrm_text = (
                                getattr(_vrm, "text", "") or getattr(_vrm, "caption", "") or ""
                            ).lower()
                            if "verified" in _vrm_text or ("captcha" in _vrm_text and "✅" in _vrm_text):
                                _cap_verified = True
                                break
                            if any(w in _vrm_text for w in ("incorrect", "wrong", "invalid", "try again", "failed")):
                                _cap_rejected = True
                                break
                        if _cap_verified or _cap_rejected:
                            break

                    if _cap_verified:
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "captcha_solve", "success",
                            f"Captcha '{solved_text}' accepted — captcha verified by bot"
                        )
                        logger.info(f"Account {phone}: captcha verified — proceeding to channel joining")
                        await asyncio.sleep(3)
                        break  # Done with captcha — main loop handles channel joining
                    else:
                        reason = "rejected" if _cap_rejected else "no confirmation within 16s"
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "captcha_solve", "failed",
                            f"Round {_cap_round + 1}: answer '{solved_text}' {reason} — retrying"
                        )
                        logger.warning(
                            f"Account {phone}: captcha answer '{solved_text}' {reason}"
                        )
                        await asyncio.sleep(3)
                # ── END CAPTCHA HANDLING ──────────────────────────────────────

                # Steps 2-4 repeat: read channels → join → click "I subscribed" → check again
                # This handles bots that require multiple rounds of channel subscriptions.
                joined_any = False
                confirmed_any = False  # True if confirmation button was clicked at least once
                all_joined_links: set = set()

                for round_num in range(5):  # up to 5 rounds of subscription requirements
                    # Step 2: Read ONLY bot responses that arrived after our /start
                    # (skip our own outgoing messages and any older history)
                    bot_messages = []
                    async for msg in client.get_chat_history(bot_username, limit=10):
                        # Stop when we pass the point where we sent /start
                        if sent_msg_id and msg.id < sent_msg_id:
                            break
                        # Skip our own outgoing messages
                        if getattr(msg, "outgoing", False):
                            continue
                        bot_messages.append(msg)

                    # ── Raw message dump (for debugging join failures) ────────
                    for _dbg_msg in bot_messages:
                        _dbg_parts = []
                        _dbg_text = getattr(_dbg_msg, "text", None) or getattr(_dbg_msg, "caption", None)
                        if _dbg_text:
                            _dbg_parts.append(f"text={_dbg_text[:120]!r}")
                        _dbg_markup = getattr(_dbg_msg, "reply_markup", None)
                        if _dbg_markup:
                            for _row in (getattr(_dbg_markup, "inline_keyboard", None) or []):
                                for _btn in _row:
                                    _btn_url = getattr(_btn, "url", None)
                                    _btn_text = getattr(_btn, "text", "")
                                    _login_url = getattr(getattr(_btn, "login_url", None), "url", None)
                                    _web_app_url = getattr(getattr(_btn, "web_app", None), "url", None)
                                    _dbg_parts.append(
                                        f"btn={_btn_text!r} url={_btn_url!r} login_url={_login_url!r} web_app={_web_app_url!r}"
                                    )
                        if _dbg_parts:
                            logger.info(f"Account {phone}: RAW BOT MSG — " + " | ".join(_dbg_parts))
                    # ─────────────────────────────────────────────────────────

                    seen_links: set = set()
                    channel_links: list = []
                    for msg in bot_messages:
                        for lnk in _extract_channel_links_from_message(msg):
                            if lnk not in seen_links and lnk not in all_joined_links:
                                seen_links.add(lnk)
                                channel_links.append(lnk)

                    if not channel_links:
                        if round_num == 0:
                            # First round: no channels at all.
                            # Log what the bot actually said so we can diagnose.
                            _r0_texts = [
                                (m.text or m.caption or "")[:200]
                                for m in bot_messages if (m.text or m.caption or "")
                            ]
                            _r0_summary = " | ".join(_r0_texts) if _r0_texts else "<no text>"
                            logger.info(
                                f"Account {phone}: no channel links in round 0 — "
                                f"bot said: {_r0_summary}"
                            )
                            # Check if the bot is asking for verification (has a callback button)
                            # without showing channel links — some bots skip channels for returning users.
                            _has_cb_btn = any(
                                getattr(b, "callback_data", None)
                                for m in bot_messages
                                for row in (getattr(getattr(m, "reply_markup", None), "inline_keyboard", None) or [])
                                for b in row
                            )
                            if _has_cb_btn:
                                # Bot is showing a selection/task menu — click the button
                                # to make a selection, then continue to the next round so
                                # we can process whatever channels/confirmation it sends back.
                                logger.info(f"Account {phone}: no channels but has callback button — clicking selection button")
                                await db.add_campaign_log(
                                    campaign_id, account_id, phone, "bot_response", "info",
                                    f"No channel links; has callback button — clicking selection. Bot said: {_r0_summary}"
                                )
                                clicked = await _click_confirmation_button(
                                    client, bot_username, phone, campaign_id, account_id
                                )
                                if clicked:
                                    logger.info(f"Account {phone}: selection button clicked — waiting for bot task response")
                                    await asyncio.sleep(5)
                                    # Continue to next round to process the bot's response
                                    # (channel links or confirmation)
                                    continue
                                else:
                                    # No button could be clicked — treat /start alone as success
                                    logger.info(f"Account {phone}: could not click selection button — counting /start")
                                    confirmed_any = True
                            else:
                                # Bot sent just a text message / main menu with no action button.
                                # This usually means account is already registered — referral
                                # might still be credited by the /start alone (bot-dependent).
                                logger.info(
                                    f"Account {phone}: bot sent main menu / no action button — "
                                    f"referral may or may not be counted by /start alone"
                                )
                                await db.add_campaign_log(
                                    campaign_id, account_id, phone, "bot_response", "info",
                                    f"No channels, no callback button. Bot said: {_r0_summary}"
                                )
                                confirmed_any = True  # /start was sent; let /start count
                        else:
                            # Subsequent rounds: no new channels → bot granted access, done
                            logger.info(f"Account {phone}: no more channels after round {round_num} — done")
                        break

                    logger.info(f"Account {phone}: round {round_num + 1} — {len(channel_links)} channel(s): {channel_links}")

                    # Step 3: Join each channel.
                    # _join_and_cache handles FloodWait internally:
                    #   ≤ 300s → waits once and retries
                    #   > 300s or second consecutive FloodWait → returns rate_limited=True
                    # When rate_limited, we stop joining further channels immediately and
                    # proceed to clicking the confirmation button (the referral was still
                    # registered when /start was sent; clicking confirm is what matters).
                    account_rate_limited = False
                    for ch_link in channel_links:
                        if account_rate_limited:
                            await db.add_campaign_log(
                                campaign_id, account_id, phone, "bot_ref_join", "skipped",
                                f"Skipped {ch_link} — account rate-limited on previous channel"
                            )
                            continue

                        join_result = await _join_and_cache(client, ch_link, phone)
                        if join_result["ok"]:
                            await db.add_campaign_log(
                                campaign_id, account_id, phone, "bot_ref_join", "success",
                                f"Round {round_num + 1}: Joined {ch_link}: {join_result.get('note', '')}"
                            )
                            all_joined_links.add(ch_link)
                            joined_any = True
                            await asyncio.sleep(2)
                            await _ai_verify_channel(client, ch_link, phone, campaign_id, account_id)
                        else:
                            err = join_result.get("error", "")
                            if join_result.get("rate_limited"):
                                account_rate_limited = True
                                await db.add_campaign_log(
                                    campaign_id, account_id, phone, "bot_ref_join", "rate_limited",
                                    f"{ch_link}: {err} — skipping remaining channels"
                                )
                            elif "already" in err.lower() or "USER_ALREADY" in err:
                                all_joined_links.add(ch_link)
                                joined_any = True
                            else:
                                await db.add_campaign_log(
                                    campaign_id, account_id, phone, "bot_ref_join", "failed",
                                    f"{ch_link}: {err}"
                                )
                        await asyncio.sleep(2)

                    # Step 4: Click the "I subscribed / Я подписался" confirmation button.
                    # Wait 15 s first — Telegram needs time to propagate the membership
                    # status to all servers so the bot's re-check sees us as a member.
                    await asyncio.sleep(15)
                    clicked = await _click_confirmation_button(
                        client, bot_username, phone, campaign_id, account_id
                    )
                    if clicked:
                        confirmed_any = True
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "bot_ref_confirm", "success",
                            f"Round {round_num + 1}: clicked confirmation button"
                        )
                        # Wait for the bot to process and respond
                        await asyncio.sleep(5)
                        # Check what the bot replied — if it sends NO new channel list,
                        # the referral was accepted and we're done.
                        post_claim_msgs = []
                        async for _pc in client.get_chat_history(bot_username, limit=5):
                            if _pc.id <= sent_msg_id:
                                break
                            if not getattr(_pc, "outgoing", False):
                                post_claim_msgs.append(_pc)
                        # Count channel links still being demanded
                        pending_links_after = []
                        for _pcm in post_claim_msgs:
                            for _lnk in _extract_channel_links_from_message(_pcm):
                                if _lnk not in pending_links_after:
                                    pending_links_after.append(_lnk)
                        # Log exactly what the bot said after the claim click
                        _post_texts = [
                            (m.text or m.caption or "")[:300]
                            for m in post_claim_msgs if (m.text or m.caption or "")
                        ]
                        if _post_texts:
                            logger.info(
                                f"Account {phone}: bot response after claim click — "
                                + " | ".join(_post_texts)
                            )

                        if not pending_links_after:
                            logger.info(f"Account {phone}: bot accepted claim — no more channels demanded ✅")
                            break  # success, exit round loop
                        # Bot still demands channels — decide whether to keep retrying.
                        # Only stop early if channels truly can't be joined (non-FloodWait
                        # failures like USERNAME_INVALID). If rate-limited, keep retrying
                        # so the account can join once the flood wait expires.
                        if channel_links and not joined_any and not account_rate_limited:
                            logger.info(
                                f"Account {phone}: all {len(channel_links)} channel(s) failed to join "
                                f"(non-rate-limit errors) — stopping rounds."
                            )
                            break
                        logger.info(
                            f"Account {phone}: bot still demands {len(pending_links_after)} channel(s) "
                            f"— continuing to round {round_num + 2}"
                        )
                    else:
                        # No callback/confirm button found after joining channels.
                        # Some bots (e.g. TGLionAPI) only have URL buttons — the
                        # membership check happens when /start is resent.
                        # If we've already joined every channel the bot demanded,
                        # don't loop — just wait a bit longer and break out.
                        all_demanded = set(channel_links) if channel_links else set()
                        already_all_joined = all_demanded and all_demanded.issubset(all_joined_links)
                        if already_all_joined and round_num > 0:
                            logger.info(
                                f"Account {phone}: no confirm button but all channels already joined "
                                f"— counting as success ✅"
                            )
                            confirmed_any = True
                            break
                        logger.info(f"Account {phone}: no confirmation button found in round {round_num + 1}")
                        # Wait before the next round in case the bot is still loading
                        await asyncio.sleep(8)

                # Success = joined at least one channel OR confirmation button was clicked.
                # Sending /start PARAM + clicking confirmation is the referral trigger;
                # channel-join failures (e.g. renamed/private channels) don't invalidate it.
                if joined_any or confirmed_any:
                    success += 1
                    await db.increment_campaign_counts(campaign_id, success=1)
                else:
                    fail += 1
                    await db.increment_campaign_counts(campaign_id, fail=1)

            except Exception as e:
                err_str = str(e)
                logger.warning(f"Account {phone}: bot referral error: {err_str}")
                # Auto-disable frozen / permanently dead accounts.
                # USERNAME_NOT_OCCUPIED on contacts.ResolveUsername is how frozen
                # accounts manifest in bot_ref (they can't resolve any username).
                if ("FROZEN_METHOD_INVALID" in err_str
                        or "AUTH_KEY_UNREGISTERED" in err_str
                        or ("USERNAME_NOT_OCCUPIED" in err_str and "ResolveUsername" in err_str)):
                    logger.warning(f"Account {phone}: frozen/unregistered — marking inactive")
                    await db.set_account_active(account_id, False)
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "bot_ref", "frozen", err_str
                    )
                else:
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "bot_ref", "failed", err_str
                    )
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)

            if delay_seconds > 1.5:
                await asyncio.sleep(delay_seconds)

    # Run all accounts concurrently (up to 5 at a time via semaphore)
    try:
        await asyncio.gather(*[_run_one_account(a) for a in accounts])
    except asyncio.CancelledError:
        await db.update_campaign_status(campaign_id, "stopped")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"⏹ Bot Referral Campaign #{campaign_id} stopped.\n✅ {success} · ❌ {fail}"
            )
        return
    finally:
        _running.pop(campaign_id, None)

    final_status = "completed" if fail == 0 else ("failed" if success == 0 else "completed")
    await db.update_campaign_status(campaign_id, final_status)

    if bot_notify_callback:
        emoji = "✅" if final_status == "completed" else "⚠️"
        err_breakdown = ""
        if fail > 0:
            try:
                logs = await db.get_campaign_logs(campaign_id, limit=200)
                err_counts: dict = {}
                samples: dict = {}
                for log in logs:
                    if log["status"] not in ("failed", "frozen"):
                        continue
                    raw = (log["error_message"] or "Unknown error").strip()
                    bucket = raw[:80]
                    err_counts[bucket] = err_counts.get(bucket, 0) + 1
                    samples.setdefault(bucket, raw)
                if err_counts:
                    top = sorted(err_counts.items(), key=lambda x: -x[1])[:5]
                    lines = []
                    for b, n in top:
                        msg = samples[b].replace("<", "&lt;").replace(">", "&gt;")
                        if len(msg) > 180:
                            msg = msg[:177] + "…"
                        lines.append(f"• <code>{msg}</code> ×{n}")
                    err_breakdown = "\n\n<b>❗ Failure reasons:</b>\n" + "\n".join(lines)
            except Exception:
                pass
        await bot_notify_callback(
            notify_user_id,
            f"{emoji} Bot Referral Campaign #{campaign_id} finished!\n"
            f"📱 Accounts processed: {total}\n"
            f"✅ Success: {success}  ❌ Failed: {fail}"
            + err_breakdown,
            parse_mode="HTML",
        )



# ─────────────────────────────────────────────────────────────────────────────
# Leave Channels Campaign — leave a list of channels with all accounts
# ─────────────────────────────────────────────────────────────────────────────

async def run_leave_channels_campaign(
    campaign_id: int,
    accounts: List[dict],
    channel_list: List[str],
    notify_user_id: int = 0,
    bot_notify_callback=None,
    delay_seconds: float = 1.5,
):
    """Leave all specified channels with every account."""
    await db.update_campaign_status(campaign_id, "running")
    task = asyncio.current_task()
    _running[campaign_id] = task

    total = len(accounts) * len(channel_list)
    success = 0
    fail = 0

    try:
        for account in accounts:
            if _running.get(campaign_id) is None:
                break
            _db_status = await db.get_campaign_status(campaign_id)
            if _db_status == "stopped":
                logger.info(f"Campaign {campaign_id}: stop signal received from DB — halting")
                _running.pop(campaign_id, None)
                break

            account_id = account["id"]
            session_str = account["session_string"]
            phone = account.get("phone") or f"ID:{account_id}"

            client = await get_client_for_session(account_id, session_str)
            if not client:
                await db.set_account_active(account_id, False)
                fail += len(channel_list)
                await db.increment_campaign_counts(campaign_id, fail=len(channel_list))
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "connect", "failed", "Could not connect"
                )
                continue

            for channel in channel_list:
                channel = channel.strip()
                if not channel:
                    continue
                try:
                    await client.leave_chat(channel)
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "leave_channel", "success",
                        f"Left {channel}"
                    )
                    success += 1
                    await db.increment_campaign_counts(campaign_id, success=1)
                    logger.info(f"Account {phone}: left {channel} ✅")
                except Exception as e:
                    err = str(e)
                    if "USER_NOT_PARTICIPANT" in err.upper() or "not a member" in err.lower():
                        # Already not a member — count as success
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "leave_channel", "success",
                            f"Already not in {channel}"
                        )
                        success += 1
                        await db.increment_campaign_counts(campaign_id, success=1)
                    else:
                        await db.add_campaign_log(
                            campaign_id, account_id, phone, "leave_channel", "failed", err
                        )
                        fail += 1
                        await db.increment_campaign_counts(campaign_id, fail=1)
                await asyncio.sleep(1.5)

            # Delay between accounts (configurable slow mode)
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

    except asyncio.CancelledError:
        await db.update_campaign_status(campaign_id, "stopped")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"⏹ Leave Campaign #{campaign_id} stopped.\n✅ {success} · ❌ {fail}"
            )
        return
    finally:
        _running.pop(campaign_id, None)

    final_status = "completed" if fail == 0 else ("failed" if success == 0 else "completed")
    await db.update_campaign_status(campaign_id, final_status)

    if bot_notify_callback:
        emoji = "✅" if final_status == "completed" else "⚠️"
        await bot_notify_callback(
            notify_user_id,
            f"{emoji} Leave Campaign #{campaign_id} finished!\n"
            f"📊 Total operations: {total}\n"
            f"✅ Left: {success}  ❌ Failed: {fail}"
        )


async def run_comment_campaign(
    campaign_id: int,
    accounts: List[dict],
    target_url: str,         # group/channel URL or post URL (for reply)
    message_text: str,       # text to send / comment
    notify_user_id: int = 0,
    bot_notify_callback=None,
    delay_seconds: float = 1.5,
):
    """
    Comment Campaign: each account sends a message (or reply) to a group/channel.

    target_url examples:
      https://t.me/groupname         → send new message to the group
      https://t.me/groupname/123     → reply to message 123 in the group
      https://t.me/c/1234567890/123  → reply to message in private channel
    """
    await db.update_campaign_status(campaign_id, "running")
    task = asyncio.current_task()
    _running[campaign_id] = task

    # Parse target_url → (chat_id, reply_to_msg_id or None)
    parsed = parse_post_url(target_url.strip())
    if parsed:
        target_chat, reply_to_msg_id = parsed
    else:
        # Plain group link — no specific message
        target_chat = target_url.strip().rstrip("/")
        # Strip to just the username if it's t.me/username
        _m = re.match(r"https?://t\.me/([a-zA-Z0-9_]+)$", target_chat)
        if _m:
            target_chat = _m.group(1)
        reply_to_msg_id = None

    if not target_chat:
        logger.error(f"run_comment_campaign: cannot parse target from {target_url!r}")
        await db.update_campaign_status(campaign_id, "failed")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"❌ Cannot parse target from: <code>{target_url}</code>"
            )
        _running.pop(campaign_id, None)
        return

    total = len(accounts)
    success = 0
    fail = 0

    sem = asyncio.Semaphore(5)

    async def _comment_one(account):
        nonlocal success, fail
        account_id = account["id"]
        session_str = account["session_string"]
        phone = account.get("phone") or f"ID:{account_id}"

        if _running.get(campaign_id) is None:
            return

        async with sem:
            if _running.get(campaign_id) is None:
                return

            client = await get_client_for_session(account_id, session_str)
            if not client:
                await db.set_account_active(account_id, False)
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "connect", "failed", "Could not connect"
                )
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
                return

            try:
                # Try to send the message (or reply)
                _kwargs = {}
                if reply_to_msg_id:
                    _kwargs["reply_to_message_id"] = reply_to_msg_id

                try:
                    await client.send_message(target_chat, message_text, **_kwargs)
                    await db.add_campaign_log(
                        campaign_id, account_id, phone, "comment", "success",
                        f"Sent comment to {target_url}"
                    )
                    success += 1
                    await db.increment_campaign_counts(campaign_id, success=1)
                    logger.info(f"Account {phone}: commented in {target_chat} ✅")

                except (ChatWriteForbidden, UserBannedInChannel):
                    raise  # re-raise; handled below
                except Exception as send_err:
                    err_str = str(send_err)
                    # Not a member → try to join the group first (public groups only)
                    if any(k in err_str.upper() for k in (
                        "USER_NOT_PARTICIPANT", "CHANNEL_PRIVATE",
                        "CHAT_WRITE_FORBIDDEN", "NOT_MEMBER"
                    )):
                        try:
                            await client.join_chat(
                                target_chat if isinstance(target_chat, str) else target_chat
                            )
                            await asyncio.sleep(2)
                            # Retry sending
                            await client.send_message(target_chat, message_text, **_kwargs)
                            await db.add_campaign_log(
                                campaign_id, account_id, phone, "comment", "success",
                                f"Joined + commented in {target_url}"
                            )
                            success += 1
                            await db.increment_campaign_counts(campaign_id, success=1)
                            logger.info(f"Account {phone}: joined + commented in {target_chat} ✅")
                        except Exception as join_err:
                            await db.add_campaign_log(
                                campaign_id, account_id, phone, "comment", "failed",
                                f"Join failed: {join_err}"
                            )
                            fail += 1
                            await db.increment_campaign_counts(campaign_id, fail=1)
                    else:
                        raise

            except _DEAD_SESSION_ERRORS:
                await db.set_account_active(account_id, False)
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "comment", "failed", "Dead session"
                )
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
            except FloodWait as fw:
                logger.warning(f"Account {phone}: FloodWait {fw.value}s")
                await asyncio.sleep(min(fw.value, 60))
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
            except (ChatWriteForbidden, UserBannedInChannel) as e:
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "comment", "failed",
                    f"Forbidden: {e}"
                )
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
            except Exception as e:
                err = str(e)
                if _is_dead_session_error(err):
                    await db.set_account_active(account_id, False)
                await db.add_campaign_log(
                    campaign_id, account_id, phone, "comment", "failed", err
                )
                fail += 1
                await db.increment_campaign_counts(campaign_id, fail=1)
                logger.error(f"Account {phone}: comment error: {e}")
            finally:
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)

    try:
        tasks = [asyncio.create_task(_comment_one(acc)) for acc in accounts]
        await asyncio.gather(*tasks, return_exceptions=True)

        _db_status = await db.get_campaign_status(campaign_id)
        if _db_status == "stopped":
            _running.pop(campaign_id, None)
            if bot_notify_callback:
                await bot_notify_callback(
                    notify_user_id,
                    f"⏹ Comment Campaign #{campaign_id} stopped.\n✅ {success} · ❌ {fail}"
                )
            return

    except asyncio.CancelledError:
        await db.update_campaign_status(campaign_id, "stopped")
        if bot_notify_callback:
            await bot_notify_callback(
                notify_user_id,
                f"⏹ Comment Campaign #{campaign_id} stopped.\n✅ {success} · ❌ {fail}"
            )
        return
    finally:
        _running.pop(campaign_id, None)

    final_status = "completed" if fail == 0 else ("failed" if success == 0 else "completed")
    await db.update_campaign_status(campaign_id, final_status)

    if bot_notify_callback:
        emoji = "✅" if final_status == "completed" else "⚠️"
        await bot_notify_callback(
            notify_user_id,
            f"{emoji} Comment Campaign #{campaign_id} finished!\n"
            f"📊 Accounts: {total}\n"
            f"✅ Commented: {success}  ❌ Failed: {fail}"
        )
''',
    'services.campaign_poller': r'''"""
Campaign Poller — picks up campaigns queued via the admin web panel.
Polls the DB every 10 seconds for campaigns with status 'queued'.
Runs them using existing campaign_runner logic.
"""
import asyncio
import logging

import database as db
from services.campaign_runner import (
    run_referral_campaign,
    run_leave_channels_campaign,
    run_admin_campaign,
    run_comment_campaign,
    parse_post_url,
)

logger = logging.getLogger(__name__)

_running = False


async def start_poller():
    global _running
    if _running:
        return
    _running = True
    logger.info("Campaign poller: started — polling every 10s for queued campaigns")
    asyncio.create_task(_poll_loop())


async def _poll_loop():
    while True:
        try:
            await _check_queued_campaigns()
        except Exception as e:
            logger.warning(f"Campaign poller error: {e}")
        await asyncio.sleep(10)


async def _check_queued_campaigns():
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM campaigns WHERE status = 'queued' ORDER BY created_at ASC LIMIT 5"
        )

    for row in rows:
        campaign_id = row["id"]
        action_type = row["action_type"]
        notify_user_id = row["user_id"]

        # Mark as running immediately to prevent double-pickup
        pool2 = await db.get_pool()
        async with pool2.acquire() as conn2:
            updated = await conn2.fetchval(
                "UPDATE campaigns SET status = 'running' WHERE id = $1 AND status = 'queued' RETURNING id",
                campaign_id
            )
        if not updated:
            continue  # Another process grabbed it

        logger.info(f"Campaign poller: launching campaign #{campaign_id} (type={action_type})")

        # Gather accounts — either from a source campaign or all active accounts
        source_campaign_id = row.get("source_campaign_id")
        if source_campaign_id:
            active_accounts = await db.get_accounts_from_campaign(source_campaign_id)
            logger.info(
                f"Campaign #{campaign_id}: using {len(active_accounts)} accounts "
                f"from source campaign #{source_campaign_id}"
            )
        else:
            all_accs = await db.list_all_accounts()
            active_accounts = [dict(a) for a in all_accs if a["is_active"]]

        if not active_accounts:
            await db.update_campaign_status(campaign_id, "failed")
            logger.warning(f"Campaign #{campaign_id}: no active accounts")
            continue

        if action_type == "referral":
            channel_list_raw = row.get("channel_list") or ""
            channel_list = [c.strip() for c in channel_list_raw.split(",") if c.strip()]
            delay_seconds = float(row.get("delay_seconds") or 1.5)
            asyncio.create_task(run_referral_campaign(
                campaign_id=campaign_id,
                accounts=active_accounts,
                channel_list=channel_list,
                ai_assisted=bool(row.get("ai_assisted")),
                notify_user_id=notify_user_id,
                delay_seconds=delay_seconds,
            ))

        elif action_type == "leave_channels":
            channel_list_raw = row.get("channel_list") or ""
            channel_list = [c.strip() for c in channel_list_raw.split(",") if c.strip()]
            delay_seconds = float(row.get("delay_seconds") or 1.5)
            asyncio.create_task(run_leave_channels_campaign(
                campaign_id=campaign_id,
                accounts=active_accounts,
                channel_list=channel_list,
                notify_user_id=notify_user_id,
                delay_seconds=delay_seconds,
            ))

        elif action_type == "comment":
            target_url = row.get("post_url") or ""
            message_text = row.get("channel_list") or ""
            delay_seconds = float(row.get("delay_seconds") or 1.5)
            asyncio.create_task(run_comment_campaign(
                campaign_id=campaign_id,
                accounts=active_accounts,
                target_url=target_url,
                message_text=message_text,
                notify_user_id=notify_user_id,
                delay_seconds=delay_seconds,
            ))

        else:
            # Standard campaign (react/vote/both)
            post_url = row.get("post_url") or ""
            parsed = parse_post_url(post_url)
            if not parsed:
                await db.update_campaign_status(campaign_id, "failed")
                continue
            chat_id, message_id = parsed
            delay_seconds = float(row.get("delay_seconds") or 1.5)
            mixed_reactions = bool(row.get("mixed_reactions") or False)
            asyncio.create_task(run_admin_campaign(
                campaign_id=campaign_id,
                accounts=active_accounts,
                chat_id=chat_id,
                message_id=message_id,
                action_type=action_type,
                reaction=row.get("reaction"),
                button_index=row.get("button_index"),
                post_url=post_url,
                channel_link=row.get("channel_link") or "",
                notify_user_id=notify_user_id,
                delay_seconds=delay_seconds,
                mixed_reactions=mixed_reactions,
            ))
''',
    'handlers.start': r'''from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
import database as db

router = Router()


def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Add Account",  callback_data="menu:addaccount"),
            InlineKeyboardButton(text="📋 My Accounts",  callback_data="menu:myaccounts"),
        ],
        [
            InlineKeyboardButton(text="🚀 New Campaign", callback_data="menu:newcampaign"),
            InlineKeyboardButton(text="📊 My Campaigns", callback_data="menu:campaigns"),
        ],
        [
            InlineKeyboardButton(text="📨 Auto Forward", callback_data="menu:autoforward"),
            InlineKeyboardButton(text="💬 Auto Reply",   callback_data="menu:autoreply"),
        ],
        [
            InlineKeyboardButton(text="🛒 Marketplace",  callback_data="menu:marketplace"),
        ],
        [
            InlineKeyboardButton(text="👤 My Profile",   callback_data="menu:profile"),
            InlineKeyboardButton(text="❓ Help",          callback_data="menu:help"),
        ],
    ])


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    name = message.from_user.first_name or "there"
    await message.answer(
        f"👋 <b>Welcome, {name}!</b>\n\n"
        "I'm your <b>Telegram Promotion Bot</b>.\n"
        "Add your accounts, then run campaigns to react to posts and click vote buttons.\n\n"
        "Choose an option below:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🏠 <b>Main Menu</b>\n\nChoose an option:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


@router.callback_query(F.data == "menu:back")
async def handle_back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.edit_text(
        "🏠 <b>Main Menu</b>\n\nChoose an option:",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


@router.callback_query(F.data.startswith("menu:"))
async def handle_menu(callback: CallbackQuery, state: FSMContext):
    """
    Central menu dispatcher.
    IMPORTANT: Must set FSM state BEFORE calling any prompt function,
    so subsequent message/callback handlers (which filter by state) fire correctly.
    """
    action = callback.data.split(":")[1]
    await callback.answer()

    if action == "addaccount":
        # Set state FIRST so the method-choice callback is in the right state
        from handlers.accounts import AddAccountStates, send_add_account_prompt
        await state.clear()
        await state.set_state(AddAccountStates.choosing_method)
        await send_add_account_prompt(callback.message, callback.from_user.id)

    elif action == "myaccounts":
        await state.clear()
        from handlers.accounts import send_my_accounts
        await send_my_accounts(callback.message, callback.from_user.id, edit=True)

    elif action == "newcampaign":
        # Set state FIRST so the URL message handler fires correctly
        from handlers.campaigns import CampaignStates, send_new_campaign_prompt
        await state.clear()
        await state.set_state(CampaignStates.entering_url)
        await send_new_campaign_prompt(callback.message, callback.from_user.id)

    elif action == "campaigns":
        await state.clear()
        from handlers.campaigns import send_campaigns_list
        await send_campaigns_list(callback.message, callback.from_user.id, edit=True)

    elif action == "marketplace":
        await state.clear()
        from handlers.marketplace import marketplace_menu
        await marketplace_menu(callback, state)

    elif action == "autoforward":
        await state.clear()
        from handlers.forwarding import autoforward_menu
        await autoforward_menu(callback, state)

    elif action == "autoreply":
        await state.clear()
        from handlers.auto_reply import autoreply_menu
        await autoreply_menu(callback, state)

    elif action == "profile":
        await state.clear()
        await send_profile(callback.message, callback.from_user.id, edit=True)

    elif action == "help":
        await state.clear()
        await send_help(callback.message, edit=True)

    elif action == "back":
        await state.clear()
        await callback.message.edit_text(
            "🏠 <b>Main Menu</b>\n\nChoose an option:",
            parse_mode="HTML",
            reply_markup=main_menu_keyboard()
        )


async def send_profile(message: Message, user_id: int, edit: bool = False):
    user = await db.get_user(user_id)
    if not user:
        text = "❌ Not registered. Use /start first."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    accounts = await db.get_user_accounts(user_id)
    campaigns = await db.list_campaigns(user_id=user_id)
    active_acc = sum(1 for a in accounts if a["is_active"])
    running = sum(1 for c in campaigns if c["status"] == "running")
    done = sum(1 for c in campaigns if c["status"] == "completed")

    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"🆔 Telegram ID: <code>{user['telegram_id']}</code>\n"
        f"👤 Name: {user['first_name'] or ''} {user['last_name'] or ''}\n\n"
        f"📱 Accounts: <b>{len(accounts)}</b> total · <b>{active_acc}</b> active\n"
        f"📊 Campaigns: <b>{len(campaigns)}</b> total · <b>{running}</b> running · <b>{done}</b> done\n"
        f"📅 Joined: {user['created_at'].strftime('%Y-%m-%d')}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def send_help(message: Message, edit: bool = False):
    text = (
        "📖 <b>How to use this bot</b>\n\n"
        "<b>Step 1 — Add your accounts</b>\n"
        "📱 Add Account → choose:\n"
        "  • <b>Phone + OTP</b>: enter number, receive SMS, verify\n"
        "  • <b>Session String</b>: paste a Pyrogram session\n\n"
        "<b>Step 2 — Run a campaign</b>\n"
        "🚀 New Campaign → paste post URL → choose action:\n\n"
        "😀 <b>React</b> — adds emoji reaction to the post\n"
        "🗳 <b>Vote</b> — clicks an inline button on the post\n"
        "⚡ <b>Both</b> — reacts AND clicks a button\n\n"
        "<b>Supported URL formats:</b>\n"
        "<code>https://t.me/channelname/123</code>\n"
        "<code>https://t.me/c/1234567890/123</code>\n\n"
        "<b>Commands:</b>\n"
        "/start — main menu\n"
        "/addaccount — add account\n"
        "/myaccounts — manage accounts\n"
        "/newcampaign — new campaign\n"
        "/campaigns — your campaigns"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await send_help(message, edit=False)


@router.message(Command("me"))
async def cmd_me(message: Message):
    await send_profile(message, message.from_user.id, edit=False)


@router.message(Command("ping"))
async def cmd_ping(message: Message):
    """Keep-alive ping — used by the virtual user to keep the bot alive 24/7."""
    await message.answer("🏓 pong")
''',
    'handlers.accounts': r'''from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import database as db
from services.pyrogram_manager import (
    start_login, complete_login, cancel_login, validate_session_string
)

router = Router()


class AddAccountStates(StatesGroup):
    choosing_method = State()
    entering_phone = State()
    entering_otp = State()
    entering_password = State()
    entering_session = State()


def method_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Phone + OTP", callback_data="add_method:phone"),
            InlineKeyboardButton(text="🔑 Session String", callback_data="add_method:session"),
        ],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="add_method:cancel")],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data="add_method:cancel")]
    ])


def back_to_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Add Another Account", callback_data="menu:addaccount")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")],
    ])


async def send_add_account_prompt(message: Message, user_id: int):
    """Show the method-choice keyboard. Caller must set state to choosing_method first."""
    accounts = await db.get_user_accounts(user_id)
    count_text = f"You have <b>{len(accounts)}</b> account(s) connected.\n\n" if accounts else ""
    await message.edit_text(
        f"📱 <b>Add Telegram Account</b>\n\n"
        f"{count_text}"
        "How would you like to add an account?",
        parse_mode="HTML",
        reply_markup=method_keyboard()
    )


@router.message(Command("addaccount"))
async def cmd_add_account(message: Message, state: FSMContext):
    await db.upsert_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    accounts = await db.get_user_accounts(message.from_user.id)
    count_text = f"You have <b>{len(accounts)}</b> account(s) connected.\n\n" if accounts else ""
    await state.set_state(AddAccountStates.choosing_method)
    await message.answer(
        f"📱 <b>Add Telegram Account</b>\n\n"
        f"{count_text}"
        "How would you like to add an account?",
        parse_mode="HTML",
        reply_markup=method_keyboard()
    )


# ── Cancel — no state filter so it always works ──────────────────────────────

@router.callback_query(F.data == "add_method:cancel")
async def handle_cancel(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    await cancel_login(user_id)
    await state.clear()
    from handlers.start import main_menu_keyboard
    await callback.answer("Cancelled")
    await callback.message.edit_text(
        "❌ Cancelled.\n\n🏠 <b>Main Menu</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


# ── Method choice (must be in choosing_method state) ─────────────────────────

@router.callback_query(F.data.startswith("add_method:"), AddAccountStates.choosing_method)
async def handle_method_choice(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split(":")[1]
    if method == "cancel":
        # Handled above, but just in case
        await handle_cancel(callback, state)
        return

    await callback.answer()

    if method == "phone":
        await state.set_state(AddAccountStates.entering_phone)
        await callback.message.edit_text(
            "📞 <b>Enter Phone Number</b>\n\n"
            "Send your number in international format:\n"
            "<code>+12345678900</code>\n\n"
            "⬇️ Reply to the prompt below:",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )
        await callback.message.answer(
            "📞 Your phone number:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="+12345678900")
        )

    elif method == "session":
        await state.set_state(AddAccountStates.entering_session)
        await callback.message.edit_text(
            "🔑 <b>Paste Session String</b>\n\n"
            "Generate one with Pyrogram:\n"
            "<code>from pyrogram import Client\n"
            "with Client('x', api_id, api_hash) as c:\n"
            "    print(c.export_session_string())</code>\n\n"
            "⬇️ Reply to the prompt below:",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )
        await callback.message.answer(
            "🔑 Your session string:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Paste session string here...")
        )


# ── Phone number input ────────────────────────────────────────────────────────

@router.message(AddAccountStates.entering_phone)
async def handle_phone_input(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await cancel_login(message.from_user.id)
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    phone = (message.text or "").strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+") or len(phone) < 8:
        await message.reply(
            "❌ <b>Invalid format</b>\n\nUse: <code>+12345678900</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="+12345678900")
        )
        return

    status_msg = await message.answer(f"⏳ Sending OTP to <code>{phone}</code>...", parse_mode="HTML")
    result = await start_login(message.from_user.id, phone)

    if result["status"] == "code_sent":
        await state.set_state(AddAccountStates.entering_otp)
        await state.update_data(phone=phone)
        await status_msg.edit_text(
            f"✅ <b>OTP sent to {phone}!</b>\n\n"
            "Check Telegram/SMS for the code.\n"
            "⬇️ Reply with the code below:",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )
        await message.answer(
            "🔢 Enter your OTP code:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 12345")
        )
    elif result["status"] == "flood_wait":
        await state.clear()
        await status_msg.edit_text(
            f"⚠️ <b>Too many attempts</b>\n\nWait <b>{result['seconds']} seconds</b> and try again.",
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard()
        )
    else:
        await state.clear()
        await status_msg.edit_text(
            f"❌ <b>Error:</b> {result.get('message', 'Unknown error')}",
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard()
        )


# ── OTP code input ────────────────────────────────────────────────────────────

@router.message(AddAccountStates.entering_otp)
async def handle_otp_input(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await cancel_login(message.from_user.id)
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    code = (message.text or "").strip().replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) < 4:
        await message.reply(
            "❌ Enter digits only, e.g. <code>12345</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 12345")
        )
        return

    processing = await message.answer("⏳ Verifying code...")
    result = await complete_login(message.from_user.id, code)

    if result["status"] == "need_password":
        await state.set_state(AddAccountStates.entering_password)
        await processing.edit_text(
            "🔐 <b>2FA Password Required</b>\n\n"
            "This account has Two-Factor Authentication enabled.\n"
            "⬇️ Reply with your 2FA password below:",
            parse_mode="HTML",
            reply_markup=cancel_keyboard()
        )
        await message.answer(
            "🔐 Your 2FA password:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Enter 2FA password...")
        )

    elif result["status"] == "success":
        await _save_and_confirm(message, state, result, processing)

    else:
        msg = result.get("message", "Unknown error")
        if "invalid" in msg.lower() or "code" in msg.lower():
            # Allow retry
            await processing.edit_text(
                f"❌ <b>Wrong code.</b> Try again:\n\n<i>{msg}</i>",
                parse_mode="HTML",
                reply_markup=cancel_keyboard()
            )
            await message.answer(
                "🔢 Enter the correct OTP code:",
                reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 12345")
            )
        elif "expired" in msg.lower():
            await state.clear()
            await processing.edit_text(
                f"⏰ <b>Code expired.</b>\n\nStart over:",
                parse_mode="HTML",
                reply_markup=back_to_menu_keyboard()
            )
        else:
            await state.clear()
            await processing.edit_text(
                f"❌ <b>Error:</b> {msg}",
                parse_mode="HTML",
                reply_markup=back_to_menu_keyboard()
            )


# ── 2FA password input ────────────────────────────────────────────────────────

@router.message(AddAccountStates.entering_password)
async def handle_password_input(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await cancel_login(message.from_user.id)
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    processing = await message.answer("⏳ Checking 2FA password...")
    result = await complete_login(message.from_user.id, "", password=(message.text or "").strip())

    if result["status"] == "success":
        await _save_and_confirm(message, state, result, processing)
    else:
        msg = result.get("message", "Unknown error")
        if "wrong" in msg.lower() or "password" in msg.lower() or "invalid" in msg.lower():
            await processing.edit_text(
                f"❌ <b>Wrong password.</b> Try again:",
                parse_mode="HTML",
                reply_markup=cancel_keyboard()
            )
            await message.answer(
                "🔐 Your 2FA password:",
                reply_markup=ForceReply(selective=True, input_field_placeholder="Enter 2FA password...")
            )
        else:
            await state.clear()
            await processing.edit_text(
                f"❌ <b>Error:</b> {msg}",
                parse_mode="HTML",
                reply_markup=back_to_menu_keyboard()
            )


# ── Session string input ──────────────────────────────────────────────────────

@router.message(AddAccountStates.entering_session)
async def handle_session_input(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    session_str = (message.text or "").strip()
    if len(session_str) < 100:
        await message.reply(
            "❌ That doesn't look like a valid session string.\n"
            "It should be a long string starting with characters like <code>BQ</code>...",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Paste session string here...")
        )
        return

    processing = await message.answer("⏳ Validating session...")
    result = await validate_session_string(session_str)

    if result["status"] == "success":
        account_id = await db.add_account(
            user_id=message.from_user.id,
            session_string=session_str,
            phone=result["phone"],
            first_name=result["first_name"],
            last_name=result["last_name"],
            username=result["username"],
            telegram_account_id=result["telegram_account_id"],
        )
        await state.clear()
        name = f"{result['first_name']} {result['last_name']}".strip() or result.get("phone") or "Unknown"
        uname = f"\n🔗 @{result['username']}" if result['username'] else ""
        await processing.edit_text(
            f"✅ <b>Account Added!</b>\n\n"
            f"👤 {name}{uname}\n"
            f"📞 {result['phone'] or 'N/A'}\n"
            f"🆔 Account #{account_id}\n\n"
            "Ready to use in campaigns!",
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard()
        )
    else:
        await state.clear()
        await processing.edit_text(
            f"❌ <b>Invalid session:</b>\n{result.get('message', 'Unknown error')}\n\n"
            "Generate a fresh session string and try again.",
            parse_mode="HTML",
            reply_markup=back_to_menu_keyboard()
        )


async def _save_and_confirm(message: Message, state: FSMContext, result: dict, processing_msg):
    account_id = await db.add_account(
        user_id=message.from_user.id,
        session_string=result["session_string"],
        phone=result["phone"],
        first_name=result["first_name"],
        last_name=result["last_name"],
        username=result["username"],
        telegram_account_id=result["telegram_account_id"],
    )
    await state.clear()
    name = f"{result['first_name']} {result['last_name']}".strip() or result.get("phone") or "Unknown"
    uname = f"\n🔗 @{result['username']}" if result['username'] else ""
    await processing_msg.edit_text(
        f"✅ <b>Account Added Successfully!</b>\n\n"
        f"👤 {name}{uname}\n"
        f"📞 {result['phone'] or 'N/A'}\n"
        f"🆔 Account #{account_id}\n\n"
        "This account is now ready to use in campaigns! 🎉",
        parse_mode="HTML",
        reply_markup=back_to_menu_keyboard()
    )


# ── My Accounts ───────────────────────────────────────────────────────────────

async def send_my_accounts(message: Message, user_id: int, edit: bool = False):
    accounts = await db.get_user_accounts(user_id)
    if not accounts:
        text = (
            "📭 <b>No accounts connected yet</b>\n\n"
            "Add your first Telegram account to run campaigns:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📱 Add Account", callback_data="menu:addaccount")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")],
        ])
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    active = sum(1 for a in accounts if a["is_active"])
    text = f"📱 <b>Your Accounts</b> — {len(accounts)} total, {active} active\n\n"

    for acc in accounts:
        name = f"{acc['first_name'] or ''} {acc['last_name'] or ''}".strip() or "Unknown"
        uname = f" @{acc['username']}" if acc['username'] else ""
        phone = acc['phone'] or "No phone"
        status = "✅ Active" if acc['is_active'] else "❌ Inactive"
        text += f"<b>{name}</b>{uname}\n   📞 {phone} · {status} · <code>#{acc['id']}</code>\n\n"

    buttons = []
    for acc in accounts:
        name = f"{acc['first_name'] or ''} {acc['last_name'] or ''}".strip() or acc['phone'] or f"#{acc['id']}"
        icon = "✅" if acc['is_active'] else "❌"
        buttons.append([InlineKeyboardButton(
            text=f"🗑 Remove {icon} {name[:22]}",
            callback_data=f"remove_acc:{acc['id']}"
        )])

    buttons.append([InlineKeyboardButton(text="📱 Add Another", callback_data="menu:addaccount")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("myaccounts"))
async def cmd_my_accounts(message: Message):
    await send_my_accounts(message, message.from_user.id, edit=False)


@router.callback_query(F.data.startswith("remove_acc:"))
async def handle_remove_account(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    account = await db.get_account(account_id)

    if not account or account["user_id"] != callback.from_user.id:
        await callback.answer("❌ Account not found.", show_alert=True)
        return

    name = f"{account['first_name'] or ''} {account['last_name'] or ''}".strip() or account['phone'] or f"#{account_id}"

    # Inject a confirmation row above the existing buttons
    current_buttons = callback.message.reply_markup.inline_keyboard if callback.message.reply_markup else []
    # Replace with just the confirm row
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"✅ Yes, remove", callback_data=f"confirm_remove:{account_id}"),
            InlineKeyboardButton(text="❌ No, keep", callback_data="back_to_accounts"),
        ],
        [InlineKeyboardButton(text=f"← Back to accounts", callback_data="back_to_accounts")]
    ]))
    await callback.answer(f"Remove {name[:30]}?")


@router.callback_query(F.data.startswith("confirm_remove:"))
async def handle_confirm_remove(callback: CallbackQuery):
    account_id = int(callback.data.split(":")[1])
    account = await db.get_account(account_id)
    if not account or account["user_id"] != callback.from_user.id:
        await callback.answer("❌ Not found.", show_alert=True)
        return

    name = f"{account['first_name'] or ''} {account['last_name'] or ''}".strip() or account['phone'] or f"#{account_id}"
    await db.delete_account(account_id)
    await callback.answer(f"✅ {name[:30]} removed!")
    await send_my_accounts(callback.message, callback.from_user.id, edit=True)


@router.callback_query(F.data == "back_to_accounts")
async def handle_back_to_accounts(callback: CallbackQuery):
    await callback.answer()
    await send_my_accounts(callback.message, callback.from_user.id, edit=True)


@router.message(Command("removeaccount"))
async def cmd_remove_account(message: Message):
    await send_my_accounts(message, message.from_user.id, edit=False)
''',
    'handlers.campaigns': r'''import asyncio
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import database as db
from services.campaign_runner import run_campaign, stop_campaign, parse_post_url

router = Router()

REACTIONS = ["👍", "❤️", "🔥", "🥰", "👏", "😁", "🤩", "🎉", "🙏", "💯", "😎", "🤣", "😢", "👎", "💩"]


class CampaignStates(StatesGroup):
    entering_url = State()
    entering_channel_link = State()
    choosing_action = State()
    choosing_reaction = State()
    entering_button_index = State()
    choosing_slow_mode = State()
    entering_delay = State()
    confirming = State()


def action_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😀 React to Post", callback_data="camp_action:react")],
        [InlineKeyboardButton(text="🗳 Click Vote Button", callback_data="camp_action:vote")],
        [InlineKeyboardButton(text="⚡ Both (React + Vote)", callback_data="camp_action:both")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")],
    ])


def reaction_keyboard():
    rows = []
    row = []
    for i, emoji in enumerate(REACTIONS):
        row.append(InlineKeyboardButton(text=emoji, callback_data=f"camp_react:{emoji}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🎲 Mixed (random per account)", callback_data="camp_react:mixed")])
    rows.append([InlineKeyboardButton(text="✏️ Custom emoji", callback_data="camp_react:custom")])
    rows.append([InlineKeyboardButton(text="⭐ Premium/pack emoji", callback_data="camp_react:premium")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button_index_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1️⃣ 1st", callback_data="camp_btn:0"),
            InlineKeyboardButton(text="2️⃣ 2nd", callback_data="camp_btn:1"),
            InlineKeyboardButton(text="3️⃣ 3rd", callback_data="camp_btn:2"),
        ],
        [
            InlineKeyboardButton(text="4️⃣ 4th", callback_data="camp_btn:3"),
            InlineKeyboardButton(text="5️⃣ 5th", callback_data="camp_btn:4"),
            InlineKeyboardButton(text="✏️ Other #", callback_data="camp_btn:custom"),
        ],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")],
    ])


def slow_mode_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Normal (1.5s per account)", callback_data="camp_delay:normal")],
        [InlineKeyboardButton(text="🐢 Slow — 5s per account", callback_data="camp_delay:5")],
        [InlineKeyboardButton(text="🐌 Very slow — 10s per account", callback_data="camp_delay:10")],
        [InlineKeyboardButton(text="🦥 Custom delay (type seconds)", callback_data="camp_delay:custom")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")],
    ])


def back_to_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 New Campaign", callback_data="menu:newcampaign")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")],
    ])


# ── Cancel ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "camp_cancel")
async def handle_campaign_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Cancelled")
    from handlers.start import main_menu_keyboard
    await callback.message.edit_text(
        "❌ Campaign cancelled.\n\n🏠 <b>Main Menu</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def send_new_campaign_prompt(message: Message, user_id: int):
    """Show the URL prompt. Caller MUST have set CampaignStates.entering_url first."""
    accounts = await db.get_user_accounts(user_id)
    active = [a for a in accounts if a["is_active"]]

    if not active:
        await message.edit_text(
            "❌ <b>No active accounts!</b>\n\n"
            "You need at least one connected account to run a campaign.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Add Account", callback_data="menu:addaccount")],
                [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")],
            ])
        )
        return

    await message.edit_text(
        f"🚀 <b>New Campaign</b>\n\n"
        f"📱 <b>{len(active)} active account(s)</b> ready.\n\n"
        "Step 1️⃣ — Paste the Telegram <b>post URL</b>:\n"
        "<code>https://t.me/channelname/123</code>\n"
        "<code>https://t.me/c/1234567890/123</code>\n\n"
        "⬇️ Reply with the link below:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")]
        ])
    )
    await message.answer(
        "🔗 Post URL:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
    )


@router.message(Command("newcampaign"))
async def cmd_new_campaign(message: Message, state: FSMContext):
    accounts = await db.get_user_accounts(message.from_user.id)
    active = [a for a in accounts if a["is_active"]]

    if not active:
        await message.answer(
            "❌ <b>No active accounts!</b>\n\nAdd an account first.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Add Account", callback_data="menu:addaccount")]
            ])
        )
        return

    await state.set_state(CampaignStates.entering_url)
    await message.answer(
        f"🚀 <b>New Campaign</b>\n\n"
        f"📱 <b>{len(active)} active account(s)</b> ready.\n\n"
        "Step 1️⃣ — Paste the Telegram <b>post URL</b>:\n"
        "<code>https://t.me/channelname/123</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")]
        ])
    )
    await message.answer(
        "🔗 Post URL:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
    )


# ── Step 1: URL input ─────────────────────────────────────────────────────────

@router.message(CampaignStates.entering_url)
async def handle_campaign_url(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    url = (message.text or "").strip()
    parsed = parse_post_url(url)
    if not parsed:
        await message.reply(
            "❌ <b>Invalid URL</b>\n\n"
            "Use:\n<code>https://t.me/channelname/123</code>\n"
            "<code>https://t.me/c/1234567890/123</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
        )
        return

    await state.update_data(post_url=url)
    await state.set_state(CampaignStates.entering_channel_link)
    await message.answer(
        "✅ <b>Post URL saved!</b>\n\n"
        "Step 2️⃣ — Paste the <b>channel join link</b>.\n\n"
        "📢 This is used to <b>auto-join accounts</b> that are not yet members before voting.\n\n"
        "• Public channel: <code>https://t.me/channelname</code>\n"
        "• Private invite: <code>https://t.me/+InviteCode</code>\n\n"
        "Tap <b>⏭ Skip</b> if all accounts are already joined:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip (already joined)", callback_data="camp_chlink:skip")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")],
        ])
    )
    await message.answer(
        "🔗 Channel join link:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
    )


# ── Step 2: Channel link ──────────────────────────────────────────────────────

@router.callback_query(F.data == "camp_chlink:skip", CampaignStates.entering_channel_link)
async def handle_channel_link_skip(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(channel_link=None)
    await state.set_state(CampaignStates.choosing_action)
    await callback.message.edit_text(
        "⏭ <b>Channel link skipped.</b>\n\n"
        "Step 3️⃣ — 🎯 What should your accounts do on this post?",
        parse_mode="HTML",
        reply_markup=action_keyboard()
    )


@router.message(CampaignStates.entering_channel_link)
async def handle_channel_link_input(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    import re
    link = (message.text or "").strip()

    if re.match(r"https?://t\.me/c/\d+/\d+", link) or re.match(r"https?://t\.me/[^+][^/]*/\d+$", link):
        await message.reply(
            "❌ <b>That's a post link, not a channel link.</b>\n\n"
            "I need the <b>channel join link</b>, not the post URL.\n\n"
            "✅ <b>Valid examples:</b>\n"
            "• Public: <code>https://t.me/channelname</code>\n"
            "• Private invite: <code>https://t.me/+AbCdEfGh1234</code>\n\n"
            "Or tap <b>⏭ Skip</b> if all accounts are already members:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⏭ Skip (already joined)", callback_data="camp_chlink:skip")],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")],
            ])
        )
        return

    if not ("t.me" in link or link.startswith("@")):
        await message.reply(
            "❌ <b>Invalid link.</b>\n\n"
            "Use <code>https://t.me/channelname</code> or <code>https://t.me/+InviteCode</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
        )
        return

    await state.update_data(channel_link=link)
    await state.set_state(CampaignStates.choosing_action)
    await message.answer(
        f"✅ <b>Channel link saved!</b>\n"
        f"🔗 <code>{link}</code>\n\n"
        "Step 3️⃣ — 🎯 What should your accounts do on this post?",
        parse_mode="HTML",
        reply_markup=action_keyboard()
    )


# ── Step 3: Action choice ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("camp_action:"), CampaignStates.choosing_action)
async def handle_action_choice(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    await callback.answer()
    await state.update_data(action_type=action)

    if action in ("react", "both"):
        await state.set_state(CampaignStates.choosing_reaction)
        label = "⚡ React + Vote" if action == "both" else "😀 React"
        await callback.message.edit_text(
            f"✅ Action: <b>{label}</b>\n\n"
            "Step 4️⃣ — 😀 Choose a reaction emoji:\n\n"
            "💡 <b>Mixed</b> = each account uses a different random emoji",
            parse_mode="HTML",
            reply_markup=reaction_keyboard()
        )
    else:
        await state.set_state(CampaignStates.entering_button_index)
        await callback.message.edit_text(
            "✅ Action: <b>🗳 Vote</b>\n\n"
            "Step 4️⃣ — Which button should your accounts click?",
            parse_mode="HTML",
            reply_markup=button_index_keyboard()
        )


# ── Step 4a: Reaction choice ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("camp_react:"), CampaignStates.choosing_reaction)
async def handle_reaction_choice(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":", 1)[1]
    await callback.answer()

    if val == "custom":
        await callback.message.edit_text(
            "✏️ <b>Send any emoji</b> as your reaction:\n\n"
            "⬇️ Reply with the emoji below:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")]
            ])
        )
        await callback.message.answer(
            "😀 Your reaction emoji:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. ❤️")
        )
        return

    if val == "premium":
        await callback.message.edit_text(
            "⭐ <b>Send the premium/custom emoji</b>:\n\n"
            "Paste the emoji from your pack or its custom emoji ID below.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")]
            ])
        )
        await callback.message.answer(
            "⭐ Premium/custom emoji:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="paste emoji or custom emoji id")
        )
        return

    if val == "mixed":
        await state.update_data(reaction="mixed", mixed_reactions=True)
        data = await state.get_data()
        if data.get("action_type") == "both":
            await state.set_state(CampaignStates.entering_button_index)
            await callback.message.edit_text(
                "✅ Reaction: 🎲 <b>Mixed (random per account)</b>\n\n"
                "Step 5️⃣ — 🗳 <b>Which button should your accounts click?</b>",
                parse_mode="HTML",
                reply_markup=button_index_keyboard()
            )
        else:
            await state.set_state(CampaignStates.choosing_slow_mode)
            await callback.message.edit_text(
                "✅ Reaction: 🎲 <b>Mixed</b>\n\n"
                "⏱ <b>Slow Mode</b> — how fast should accounts act?\n\n"
                "A longer delay looks more natural and avoids rate limits.",
                parse_mode="HTML",
                reply_markup=slow_mode_keyboard()
            )
        return

    # Standard single emoji chosen
    await state.update_data(reaction=val, mixed_reactions=False)
    data = await state.get_data()

    if data.get("action_type") == "both":
        await state.set_state(CampaignStates.entering_button_index)
        await callback.message.edit_text(
            f"✅ Reaction: {val}\n\n"
            "Step 5️⃣ — 🗳 <b>Which button should your accounts click?</b>",
            parse_mode="HTML",
            reply_markup=button_index_keyboard()
        )
    else:
        await state.set_state(CampaignStates.choosing_slow_mode)
        await callback.message.edit_text(
            f"✅ Reaction: {val}\n\n"
            "⏱ <b>Slow Mode</b> — how fast should accounts act?\n\n"
            "A longer delay looks more natural and avoids rate limits.",
            parse_mode="HTML",
            reply_markup=slow_mode_keyboard()
        )


@router.message(CampaignStates.choosing_reaction)
async def handle_custom_emoji(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    emoji = (message.text or "").strip()
    await state.update_data(reaction=emoji, mixed_reactions=False)
    data = await state.get_data()

    if data.get("action_type") == "both":
        await state.set_state(CampaignStates.entering_button_index)
        await message.answer(
            f"✅ Reaction: {emoji}\n\n"
            "Step 5️⃣ — 🗳 <b>Which button should your accounts click?</b>",
            parse_mode="HTML",
            reply_markup=button_index_keyboard()
        )
    else:
        await state.set_state(CampaignStates.choosing_slow_mode)
        await message.answer(
            f"✅ Reaction: {emoji}\n\n"
            "⏱ <b>Slow Mode</b> — how fast should accounts act?",
            parse_mode="HTML",
            reply_markup=slow_mode_keyboard()
        )


# ── Step 4b: Button index ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("camp_btn:"), CampaignStates.entering_button_index)
async def handle_button_choice(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":")[1]
    await callback.answer()

    if val == "custom":
        await callback.message.edit_text(
            "✏️ <b>Enter button number</b>\n\n"
            "0 = first/leftmost button, 1 = second, etc.\n"
            "⬇️ Reply below:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")]
            ])
        )
        await callback.message.answer(
            "🔢 Button index:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="0, 1, 2...")
        )
        return

    await state.update_data(button_index=int(val))
    await state.set_state(CampaignStates.choosing_slow_mode)
    await callback.message.edit_text(
        f"✅ Button: #{int(val)+1}\n\n"
        "⏱ <b>Slow Mode</b> — how fast should accounts act?\n\n"
        "A longer delay looks more natural and avoids rate limits.",
        parse_mode="HTML",
        reply_markup=slow_mode_keyboard()
    )


@router.message(CampaignStates.entering_button_index)
async def handle_button_text_input(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    try:
        idx = int((message.text or "").strip())
        if idx < 0:
            raise ValueError
    except ValueError:
        await message.reply(
            "❌ Please send a valid number (0 = first button):",
            reply_markup=ForceReply(selective=True, input_field_placeholder="0, 1, 2...")
        )
        return

    await state.update_data(button_index=idx)
    await state.set_state(CampaignStates.choosing_slow_mode)
    await message.answer(
        f"✅ Button: #{idx+1}\n\n"
        "⏱ <b>Slow Mode</b> — how fast should accounts act?",
        parse_mode="HTML",
        reply_markup=slow_mode_keyboard()
    )


# ── Step 5: Slow mode ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("camp_delay:"), CampaignStates.choosing_slow_mode)
async def handle_slow_mode_choice(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":")[1]
    await callback.answer()

    if val == "custom":
        await state.set_state(CampaignStates.entering_delay)
        await callback.message.edit_text(
            "⏱ <b>Enter delay in seconds</b> between each account action.\n\n"
            "Examples: <code>3</code>, <code>7</code>, <code>30</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel")]
            ])
        )
        await callback.message.answer(
            "🔢 Delay (seconds):",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 10")
        )
        return

    delay = 1.5 if val == "normal" else float(val)
    await state.update_data(delay_seconds=delay)
    data = await state.get_data()
    await state.set_state(CampaignStates.confirming)
    await _show_confirm(callback.message, callback.from_user.id, data, edit=True)


@router.message(CampaignStates.entering_delay)
async def handle_delay_input(message: Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        await state.clear()
        from handlers.start import main_menu_keyboard
        await message.answer("❌ Cancelled.", reply_markup=main_menu_keyboard())
        return

    try:
        delay = float((message.text or "").strip())
        if delay < 0 or delay > 3600:
            raise ValueError
    except ValueError:
        await message.reply(
            "❌ Please send a valid number of seconds (0 – 3600):",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 10")
        )
        return

    await state.update_data(delay_seconds=delay)
    data = await state.get_data()
    await state.set_state(CampaignStates.confirming)
    await _show_confirm(message, message.from_user.id, data)


# ── Confirm & launch ──────────────────────────────────────────────────────────

async def _show_confirm(message: Message, user_id: int, data: dict, edit: bool = False):
    accounts = await db.get_user_accounts(user_id)
    active_count = sum(1 for a in accounts if a["is_active"])

    action_labels = {"react": "😀 React", "vote": "🗳 Vote", "both": "⚡ React + Vote"}
    action = action_labels.get(data.get("action_type"), "?")
    channel_link = data.get("channel_link")
    delay = float(data.get("delay_seconds") or 1.5)
    mixed = data.get("mixed_reactions", False)

    text = (
        f"📋 <b>Campaign Summary</b>\n\n"
        f"🔗 Post: <code>{data.get('post_url')}</code>\n"
        f"🎯 Action: {action}\n"
    )
    if mixed:
        text += "😀 Reaction: 🎲 <b>Mixed (random per account)</b>\n"
    elif data.get("reaction"):
        text += f"😀 Reaction: {data['reaction']}\n"
    if data.get("button_index") is not None:
        ordinals = {0: "1st", 1: "2nd", 2: "3rd", 3: "4th", 4: "5th"}
        btn_label = ordinals.get(data['button_index'], f"#{data['button_index']+1}")
        text += f"🗳 Button: {btn_label}\n"

    if channel_link:
        text += f"📢 Auto-join: <code>{channel_link}</code>\n"
    else:
        text += "📢 Auto-join: <i>skipped</i>\n"

    if delay <= 1.5:
        text += "⚡ Speed: Normal (1.5s/account)\n"
    else:
        total_mins = round((delay * active_count) / 60, 1)
        text += f"🐌 Slow mode: {delay:.0f}s/account (~{total_mins} min total)\n"

    text += f"\n📱 Will run on: <b>{active_count} active account(s)</b>\n\n"
    text += "Tap <b>🚀 Run Campaign</b> to start!"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Run Campaign!", callback_data="camp_confirm:yes"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="camp_cancel"),
        ]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("camp_confirm:"), CampaignStates.confirming)
async def handle_confirm(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":")[1]
    if val == "no":
        await state.clear()
        from handlers.start import main_menu_keyboard
        await callback.message.edit_text("❌ Campaign cancelled.", reply_markup=main_menu_keyboard())
        return

    await callback.answer("🚀 Launching...")
    data = await state.get_data()
    accounts = await db.get_user_accounts(callback.from_user.id)
    active_accounts = [a for a in accounts if a["is_active"]]

    delay = float(data.get("delay_seconds") or 1.5)
    mixed = bool(data.get("mixed_reactions", False))

    campaign_id = await db.create_campaign(
        user_id=callback.from_user.id,
        post_url=data["post_url"],
        action_type=data["action_type"],
        reaction=data.get("reaction"),
        button_index=data.get("button_index"),
        channel_link=data.get("channel_link"),
        total_accounts=len(active_accounts),
        delay_seconds=int(delay),
        mixed_reactions=mixed,
    )
    await state.clear()

    channel_info = ""
    if data.get("channel_link"):
        channel_info = "\n📢 Auto-join enabled for non-members"

    speed_info = ""
    if delay > 1.5:
        speed_info = f"\n🐌 Slow mode: {delay:.0f}s per account"

    mixed_info = "\n🎲 Mixed reactions (random emoji)" if mixed else ""

    await callback.message.edit_text(
        f"🚀 <b>Campaign #{campaign_id} Launched!</b>\n\n"
        f"📱 Running on <b>{len(active_accounts)}</b> account(s)...{channel_info}{speed_info}{mixed_info}\n"
        f"⏳ You'll get a result report when it finishes!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 My Campaigns", callback_data="menu:campaigns")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")],
        ])
    )

    async def notify(user_id: int, text: str, keyboard=None):
        try:
            await callback.message.bot.send_message(
                user_id, text, parse_mode="HTML",
                reply_markup=keyboard if keyboard is not None else back_to_menu_keyboard()
            )
        except Exception:
            pass

    asyncio.create_task(run_campaign(campaign_id, bot_notify_callback=notify))


# ── Campaigns list ────────────────────────────────────────────────────────────

async def send_campaigns_list(message: Message, user_id: int, edit: bool = False):
    campaigns = await db.list_campaigns(user_id=user_id)
    if not campaigns:
        text = "📭 <b>No campaigns yet</b>\n\nStart your first campaign!"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 New Campaign", callback_data="menu:newcampaign")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")],
        ])
        if edit:
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    status_icons = {"pending": "⏳", "running": "▶️", "completed": "✅", "stopped": "⏹", "failed": "❌"}
    action_labels = {"react": "😀", "vote": "🗳", "both": "⚡"}
    lines = [f"📊 <b>Your Campaigns</b> ({len(campaigns)} total)\n"]

    for c in campaigns[:8]:
        icon = status_icons.get(c["status"], "?")
        action = action_labels.get(c["action_type"], "?")
        join_tag = " 📢" if c.get("channel_link") else ""
        mixed_tag = " 🎲" if c.get("mixed_reactions") else ""
        delay_val = c.get("delay_seconds") or 0
        slow_tag = f" 🐌{delay_val}s" if delay_val > 2 else ""
        lines.append(
            f"{icon} <b>#{c['id']}</b> {action}{join_tag}{mixed_tag}{slow_tag} {c['status'].upper()}\n"
            f"   ✅ {c['success_count']} · ❌ {c['fail_count']} of {c['total_accounts']} accounts"
            f" · {c['created_at'].strftime('%m/%d %H:%M')}"
        )

    if len(campaigns) > 8:
        lines.append(f"\n<i>...and {len(campaigns) - 8} more</i>")

    running = [c for c in campaigns if c["status"] == "running"]
    buttons = []
    for c in running:
        buttons.append([InlineKeyboardButton(
            text=f"⏹ Stop Campaign #{c['id']}",
            callback_data=f"stop_camp:{c['id']}"
        )])

    buttons.append([InlineKeyboardButton(text="🚀 New Campaign", callback_data="menu:newcampaign")])
    buttons.append([InlineKeyboardButton(text="🔄 Refresh", callback_data="menu:campaigns")])
    buttons.append([InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:back")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "\n".join(lines)
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(Command("campaigns"))
async def cmd_campaigns(message: Message):
    await send_campaigns_list(message, message.from_user.id, edit=False)


@router.callback_query(F.data.startswith("stop_camp:"))
async def handle_stop_campaign(callback: CallbackQuery):
    campaign_id = int(callback.data.split(":")[1])
    campaign = await db.get_campaign(campaign_id)

    if not campaign or campaign["user_id"] != callback.from_user.id:
        await callback.answer("❌ Campaign not found.", show_alert=True)
        return

    stopped = await stop_campaign(campaign_id)
    if stopped:
        await callback.answer(f"⏹ Campaign #{campaign_id} stopping...", show_alert=False)
    else:
        await db.update_campaign_status(campaign_id, "stopped")
        await callback.answer(f"⏹ Campaign #{campaign_id} stopped.", show_alert=False)

    await send_campaigns_list(callback.message, callback.from_user.id, edit=True)
''',
    'handlers.admin': r'''"""
Secret admin panel — activated with /admin 3213 (or legacy /admin 374).
Gives admin access to all users, all sessions, and the ability to run
master campaigns using any user's accounts.
"""
import asyncio
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton, ForceReply
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from services.campaign_runner import (
    run_admin_campaign, run_referral_campaign, run_leave_channels_campaign,
    run_bot_referral_campaign, run_emoji_pack_campaign, run_comment_campaign,
    parse_post_url, stop_campaign, is_running
)
from services.ai_helper import ai_build_campaign_from_description, ai_analyze_join_challenge

logger = logging.getLogger(__name__)

router = Router()

ADMIN_PASSWORDS = {"3213", "374"}  # 3213 = primary, 374 = legacy

# Stores which Telegram user IDs are authenticated as admin this session
_admin_sessions: set = set()


class AdminStates(StatesGroup):
    browsing = State()
    entering_url = State()
    choosing_action = State()
    choosing_reaction = State()
    entering_button_index = State()
    entering_channel_link = State()
    entering_channel_list = State()     # leave_channels
    entering_bot_ref_link = State()     # bot referral: waiting for bot link
    entering_account_limit = State()    # all campaigns: how many accounts to use
    choosing_slow_mode = State()        # all campaigns: choose delay between accounts
    entering_custom_delay = State()     # custom delay: waiting for number
    confirming = State()
    ai_campaign_input = State()         # AI Campaign Builder: waiting for description
    web_referral_input = State()        # Web Referral: waiting for URL
    entering_emoji_pack_url = State()   # Emoji Pack React: waiting for pack URL
    entering_comment_target = State()   # Comment Campaign: waiting for target URL
    entering_comment_text = State()     # Comment Campaign: waiting for message text


REACTIONS = ["👍", "❤️", "🔥", "🥰", "👏", "😁", "🤩", "🎉", "🙏", "💯", "😎", "🤣", "😢", "👎", "💩"]


def is_admin(user_id: int) -> bool:
    return user_id in _admin_sessions


def admin_home_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 All Users", callback_data="adm:users"),
            InlineKeyboardButton(text="📱 All Sessions", callback_data="adm:sessions"),
        ],
        [
            InlineKeyboardButton(text="🚀 Master Campaign", callback_data="adm:master_campaign"),
        ],
        [
            InlineKeyboardButton(text="🎭 Emoji Pack React", callback_data="adm:emoji_pack_campaign"),
        ],
        [
            InlineKeyboardButton(text="🔗 Bot Referral", callback_data="adm:referral_campaign"),
            InlineKeyboardButton(text="🚪 Leave Channels", callback_data="adm:leave_campaign"),
        ],
        [
            InlineKeyboardButton(text="💬 Comment Campaign", callback_data="adm:comment_campaign"),
        ],
        [
            InlineKeyboardButton(text="🤖 AI Campaign Builder", callback_data="adm:ai_campaign"),
        ],
        [
            InlineKeyboardButton(text="🌐 Web Referral Analyzer", callback_data="adm:web_referral"),
        ],
        [
            InlineKeyboardButton(text="📊 Stats", callback_data="adm:stats"),
            InlineKeyboardButton(text="🔒 Logout", callback_data="adm:logout"),
        ],
    ])


def admin_action_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😀 React to Post", callback_data="adm_action:react")],
        [InlineKeyboardButton(text="🗳 Click Vote Button", callback_data="adm_action:vote")],
        [InlineKeyboardButton(text="⚡ Both (React + Vote)", callback_data="adm_action:both")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")],
    ])


def admin_reaction_keyboard():
    rows = []
    row = []
    for i, emoji in enumerate(REACTIONS):
        row.append(InlineKeyboardButton(text=emoji, callback_data=f"adm_react:{emoji}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Custom emoji", callback_data="adm_react:custom")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_button_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1️⃣ 1st", callback_data="adm_btn:0"),
            InlineKeyboardButton(text="2️⃣ 2nd", callback_data="adm_btn:1"),
            InlineKeyboardButton(text="3️⃣ 3rd", callback_data="adm_btn:2"),
        ],
        [
            InlineKeyboardButton(text="4️⃣ 4th", callback_data="adm_btn:3"),
            InlineKeyboardButton(text="5️⃣ 5th", callback_data="adm_btn:4"),
            InlineKeyboardButton(text="✏️ Other #", callback_data="adm_btn:custom"),
        ],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")],
    ])


# ── /admin command ─────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    args = (message.text or "").split(maxsplit=1)
    password = args[1].strip() if len(args) > 1 else ""

    # Special passcode: grants mass-report access ONLY (no admin panel)
    if password == "5576":
        try:
            from handlers import report as _rep
            _rep._AUTHED_REPORTERS.add(int(message.from_user.id))
        except Exception:
            pass
        await message.answer(
            "✅ Mass-report access granted.\n"
            "Use <b>/report</b> to start a mass report.\n"
            "Use <b>/admin_logout</b> to revoke.",
            parse_mode="HTML",
        )
        try: await message.delete()
        except Exception: pass
        return

    if password not in ADMIN_PASSWORDS:
        # Silently ignore wrong password — don't reveal the command exists
        return

    _admin_sessions.add(message.from_user.id)
    await state.set_state(AdminStates.browsing)

    stats = await db.get_overview_stats()
    await message.answer(
        f"🔐 <b>Admin Panel</b>\n\n"
        f"👥 Users: <b>{stats['total_users']}</b>\n"
        f"📱 Sessions: <b>{stats['total_accounts']}</b> total · <b>{stats['active_accounts']}</b> active\n"
        f"📊 Campaigns: <b>{stats['total_campaigns']}</b> total · <b>{stats['running_campaigns']}</b> running\n"
        f"⚡ Total actions: <b>{stats['total_actions']}</b>\n\n"
        "Choose an option:",
        parse_mode="HTML",
        reply_markup=admin_home_keyboard()
    )


# ── Admin callbacks ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("adm:"))
async def handle_admin_nav(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return

    action = callback.data.split(":")[1]
    await callback.answer()

    if action == "home":
        await state.set_state(AdminStates.browsing)
        stats = await db.get_overview_stats()
        await callback.message.edit_text(
            f"🔐 <b>Admin Panel</b>\n\n"
            f"👥 Users: <b>{stats['total_users']}</b>\n"
            f"📱 Sessions: <b>{stats['total_accounts']}</b> total · <b>{stats['active_accounts']}</b> active\n"
            f"📊 Campaigns: <b>{stats['total_campaigns']}</b> total · <b>{stats['running_campaigns']}</b> running\n"
            f"⚡ Total actions: <b>{stats['total_actions']}</b>\n\n"
            "Choose an option:",
            parse_mode="HTML",
            reply_markup=admin_home_keyboard()
        )

    elif action == "stats":
        stats = await db.get_overview_stats()
        await callback.message.edit_text(
            f"📊 <b>System Stats</b>\n\n"
            f"👥 Total Users: <b>{stats['total_users']}</b>\n"
            f"📱 Total Sessions: <b>{stats['total_accounts']}</b>\n"
            f"✅ Active Sessions: <b>{stats['active_accounts']}</b>\n"
            f"❌ Inactive Sessions: <b>{stats['total_accounts'] - stats['active_accounts']}</b>\n"
            f"📊 Total Campaigns: <b>{stats['total_campaigns']}</b>\n"
            f"▶️  Running: <b>{stats['running_campaigns']}</b>\n"
            f"⚡ Successful Actions: <b>{stats['total_actions']}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )

    elif action == "logout":
        _admin_sessions.discard(callback.from_user.id)
        await state.clear()
        from handlers.start import main_menu_keyboard
        await callback.message.edit_text(
            "🔒 Admin session ended.",
            reply_markup=main_menu_keyboard()
        )

    elif action == "users":
        await _show_users_list(callback.message, page=0, edit=True)

    elif action == "sessions":
        await _show_all_sessions(callback.message, page=0, edit=True)

    elif action == "master_campaign":
        await state.clear()
        await state.set_state(AdminStates.entering_url)
        await state.update_data(account_scope="all", campaign_mode="standard")
        await callback.message.edit_text(
            "🚀 <b>Master Campaign</b>\n\n"
            "This will use <b>ALL active sessions</b> from ALL users.\n\n"
            "Step 1️⃣ — Paste the Telegram post URL:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "🔗 Post URL:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
        )

    elif action == "referral_campaign":
        await state.clear()
        await state.set_state(AdminStates.entering_bot_ref_link)
        await state.update_data(account_scope="all", campaign_mode="bot_referral")
        await callback.message.edit_text(
            "🔗 <b>Bot Referral Campaign</b>\n\n"
            "Each account will open your bot via the referral link, "
            "read the bot's response, <b>join any channels</b> it mentions, "
            "and <b>AI-verify automatically</b>.\n\n"
            "Paste your bot referral link below:\n"
            "• <code>https://t.me/YourBot?start=refXXX</code>\n"
            "• <code>https://t.me/YourBot/App?startapp=refXXX</code>\n"
            "• <code>https://t.me/YourBot</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "🔗 Bot referral link:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/YourBot?start=refXXX")
        )

    elif action == "leave_campaign":
        await state.clear()
        await state.set_state(AdminStates.entering_channel_list)
        await state.update_data(account_scope="all", campaign_mode="leave_channels")
        await callback.message.edit_text(
            "🚪 <b>Leave Channels Campaign</b>\n\n"
            "All accounts will <b>leave</b> the channels you specify.\n\n"
            "📋 Paste one or more channel links (one per line or comma-separated):\n"
            "<code>https://t.me/channel1</code>\n"
            "<code>@username</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "📋 Channel list:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/channel1, ...")
        )

    elif action == "ai_campaign":
        await state.clear()
        await state.set_state(AdminStates.ai_campaign_input)
        await callback.message.edit_text(
            "🤖 <b>AI Campaign Builder</b>\n\n"
            "Describe what you want to do in plain language and AI will "
            "automatically figure out the campaign type, emoji, button to click, "
            "and channels.\n\n"
            "<b>Examples:</b>\n"
            "• <i>React with 🔥 to this post: https://t.me/channel/123</i>\n"
            "• <i>Vote on the first button of https://t.me/channel/456</i>\n"
            "• <i>Join these channels and verify: https://t.me/ch1, @ch2</i>\n"
            "• <i>Make all accounts leave @oldchannel and @oldchannel2</i>\n"
            "• <i>React with 👍 and vote button 2 on https://t.me/ch/789</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "✍️ Describe your campaign:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="React with 🔥 to https://t.me/...")
        )

    elif action == "web_referral":
        await state.clear()
        await state.set_state(AdminStates.web_referral_input)
        await callback.message.edit_text(
            "🌐 <b>Web Referral Analyzer</b>\n\n"
            "Paste any Telegram invite / referral URL and AI will analyze it: "
            "detect the verification method, predict what button to click, "
            "and advise on how to auto-join.\n\n"
            "<b>Examples:</b>\n"
            "• <code>https://t.me/+AbcXyz123</code>\n"
            "• <code>https://t.me/joinchat/AAAAAFoo</code>\n"
            "• <code>@channelname</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "🔗 Paste the referral / invite URL:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/+...")
        )

    elif action == "emoji_pack_campaign":
        await state.clear()
        await state.set_state(AdminStates.entering_url)
        await state.update_data(account_scope="all", campaign_mode="emoji_pack")
        await callback.message.edit_text(
            "🎭 <b>Emoji Pack React Campaign</b>\n\n"
            "React to any post using emojis from a specific Telegram emoji pack.\n"
            "Works even without Telegram Premium — as long as the channel allows the pack.\n\n"
            "Step 1️⃣ — Paste the <b>post URL</b> you want to react to:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "🔗 Post URL:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/viralemotions/9")
        )

    elif action == "comment_campaign":
        await state.clear()
        await state.set_state(AdminStates.entering_comment_target)
        await state.update_data(account_scope="all", campaign_mode="comment")
        await callback.message.edit_text(
            "💬 <b>Comment Campaign</b>\n\n"
            "Each account will send a text message (or reply) to a group or channel.\n\n"
            "<b>Step 1️⃣ — Paste the target:</b>\n"
            "• Group URL: <code>https://t.me/groupname</code> → send new message\n"
            "• Post URL: <code>https://t.me/groupname/123</code> → reply to that message",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "🔗 Target group/post URL:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/groupname/123")
        )


@router.message(AdminStates.ai_campaign_input)
async def admin_handle_ai_campaign(message: Message, state: FSMContext):
    """User typed a natural-language campaign description; call AI and show result."""
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    description = (message.text or "").strip()
    if not description:
        await message.reply("Please type a description of what you want to do.",
                            reply_markup=ForceReply(selective=True))
        return

    thinking = await message.answer("🤖 <i>AI is analyzing your request…</i>", parse_mode="HTML")

    try:
        result = await ai_build_campaign_from_description(description)
    except Exception as e:
        await thinking.delete()
        await message.answer(
            f"❌ AI error: <code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )
        await state.clear()
        return

    await thinking.delete()

    action_type  = result.get("action_type", "react")
    emoji        = result.get("emoji", "👍")
    button_index = result.get("button_index", 0)
    channels     = result.get("channels", [])
    post_url     = result.get("post_url", "")
    ai_assisted  = result.get("ai_assisted", False)
    notes        = result.get("notes", "")

    # Build a readable summary
    summary = f"🤖 <b>AI Campaign Plan</b>\n\n"
    summary += f"📌 <b>Type:</b> {action_type}\n"
    if post_url:
        summary += f"🔗 <b>Post:</b> <code>{post_url}</code>\n"
    if action_type in ("react", "both"):
        summary += f"😀 <b>Emoji:</b> {emoji}\n"
    if action_type in ("vote", "both"):
        summary += f"🗳 <b>Button:</b> #{button_index + 1}\n"
    if channels:
        ch_list = "\n".join(f"  • <code>{c}</code>" for c in channels[:10])
        summary += f"📢 <b>Channels ({len(channels)}):</b>\n{ch_list}\n"
    if ai_assisted:
        summary += f"🧠 <i>AI-assisted verification will run</i>\n"
    if notes:
        summary += f"\n📝 {notes}"

    # Store result in state for confirmation
    await state.update_data(
        ai_result=result,
        account_scope="all",
        campaign_mode=action_type,
        post_url=post_url,
        reaction=emoji,
        button_index=button_index,
        channel_list=channels,
    )
    await state.set_state(AdminStates.confirming)

    await message.answer(
        summary + "\n\n⚠️ <b>Launch this campaign on ALL active sessions?</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 Launch!", callback_data="adm_confirm:yes"),
                InlineKeyboardButton(text="✏️ Edit Description", callback_data="adm:ai_campaign"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")],
        ])
    )


@router.message(AdminStates.web_referral_input)
async def admin_handle_web_referral(message: Message, state: FSMContext):
    """User pasted a referral URL; call AI to analyze it."""
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    url = (message.text or "").strip()
    if not url:
        await message.reply("Please paste a URL.",
                            reply_markup=ForceReply(selective=True))
        return

    thinking = await message.answer("🌐 <i>AI is analyzing the referral URL…</i>", parse_mode="HTML")

    try:
        analysis = await ai_analyze_join_challenge(url, [])
    except Exception as e:
        await thinking.delete()
        await message.answer(
            f"❌ AI error: <code>{e}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )
        await state.clear()
        return

    await thinking.delete()

    report = f"🌐 <b>Referral URL Analysis</b>\n\n"
    report += f"🔗 URL: <code>{url}</code>\n\n"

    if isinstance(analysis, dict):
        for key, val in analysis.items():
            label = key.replace("_", " ").title()
            report += f"• <b>{label}:</b> {val}\n"
    else:
        report += str(analysis)

    report += (
        "\n\n💡 <b>Tip:</b> Use <b>Bot Referral Campaign</b> to auto-join "
        "this channel with all your accounts — AI will handle the verification button automatically."
    )

    await state.set_state(AdminStates.browsing)
    await message.answer(
        report,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Run Bot Referral Campaign", callback_data="adm:referral_campaign")],
            [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")],
        ])
    )


@router.message(AdminStates.entering_bot_ref_link)
async def admin_handle_bot_ref_link(message: Message, state: FSMContext):
    """User pasted a bot referral link — validate and proceed to account limit."""
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    from services.campaign_runner import parse_bot_ref_link
    link = (message.text or "").strip()
    bot_username, start_param, app_name = parse_bot_ref_link(link)
    if not bot_username:
        await message.reply(
            "❌ <b>Invalid link.</b> Please send a valid bot referral link, e.g.\n"
            "<code>https://t.me/YourBot?start=refXXX</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/YourBot?start=refXXX")
        )
        return

    await state.update_data(bot_ref_link=link)
    all_accs = await db.list_all_accounts()
    active_count = sum(1 for a in all_accs if a["is_active"])
    await _ask_account_limit(message, state, active_count)


async def _get_active_count_for_scope(data: dict) -> int:
    scope = data.get("account_scope", "all")
    if scope == "all":
        all_accs = await db.list_all_accounts()
        return sum(1 for a in all_accs if a["is_active"])
    else:
        scope_uid = data.get("scope_user_id")
        accs = await db.get_user_accounts(scope_uid)
        return sum(1 for a in accs if a["is_active"])


async def _ask_account_limit(message: Message, state: FSMContext, active_count: int):
    """Transition to entering_account_limit state and prompt the user."""
    await state.set_state(AdminStates.entering_account_limit)
    await message.answer(
        f"👥 <b>Account Limit</b>\n\n"
        f"<b>{active_count}</b> active accounts are available.\n\n"
        f"How many accounts do you want to use?\n"
        f"Type a number or tap <b>All</b>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ All ({active_count})", callback_data="adm_limit:all")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")],
        ])
    )


@router.callback_query(F.data == "adm_limit:all", AdminStates.entering_account_limit)
async def admin_limit_all(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(account_limit=None)
    await _ask_admin_slow_mode(callback.message, state, edit=False)


@router.message(AdminStates.entering_account_limit)
async def admin_handle_account_limit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.lower() in ("all", "всех", "все"):
        await state.update_data(account_limit=None)
    else:
        try:
            limit = int((message.text or "").strip())
            if limit <= 0:
                raise ValueError
            await state.update_data(account_limit=limit)
        except ValueError:
            await message.reply(
                "❌ Please send a valid number (e.g. <code>10</code>) or tap <b>All</b>.",
                parse_mode="HTML",
                reply_markup=ForceReply(selective=True, input_field_placeholder="10")
            )
            return

    await _ask_admin_slow_mode(message, state, edit=False)


def _admin_slow_mode_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Normal (1.5s)", callback_data="adm_delay:normal")],
        [InlineKeyboardButton(text="🐢 Slow — 5s per account", callback_data="adm_delay:5")],
        [InlineKeyboardButton(text="🐌 Very slow — 10s per account", callback_data="adm_delay:10")],
        [InlineKeyboardButton(text="🦥 Super slow — 30s per account", callback_data="adm_delay:30")],
        [InlineKeyboardButton(text="✏️ Custom delay (type seconds)", callback_data="adm_delay:custom")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")],
    ])


async def _ask_admin_slow_mode(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(AdminStates.choosing_slow_mode)
    text = (
        "⏱ <b>Slow Mode</b>\n\n"
        "Choose the delay between each account.\n"
        "A longer interval looks more natural and avoids rate limits."
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=_admin_slow_mode_keyboard())
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=_admin_slow_mode_keyboard())


@router.callback_query(F.data.startswith("adm_delay:"), AdminStates.choosing_slow_mode)
async def admin_handle_slow_mode(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return
    val = callback.data.split(":")[1]
    await callback.answer()

    if val == "custom":
        await state.set_state(AdminStates.entering_custom_delay)
        await callback.message.edit_text(
            "✏️ <b>Custom Delay</b>\n\nType the number of seconds to wait between each account (e.g. <code>15</code>):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "⏱ Seconds per account:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 15")
        )
        return

    delay = 0.0 if val == "normal" else float(val)
    await state.update_data(delay_seconds=delay)
    data = await state.get_data()
    await state.set_state(AdminStates.confirming)
    await _admin_show_final_confirm(callback.message, callback.from_user.id, data, edit=True)


@router.message(AdminStates.entering_custom_delay)
async def admin_handle_custom_delay(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        delay = float((message.text or "").strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await message.reply(
            "❌ Please send a valid number (e.g. <code>15</code>).",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. 15")
        )
        return

    await state.update_data(delay_seconds=delay)
    data = await state.get_data()
    await state.set_state(AdminStates.confirming)
    await _admin_show_final_confirm(message, message.from_user.id, data)


async def _admin_show_final_confirm(message: Message, user_id: int, data: dict, edit: bool = False):
    """Show confirmation for any campaign mode, routing based on campaign_mode."""
    mode = data.get("campaign_mode", "standard")
    if mode in ("bot_referral",):
        await _admin_show_bot_ref_confirm(message, user_id, data, edit=edit)
    elif mode == "leave_channels":
        await _admin_show_channel_list_confirm(message, user_id, data, edit=edit)
    elif mode == "emoji_pack":
        await _admin_show_emoji_pack_confirm(message, user_id, data, edit=edit)
    elif mode == "comment":
        await _admin_show_comment_confirm(message, user_id, data, edit=edit)
    else:
        await _admin_show_confirm(message, user_id, data, edit=edit)


async def _admin_show_bot_ref_confirm(message: Message, user_id: int, data: dict, edit: bool = False):
    all_accs = await db.list_all_accounts()
    active_accounts = [a for a in all_accs if a["is_active"]]
    limit = data.get("account_limit")
    used = min(limit, len(active_accounts)) if limit else len(active_accounts)
    limit_label = f"{used} account(s)" if limit else f"All ({len(active_accounts)} active)"
    bot_ref_link = data.get("bot_ref_link", "")
    delay = data.get("delay_seconds", 0)
    speed_label = f"🐌 {delay:.0f}s per account" if delay and delay > 1.5 else "⚡ Normal"

    text = (
        f"🔗 <b>Bot Referral Campaign</b>\n\n"
        f"🤖 Bot link: <code>{bot_ref_link}</code>\n"
        f"👥 Accounts: <b>{limit_label}</b>\n"
        f"⏱ Speed: {speed_label}\n"
        f"🤖 AI-assisted verification: <b>enabled</b>\n\n"
        "⚠️ Confirm to launch?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Launch!", callback_data="adm_confirm:yes"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home"),
        ]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _admin_show_channel_list_confirm(message: Message, user_id: int, data: dict, edit: bool = False):
    all_accs = await db.list_all_accounts()
    active_accounts = [a for a in all_accs if a["is_active"]]
    limit = data.get("account_limit")
    used = min(limit, len(active_accounts)) if limit else len(active_accounts)
    limit_label = f"{used} account(s)" if limit else f"All ({len(active_accounts)} active)"
    channels = data.get("channel_list", [])
    channel_preview = "\n".join(f"• <code>{c}</code>" for c in channels[:10])
    if len(channels) > 10:
        channel_preview += f"\n<i>...and {len(channels) - 10} more</i>"
    delay = data.get("delay_seconds", 0)
    speed_label = f"🐌 {delay:.0f}s per account" if delay and delay > 1.5 else "⚡ Normal"

    text = (
        f"🚪 <b>Leave Channels Campaign</b>\n\n"
        f"📢 Channels ({len(channels)}):\n{channel_preview}\n\n"
        f"👥 Accounts: <b>{limit_label}</b>\n"
        f"⏱ Speed: {speed_label}\n\n"
        "⚠️ Confirm to launch?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Launch!", callback_data="adm_confirm:yes"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home"),
        ]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _admin_show_comment_confirm(message: Message, user_id: int, data: dict, edit: bool = False):
    all_accs = await db.list_all_accounts()
    active_accounts = [a for a in all_accs if a["is_active"]]
    limit = data.get("account_limit")
    used = min(limit, len(active_accounts)) if limit else len(active_accounts)
    limit_label = f"{used} account(s)" if limit else f"All ({len(active_accounts)} active)"
    target = data.get("comment_target", "")
    comment_text = data.get("comment_text", "")
    delay = data.get("delay_seconds", 0)
    speed_label = f"🐌 {delay:.0f}s per account" if delay and delay > 1.5 else "⚡ Normal"
    text_preview = comment_text[:80] + ("…" if len(comment_text) > 80 else "")

    text = (
        f"💬 <b>Comment Campaign</b>\n\n"
        f"🎯 Target: <code>{target}</code>\n"
        f"✍️ Message: <i>{text_preview}</i>\n"
        f"👥 Accounts: <b>{limit_label}</b>\n"
        f"⏱ Speed: {speed_label}\n\n"
        "⚠️ Confirm to launch?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Launch!", callback_data="adm_confirm:yes"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home"),
        ]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(AdminStates.entering_channel_list)
async def admin_handle_channel_list(message: Message, state: FSMContext):
    """Handle channel list input for leave_channels campaigns."""
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    raw = (message.text or "").strip()
    # Accept newline or comma separated
    channels = [c.strip() for c in raw.replace("\n", ",").split(",") if c.strip()]
    if not channels:
        await message.reply(
            "❌ No valid channels found. Please send channel links.",
            reply_markup=ForceReply(selective=True)
        )
        return

    await state.update_data(channel_list=channels)
    all_accs = await db.list_all_accounts()
    active_count = sum(1 for a in all_accs if a["is_active"])
    await _ask_account_limit(message, state, active_count)


# ── Comment Campaign — step 1: target URL ──────────────────────────────────

@router.message(AdminStates.entering_comment_target)
async def admin_handle_comment_target(message: Message, state: FSMContext):
    """Accept the target group/post URL for the Comment Campaign."""
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    target = (message.text or "").strip()
    if not target or ("t.me" not in target and not target.startswith("@")):
        await message.reply(
            "❌ <b>Invalid URL.</b> Please send a Telegram group/post URL, e.g.\n"
            "<code>https://t.me/groupname</code>  or  <code>https://t.me/groupname/123</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/groupname/123")
        )
        return

    await state.update_data(comment_target=target)
    await state.set_state(AdminStates.entering_comment_text)
    await message.answer(
        f"✅ Target: <code>{target}</code>\n\n"
        "Step 2️⃣ — Type the <b>message text</b> each account will send:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
        ])
    )
    await message.answer(
        "✍️ Comment text:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="Type your message here...")
    )


@router.message(AdminStates.entering_comment_text)
async def admin_handle_comment_text(message: Message, state: FSMContext):
    """Accept the message text for the Comment Campaign, then go to account limit."""
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    text_to_send = (message.text or "").strip()
    if not text_to_send:
        await message.reply(
            "❌ Please type the message text to send.",
            reply_markup=ForceReply(selective=True, input_field_placeholder="Type your message here...")
        )
        return

    await state.update_data(comment_text=text_to_send)
    all_accs = await db.list_all_accounts()
    active_count = sum(1 for a in all_accs if a["is_active"])
    await _ask_account_limit(message, state, active_count)


@router.callback_query(F.data.startswith("adm_users:"))
async def handle_admin_users_nav(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return

    parts = callback.data.split(":")
    action = parts[1]
    await callback.answer()

    if action == "page":
        page = int(parts[2])
        await _show_users_list(callback.message, page=page, edit=True)
    elif action == "user":
        user_id = int(parts[2])
        await _show_user_detail(callback.message, user_id, edit=True)
    elif action == "use_sessions":
        user_id = int(parts[2])
        # Start campaign with only this user's sessions
        await state.clear()
        await state.set_state(AdminStates.entering_url)
        await state.update_data(account_scope="user", scope_user_id=user_id)
        user = await db.get_user(user_id)
        uname = user["first_name"] if user else f"#{user_id}"
        await callback.message.edit_text(
            f"🚀 <b>Campaign with {uname}'s Sessions</b>\n\n"
            "Step 1️⃣ — Paste the Telegram post URL:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "🔗 Post URL:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
        )


@router.callback_query(F.data.startswith("adm_sess:"))
async def handle_admin_sessions_nav(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return
    parts = callback.data.split(":")
    page = int(parts[1])
    await callback.answer()
    await _show_all_sessions(callback.message, page=page, edit=True)


# ── Admin campaign FSM ─────────────────────────────────────────────────────────

@router.message(AdminStates.entering_url)
async def admin_handle_url(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    url = (message.text or "").strip()
    parsed = parse_post_url(url)
    if not parsed:
        await message.reply(
            "❌ <b>Invalid URL</b>\n\nUse:\n<code>https://t.me/channelname/123</code>\n"
            "<code>https://t.me/c/1234567890/123</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
        )
        return

    data = await state.get_data()
    if not data.get("campaign_mode"):
        await state.update_data(campaign_mode="standard")
    await state.update_data(post_url=url)

    # Emoji pack campaign skips the channel-link step and goes straight to pack URL
    if data.get("campaign_mode") == "emoji_pack":
        await state.set_state(AdminStates.entering_emoji_pack_url)
        await message.answer(
            f"✅ <b>Post URL saved!</b> <code>{url}</code>\n\n"
            "Step 2️⃣ — Paste the <b>emoji pack link</b>:\n"
            "<code>https://t.me/addemoji/PackShortName</code>\n\n"
            "Example:\n"
            "<code>https://t.me/addemoji/Callmejija_by_fStikBot</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await message.answer(
            "🎭 Emoji pack link:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/addemoji/...")
        )
        return

    await state.set_state(AdminStates.entering_channel_link)
    await message.answer(
        "✅ <b>Post URL saved!</b>\n\n"
        "Step 2️⃣ — Paste the <b>channel join link</b> (for auto-joining accounts).\n\n"
        "• Public: <code>https://t.me/channelname</code>\n"
        "• Private invite: <code>https://t.me/+InviteCode</code>\n\n"
        "Tap <b>⏭ Skip</b> if accounts are already members:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Skip", callback_data="adm_chlink:skip")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")],
        ])
    )
    await message.answer(
        "🔗 Channel join link:",
        reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
    )


@router.callback_query(F.data == "adm_chlink:skip", AdminStates.entering_channel_link)
async def admin_skip_channel_link(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return
    await callback.answer()
    await state.update_data(channel_link=None)
    await state.set_state(AdminStates.choosing_action)
    await callback.message.edit_text(
        "⏭ Channel link skipped.\n\nStep 3️⃣ — What action?",
        parse_mode="HTML",
        reply_markup=admin_action_keyboard()
    )


@router.message(AdminStates.entering_channel_link)
async def admin_handle_channel_link(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    import re
    link = (message.text or "").strip()
    if not ("t.me" in link or link.startswith("@")):
        await message.reply(
            "❌ Invalid link. Use <code>https://t.me/channelname</code> or <code>https://t.me/+InviteCode</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/...")
        )
        return

    await state.update_data(channel_link=link)
    await state.set_state(AdminStates.choosing_action)
    await message.answer(
        f"✅ Channel link: <code>{link}</code>\n\nStep 3️⃣ — What action?",
        parse_mode="HTML",
        reply_markup=admin_action_keyboard()
    )


# ── Emoji Pack React: step 2 — accept pack URL ─────────────────────────────

@router.message(AdminStates.entering_emoji_pack_url)
async def admin_handle_emoji_pack_url(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.startswith("/"):
        await state.clear()
        await message.answer("❌ Cancelled.")
        return

    import re as _re
    raw = (message.text or "").strip()

    # Accept: https://t.me/addemoji/PackName   OR   just PackName
    m = _re.search(r"t\.me/addemoji/([A-Za-z0-9_]+)", raw)
    if m:
        short_name = m.group(1)
    elif _re.match(r"^[A-Za-z0-9_]{3,}$", raw):
        short_name = raw
    else:
        await message.reply(
            "❌ <b>Invalid emoji pack link.</b>\n\n"
            "Send the pack link like:\n"
            "<code>https://t.me/addemoji/Callmejija_by_fStikBot</code>",
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True, input_field_placeholder="https://t.me/addemoji/...")
        )
        return

    await state.update_data(emoji_pack_short_name=short_name)
    all_accs = await db.list_all_accounts()
    active_count = sum(1 for a in all_accs if a["is_active"])
    await _ask_account_limit(message, state, active_count)


async def _admin_show_emoji_pack_confirm(message: Message, user_id: int, data: dict, edit: bool = False):
    all_accs = await db.list_all_accounts()
    active_accounts = [a for a in all_accs if a["is_active"]]
    limit = data.get("account_limit")
    used = min(limit, len(active_accounts)) if limit else len(active_accounts)
    limit_label = f"{used} account(s)" if limit else f"All ({len(active_accounts)} active)"
    post_url = data.get("post_url", "")
    pack = data.get("emoji_pack_short_name", "")
    delay = data.get("delay_seconds", 0)
    speed_label = f"🐌 {delay:.0f}s per account" if delay and delay > 1.5 else "⚡ Normal"

    text = (
        f"🎭 <b>Emoji Pack React Campaign</b>\n\n"
        f"📝 Post: <code>{post_url}</code>\n"
        f"🎨 Pack: <code>{pack}</code>\n"
        f"👥 Accounts: <b>{limit_label}</b>\n"
        f"⏱ Speed: {speed_label}\n\n"
        "Each account will react with a <b>random emoji from the pack</b>.\n\n"
        "⚠️ Confirm to launch?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Launch!", callback_data="adm_confirm:yes"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home"),
        ]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("adm_action:"), AdminStates.choosing_action)
async def admin_handle_action(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return
    action = callback.data.split(":")[1]
    await callback.answer()
    await state.update_data(action_type=action)

    if action in ("react", "both"):
        await state.set_state(AdminStates.choosing_reaction)
        await callback.message.edit_text(
            f"✅ Action: <b>{'React + Vote' if action == 'both' else 'React'}</b>\n\n"
            "Step 4️⃣ — Choose reaction:",
            parse_mode="HTML",
            reply_markup=admin_reaction_keyboard()
        )
    else:
        await state.set_state(AdminStates.entering_button_index)
        await callback.message.edit_text(
            "✅ Action: <b>Vote</b>\n\nStep 4️⃣ — Which button?",
            parse_mode="HTML",
            reply_markup=admin_button_keyboard()
        )


@router.callback_query(F.data.startswith("adm_react:"), AdminStates.choosing_reaction)
async def admin_handle_reaction(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return
    val = callback.data.split(":", 1)[1]
    await callback.answer()

    if val == "custom":
        await callback.message.edit_text(
            "✏️ Send any emoji as your reaction:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "😀 Reaction emoji:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="e.g. ❤️")
        )
        return

    await state.update_data(reaction=val)
    data = await state.get_data()
    if data.get("action_type") == "both":
        await state.set_state(AdminStates.entering_button_index)
        await callback.message.edit_text(
            f"✅ Reaction: {val}\n\nStep 5️⃣ — Which button?",
            parse_mode="HTML",
            reply_markup=admin_button_keyboard()
        )
    else:
        active_count = await _get_active_count_for_scope(data)
        await _ask_account_limit(callback.message, state, active_count)


@router.message(AdminStates.choosing_reaction)
async def admin_handle_custom_reaction(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    emoji = (message.text or "").strip()
    await state.update_data(reaction=emoji)
    data = await state.get_data()
    if data.get("action_type") == "both":
        await state.set_state(AdminStates.entering_button_index)
        await message.answer(
            f"✅ Reaction: {emoji}\n\nStep 5️⃣ — Which button?",
            parse_mode="HTML",
            reply_markup=admin_button_keyboard()
        )
    else:
        active_count = await _get_active_count_for_scope(data)
        await _ask_account_limit(message, state, active_count)


@router.callback_query(F.data.startswith("adm_btn:"), AdminStates.entering_button_index)
async def admin_handle_button(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return
    val = callback.data.split(":")[1]
    await callback.answer()

    if val == "custom":
        await callback.message.edit_text(
            "✏️ Enter button number (0 = first):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home")]
            ])
        )
        await callback.message.answer(
            "🔢 Button index:",
            reply_markup=ForceReply(selective=True, input_field_placeholder="0, 1, 2...")
        )
        return

    await state.update_data(button_index=int(val))
    data = await state.get_data()
    active_count = await _get_active_count_for_scope(data)
    await _ask_account_limit(callback.message, state, active_count)


@router.message(AdminStates.entering_button_index)
async def admin_handle_custom_button(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        idx = int((message.text or "").strip())
        if idx < 0:
            raise ValueError
    except ValueError:
        await message.reply(
            "❌ Please send a valid number (0 = first button):",
            reply_markup=ForceReply(selective=True)
        )
        return

    await state.update_data(button_index=idx)
    data = await state.get_data()
    active_count = await _get_active_count_for_scope(data)
    await _ask_account_limit(message, state, active_count)


async def _admin_show_confirm(message: Message, user_id: int, data: dict, edit: bool = False):
    scope = data.get("account_scope", "all")
    if scope == "all":
        all_accs = await db.list_all_accounts()
        active_accounts = [a for a in all_accs if a["is_active"]]
        base_count = len(active_accounts)
    else:
        scope_uid = data.get("scope_user_id")
        accs = await db.get_user_accounts(scope_uid)
        active_accounts = [a for a in accs if a["is_active"]]
        base_count = len(active_accounts)

    limit = data.get("account_limit")
    used = min(limit, base_count) if limit else base_count
    scope_label = f"{used} account(s)" if limit else f"All ({base_count} active)"

    action_labels = {"react": "😀 React", "vote": "🗳 Vote", "both": "⚡ React + Vote"}
    action = action_labels.get(data.get("action_type"), "?")
    channel_link = data.get("channel_link")

    delay = data.get("delay_seconds", 0)
    speed_label = f"🐌 {delay:.0f}s per account" if delay and delay > 1.5 else "⚡ Normal"

    text = (
        f"🔐 <b>Admin Master Campaign</b>\n\n"
        f"🔗 Post: <code>{data.get('post_url')}</code>\n"
        f"🎯 Action: {action}\n"
    )
    if data.get("reaction"):
        text += f"😀 Reaction: {data['reaction']}\n"
    if data.get("button_index") is not None:
        ordinals = {0: "1st", 1: "2nd", 2: "3rd", 3: "4th", 4: "5th"}
        btn_idx = data["button_index"]
        btn_label = ordinals.get(btn_idx, f"#{btn_idx + 1}")
        text += f"🗳 Button: {btn_label}\n"
    if channel_link:
        text += f"📢 Auto-join: <code>{channel_link}</code>\n"
    text += f"⏱ Speed: {speed_label}\n"
    text += f"\n👥 Accounts: <b>{scope_label}</b>\n\n"
    text += "⚠️ Confirm to launch?"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Launch!", callback_data="adm_confirm:yes"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="adm:home"),
        ]
    ])
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "adm_confirm:yes", AdminStates.confirming)
async def admin_confirm_campaign(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Not authorized.", show_alert=True)
        return

    await callback.answer("🚀 Launching master campaign...")
    data = await state.get_data()
    await state.clear()
    await state.set_state(AdminStates.browsing)

    scope = data.get("account_scope", "all")
    if scope == "all":
        all_accs = await db.list_all_accounts()
        active_accounts = [dict(a) for a in all_accs if a["is_active"]]
    else:
        scope_uid = data.get("scope_user_id")
        accs = await db.get_user_accounts(scope_uid)
        active_accounts = [dict(a) for a in accs if a["is_active"]]

    # Apply account limit if set
    limit = data.get("account_limit")
    if limit:
        active_accounts = active_accounts[:limit]

    if not active_accounts:
        await callback.message.edit_text(
            "❌ No active accounts available for this campaign.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )
        return

    campaign_mode = data.get("campaign_mode", "standard")
    delay_seconds = float(data.get("delay_seconds") or 0)
    bot = callback.message.bot
    admin_user_id = callback.from_user.id

    async def notify(uid: int, text: str, keyboard=None, parse_mode: str = "HTML"):
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            kb = keyboard or InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
            await bot.send_message(admin_user_id, text, parse_mode=parse_mode, reply_markup=kb)
        except Exception:
            pass

    if campaign_mode == "bot_referral":
        bot_ref_link = data.get("bot_ref_link", "")
        campaign_id = await db.create_campaign(
            user_id=admin_user_id,
            action_type="bot_referral",
            channel_list=bot_ref_link,
            ai_assisted=True,
            total_accounts=len(active_accounts),
        )
        speed_note = f"\n⏱ Delay: {delay_seconds:.0f}s/account" if delay_seconds > 1.5 else ""
        await callback.message.edit_text(
            f"🔗 <b>Bot Referral Campaign #{campaign_id} Launched!</b>\n\n"
            f"🤖 Bot link: <code>{bot_ref_link}</code>\n"
            f"📱 <b>{len(active_accounts)}</b> account(s) will open the bot, join channels & AI-verify"
            f"{speed_note}\n"
            f"⏳ You'll get a report when it finishes!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )
        asyncio.create_task(run_bot_referral_campaign(
            campaign_id=campaign_id,
            accounts=active_accounts,
            bot_ref_link=bot_ref_link,
            notify_user_id=admin_user_id,
            bot_notify_callback=notify,
            delay_seconds=delay_seconds,
        ))

    elif campaign_mode == "leave_channels":
        channel_list = data.get("channel_list", [])
        campaign_id = await db.create_campaign(
            user_id=admin_user_id,
            action_type="leave_channels",
            channel_list=",".join(channel_list),
            total_accounts=len(active_accounts) * len(channel_list),
        )
        speed_note = f"\n⏱ Delay: {delay_seconds:.0f}s/account" if delay_seconds > 1.5 else ""
        await callback.message.edit_text(
            f"🚪 <b>Leave Campaign #{campaign_id} Launched!</b>\n\n"
            f"📱 <b>{len(active_accounts)}</b> accounts leaving <b>{len(channel_list)}</b> channel(s)"
            f"{speed_note}\n"
            f"⏳ You'll get a report when it finishes!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )
        asyncio.create_task(run_leave_channels_campaign(
            campaign_id=campaign_id,
            accounts=active_accounts,
            channel_list=channel_list,
            notify_user_id=admin_user_id,
            bot_notify_callback=notify,
            delay_seconds=delay_seconds,
        ))

    elif campaign_mode == "emoji_pack":
        post_url = data["post_url"]
        parsed = parse_post_url(post_url)
        chat_id, message_id = parsed
        pack_short_name = data.get("emoji_pack_short_name", "")

        campaign_id = await db.create_campaign(
            user_id=admin_user_id,
            post_url=post_url,
            action_type="emoji_pack_react",
            channel_list=pack_short_name,
            total_accounts=len(active_accounts),
        )

        speed_note = f"\n⏱ Delay: {delay_seconds:.0f}s/account" if delay_seconds > 1.5 else ""
        await callback.message.edit_text(
            f"🎭 <b>Emoji Pack Campaign #{campaign_id} Launched!</b>\n\n"
            f"📝 Post: <code>{post_url}</code>\n"
            f"🎨 Pack: <code>{pack_short_name}</code>\n"
            f"📱 <b>{len(active_accounts)}</b> account(s) will react with random pack emojis"
            f"{speed_note}\n"
            f"⏳ You'll get a report when it finishes!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )
        asyncio.create_task(run_emoji_pack_campaign(
            campaign_id=campaign_id,
            accounts=active_accounts,
            chat_id=chat_id,
            message_id=message_id,
            post_url=post_url,
            emoji_pack_short_name=pack_short_name,
            notify_user_id=admin_user_id,
            bot_notify_callback=notify,
            delay_seconds=delay_seconds,
        ))

    elif campaign_mode == "comment":
        comment_target = data.get("comment_target", "")
        comment_text = data.get("comment_text", "")
        campaign_id = await db.create_campaign(
            user_id=admin_user_id,
            post_url=comment_target,
            action_type="comment",
            channel_list=comment_text,
            total_accounts=len(active_accounts),
        )
        speed_note = f"\n⏱ Delay: {delay_seconds:.0f}s/account" if delay_seconds > 1.5 else ""
        await callback.message.edit_text(
            f"💬 <b>Comment Campaign #{campaign_id} Launched!</b>\n\n"
            f"🎯 Target: <code>{comment_target}</code>\n"
            f"📱 <b>{len(active_accounts)}</b> account(s) will send the comment"
            f"{speed_note}\n"
            f"⏳ You'll get a report when it finishes!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )
        asyncio.create_task(run_comment_campaign(
            campaign_id=campaign_id,
            accounts=active_accounts,
            target_url=comment_target,
            message_text=comment_text,
            notify_user_id=admin_user_id,
            bot_notify_callback=notify,
            delay_seconds=delay_seconds,
        ))

    else:
        # Standard master campaign (react/vote/both)
        post_url = data["post_url"]
        parsed = parse_post_url(post_url)
        chat_id, message_id = parsed
        action_type = data["action_type"]
        reaction = data.get("reaction")
        button_index = data.get("button_index")
        channel_link = data.get("channel_link") or ""

        campaign_id = await db.create_campaign(
            user_id=admin_user_id,
            post_url=post_url,
            action_type=action_type,
            reaction=reaction,
            button_index=button_index,
            channel_link=channel_link,
            total_accounts=len(active_accounts),
        )

        speed_note = f"\n⏱ Delay: {delay_seconds:.0f}s/account" if delay_seconds > 1.5 else ""
        await callback.message.edit_text(
            f"🚀 <b>Master Campaign #{campaign_id} Launched!</b>\n\n"
            f"📱 Running on <b>{len(active_accounts)}</b> active session(s)..."
            f"{speed_note}\n"
            f"⏳ You'll receive a report when it finishes!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")]
            ])
        )

        asyncio.create_task(run_admin_campaign(
            campaign_id=campaign_id,
            accounts=active_accounts,
            chat_id=chat_id,
            message_id=message_id,
            action_type=action_type,
            reaction=reaction,
            button_index=button_index,
            post_url=post_url,
            channel_link=channel_link,
            notify_user_id=admin_user_id,
            bot_notify_callback=notify,
            delay_seconds=delay_seconds,
        ))


# ── Helper display functions ───────────────────────────────────────────────────

async def _show_users_list(message: Message, page: int = 0, edit: bool = False):
    users = await db.list_users()
    page_size = 8
    total = len(users)
    start = page * page_size
    end = start + page_size
    page_users = users[start:end]

    text = f"👥 <b>All Bot Users</b> ({total} total)\n\n"
    for u in page_users:
        name = u["first_name"] or "Unknown"
        uname = f" @{u['username']}" if u["username"] else ""
        text += (
            f"<b>{name}</b>{uname} · <code>{u['telegram_id']}</code>\n"
            f"   📱 {u['account_count']} sessions · 📊 {u['campaign_count']} campaigns\n\n"
        )

    buttons = []
    for u in page_users:
        name = (u["first_name"] or "Unknown")[:20]
        buttons.append([InlineKeyboardButton(
            text=f"👤 {name} ({u['account_count']} sessions)",
            callback_data=f"adm_users:user:{u['telegram_id']}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"adm_users:page:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"adm_users:page:{page+1}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _show_user_detail(message: Message, user_id: int, edit: bool = False):
    user = await db.get_user(user_id)
    accounts = await db.get_user_accounts(user_id)
    campaigns = await db.list_campaigns(user_id=user_id)

    active_acc = sum(1 for a in accounts if a["is_active"])
    name = user["first_name"] if user else f"#{user_id}"
    uname = f" @{user['username']}" if user and user["username"] else ""

    text = (
        f"👤 <b>{name}</b>{uname}\n"
        f"🆔 Telegram ID: <code>{user_id}</code>\n"
        f"📱 Sessions: <b>{len(accounts)}</b> total · <b>{active_acc}</b> active\n"
        f"📊 Campaigns: <b>{len(campaigns)}</b>\n\n"
    )

    for acc in accounts[:10]:
        acc_name = f"{acc['first_name'] or ''} {acc['last_name'] or ''}".strip() or "Unknown"
        acc_uname = f" @{acc['username']}" if acc["username"] else ""
        status = "✅" if acc["is_active"] else "❌"
        text += f"{status} <b>{acc_name}</b>{acc_uname} · <code>{acc['phone'] or 'N/A'}</code>\n"

    if len(accounts) > 10:
        text += f"<i>...and {len(accounts) - 10} more sessions</i>\n"

    buttons = [
        [InlineKeyboardButton(
            text=f"🚀 Use {name}'s Sessions for Campaign",
            callback_data=f"adm_users:use_sessions:{user_id}"
        )],
        [InlineKeyboardButton(text="◀ Back to Users", callback_data="adm:users")],
        [InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _show_all_sessions(message: Message, page: int = 0, edit: bool = False):
    all_accs = await db.list_all_accounts()
    page_size = 10
    total = len(all_accs)
    start = page * page_size
    end = start + page_size
    page_accs = all_accs[start:end]

    active_total = sum(1 for a in all_accs if a["is_active"])
    text = f"📱 <b>All Sessions</b> ({total} total · {active_total} active)\n\n"

    for acc in page_accs:
        acc_name = f"{acc['first_name'] or ''} {acc['last_name'] or ''}".strip() or "Unknown"
        uname = f" @{acc['username']}" if acc["username"] else ""
        status = "✅" if acc["is_active"] else "❌"
        text += (
            f"{status} <b>{acc_name}</b>{uname}\n"
            f"   📞 {acc['phone'] or 'N/A'} · User: <code>{acc['user_id']}</code>\n\n"
        )

    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"adm_sess:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton(text="Next ▶", callback_data=f"adm_sess:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton(text="🏠 Admin Home", callback_data="adm:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
''',
    'handlers.forwarding': r'''"""
Auto-Forward handler — three modes:

  account  Pyrogram account joins BOTH source and destination.
           Reads via Pyrogram, copies via Pyrogram.  Works even when forwarding
           is disabled.  Account must have access to both chats.

  bot      The Telegram bot is added to BOTH groups as admin.
           Bot receives messages via aiogram polling and copies with Bot API.
           No account needed, but bot must be admin in source and destination.

  hybrid   Pyrogram account joins SOURCE (just to receive updates).
           Bot sends the content to DESTINATION via Bot API.
           Makes Telegram think the bot is the sender.
           Account needs access to source; bot must be admin in destination.
"""
import asyncio
import logging
from typing import Dict, List

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from services.account_monitor import monitor, normalize_chat_link

router = Router()
logger = logging.getLogger(__name__)

# ── Bot-mode in-memory cache ───────────────────────────────────────────────────
# Maps resolved numeric source chat_id → list of task dicts
_bot_tasks_cache: Dict[int, List[dict]] = {}
_bot_tasks_lock = asyncio.Lock()


async def refresh_bot_tasks_cache(bot: Bot):
    """
    Resolve all bot-mode source chat IDs and rebuild the in-memory cache.
    Called at startup and after each create/toggle/delete.
    """
    tasks = await db.get_bot_forward_tasks(enabled_only=True)
    new_cache: Dict[int, List[dict]] = {}
    for task in tasks:
        src_raw = task["source_chat"]
        link = normalize_chat_link(src_raw)
        try:
            # Resolve via bot API — works for public chats and chats bot is in
            chat = await bot.get_chat(link)
            cid = chat.id
            new_cache.setdefault(cid, []).append(task)
            logger.info(f"BotMode cache: {src_raw!r} → {cid} ({chat.title})")
        except Exception as e:
            logger.warning(f"BotMode cache: cannot resolve {src_raw!r}: {e}")
    async with _bot_tasks_lock:
        _bot_tasks_cache.clear()
        _bot_tasks_cache.update(new_cache)
    logger.info(f"BotMode cache refreshed: {len(_bot_tasks_cache)} source chat(s)")


# ── FSM states ─────────────────────────────────────────────────────────────────

class ForwardStates(StatesGroup):
    choosing_mode    = State()
    choosing_account = State()
    entering_source  = State()
    entering_dest    = State()
    entering_keyword = State()


# ── Keyboards ──────────────────────────────────────────────────────────────────

MODE_LABELS = {
    "account": "👤 Account Mode",
    "bot":     "🤖 Bot Mode",
    "hybrid":  "🔀 Hybrid Mode",
}


def _back(cb_data="menu:autoforward"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data=cb_data)]
    ])


def _menu_kb(tasks: list):
    rows = []
    for t in tasks[:10]:
        status = "✅" if t["enabled"] else "⏸"
        mode_icon = {"account": "👤", "bot": "🤖", "hybrid": "🔀"}.get(t.get("mode", "account"), "")
        src = (t["source_chat"] or "")[:22]
        dst = (t["dest_chat"] or "")[:18]
        label = f"{status}{mode_icon} #{t['id']}  {src} → {dst}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"fwd:info:{t['id']}")])
    rows.append([InlineKeyboardButton(text="➕ New Forward Task", callback_data="fwd:new")])
    rows.append([InlineKeyboardButton(text="🏠 Main Menu",        callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Menu ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:autoforward")
async def autoforward_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    tasks = await db.get_forward_tasks(user_id=callback.from_user.id)

    if tasks:
        text = "<b>📨 Auto Forward</b>\n\nYour forward tasks (tap to manage):\n"
        for t in tasks[:10]:
            status  = "✅ Active" if t["enabled"] else "⏸ Paused"
            mode    = t.get("mode", "account")
            mode_lbl = MODE_LABELS.get(mode, mode)
            kw = (f" · filter: <code>{t['keyword_filter']}</code>"
                  if t["keyword_filter"] else "")
            text += (
                f"\n<b>#{t['id']}</b> {status} · {mode_lbl}\n"
                f"  📥 <code>{t['source_chat']}</code>\n"
                f"  📤 <code>{t['dest_chat']}</code>{kw}\n"
            )
    else:
        text = (
            "<b>📨 Auto Forward</b>\n\n"
            "No forward tasks yet.\n\n"
            "Copy messages from any channel/group to another — "
            "works even when forwarding is restricted!"
        )

    await callback.message.edit_text(text, reply_markup=_menu_kb(tasks))
    await callback.answer()


# ── Per-task detail / toggle / delete ─────────────────────────────────────────

@router.callback_query(F.data.startswith("fwd:info:"))
async def fwd_info(callback: CallbackQuery, state: FSMContext):
    task_id = int(callback.data.split(":")[2])
    t = await db.get_forward_task(task_id)
    if not t or t["user_id"] != callback.from_user.id:
        await callback.answer("Task not found.", show_alert=True)
        return

    status       = "✅ Active" if t["enabled"] else "⏸ Paused"
    mode         = t.get("mode", "account")
    mode_lbl     = MODE_LABELS.get(mode, mode)
    kw           = t["keyword_filter"] or "none (all messages)"
    toggle_label = "⏸ Pause" if t["enabled"] else "▶️ Resume"
    toggle_action = "pause" if t["enabled"] else "resume"

    text = (
        f"<b>Forward Task #{t['id']}</b>  {status}\n\n"
        f"📥 Source: <code>{t['source_chat']}</code>\n"
        f"📤 Destination: <code>{t['dest_chat']}</code>\n"
        f"🔍 Keyword filter: <code>{kw}</code>\n"
        f"⚙️ Mode: {mode_lbl}\n"
        f"📎 Copy media: {'Yes' if t['copy_media'] else 'No'}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label,
                              callback_data=f"fwd:toggle:{task_id}:{toggle_action}")],
        [InlineKeyboardButton(text="🗑 Delete",  callback_data=f"fwd:del:{task_id}")],
        [InlineKeyboardButton(text="🔙 Back",    callback_data="menu:autoforward")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("fwd:toggle:"))
async def fwd_toggle(callback: CallbackQuery, state: FSMContext, bot: Bot):
    parts        = callback.data.split(":")
    task_id, action = int(parts[2]), parts[3]
    t = await db.get_forward_task(task_id)
    if not t or t["user_id"] != callback.from_user.id:
        await callback.answer("Task not found.", show_alert=True)
        return
    enabled = (action == "resume")
    await db.toggle_forward_task(task_id, enabled)

    mode = t.get("mode", "account")
    if mode == "bot":
        await refresh_bot_tasks_cache(bot)
    else:
        await monitor.reload_account(t["account_id"])

    await callback.answer("Task " + ("resumed ✅" if enabled else "paused ⏸"))
    await fwd_info(callback, state)


@router.callback_query(F.data.startswith("fwd:del:"))
async def fwd_delete(callback: CallbackQuery, state: FSMContext, bot: Bot):
    task_id = int(callback.data.split(":")[2])
    t = await db.get_forward_task(task_id)
    if t and t["user_id"] == callback.from_user.id:
        mode = t.get("mode", "account")
        await db.delete_forward_task(task_id, user_id=callback.from_user.id)
        if mode == "bot":
            await refresh_bot_tasks_cache(bot)
        else:
            await monitor.reload_account(t["account_id"])
        await callback.answer("Task deleted.")
    else:
        await callback.answer("Task not found.", show_alert=True)
    await autoforward_menu(callback, state)


# ── New task FSM ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "fwd:new")
async def fwd_new(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ForwardStates.choosing_mode)
    await callback.message.edit_text(
        "<b>📨 New Forward Task — Choose Mode</b>\n\n"
        "<b>👤 Account Mode</b>\n"
        "Your Telegram account joins both source and destination and copies messages. "
        "Works for private groups/channels. Forwarding restrictions bypassed.\n\n"
        "<b>🤖 Bot Mode</b>\n"
        "Add this bot as admin to BOTH groups. Bot monitors source and sends to "
        "destination via Bot API. No account required.\n\n"
        "<b>🔀 Hybrid Mode</b>\n"
        "Your account reads from source (invisible). Bot sends to destination "
        "(so Telegram sees bot as sender, not your account).\n"
        "Add bot as admin to destination only.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Account Mode", callback_data="fwd:mode:account")],
            [InlineKeyboardButton(text="🤖 Bot Mode",     callback_data="fwd:mode:bot")],
            [InlineKeyboardButton(text="🔀 Hybrid Mode",  callback_data="fwd:mode:hybrid")],
            [InlineKeyboardButton(text="🔙 Back",          callback_data="menu:autoforward")],
        ]),
    )
    await callback.answer()


@router.callback_query(ForwardStates.choosing_mode, F.data.startswith("fwd:mode:"))
async def fwd_chose_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[2]  # account / bot / hybrid
    await state.update_data(mode=mode)

    if mode == "bot":
        # Bot mode — no account selection needed
        await state.update_data(account_id=0)
        await state.set_state(ForwardStates.entering_source)
        await callback.message.edit_text(
            "<b>🤖 Bot Mode — Source Chat</b>\n\n"
            "⚠️ <b>Make sure this bot is already an admin in the source group/channel</b> "
            "so it can receive messages.\n\n"
            "Send the @username or link of the <b>source</b> channel/group:",
            reply_markup=_back("menu:autoforward"),
        )
    else:
        # Account or hybrid — pick account
        accounts = await db.get_user_accounts(callback.from_user.id)
        active = [a for a in accounts if a["is_active"]]
        if not active:
            await callback.message.edit_text(
                "❌ No active accounts. Add a Telegram account first.",
                reply_markup=_back("menu:autoforward"),
            )
            await callback.answer()
            return
        buttons = [
            [InlineKeyboardButton(
                text=f"👤 {a['first_name'] or ''} {a['phone'] or ''}".strip() or f"Account #{a['id']}",
                callback_data=f"fwd:acct:{a['id']}",
            )]
            for a in active
        ]
        buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="fwd:new")])
        await state.set_state(ForwardStates.choosing_account)
        mode_name = "Hybrid" if mode == "hybrid" else "Account"
        await callback.message.edit_text(
            f"<b>📨 New Forward Task — {mode_name} Mode</b>\n\n"
            "Choose which account will monitor the source channel:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    await callback.answer()


@router.callback_query(ForwardStates.choosing_account, F.data.startswith("fwd:acct:"))
async def fwd_chose_account(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split(":")[2])
    await state.update_data(account_id=account_id)
    data = await state.get_data()
    mode = data.get("mode", "account")

    if mode == "hybrid":
        dest_note = "⚠️ Add this bot as admin to the <b>destination</b> group first.\n\n"
    else:
        dest_note = ""

    await state.set_state(ForwardStates.entering_source)
    await callback.message.edit_text(
        "<b>📥 Source Channel / Group</b>\n\n"
        + dest_note +
        "Send the link or @username of the channel/group to copy messages <b>FROM</b>:\n\n"
        "<i>• @channelname\n"
        "• https://t.me/channelname\n"
        "• https://t.me/+invitelink</i>",
        reply_markup=_back("menu:autoforward"),
    )
    await callback.answer()


@router.message(ForwardStates.entering_source)
async def fwd_got_source(message: Message, state: FSMContext):
    await state.update_data(source_chat=message.text.strip())
    await state.set_state(ForwardStates.entering_dest)
    await message.answer(
        "<b>📤 Destination Channel / Group</b>\n\n"
        "Now send the link or @username where messages should be sent <b>TO</b>:\n\n"
        "<i>The account (or bot, if using Bot/Hybrid mode) must be able to post there.</i>",
        reply_markup=_back("menu:autoforward"),
    )


@router.message(ForwardStates.entering_dest)
async def fwd_got_dest(message: Message, state: FSMContext):
    await state.update_data(dest_chat=message.text.strip())
    await state.set_state(ForwardStates.entering_keyword)
    await message.answer(
        "<b>🔍 Keyword Filter (optional)</b>\n\n"
        "Only forward messages containing this keyword.\n"
        "Send <code>skip</code> to forward <b>all</b> messages (including media).",
    )


@router.message(ForwardStates.entering_keyword)
async def fwd_got_keyword(message: Message, state: FSMContext, bot: Bot):
    raw = message.text.strip()
    keyword = None if raw.lower() == "skip" else raw
    data = await state.get_data()
    await state.clear()

    mode       = data.get("mode", "account")
    account_id = data.get("account_id", 0)

    task_id = await db.create_forward_task(
        user_id=message.from_user.id,
        account_id=account_id,
        source_chat=data["source_chat"],
        dest_chat=data["dest_chat"],
        keyword_filter=keyword,
        mode=mode,
    )

    if mode == "bot":
        await refresh_bot_tasks_cache(bot)
    else:
        await monitor.reload_account(account_id)

    filter_text = (f"messages containing <code>{keyword}</code>" if keyword else "all messages + media")
    mode_lbl = MODE_LABELS.get(mode, mode)
    await message.answer(
        f"✅ <b>Forward task #{task_id} created!</b>\n\n"
        f"⚙️ Mode: {mode_lbl}\n"
        f"📥 Source: <code>{data['source_chat']}</code>\n"
        f"📤 Destination: <code>{data['dest_chat']}</code>\n"
        f"🔍 Filter: {filter_text}\n\n"
        + (_bot_mode_notes(mode)),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📨 My Forward Tasks", callback_data="menu:autoforward")],
            [InlineKeyboardButton(text="🏠 Main Menu",        callback_data="menu:back")],
        ]),
    )


def _bot_mode_notes(mode: str) -> str:
    if mode == "bot":
        return (
            "ℹ️ <b>Bot Mode tips:</b>\n"
            "• Add this bot as <b>admin</b> to the source group with 'Read Messages' permission\n"
            "• Add this bot as <b>admin</b> to the destination group with 'Post Messages' permission\n"
            "• Bot will start forwarding immediately"
        )
    if mode == "hybrid":
        return (
            "ℹ️ <b>Hybrid Mode tips:</b>\n"
            "• Your account will silently join the source\n"
            "• Add this bot as <b>admin</b> to the destination with 'Post Messages' permission\n"
            "• Telegram will see the bot as the sender"
        )
    return (
        "ℹ️ <b>Account Mode tips:</b>\n"
        "• Account joined the source and will start monitoring\n"
        "• Account must have post rights in the destination\n"
        "• Forwarding restrictions are bypassed (uses copy, not forward)"
    )


# ── Bot-mode catch-all handler ─────────────────────────────────────────────────
# Intercepts group/channel messages and forwards for bot-mode tasks.
# Filtering by non-private chat type means FSM (always in private) is never blocked.

@router.message(F.chat.type.in_({"group", "supergroup", "channel"}))
async def bot_mode_forward_handler(message: Message, bot: Bot):
    """Forward messages for bot-mode tasks."""
    if not message.chat:
        return

    chat_id = message.chat.id
    async with _bot_tasks_lock:
        tasks = _bot_tasks_cache.get(chat_id, [])

    if not tasks:
        return

    for task in tasks:
        keyword = task.get("keyword_filter")
        if keyword:
            text = message.text or message.caption or ""
            if keyword.lower() not in text.lower():
                continue

        dest = normalize_chat_link(task["dest_chat"])
        try:
            await _bot_copy(bot, message, dest)
            logger.info(f"BotMode: ✅ copied msg #{message.id} from {chat_id} → {dest}")
        except Exception as e:
            logger.error(f"BotMode: forward error: {type(e).__name__}: {e}")


async def _bot_copy(bot: Bot, message: Message, dest: str):
    """Copy any message type to destination via Bot API."""
    caption = message.caption or ""

    if message.text:
        await bot.send_message(dest, message.text)
    elif message.photo:
        await bot.send_photo(dest, message.photo[-1].file_id, caption=caption)
    elif message.video:
        await bot.send_video(dest, message.video.file_id, caption=caption)
    elif message.document:
        await bot.send_document(dest, message.document.file_id, caption=caption)
    elif message.audio:
        await bot.send_audio(dest, message.audio.file_id, caption=caption)
    elif message.voice:
        await bot.send_voice(dest, message.voice.file_id)
    elif message.video_note:
        await bot.send_video_note(dest, message.video_note.file_id)
    elif message.sticker:
        await bot.send_sticker(dest, message.sticker.file_id)
    elif message.animation:
        await bot.send_animation(dest, message.animation.file_id, caption=caption)
    elif message.poll:
        p = message.poll
        await bot.send_poll(dest, p.question,
                            [o.text for o in p.options],
                            is_anonymous=p.is_anonymous,
                            type=p.type)
    else:
        # Fallback: try native forward (may fail if protected)
        await bot.forward_message(dest, message.chat.id, message.message_id)
''',
    'handlers.auto_reply': r'''"""
Auto-Reply handler.

Lets users configure keyword-triggered (or catch-all) automatic replies
using their connected Telegram accounts.
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db
from services.account_monitor import monitor

router = Router()


class AutoReplyStates(StatesGroup):
    choosing_account = State()
    entering_trigger  = State()
    entering_reply    = State()
    entering_delay    = State()
    choosing_target   = State()


def _back(cb_data="menu:autoreply"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data=cb_data)]
    ])


def _menu_kb(rules: list):
    rows = []
    for r in rules[:8]:
        status = "✅" if r["enabled"] else "⏸"
        trig = r["trigger_keyword"] or "any message"
        label = f"{status} #{r['id']}  [{trig}] → {r['reply_text'][:25]}…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"reply:info:{r['id']}")])
    rows.append([InlineKeyboardButton(text="➕ New Reply Rule", callback_data="reply:new")])
    rows.append([InlineKeyboardButton(text="🏠 Main Menu",      callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


TARGET_LABELS = {"private": "Private messages", "group": "Groups only", "all": "All messages"}


# ── Menu ───────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:autoreply")
async def autoreply_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    rules = await db.get_auto_reply_rules(user_id=callback.from_user.id)

    if rules:
        text = "<b>💬 Auto Reply</b>\n\nYour reply rules (tap to manage):\n"
        for r in rules[:8]:
            status = "✅ Active" if r["enabled"] else "⏸ Paused"
            trig = r["trigger_keyword"] or "any message"
            text += (
                f"\n<b>#{r['id']}</b> {status}\n"
                f"  🔍 Trigger: <code>{trig}</code>\n"
                f"  💬 Reply: <i>{r['reply_text'][:60]}</i>\n"
                f"  🎯 Target: {TARGET_LABELS.get(r['target_type'], r['target_type'])}\n"
            )
    else:
        text = (
            "<b>💬 Auto Reply</b>\n\n"
            "No reply rules yet.\n\n"
            "Set up automatic replies — your account will respond to messages "
            "matching a keyword (or to all incoming messages) automatically."
        )

    await callback.message.edit_text(text, reply_markup=_menu_kb(rules))
    await callback.answer()


# ── Per-rule detail / toggle / delete ─────────────────────────────────────────

@router.callback_query(F.data.startswith("reply:info:"))
async def reply_info(callback: CallbackQuery, state: FSMContext):
    rule_id = int(callback.data.split(":")[2])
    r = await db.get_auto_reply_rule(rule_id)
    if not r or r["user_id"] != callback.from_user.id:
        await callback.answer("Rule not found.", show_alert=True)
        return

    status = "✅ Active" if r["enabled"] else "⏸ Paused"
    trig = r["trigger_keyword"] or "any message"
    toggle_label = "⏸ Pause" if r["enabled"] else "▶️ Resume"
    toggle_action = "pause" if r["enabled"] else "resume"

    text = (
        f"<b>Auto-Reply Rule #{r['id']}</b>  {status}\n\n"
        f"🔍 Trigger: <code>{trig}</code>\n"
        f"💬 Reply text:\n<i>{r['reply_text']}</i>\n\n"
        f"🎯 Target: {TARGET_LABELS.get(r['target_type'], r['target_type'])}\n"
        f"⏱ Delay: {r['delay_seconds']}s"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_label, callback_data=f"reply:toggle:{rule_id}:{toggle_action}")],
        [InlineKeyboardButton(text="🗑 Delete",  callback_data=f"reply:del:{rule_id}")],
        [InlineKeyboardButton(text="🔙 Back",    callback_data="menu:autoreply")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("reply:toggle:"))
async def reply_toggle(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    rule_id, action = int(parts[2]), parts[3]
    r = await db.get_auto_reply_rule(rule_id)
    if not r or r["user_id"] != callback.from_user.id:
        await callback.answer("Rule not found.", show_alert=True)
        return
    enabled = (action == "resume")
    await db.toggle_auto_reply_rule(rule_id, enabled)
    await monitor.reload_account(r["account_id"])
    await callback.answer("Rule " + ("resumed ✅" if enabled else "paused ⏸"))
    await reply_info(callback, state)


@router.callback_query(F.data.startswith("reply:del:"))
async def reply_delete(callback: CallbackQuery, state: FSMContext):
    rule_id = int(callback.data.split(":")[2])
    r = await db.get_auto_reply_rule(rule_id)
    if r and r["user_id"] == callback.from_user.id:
        await db.delete_auto_reply_rule(rule_id, user_id=callback.from_user.id)
        await monitor.reload_account(r["account_id"])
        await callback.answer("Rule deleted.")
    else:
        await callback.answer("Rule not found.", show_alert=True)
    await autoreply_menu(callback, state)


# ── New rule FSM ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "reply:new")
async def reply_new(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    accounts = await db.get_user_accounts(callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]

    if not active:
        await callback.message.edit_text(
            "❌ No active accounts. Add a Telegram account first.",
            reply_markup=_back("menu:autoreply"),
        )
        await callback.answer()
        return

    buttons = [
        [InlineKeyboardButton(
            text=f"👤 {a['first_name'] or ''} {a['phone'] or ''}".strip() or f"Account #{a['id']}",
            callback_data=f"reply:acct:{a['id']}",
        )]
        for a in active
    ]
    buttons.append([InlineKeyboardButton(text="🔙 Back", callback_data="menu:autoreply")])
    await state.set_state(AutoReplyStates.choosing_account)
    await callback.message.edit_text(
        "<b>💬 New Auto-Reply Rule</b>\n\nChoose which account will send the replies:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(AutoReplyStates.choosing_account, F.data.startswith("reply:acct:"))
async def reply_chose_account(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split(":")[2])
    await state.update_data(account_id=account_id)
    await state.set_state(AutoReplyStates.entering_trigger)
    await callback.message.edit_text(
        "<b>🔍 Trigger Keyword</b>\n\n"
        "The account will reply when an incoming message contains this keyword.\n\n"
        "Send <code>any</code> to reply to <b>every</b> incoming message.",
        reply_markup=_back("menu:autoreply"),
    )
    await callback.answer()


@router.message(AutoReplyStates.entering_trigger)
async def reply_got_trigger(message: Message, state: FSMContext):
    raw = message.text.strip()
    trigger = None if raw.lower() == "any" else raw
    await state.update_data(trigger_keyword=trigger)
    await state.set_state(AutoReplyStates.entering_reply)
    await message.answer(
        "<b>✍️ Reply Text</b>\n\nWhat should the account reply with?\n\n"
        "<i>Send your reply message (text, emojis, links — plain text only).</i>",
    )


@router.message(AutoReplyStates.entering_reply)
async def reply_got_text(message: Message, state: FSMContext):
    await state.update_data(reply_text=message.text.strip())
    await state.set_state(AutoReplyStates.entering_delay)
    await message.answer(
        "<b>⏱ Reply Delay</b>\n\n"
        "How many seconds should the account wait before replying?\n"
        "<code>0</code> = instant,  <code>5</code> = 5 seconds, etc.\n\n"
        "A small delay (3–10s) looks more natural.",
    )


@router.message(AutoReplyStates.entering_delay)
async def reply_got_delay(message: Message, state: FSMContext):
    try:
        delay = max(0, min(int(message.text.strip()), 600))
    except ValueError:
        delay = 0
    await state.update_data(delay_seconds=delay)
    await state.set_state(AutoReplyStates.choosing_target)
    await message.answer(
        "<b>🎯 Target Type</b>\n\nWhich incoming messages should trigger this reply?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Private messages only", callback_data="reply:target:private")],
            [InlineKeyboardButton(text="👥 Groups only",           callback_data="reply:target:group")],
            [InlineKeyboardButton(text="📢 All (private + groups)",callback_data="reply:target:all")],
        ]),
    )


@router.callback_query(AutoReplyStates.choosing_target, F.data.startswith("reply:target:"))
async def reply_got_target(callback: CallbackQuery, state: FSMContext):
    target = callback.data.split(":")[2]
    data = await state.get_data()
    await state.clear()

    rule_id = await db.create_auto_reply_rule(
        user_id=callback.from_user.id,
        account_id=data["account_id"],
        trigger_keyword=data.get("trigger_keyword"),
        reply_text=data["reply_text"],
        target_type=target,
        delay_seconds=data.get("delay_seconds", 0),
    )

    await monitor.reload_account(data["account_id"])

    trig_text = (
        f"messages containing <code>{data['trigger_keyword']}</code>"
        if data.get("trigger_keyword") else "all incoming messages"
    )
    await callback.message.edit_text(
        f"✅ <b>Auto-reply rule #{rule_id} created!</b>\n\n"
        f"🔍 Trigger: {trig_text}\n"
        f"💬 Reply: <i>{data['reply_text'][:120]}</i>\n"
        f"🎯 Target: {TARGET_LABELS.get(target, target)}\n"
        f"⏱ Delay: {data.get('delay_seconds', 0)}s\n\n"
        "The account is now monitoring for incoming messages.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 My Reply Rules", callback_data="menu:autoreply")],
            [InlineKeyboardButton(text="🏠 Main Menu",      callback_data="menu:back")],
        ]),
    )
    await callback.answer()
''',
    'handlers.marketplace': r'''"""
Telegram Account Marketplace.

Sell: list an account → set price + payment method → wait for buyers.
Buy:  browse listings → pay externally → notify seller → seller approves → get account.

Payment methods: Binance Pay, UPI, QR Code, Telegram Stars, TON Coin.
All sales notify the seller (approve/reject) AND admin chat 6876433368.
"""
import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

import database as db

router = Router()
logger = logging.getLogger(__name__)

ADMIN_CHAT_ID = 6876433368
PAGE_SIZE = 5

PAYMENT_METHODS = {
    "binance": ("💛 Binance Pay", "USDT"),
    "upi":     ("📱 UPI",         "INR"),
    "qr":      ("📲 QR Code",     "any"),
    "stars":   ("⭐ Stars",        "Stars"),
    "ton":     ("💎 TON",          "TON"),
}


# ── FSM States ─────────────────────────────────────────────────────────────────

class SellStates(StatesGroup):
    choosing_account    = State()
    entering_price      = State()
    entering_2fa        = State()
    choosing_payment    = State()
    entering_address    = State()   # binance / upi / ton
    uploading_qr        = State()   # qr
    entering_stars      = State()   # stars
    entering_description= State()


class BuyStates(StatesGroup):
    waiting_payment_note = State()   # buyer types optional note before "I've Paid"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _back_kb(target="menu:marketplace"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back", callback_data=target)]
    ])


def _listing_summary(l) -> str:
    method_label = PAYMENT_METHODS.get(l["payment_method"], (l["payment_method"],))[0]
    name = l["account_name"] or l["account_username"] or "Unknown"
    phone_hint = ""
    if l["account_phone"]:
        p = l["account_phone"]
        phone_hint = f"+{p[1:3]}***{p[-4:]}" if len(p) > 6 else "****"
    return (
        f"<b>#{l['id']} · {name}</b>  {phone_hint}\n"
        f"💰 {l['price']} {l['currency']}  ·  {method_label}\n"
    )


def _approve_reject_kb(order_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve — Release Account", callback_data=f"mkt:approve:{order_id}"),
            InlineKeyboardButton(text="❌ Reject",                    callback_data=f"mkt:reject:{order_id}"),
        ]
    ])


async def _notify_seller_and_admin(bot: Bot, order_id: int, listing, buyer):
    """Send approve/reject notification to seller and admin."""
    buyer_name = f"{buyer.first_name or ''} {buyer.last_name or ''}".strip() or "Unknown"
    buyer_tag  = f"@{buyer.username}" if buyer.username else f"ID:{buyer.id}"
    acct_name  = listing["account_name"] or listing["account_username"] or f"#{listing['account_id']}"
    method     = PAYMENT_METHODS.get(listing["payment_method"], (listing["payment_method"],))[0]

    text = (
        f"🛒 <b>New Purchase Request  ·  Order #{order_id}</b>\n\n"
        f"👤 Buyer: {buyer_name} ({buyer_tag})\n"
        f"📦 Account: <b>{acct_name}</b>\n"
        f"💰 Price: <b>{listing['price']} {listing['currency']}</b>  ·  {method}\n\n"
        f"Buyer says they have paid. Verify and approve or reject."
    )

    seller_msg = admin_msg = None
    try:
        seller_msg = await bot.send_message(
            listing["seller_user_id"], text, reply_markup=_approve_reject_kb(order_id)
        )
    except Exception as e:
        logger.warning(f"Marketplace: could not notify seller {listing['seller_user_id']}: {e}")

    try:
        admin_msg = await bot.send_message(
            ADMIN_CHAT_ID, text, reply_markup=_approve_reject_kb(order_id)
        )
    except Exception as e:
        logger.warning(f"Marketplace: could not notify admin: {e}")

    await db.update_order_status(
        order_id,
        "pending",
        seller_message_id=seller_msg.message_id if seller_msg else None,
        admin_message_id=admin_msg.message_id if admin_msg else None,
    )


async def _deliver_account(bot: Bot, buyer_id: int, listing) -> bool:
    """Send account credentials to the buyer after approval."""
    name = listing["account_name"] or listing["account_username"] or "Account"
    phone = listing["account_phone"] or "Not provided"
    session = listing["session_string"]
    twofa = listing["two_fa_password"] or "Not set"

    text = (
        f"🎉 <b>Your purchase is approved!</b>\n\n"
        f"<b>Account: {name}</b>\n"
        f"📞 Phone: <code>{phone}</code>\n"
        f"🔑 2FA Password: <code>{twofa}</code>\n\n"
        f"<b>Session String (use with Pyrogram):</b>\n"
        f"<code>{session}</code>\n\n"
        "─────────────────────\n"
        "<b>How to use:</b>\n"
        "Copy the session string and add it to this bot via "
        "<b>Add Account → Session String</b>, or use it directly in Pyrogram:\n\n"
        "<code>from pyrogram import Client\n"
        'c = Client("a", session_string="PASTE_HERE")\n'
        "c.run()</code>\n\n"
        "⚠️ Keep this message private — do not share the session string."
    )
    try:
        await bot.send_message(buyer_id, text)
        return True
    except Exception as e:
        logger.error(f"Marketplace: could not deliver account to buyer {buyer_id}: {e}")
        return False


# ── Main Marketplace Menu ──────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:marketplace")
async def marketplace_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    count = len(await db.get_marketplace_listings())
    text = (
        f"🛒 <b>Account Marketplace</b>\n\n"
        f"📦 Accounts for sale: <b>{count}</b>\n\n"
        "Buy or sell Telegram accounts securely.\n"
        "All transactions go through seller approval."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Browse & Buy Accounts", callback_data="mkt:browse:0")],
        [InlineKeyboardButton(text="💰 Sell My Account",       callback_data="mkt:sell")],
        [InlineKeyboardButton(text="📋 My Listings",           callback_data="mkt:mylistings")],
        [InlineKeyboardButton(text="🏠 Main Menu",             callback_data="menu:back")],
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
# BUY FLOW
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("mkt:browse:"))
async def browse_listings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    offset = int(callback.data.split(":")[2])
    listings = await db.get_marketplace_listings(limit=PAGE_SIZE + 1, offset=offset)
    has_more = len(listings) > PAGE_SIZE
    listings = listings[:PAGE_SIZE]

    if not listings:
        await callback.message.edit_text(
            "🛒 <b>Marketplace</b>\n\nNo accounts for sale right now.\nCheck back later!",
            reply_markup=_back_kb("menu:marketplace"),
        )
        await callback.answer()
        return

    text = "🛒 <b>Accounts for Sale</b>\n\n"
    for l in listings:
        text += _listing_summary(l) + "\n"

    rows = [
        [InlineKeyboardButton(text=f"👀 View #{l['id']}", callback_data=f"mkt:view:{l['id']}")]
        for l in listings
    ]
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️ Prev", callback_data=f"mkt:browse:{max(0, offset-PAGE_SIZE)}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="▶️ Next", callback_data=f"mkt:browse:{offset+PAGE_SIZE}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="menu:marketplace")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("mkt:view:"))
async def view_listing(callback: CallbackQuery, state: FSMContext):
    listing_id = int(callback.data.split(":")[2])
    l = await db.get_marketplace_listing(listing_id)

    if not l or l["status"] != "available":
        await callback.answer("This listing is no longer available.", show_alert=True)
        await browse_listings_refresh(callback, state)
        return

    if l["seller_user_id"] == callback.from_user.id:
        await callback.answer("This is your own listing.", show_alert=True)
        return

    method = PAYMENT_METHODS.get(l["payment_method"], (l["payment_method"], "?"))
    name = l["account_name"] or l["account_username"] or "Telegram Account"
    phone_hint = ""
    if l["account_phone"]:
        p = l["account_phone"]
        phone_hint = f"\n📞 Phone: +{p[1:3]}***{p[-4:]}" if len(p) > 6 else "\n📞 Phone: ****"

    text = (
        f"📦 <b>Listing #{l['id']}</b>\n\n"
        f"👤 Account: <b>{name}</b>{phone_hint}\n"
        f"💰 Price: <b>{l['price']} {l['currency']}</b>\n"
        f"💳 Payment: {method[0]}\n"
    )
    if l["description"]:
        text += f"📝 Description: {l['description']}\n"
    text += "\n"

    # Show payment details
    if l["payment_method"] == "stars":
        text += f"⭐ Pay <b>{l['stars_amount']} Stars</b> via Telegram"
        rows = [
            [InlineKeyboardButton(text=f"⭐ Pay {l['stars_amount']} Stars", callback_data=f"mkt:pay_stars:{listing_id}")],
            [InlineKeyboardButton(text="🔙 Back", callback_data="mkt:browse:0")],
        ]
    else:
        if l["payment_method"] == "binance":
            text += f"💛 Send to Binance ID/address:\n<code>{l['payment_address']}</code>"
        elif l["payment_method"] == "upi":
            text += f"📱 Send to UPI ID:\n<code>{l['payment_address']}</code>"
        elif l["payment_method"] == "ton":
            text += f"💎 Send to TON wallet:\n<code>{l['payment_address']}</code>"
        elif l["payment_method"] == "qr":
            text += "📲 Scan the QR code below to pay."

        rows = [
            [InlineKeyboardButton(text="✅ I've Paid — Notify Seller", callback_data=f"mkt:paid:{listing_id}")],
            [InlineKeyboardButton(text="🔙 Back",                       callback_data="mkt:browse:0")],
        ]

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    # Send QR image separately if payment method is QR
    if l["payment_method"] == "qr" and l["payment_qr_file_id"]:
        try:
            await callback.message.answer_photo(
                l["payment_qr_file_id"],
                caption=f"📲 QR Code for listing #{l['id']} — send <b>{l['price']} {l['currency']}</b>",
            )
        except Exception as e:
            logger.warning(f"Could not send QR photo: {e}")

    await callback.answer()


async def browse_listings_refresh(callback: CallbackQuery, state: FSMContext):
    """Helper to redirect to browse page."""
    class FakeData:
        data = "mkt:browse:0"
    callback.data = "mkt:browse:0"
    await browse_listings(callback, state)


# ── Stars payment ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("mkt:pay_stars:"))
async def initiate_stars_payment(callback: CallbackQuery, bot: Bot, state: FSMContext):
    listing_id = int(callback.data.split(":")[2])
    l = await db.get_marketplace_listing(listing_id)

    if not l or l["status"] != "available":
        await callback.answer("Listing no longer available.", show_alert=True)
        return

    stars = l["stars_amount"] or 1
    name = l["account_name"] or l["account_username"] or "Telegram Account"

    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title=f"Buy: {name}",
            description=f"Telegram account · Listing #{listing_id}",
            payload=f"mkt_{listing_id}",
            currency="XTR",
            prices=[LabeledPrice(label="Account", amount=stars)],
        )
        await callback.answer("Invoice sent — complete payment in Telegram.")
    except Exception as e:
        logger.error(f"Marketplace: Stars invoice error: {e}")
        await callback.answer(f"Could not send invoice: {e}", show_alert=True)


@router.pre_checkout_query(lambda q: q.invoice_payload.startswith("mkt_"))
async def pre_checkout(query: PreCheckoutQuery, bot: Bot):
    listing_id = int(query.invoice_payload.split("_")[1])
    l = await db.get_marketplace_listing(listing_id)
    if l and l["status"] == "available":
        await bot.answer_pre_checkout_query(query.id, ok=True)
    else:
        await bot.answer_pre_checkout_query(query.id, ok=False, error_message="This listing is no longer available.")


@router.message(F.successful_payment)
async def handle_stars_payment(message: Message, bot: Bot, state: FSMContext):
    payload = message.successful_payment.invoice_payload
    if not payload.startswith("mkt_"):
        return

    listing_id = int(payload.split("_")[1])
    l = await db.get_marketplace_listing(listing_id)
    if not l:
        return

    buyer = message.from_user
    order_id = await db.create_marketplace_order(
        listing_id=listing_id,
        buyer_user_id=buyer.id,
        buyer_username=buyer.username,
        buyer_name=f"{buyer.first_name or ''} {buyer.last_name or ''}".strip(),
        payment_note=f"Paid {message.successful_payment.total_amount} Stars via Telegram",
    )

    await message.answer(
        f"⭐ <b>Stars payment received!</b>\n\n"
        f"Order #{order_id} created. Waiting for seller to approve.\n"
        "You'll get the account details as soon as they confirm."
    )
    await _notify_seller_and_admin(bot, order_id, l, buyer)


# ── Manual payment (non-Stars) ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("mkt:paid:"))
async def buyer_paid(callback: CallbackQuery, state: FSMContext):
    listing_id = int(callback.data.split(":")[2])
    l = await db.get_marketplace_listing(listing_id)

    if not l or l["status"] != "available":
        await callback.answer("This listing is no longer available.", show_alert=True)
        return

    # Check if this buyer already has a pending order for this listing
    existing = await db.get_pending_order_for_listing(listing_id, callback.from_user.id)
    if existing:
        await callback.answer("You already have a pending order for this listing. Please wait for seller response.", show_alert=True)
        return

    await state.update_data(listing_id=listing_id)
    await state.set_state(BuyStates.waiting_payment_note)
    await callback.message.edit_text(
        "✅ <b>Payment Confirmation</b>\n\n"
        "Optionally add a short note for the seller (transaction ID, reference, etc.).\n\n"
        "Or just send <code>done</code> to skip and notify the seller now.",
        reply_markup=_back_kb(f"mkt:view:{listing_id}"),
    )
    await callback.answer()


@router.message(BuyStates.waiting_payment_note)
async def buyer_payment_note(message: Message, bot: Bot, state: FSMContext):
    data = await state.get_data()
    listing_id = data.get("listing_id")
    await state.clear()

    l = await db.get_marketplace_listing(listing_id)
    if not l or l["status"] != "available":
        await message.answer("❌ This listing is no longer available.")
        return

    note = None if message.text.strip().lower() == "done" else message.text.strip()
    buyer = message.from_user

    order_id = await db.create_marketplace_order(
        listing_id=listing_id,
        buyer_user_id=buyer.id,
        buyer_username=buyer.username,
        buyer_name=f"{buyer.first_name or ''} {buyer.last_name or ''}".strip(),
        payment_note=note,
    )

    await message.answer(
        f"✅ <b>Seller notified!</b>\n\n"
        f"Order #{order_id} is pending seller approval.\n"
        "You'll receive the account details once they confirm your payment.\n\n"
        "⏳ Most sellers respond within a few minutes."
    )
    await _notify_seller_and_admin(bot, order_id, l, buyer)


# ── Seller approve / reject ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("mkt:approve:"))
async def seller_approve(callback: CallbackQuery, bot: Bot, state: FSMContext):
    order_id = int(callback.data.split(":")[2])
    order = await db.get_marketplace_order(order_id)

    if not order:
        await callback.answer("Order not found.", show_alert=True)
        return

    if order["status"] != "pending":
        await callback.answer(f"Order already {order['status']}.", show_alert=True)
        return

    listing = await db.get_marketplace_listing(order["listing_id"])
    if not listing:
        await callback.answer("Listing not found.", show_alert=True)
        return

    # Only seller or admin can approve
    is_admin = callback.from_user.id == ADMIN_CHAT_ID
    is_seller = callback.from_user.id == listing["seller_user_id"]
    if not is_admin and not is_seller:
        await callback.answer("Only the seller can approve this order.", show_alert=True)
        return

    await db.update_order_status(order_id, "approved")
    await db.update_listing_status(listing["id"], "sold")

    # Deliver account to buyer
    delivered = await _deliver_account(bot, order["buyer_user_id"], listing)

    result_text = (
        "✅ <b>Payment approved!</b>\n\n"
        f"Account delivered to buyer (Order #{order_id}).\n"
        + ("" if delivered else "\n⚠️ Could not message the buyer — they may have blocked the bot.")
    )
    await callback.message.edit_text(result_text)

    # Also update the other notification (admin or seller's copy)
    try:
        if is_seller and order["admin_message_id"]:
            await bot.edit_message_text(
                f"✅ Order #{order_id} approved by seller. Account delivered.",
                chat_id=ADMIN_CHAT_ID,
                message_id=order["admin_message_id"],
            )
        elif is_admin and order["seller_message_id"]:
            await bot.edit_message_text(
                f"✅ Order #{order_id} approved by admin. Account delivered.",
                chat_id=listing["seller_user_id"],
                message_id=order["seller_message_id"],
            )
    except Exception:
        pass

    await callback.answer("Approved ✅")


@router.callback_query(F.data.startswith("mkt:reject:"))
async def seller_reject(callback: CallbackQuery, bot: Bot, state: FSMContext):
    order_id = int(callback.data.split(":")[2])
    order = await db.get_marketplace_order(order_id)

    if not order:
        await callback.answer("Order not found.", show_alert=True)
        return

    if order["status"] != "pending":
        await callback.answer(f"Order already {order['status']}.", show_alert=True)
        return

    listing = await db.get_marketplace_listing(order["listing_id"])
    is_admin = callback.from_user.id == ADMIN_CHAT_ID
    is_seller = listing and callback.from_user.id == listing["seller_user_id"]
    if not is_admin and not is_seller:
        await callback.answer("Only the seller can reject this order.", show_alert=True)
        return

    await db.update_order_status(order_id, "rejected")

    # Notify buyer
    try:
        await bot.send_message(
            order["buyer_user_id"],
            f"❌ <b>Purchase rejected</b>\n\n"
            f"The seller could not verify your payment for Order #{order_id}.\n"
            "If you believe this is a mistake, contact the seller directly.",
        )
    except Exception:
        pass

    await callback.message.edit_text(f"❌ Order #{order_id} rejected. Buyer has been notified.")

    # Update the other copy
    try:
        if is_seller and order["admin_message_id"]:
            await bot.edit_message_text(
                f"❌ Order #{order_id} rejected by seller.",
                chat_id=ADMIN_CHAT_ID,
                message_id=order["admin_message_id"],
            )
        elif is_admin and order["seller_message_id"] and listing:
            await bot.edit_message_text(
                f"❌ Order #{order_id} rejected by admin.",
                chat_id=listing["seller_user_id"],
                message_id=order["seller_message_id"],
            )
    except Exception:
        pass

    await callback.answer("Rejected ❌")


# ══════════════════════════════════════════════════════════════════════════════
# SELL FLOW
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "mkt:sell")
async def sell_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    accounts = await db.get_user_accounts(callback.from_user.id)
    active = [a for a in accounts if a["is_active"]]

    if not active:
        await callback.message.edit_text(
            "❌ No active accounts to sell.\nAdd a Telegram account first.",
            reply_markup=_back_kb("menu:marketplace"),
        )
        await callback.answer()
        return

    rows = [
        [InlineKeyboardButton(
            text=f"👤 {a['first_name'] or ''} {a['phone'] or ''}".strip() or f"Account #{a['id']}",
            callback_data=f"mkt:sell_acct:{a['id']}",
        )]
        for a in active
    ]
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="menu:marketplace")])
    await state.set_state(SellStates.choosing_account)
    await callback.message.edit_text(
        "<b>💰 Sell an Account</b>\n\nChoose which account to list for sale:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(SellStates.choosing_account, F.data.startswith("mkt:sell_acct:"))
async def sell_chose_account(callback: CallbackQuery, state: FSMContext):
    account_id = int(callback.data.split(":")[2])
    acct = await db.get_account(account_id)
    if not acct or acct["user_id"] != callback.from_user.id:
        await callback.answer("Account not found.", show_alert=True)
        return

    await state.update_data(
        account_id=account_id,
        account_name=acct["first_name"] or acct["username"] or "",
        account_phone=acct["phone"] or "",
        account_username=acct["username"] or "",
        session_string=acct["session_string"],
    )
    await state.set_state(SellStates.entering_price)
    await callback.message.edit_text(
        "<b>💰 Set Your Price</b>\n\n"
        "Enter the price for this account.\n\n"
        "<i>Examples:\n"
        "• <code>5 USDT</code>\n"
        "• <code>500 INR</code>\n"
        "• <code>0.5 TON</code>\n"
        "• <code>100 Stars</code></i>",
        reply_markup=_back_kb("mkt:sell"),
    )
    await callback.answer()


@router.message(SellStates.entering_price)
async def sell_got_price(message: Message, state: FSMContext):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("⚠️ Please enter price AND currency, e.g. <code>5 USDT</code>")
        return

    price_val = parts[0]
    currency = " ".join(parts[1:]).upper()
    await state.update_data(price=price_val, currency=currency)
    await state.set_state(SellStates.entering_2fa)
    await message.answer(
        "<b>🔒 2FA Password</b>\n\n"
        "Enter the account's <b>Two-Factor Authentication password</b> (cloud password).\n"
        "This will be given to the buyer after purchase.\n\n"
        "Send <code>none</code> if the account has no 2FA.",
    )


@router.message(SellStates.entering_2fa)
async def sell_got_2fa(message: Message, state: FSMContext):
    raw = message.text.strip()
    twofa = None if raw.lower() == "none" else raw
    await state.update_data(two_fa_password=twofa)
    await state.set_state(SellStates.choosing_payment)

    rows = [[InlineKeyboardButton(text=label, callback_data=f"mkt:pm:{key}")]
            for key, (label, _) in PAYMENT_METHODS.items()]
    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="mkt:sell")])

    await message.answer(
        "<b>💳 Payment Method</b>\n\nHow do you want to receive payment?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(SellStates.choosing_payment, F.data.startswith("mkt:pm:"))
async def sell_chose_payment(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split(":")[2]
    await state.update_data(payment_method=method)

    if method == "stars":
        await state.set_state(SellStates.entering_stars)
        await callback.message.edit_text(
            "<b>⭐ Stars Amount</b>\n\n"
            "How many Telegram Stars should the buyer pay?\n"
            "(1 Star ≈ $0.013 USD)\n\n"
            "Enter a number, e.g. <code>200</code>",
            reply_markup=_back_kb("mkt:sell"),
        )
    elif method == "qr":
        await state.set_state(SellStates.uploading_qr)
        await callback.message.edit_text(
            "<b>📲 Upload QR Code</b>\n\n"
            "Send your payment QR code as a <b>photo</b>.\n"
            "Buyers will scan this to pay you.",
            reply_markup=_back_kb("mkt:sell"),
        )
    else:
        labels = {"binance": "Binance Pay ID or crypto address",
                  "upi":     "UPI ID (e.g. name@upi)",
                  "ton":     "TON wallet address"}
        await state.set_state(SellStates.entering_address)
        await callback.message.edit_text(
            f"<b>📬 Payment Address</b>\n\nEnter your {labels.get(method, 'payment address')}:",
            reply_markup=_back_kb("mkt:sell"),
        )
    await callback.answer()


@router.message(SellStates.entering_stars)
async def sell_got_stars(message: Message, state: FSMContext):
    try:
        stars = int(message.text.strip())
        if stars < 1:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Enter a valid number of Stars, e.g. <code>200</code>")
        return
    await state.update_data(stars_amount=stars, payment_address=None, payment_qr_file_id=None)
    await state.set_state(SellStates.entering_description)
    await message.answer(
        "<b>📝 Description (optional)</b>\n\n"
        "Add a short description for your listing (account age, followers, etc.).\n"
        "Send <code>skip</code> to leave it blank.",
    )


@router.message(SellStates.entering_address)
async def sell_got_address(message: Message, state: FSMContext):
    addr = message.text.strip()
    await state.update_data(payment_address=addr, payment_qr_file_id=None, stars_amount=None)
    await state.set_state(SellStates.entering_description)
    await message.answer(
        "<b>📝 Description (optional)</b>\n\n"
        "Add a short description for your listing (account age, followers, etc.).\n"
        "Send <code>skip</code> to leave it blank.",
    )


@router.message(SellStates.uploading_qr)
async def sell_got_qr(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("⚠️ Please send the QR code as a <b>photo</b>.")
        return
    file_id = message.photo[-1].file_id
    await state.update_data(payment_qr_file_id=file_id, payment_address=None, stars_amount=None)
    await state.set_state(SellStates.entering_description)
    await message.answer(
        "<b>📝 Description (optional)</b>\n\n"
        "Add a short description for your listing (account age, followers, etc.).\n"
        "Send <code>skip</code> to leave it blank.",
    )


@router.message(SellStates.entering_description)
async def sell_got_description(message: Message, state: FSMContext):
    raw = message.text.strip() if message.text else ""
    description = None if raw.lower() == "skip" else raw
    data = await state.get_data()
    await state.clear()

    listing_id = await db.create_marketplace_listing(
        seller_user_id=message.from_user.id,
        account_id=data["account_id"],
        account_name=data["account_name"],
        account_phone=data["account_phone"],
        account_username=data["account_username"],
        session_string=data["session_string"],
        two_fa_password=data.get("two_fa_password"),
        price=data["price"],
        currency=data["currency"],
        payment_method=data["payment_method"],
        payment_address=data.get("payment_address"),
        payment_qr_file_id=data.get("payment_qr_file_id"),
        stars_amount=data.get("stars_amount"),
        description=description,
    )

    method_label = PAYMENT_METHODS.get(data["payment_method"], (data["payment_method"],))[0]
    await message.answer(
        f"✅ <b>Listing #{listing_id} created!</b>\n\n"
        f"👤 Account: {data['account_name'] or data['account_username'] or 'Account'}\n"
        f"💰 Price: {data['price']} {data['currency']}\n"
        f"💳 Payment: {method_label}\n\n"
        "Your account is now visible to all buyers in the marketplace.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 View Marketplace", callback_data="menu:marketplace")],
            [InlineKeyboardButton(text="📋 My Listings",      callback_data="mkt:mylistings")],
            [InlineKeyboardButton(text="🏠 Main Menu",        callback_data="menu:back")],
        ]),
    )


# ── My Listings ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "mkt:mylistings")
async def my_listings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    listings = await db.get_user_marketplace_listings(callback.from_user.id)

    if not listings:
        await callback.message.edit_text(
            "📋 <b>My Listings</b>\n\nYou haven't listed any accounts yet.",
            reply_markup=_back_kb("menu:marketplace"),
        )
        await callback.answer()
        return

    text = "📋 <b>My Listings</b>\n\n"
    rows = []
    for l in listings:
        status_icon = {"available": "🟢", "sold": "✅", "cancelled": "⛔"}.get(l["status"], "❓")
        name = l["account_name"] or l["account_username"] or f"Account #{l['account_id']}"
        text += f"{status_icon} #{l['id']} · {name} · {l['price']} {l['currency']} · {l['status']}\n"
        if l["status"] == "available":
            rows.append([
                InlineKeyboardButton(text=f"🗑 Remove #{l['id']}", callback_data=f"mkt:remove:{l['id']}")
            ])

    rows.append([InlineKeyboardButton(text="🔙 Back", callback_data="menu:marketplace")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("mkt:remove:"))
async def remove_listing(callback: CallbackQuery, state: FSMContext):
    listing_id = int(callback.data.split(":")[2])
    l = await db.get_marketplace_listing(listing_id)
    if l and l["seller_user_id"] == callback.from_user.id and l["status"] == "available":
        await db.delete_marketplace_listing(listing_id, callback.from_user.id)
        await callback.answer("Listing removed.")
    else:
        await callback.answer("Could not remove listing.", show_alert=True)
    await my_listings(callback, state)
''',
    'handlers.report': r'''"""
Mass Reporter — report a Telegram channel / group / user / message
from every active stored account, supporting all 10 Telegram report
reason variants (Spam, Violence, Pornography, Child Abuse, Copyright,
Geo-Irrelevant, Fake, Illegal Drugs, Personal Details, Other).
"""
import asyncio
import logging
import re as _re
from typing import Optional, Tuple, Union

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

import database as db
from services.pyrogram_manager import get_client_for_session

logger = logging.getLogger(__name__)
router = Router()


# ── Mass-report passcode gate ──────────────────────────────────────────────
# Only users who have authenticated this session with `/admin 5576` may
# access ANY of the mass-report commands. No one else — not regular members,
# not other admins — can use them.
_REPORT_PASSCODE = "5576"
_AUTHED_REPORTERS: set = set()


def _is_report_authed(user_id: int) -> bool:
    return int(user_id) in _AUTHED_REPORTERS


async def _deny(message_or_cb) -> None:
    msg = "🔒 Access denied. Mass report is restricted."
    try:
        if hasattr(message_or_cb, "answer") and hasattr(message_or_cb, "from_user"):
            # CallbackQuery
            if hasattr(message_or_cb, "data"):
                await message_or_cb.answer(msg, show_alert=True)
                return
            await message_or_cb.answer(msg)
    except Exception:
        pass




# All 10 Telegram report reason variants (MTProto inputReportReason*).
REPORT_REASONS = [
    ("spam",             "🚫 Spam"),
    ("violence",         "🔪 Violence"),
    ("pornography",      "🔞 Pornography"),
    ("child_abuse",      "🧒 Child Abuse"),
    ("copyright",        "©️ Copyright"),
    ("geo_irrelevant",   "📍 Geo-Irrelevant"),
    ("fake",             "🎭 Fake Account"),
    ("illegal_drugs",    "💊 Illegal Drugs"),
    ("personal_details", "🪪 Personal Details"),
    ("other",            "❓ Other"),
]


def _resolve_reason_enum(key: str):
    """Map our key → pyrogram.enums.ReportReason member, with fallbacks."""
    try:
        from pyrogram import enums as _enums
        RR = _enums.ReportReason
    except Exception:
        return key
    mapping = {
        "spam":             "SPAM",
        "violence":         "VIOLENCE",
        "pornography":      "PORNOGRAPHY",
        "child_abuse":      "CHILD_ABUSE",
        "copyright":        "COPYRIGHT",
        "geo_irrelevant":   "GEO_IRRELEVANT",
        "fake":             "FAKE",
        "illegal_drugs":    "ILLEGAL_DRUGS",
        "personal_details": "PERSONAL_DETAILS",
        "other":            "OTHER",
    }
    name = mapping.get(key, "OTHER")
    return getattr(RR, name, getattr(RR, "OTHER", None))


def _raw_reason(key: str):
    """Map our key → raw MTProto InputReportReason* object."""
    from pyrogram.raw import types as _rt
    mapping = {
        "spam":             _rt.InputReportReasonSpam,
        "violence":         _rt.InputReportReasonViolence,
        "pornography":      _rt.InputReportReasonPornography,
        "child_abuse":      _rt.InputReportReasonChildAbuse,
        "copyright":        _rt.InputReportReasonCopyright,
        "geo_irrelevant":   _rt.InputReportReasonGeoIrrelevant,
        "fake":             _rt.InputReportReasonFake,
        "illegal_drugs":    _rt.InputReportReasonIllegalDrugs,
        "personal_details": _rt.InputReportReasonPersonalDetails,
        "other":            _rt.InputReportReasonOther,
    }
    cls = mapping.get(key, _rt.InputReportReasonOther)
    return cls()


async def _mass_report_one(client, peer, mid, reason_key, comment: str):
    """
    Send a single report through one account using raw MTProto.
    Works for USERS, BOTS, CHANNELS, GROUPS, and specific MESSAGES.
    Returns (ok: bool, error_name: str|None).
    """
    from pyrogram.raw import functions as _fn
    reason = _raw_reason(reason_key)
    comment = comment or ""

    # Populate the account's peer cache (required for raw API to resolve peers).
    try:
        if isinstance(peer, int) and peer > 0:
            await client.get_users(peer)
        else:
            await client.get_chat(peer)
    except Exception:
        pass  # resolve_peer below may still succeed for cached peers

    try:
        input_peer = await client.resolve_peer(peer)
    except Exception as e:
        return False, f"resolve:{type(e).__name__}"

    # Report a specific MESSAGE
    if mid:
        try:
            await client.invoke(_fn.messages.Report(
                peer=input_peer, id=[int(mid)],
                reason=reason, message=comment,
            ))
            return True, None
        except TypeError:
            # Newer Telegram schema uses (peer, id, option, message)
            try:
                await client.invoke(_fn.messages.Report(
                    peer=input_peer, id=[int(mid)],
                    option=b"", message=comment,
                ))
                return True, None
            except Exception as e:
                return False, type(e).__name__
        except Exception as e:
            return False, type(e).__name__

    # Report the PEER itself (user / bot / channel / group)
    try:
        await client.invoke(_fn.account.ReportPeer(
            peer=input_peer, reason=reason, message=comment,
        ))
        return True, None
    except Exception as e:
        return False, type(e).__name__




# ── URL / target parser ────────────────────────────────────────────────────
_URL_RE = _re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/"
    r"(?:(?P<priv>c)/(?P<cid>\d+)/(?P<cmid>\d+)"
    r"|(?P<user>[A-Za-z0-9_]{4,})(?:/(?P<mid>\d+))?)/?$"
)

def parse_target(text: str) -> Optional[Tuple[Union[int, str], Optional[int]]]:
    """Return (peer, message_id|None). peer is @username, int chat_id, or username."""
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("@"):
        return text, None
    m = _URL_RE.match(text)
    if m:
        if m.group("priv"):
            chat_id = int("-100" + m.group("cid"))
            return chat_id, int(m.group("cmid"))
        username = "@" + m.group("user")
        mid = int(m.group("mid")) if m.group("mid") else None
        return username, mid
    try:
        return int(text), None
    except ValueError:
        pass
    if _re.fullmatch(r"[A-Za-z0-9_]{4,}", text):
        return "@" + text, None
    return None


# ── FSM ────────────────────────────────────────────────────────────────────
class ReportFlow(StatesGroup):
    waiting_target  = State()
    waiting_reason  = State()
    waiting_comment = State()


def _reason_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for key, label in REPORT_REASONS:
        row.append(InlineKeyboardButton(text=label, callback_data=f"rep:rsn:{key}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="rep:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# NOTE: the `/admin <passcode>` entry point lives in handlers.admin
# (passcode "5576" specifically grants mass-report access only).


@router.message(Command("admin_logout"))
async def cmd_admin_logout(message: Message):
    _AUTHED_REPORTERS.discard(int(message.from_user.id))
    await message.answer("🔒 Mass-report access revoked for your account.")


@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    if not _is_report_authed(message.from_user.id):
        await message.answer(
            "🔒 Mass report is restricted.\n"
            "Authenticate first with: <code>/admin &lt;code&gt;</code>"
        )
        return
    await state.clear()
    await state.set_state(ReportFlow.waiting_target)
    await message.answer(
        "📣 <b>Mass Reporter</b>\n\n"
        "Send the target as one of:\n"
        "• <code>@username</code>  (user or channel)\n"
        "• <code>https://t.me/username</code>\n"
        "• <code>https://t.me/username/123</code>  (specific message)\n"
        "• <code>https://t.me/c/2000123456/45</code>  (private channel msg)\n"
        "• numeric Telegram <b>user ID</b> (e.g. <code>123456789</code>)\n"
        "• numeric channel/chat id (e.g. <code>-1001234567890</code>)\n\n"
        "Shortcuts:\n"
        "  <code>/report_channel &lt;@username|link&gt;</code>\n"
        "  <code>/report_user &lt;user_id|@username&gt;</code>\n\n"
        "Send /cancel to abort."
    )


@router.message(Command("report_channel"))
async def cmd_report_channel(message: Message, state: FSMContext):
    """Shortcut: /report_channel @ch | https://t.me/ch | https://t.me/ch/123"""
    if not _is_report_authed(message.from_user.id):
        await message.answer("🔒 Mass report is restricted. Authenticate with <code>/admin &lt;code&gt;</code>.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/report_channel @username</code>\n"
            "or: <code>/report_channel https://t.me/username/123</code>"
        )
        return
    parsed = parse_target(parts[1].strip())
    if not parsed:
        await message.answer("⚠️ Couldn't parse that channel/link.")
        return
    peer, mid = parsed
    await state.clear()
    await state.update_data(peer=peer, message_id=mid)
    await state.set_state(ReportFlow.waiting_reason)
    target_desc = f"{peer}" + (f" (message {mid})" if mid else "")
    await message.answer(
        f"📡 <b>Channel target:</b> <code>{target_desc}</code>\n\nChoose a <b>report reason</b>:",
        reply_markup=_reason_kb(),
    )


@router.message(Command("report_user"))
async def cmd_report_user(message: Message, state: FSMContext):
    """Shortcut: /report_user 123456789  |  /report_user @someone"""
    if not _is_report_authed(message.from_user.id):
        await message.answer("🔒 Mass report is restricted. Authenticate with <code>/admin &lt;code&gt;</code>.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Usage: <code>/report_user 123456789</code>\n"
            "or: <code>/report_user @username</code>"
        )
        return
    raw = parts[1].strip()
    # Force interpretation as a USER (positive int or @username)
    parsed: Optional[Tuple[Union[int, str], Optional[int]]] = None
    if raw.startswith("@"):
        parsed = (raw, None)
    else:
        try:
            uid = int(raw)
            if uid <= 0:
                await message.answer("⚠️ That looks like a channel id, not a user id. Use /report_channel.")
                return
            parsed = (uid, None)
        except ValueError:
            parsed = parse_target(raw)
    if not parsed:
        await message.answer("⚠️ Couldn't parse that user id / username.")
        return
    peer, _ = parsed
    await state.clear()
    await state.update_data(peer=peer, message_id=None)
    await state.set_state(ReportFlow.waiting_reason)
    await message.answer(
        f"👤 <b>User target:</b> <code>{peer}</code>\n\nChoose a <b>report reason</b>:",
        reply_markup=_reason_kb(),
    )




@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    cur = await state.get_state()
    if cur and cur.startswith("ReportFlow"):
        if not _is_report_authed(message.from_user.id):
            await state.clear()
            return
        await state.clear()
        await message.answer("Cancelled.")


@router.callback_query(F.data == "rep:cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    if not _is_report_authed(callback.from_user.id):
        await callback.answer("🔒 Access denied.", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("❌ Mass report cancelled.")
    await callback.answer()


@router.message(ReportFlow.waiting_target)
async def got_target(message: Message, state: FSMContext):
    if not _is_report_authed(message.from_user.id):
        await state.clear()
        return
    parsed = parse_target(message.text or "")
    if not parsed:
        await message.answer("⚠️ Couldn't parse that. Try again or /cancel.")
        return
    peer, mid = parsed
    await state.update_data(peer=peer, message_id=mid)
    target_desc = f"{peer}" + (f" (message {mid})" if mid else "")
    await state.set_state(ReportFlow.waiting_reason)
    await message.answer(
        f"🎯 Target: <code>{target_desc}</code>\n\n"
        "Choose a <b>report reason</b>:",
        reply_markup=_reason_kb(),
    )


@router.callback_query(F.data.startswith("rep:rsn:"))
async def got_reason(callback: CallbackQuery, state: FSMContext):
    if not _is_report_authed(callback.from_user.id):
        await callback.answer("🔒 Access denied.", show_alert=True)
        return
    key = callback.data.split(":", 2)[2]
    label = dict(REPORT_REASONS).get(key, key)
    await state.update_data(reason=key, reason_label=label)
    await state.set_state(ReportFlow.waiting_comment)
    await callback.message.edit_text(
        f"📝 Reason: <b>{label}</b>\n\n"
        "Send an optional <b>comment</b> (or send <code>skip</code> to omit).",
    )
    await callback.answer()


@router.message(ReportFlow.waiting_comment)
async def got_comment(message: Message, state: FSMContext):
    if not _is_report_authed(message.from_user.id):
        await state.clear()
        return
    comment_raw = (message.text or "").strip()
    comment = "" if comment_raw.lower() in ("skip", "-", "none", "") else comment_raw
    data = await state.get_data()
    await state.clear()

    peer       = data["peer"]
    mid        = data.get("message_id")
    reason_key = data["reason"]
    reason_lbl = data["reason_label"]

    accounts = await db.get_user_accounts(message.from_user.id)
    active = [a for a in accounts if a["is_active"]]
    if not active:
        await message.answer("❌ You have no active accounts to report from.")
        return

    status = await message.answer(
        f"🚀 Mass-reporting <code>{peer}</code>"
        + (f" (msg {mid})" if mid else "")
        + f"\nReason: <b>{reason_lbl}</b>\nAccounts: {len(active)}\n\n⏳ Working…"
    )

    success = 0
    failed  = 0
    errors  = {}

    for acc in active:
        acc_id = acc["id"]
        try:
            client = await get_client_for_session(acc_id, acc["session_string"])
            if client is None:
                failed += 1
                errors["client_unavailable"] = errors.get("client_unavailable", 0) + 1
                continue

            ok, err_name = await _mass_report_one(client, peer, mid, reason_key, comment)
            if ok:
                success += 1
            else:
                failed += 1
                key_err = err_name or "UnknownError"
                errors[key_err] = errors.get(key_err, 0) + 1
                logger.warning(f"[report] acc {acc_id} failed: {key_err}")
        except Exception as outer:
            failed += 1
            err = type(outer).__name__
            errors[err] = errors.get(err, 0) + 1
            logger.warning(f"[report] acc {acc_id} outer failure: {outer}")

        await asyncio.sleep(1.5)




    breakdown = ""
    if errors:
        breakdown = "\n\n<b>Errors:</b>\n" + "\n".join(
            f"• <code>{k}</code>: {v}" for k, v in errors.items()
        )

    await status.edit_text(
        f"✅ <b>Mass report complete</b>\n\n"
        f"🎯 Target: <code>{peer}</code>" + (f" (msg {mid})" if mid else "") + "\n"
        f"📋 Reason: <b>{reason_lbl}</b>\n"
        f"👥 Accounts used: {len(active)}\n"
        f"✅ Success: {success}\n"
        f"❌ Failed: {failed}"
        + breakdown
    )
''',
    'handlers.dbtools': r'''"""
Moveable database tools — export / import the bot's SQLite database so you
can migrate all stored accounts + campaigns between hosting platforms.

Commands (admin only — set ADMIN_IDS in config):
  /export_db  → bot sends bot.db as a Telegram document
  /import_db  → reply to a .db file with this command to replace bot.db
                (a timestamped backup of the old DB is kept)
  /db_stats   → row counts for the main tables
"""
import os
import shutil
import time
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

import config
import database as db

logger = logging.getLogger(__name__)
router = Router()


def _db_path() -> str:
    return os.environ.get("BOT_DB_PATH", "bot.db")


def _is_admin(user_id: int) -> bool:
    try:
        return int(user_id) in [int(x) for x in (config.ADMIN_IDS or [])]
    except Exception:
        return False


@router.message(Command("export_db"))
async def export_db(message: Message):
    if not _is_admin(message.from_user.id):
        return await message.answer("❌ Admins only.")
    path = _db_path()
    if not os.path.exists(path):
        return await message.answer(f"❌ Database file not found at <code>{path}</code>.")

    # Best-effort WAL checkpoint so the .db file is fully self-contained.
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as e:
        logger.warning(f"wal_checkpoint failed (continuing): {e}")

    size = os.path.getsize(path)
    fname = f"bot-{time.strftime('%Y%m%d-%H%M%S')}.db"
    await message.answer_document(
        FSInputFile(path, filename=fname),
        caption=(
            f"📦 <b>Database export</b>\n"
            f"File: <code>{fname}</code>\n"
            f"Size: {size/1024:.1f} KB\n\n"
            f"To restore on another host: reply to this file with "
            f"<code>/import_db</code>."
        ),
    )


@router.message(Command("import_db"))
async def import_db(message: Message):
    if not _is_admin(message.from_user.id):
        return await message.answer("❌ Admins only.")

    src_msg = message.reply_to_message
    if not src_msg or not src_msg.document:
        return await message.answer(
            "📥 Reply to a <code>.db</code> file with <code>/import_db</code> "
            "to replace the current database."
        )

    doc = src_msg.document
    name = (doc.file_name or "").lower()
    if not (name.endswith(".db") or name.endswith(".sqlite") or name.endswith(".sqlite3")):
        return await message.answer("❌ That file doesn't look like a SQLite database.")

    path = _db_path()
    backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    tmp    = f"{path}.incoming"

    status = await message.answer("⏳ Downloading new database…")
    try:
        # Download to a temp file first
        await message.bot.download(doc, destination=tmp)

        # Basic sanity check: SQLite header
        with open(tmp, "rb") as f:
            head = f.read(16)
        if not head.startswith(b"SQLite format 3"):
            os.remove(tmp)
            return await status.edit_text("❌ File is not a valid SQLite database.")

        # Close current pool so the file isn't held open
        try:
            pool = await db.get_pool()
            await pool._db.close()  # type: ignore[attr-defined]
        except Exception as e:
            logger.warning(f"close pool: {e}")
        try:
            import database as _dbmod
            _dbmod._pool = None
        except Exception:
            pass

        # Back up old DB and WAL siblings, then move new one into place
        if os.path.exists(path):
            shutil.move(path, backup)
        for ext in ("-wal", "-shm"):
            sib = path + ext
            if os.path.exists(sib):
                try: os.remove(sib)
                except Exception: pass
        shutil.move(tmp, path)

        # Re-open + run migrations
        await db.init_db()

        await status.edit_text(
            f"✅ <b>Database imported</b>\n"
            f"Replaced: <code>{path}</code>\n"
            f"Backup of previous DB: <code>{os.path.basename(backup)}</code>\n\n"
            f"All accounts, campaigns, logs, etc. from the imported file "
            f"are now active."
        )
    except Exception as e:
        logger.exception("import_db failed")
        await status.edit_text(f"❌ Import failed: <code>{e}</code>")
        # try to restore backup
        try:
            if os.path.exists(backup) and not os.path.exists(path):
                shutil.move(backup, path)
                await db.init_db()
        except Exception:
            pass


@router.message(Command("db_stats"))
async def db_stats(message: Message):
    if not _is_admin(message.from_user.id):
        return await message.answer("❌ Admins only.")
    path = _db_path()
    size = os.path.getsize(path) if os.path.exists(path) else 0
    pool = await db.get_pool()
    tables = [
        "bot_users", "telegram_accounts", "campaigns", "campaign_logs",
    ]
    lines = [f"📊 <b>DB stats</b>", f"File: <code>{path}</code> ({size/1024:.1f} KB)", ""]
    async with pool.acquire() as conn:
        for t in tables:
            try:
                n = await conn.fetchval(f"SELECT COUNT(*) FROM {t}")
            except Exception:
                n = "—"
            lines.append(f"• <code>{t}</code>: <b>{n}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("last_errors"))
async def last_errors(message: Message):
    """Show the last failed campaign-log entries (admins only).
    Usage:
        /last_errors            → last 15 failures across all campaigns
        /last_errors <id>       → last 15 failures for that campaign
        /last_errors <id> 30    → last 30 failures for that campaign
    """
    if not _is_admin(message.from_user.id):
        return await message.answer("❌ Admins only.")
    parts = (message.text or "").split()
    camp_id = None
    limit   = 15
    try:
        if len(parts) >= 2:
            camp_id = int(parts[1])
        if len(parts) >= 3:
            limit = max(1, min(50, int(parts[2])))
    except ValueError:
        return await message.answer(
            "Usage: <code>/last_errors</code> or <code>/last_errors &lt;campaign_id&gt; [limit]</code>"
        )

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if camp_id is None:
            rows = await conn.fetch(
                "SELECT campaign_id, account_phone, action, status, error_message, timestamp "
                "FROM campaign_logs WHERE status IN ('failed','frozen') "
                "ORDER BY timestamp DESC LIMIT $1", limit,
            )
        else:
            rows = await conn.fetch(
                "SELECT campaign_id, account_phone, action, status, error_message, timestamp "
                "FROM campaign_logs WHERE campaign_id = $1 AND status IN ('failed','frozen') "
                "ORDER BY timestamp DESC LIMIT $2", camp_id, limit,
            )

    if not rows:
        return await message.answer(
            f"✅ No failures found"
            + (f" for campaign #{camp_id}" if camp_id else " in recent logs")
            + "."
        )

    header = f"📜 <b>Last {len(rows)} failures"
    header += f" for campaign #{camp_id}" if camp_id else ""
    header += "</b>\n"
    body_lines = []
    for r in rows:
        err = (r["error_message"] or "Unknown").replace("<", "&lt;").replace(">", "&gt;")
        if len(err) > 220:
            err = err[:217] + "…"
        body_lines.append(
            f"\n• #C{r['campaign_id']}  <code>{r['account_phone']}</code>  "
            f"<b>{r['action']}</b> → {r['status']}\n   {err}"
        )
    await message.answer(header + "".join(body_lines))
''',
    '__runner__': r'''import asyncio
import logging
import os
import sys


from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database as db
from handlers import start, accounts, campaigns
from handlers import admin
from handlers import forwarding, auto_reply, marketplace, report, dbtools
from services.session_keeper import start_session_keeper
from services.keep_alive import start_keep_alive, set_bot_username
from services.account_monitor import start_monitor, monitor
from services.campaign_poller import start_poller
from handlers.forwarding import refresh_bot_tasks_cache

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ── Health-check HTTP server (production only) ────────────────────────────────
# When spawned by the API server in production the parent process handles the
# HTTP health check, so we only need this when running standalone (e.g. as a
# registered artifact with its own PORT assignment).

_IS_PRODUCTION = False

async def start_health_server():
    """Start a tiny HTTP server for deployment health checks (production only)."""
    if not _IS_PRODUCTION:
        return
    try:
        from aiohttp import web
        port = int(os.environ.get("PORT", 8081))

        async def _ok(request):
            return web.Response(text="ok")

        app = web.Application()
        app.router.add_get("/", _ok)
        app.router.add_get("/healthz", _ok)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        logger.info(f"🌐 Health-check server listening on port {port}")
    except Exception as e:
        logger.warning(f"Health server failed to start: {e}")


# ── Bot setup ─────────────────────────────────────────────────────────────────

async def on_startup(bot: Bot):
    await db.init_db()
    me = await bot.get_me()
    logger.info(f"✅ Bot started: @{me.username} — running 24/7")
    set_bot_username(me.username or "")
    monitor.set_bot(bot)
    asyncio.create_task(start_session_keeper())
    asyncio.create_task(start_keep_alive())
    asyncio.create_task(start_monitor())
    asyncio.create_task(start_poller())
    asyncio.create_task(refresh_bot_tasks_cache(bot))


async def run_bot():
    if not config.BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN is not set!")
        sys.exit(1)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    dp.startup.register(on_startup)

    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(accounts.router)
    dp.include_router(campaigns.router)
    dp.include_router(forwarding.router)
    dp.include_router(auto_reply.router)
    dp.include_router(marketplace.router)
    dp.include_router(report.router)
    dp.include_router(dbtools.router)

    return bot, dp


async def main():
    """Run both the health-check server and the Telegram bot concurrently."""
    # Start health-check server first so the port is detected immediately
    await start_health_server()

    bot, dp = await run_bot()
    retry_delay = 5
    conflict_strikes = 0

    while True:
        try:
            logger.info("▶️  Starting polling loop...")
            await dp.start_polling(bot, drop_pending_updates=False, allowed_updates=[
                "message", "callback_query", "inline_query", "pre_checkout_query"
            ])
        except asyncio.CancelledError:
            logger.info("Bot polling cancelled — shutting down")
            break
        except Exception as e:
            from aiogram.exceptions import TelegramConflictError
            if isinstance(e, TelegramConflictError):
                if not _IS_PRODUCTION:
                    conflict_strikes += 1
                    if conflict_strikes >= 3:
                        logger.warning(
                            "TelegramConflictError in dev mode — another instance (production) "
                            "is already running this token. Dev workflow stepping aside so "
                            "production stays undisturbed. To run locally, set BOT_TOKEN_DEV "
                            "to a separate test-bot token."
                        )
                        await bot.session.close()
                        sys.exit(0)
            logger.error(f"💥 Polling crashed: {e}. Restarting in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue
        else:
            retry_delay = 5
            conflict_strikes = 0

    await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
''',
}


_ORDER = [
    "config",
    "database",
    "services.ai_helper",
    "services.keep_alive",
    "services.pyrogram_manager",
    "services.session_keeper",
    "services.account_monitor",
    "services.campaign_runner",
    "services.campaign_poller",
    "handlers.start",
    "handlers.accounts",
    "handlers.campaigns",
    "handlers.admin",
    "handlers.forwarding",
    "handlers.auto_reply",
    "handlers.marketplace",
    "handlers.report",
    "handlers.dbtools",
]

def _ensure_pkg(name):
    if name in sys.modules:
        return
    pkg = types.ModuleType(name)
    pkg.__path__ = []
    sys.modules[name] = pkg

# Create empty parent packages so `from handlers import x` works as attribute access.
for _pkg in ("services", "handlers"):
    _ensure_pkg(_pkg)

for _name in _ORDER:
    _src = _MODULES[_name]
    if isinstance(_src, bytes):
        _src = _src.decode("utf-8")
    # Backwards-compat: if a module was left as base64 text, decode it.
    if _src.lstrip().startswith("aW1") or (len(_src) < 200000 and re.fullmatch(r'[A-Za-z0-9+/=\s]+', _src) and 'import' not in _src):
        try:
            _src = base64.b64decode(_src).decode("utf-8")
        except Exception:
            pass
    _mod = types.ModuleType(_name)
    _mod.__file__ = _name.replace(".", "/") + ".py"
    if "." in _name:
        _parent, _leaf = _name.rsplit(".", 1)
        _ensure_pkg(_parent)
        setattr(sys.modules[_parent], _leaf, _mod)
    sys.modules[_name] = _mod
    exec(compile(_src, _mod.__file__, "exec"), _mod.__dict__)


# ────────────────────────────────────────────────────────────────────────────
#  Runner (adapted from the original main.py)
# ────────────────────────────────────────────────────────────────────────────
_RUNNER_SRC = _MODULES["__runner__"]
if isinstance(_RUNNER_SRC, bytes):
    _RUNNER_SRC = _RUNNER_SRC.decode("utf-8")
try:
    # If it was left as base64, decode it
    if re.fullmatch(r'[A-Za-z0-9+/=\s]+', _RUNNER_SRC) and 'import' not in _RUNNER_SRC:
        _RUNNER_SRC = base64.b64decode(_RUNNER_SRC).decode("utf-8")
except Exception:
    pass
# Strip the original `if __name__ == "__main__": asyncio.run(main())` because we
# call main() ourselves below.
_RUNNER_SRC = re.sub(r'\nif __name__ == "__main__":.*$', '', _RUNNER_SRC, flags=re.S)

_runner_ns = {"__name__": "__bot_runner__"}
exec(compile(_RUNNER_SRC, "main_runner.py", "exec"), _runner_ns)
main = _runner_ns["main"]

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass