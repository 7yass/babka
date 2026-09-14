"""Babka Danka (Python) — SQLite schema + helpers. Mirrors the Node bot tables."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / 'data.db'


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def conn_ctx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- shared economy rules ----------
HOUSE_IDS = {'1270782781605154922'}  # house always eats a little better


def is_house(uid) -> bool:
    return str(uid) in HOUSE_IDS


def jail_left(guild_id, user_id) -> int:
    """Remaining jail seconds, 0 = free."""
    import time
    with conn_ctx() as conn:
        row = conn.execute('SELECT until FROM jail WHERE guild_id=? AND user_id=?',
                           (str(guild_id), str(user_id))).fetchone()
    if not row or not row['until']:
        return 0
    return max(0, int(row['until']) - int(time.time()))


def jail(guild_id, user_id, minutes: int):
    import time
    until = int(time.time()) + max(1, minutes) * 60
    with conn_ctx() as conn:
        conn.execute('INSERT OR REPLACE INTO jail (guild_id, user_id, until) VALUES (?,?,?)',
                     (str(guild_id), str(user_id), until))


def unjail(guild_id, user_id):
    with conn_ctx() as conn:
        conn.execute('DELETE FROM jail WHERE guild_id=? AND user_id=?',
                     (str(guild_id), str(user_id)))


def has_shield(guild_id, user_id) -> bool:
    import time
    with conn_ctx() as conn:
        row = conn.execute("SELECT expires FROM inventory WHERE guild_id=? AND user_id=? AND item='shield'",
                           (str(guild_id), str(user_id))).fetchone()
    return bool(row and row['expires'] and int(row['expires']) > int(time.time()))


def boost_left(guild_id, user_id) -> int:
    """Active shop XP-boost seconds, 0 = none."""
    import time
    with conn_ctx() as conn:
        row = conn.execute("SELECT expires FROM inventory WHERE guild_id=? AND user_id=? AND item='xpboost'",
                           (str(guild_id), str(user_id))).fetchone()
    if not row or not row['expires']:
        return 0
    return max(0, int(row['expires']) - int(time.time()))


def init_db():
    with conn_ctx() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS levels (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0,
            last_text_xp INTEGER DEFAULT 0, voice_minutes INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id TEXT PRIMARY KEY, levelup_channel TEXT,
            levelup_dm INTEGER DEFAULT 0, modlog_channel TEXT,
            noxp_channels TEXT DEFAULT '[]', xp_multiplier REAL DEFAULT 1.0,
            stack_rewards INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS level_rewards (
            guild_id TEXT NOT NULL, level INTEGER NOT NULL, role_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, level))''')
        c.execute('''CREATE TABLE IF NOT EXISTS warns (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL, mod_id TEXT NOT NULL, reason TEXT,
            created_at INTEGER NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS automod (
            guild_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1,
            anti_invite INTEGER DEFAULT 1, anti_link INTEGER DEFAULT 0,
            badwords TEXT DEFAULT '[]', whitelist_channels TEXT DEFAULT '[]',
            whitelist_roles TEXT DEFAULT '[]')''')
        c.execute('''CREATE TABLE IF NOT EXISTS selfroles (
            guild_id TEXT NOT NULL, message_id TEXT, channel_id TEXT,
            roles TEXT DEFAULT '[]', PRIMARY KEY (guild_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS tiktok_watch (
            guild_id TEXT NOT NULL, tiktok_username TEXT NOT NULL,
            channel_id TEXT NOT NULL, last_video_id TEXT, template TEXT DEFAULT '',
            PRIMARY KEY (guild_id, tiktok_username))''')
        try:
            c.execute('ALTER TABLE tiktok_watch ADD COLUMN sec_uid TEXT')
        except Exception:
            pass  # already there
        try:
            c.execute('ALTER TABLE tiktok_watch ADD COLUMN ping_role TEXT')
        except Exception:
            pass  # already there
        c.execute('''CREATE TABLE IF NOT EXISTS voicemaster (
            guild_id TEXT PRIMARY KEY, master_channel_id TEXT, category_id TEXT,
            default_limit INTEGER DEFAULT 0, default_name TEXT DEFAULT '{user}''s pv')''')
        for col in ('interface_channel_id TEXT', 'interface_message_id TEXT'):
            try:
                c.execute(f'ALTER TABLE voicemaster ADD COLUMN {col}')
            except Exception:
                pass
        c.execute('''CREATE TABLE IF NOT EXISTS vm_icons (
            guild_id TEXT NOT NULL, key TEXT NOT NULL, emoji TEXT NOT NULL,
            PRIMARY KEY (guild_id, key))''')
        c.execute('''CREATE TABLE IF NOT EXISTS temp_vcs (
            channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, blocked TEXT DEFAULT '[]', trusted TEXT DEFAULT '[]')''')
        try:
            c.execute('ALTER TABLE temp_vcs ADD COLUMN panel_id TEXT')
        except Exception:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS antiraid (
            guild_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1,
            min_age_days INTEGER DEFAULT 60, action TEXT DEFAULT 'kick',
            honeypot_channel TEXT, burst_count INTEGER DEFAULT 5,
            burst_seconds INTEGER DEFAULT 10, lockdown INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS permabans (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, reason TEXT,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS prefixes (
            guild_id TEXT PRIMARY KEY, prefix TEXT DEFAULT '.')''')
        c.execute('''CREATE TABLE IF NOT EXISTS info_panels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, message_id TEXT, title TEXT, description TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS info_buttons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, panel_id INTEGER NOT NULL,
            label TEXT NOT NULL, text TEXT NOT NULL, style TEXT DEFAULT 'grey')''')
        c.execute('''CREATE TABLE IF NOT EXISTS verify_panels (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, message_id TEXT,
            title TEXT, description TEXT, label TEXT DEFAULT 'Verify', role_id TEXT)''')
        try:
            c.execute('ALTER TABLE verify_panels ADD COLUMN managed INTEGER DEFAULT 1')
        except Exception:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS welcome_cfg (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, join_text TEXT,
            leave_channel_id TEXT, leave_text TEXT, enabled INTEGER DEFAULT 1)''')
        for col in ('boost_channel TEXT', 'boost_text TEXT',
                    'pingjoin_channel TEXT', 'pingjoin_text TEXT',
                    'pingjoin_delete INTEGER DEFAULT 10',
                    'join_card INTEGER DEFAULT 0', 'leave_card INTEGER DEFAULT 0'):
            try:
                c.execute(f'ALTER TABLE welcome_cfg ADD COLUMN {col}')
            except Exception:
                pass  # already there
        c.execute('''CREATE TABLE IF NOT EXISTS card_cfg (
            guild_id TEXT NOT NULL, kind TEXT NOT NULL, title TEXT DEFAULT '',
            bg_url TEXT DEFAULT '', bg_color TEXT DEFAULT '', blur INTEGER DEFAULT 25,
            dim REAL DEFAULT 0.45, layout TEXT DEFAULT 'banner',
            accent TEXT DEFAULT '', show_avatar INTEGER DEFAULT 1,
            PRIMARY KEY (guild_id, kind))''')
        for col in ('show_tier INTEGER DEFAULT 1', 'show_bar INTEGER DEFAULT 1',
                    'show_xptext INTEGER DEFAULT 1', 'show_stat INTEGER DEFAULT 1'):
            try:
                c.execute(f'ALTER TABLE card_cfg ADD COLUMN {col}')
            except Exception:
                pass
        c.execute('''CREATE TABLE IF NOT EXISTS card_tiers (
            guild_id TEXT NOT NULL, tier_key TEXT NOT NULL, name TEXT DEFAULT '',
            PRIMARY KEY (guild_id, tier_key))''')
        c.execute('''CREATE TABLE IF NOT EXISTS card_skins (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            min_level INTEGER DEFAULT 0, bg_url TEXT DEFAULT '',
            bg_color TEXT DEFAULT '', accent TEXT DEFAULT '',
            layout TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS staff_roles (
            guild_id TEXT NOT NULL, role_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, role_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS lang (
            guild_id TEXT PRIMARY KEY, lang TEXT DEFAULT 'en')''')
        c.execute('''CREATE TABLE IF NOT EXISTS permkicks (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, reason TEXT,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS forcenick (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, nick TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS modcfg (
            guild_id TEXT PRIMARY KEY, mute_duration INTEGER DEFAULT 600)''')
        c.execute('''CREATE TABLE IF NOT EXISTS jailcfg (
            guild_id TEXT PRIMARY KEY, role_id TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS purgelogs (
            guild_id TEXT PRIMARY KEY, channel_id TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS purgeignore (
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, channel_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS afk (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            reason TEXT DEFAULT 'AFK', set_at REAL,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS ticket_cfg (
            guild_id TEXT PRIMARY KEY, panel_channel TEXT, panel_message TEXT,
            category_id TEXT, archive_id TEXT, log_channel TEXT,
            support_roles TEXT DEFAULT '[]', max_open INTEGER DEFAULT 3,
            auto_close_hours INTEGER DEFAULT 0, naming TEXT DEFAULT 'ticket-{user}-{n}',
            greeting TEXT DEFAULT '', btn_label TEXT DEFAULT 'Open ticket',
            title TEXT DEFAULT 'Support', description TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS ticket_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            label TEXT NOT NULL, description TEXT DEFAULT '',
            support_role TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS tickets (
            channel_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            type_id INTEGER, claimed_by TEXT, rating INTEGER,
            created_at INTEGER, last_msg_at INTEGER, closed INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS eco (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            cash INTEGER DEFAULT 1000, last_daily INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS verify_cfg (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, message_id TEXT,
            role_id TEXT, unverified_role TEXT, method TEXT DEFAULT 'button',
            kick_minutes INTEGER DEFAULT 0, title TEXT DEFAULT 'Verification',
            description TEXT DEFAULT '', label TEXT DEFAULT 'Verify')''')
        c.execute('''CREATE TABLE IF NOT EXISTS embeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, message_id TEXT, title TEXT,
            description TEXT, footer TEXT, image TEXT, thumb TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS giveaways (
            message_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            prize TEXT, winners INTEGER DEFAULT 1, ends_at INTEGER, host_id TEXT, closed INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS gentries (
            message_id TEXT NOT NULL, user_id TEXT NOT NULL,
            PRIMARY KEY (message_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS polls (
            message_id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            question TEXT, options TEXT DEFAULT '[]', ends_at INTEGER, closed INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS pvotes (
            message_id TEXT NOT NULL, user_id TEXT NOT NULL, idx INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS reactpanels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            channel_id TEXT NOT NULL, message_id TEXT, title TEXT, description TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS reactroles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, panel_id INTEGER NOT NULL,
            emoji TEXT NOT NULL, role_id TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS whitelist (
            guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, system TEXT NOT NULL,
            PRIMARY KEY (guild_id, channel_id, system))''')
        c.execute('''CREATE TABLE IF NOT EXISTS strike_cfg (
            guild_id TEXT PRIMARY KEY, s1_min INTEGER DEFAULT 5,
            s2_min INTEGER DEFAULT 10, s3_action TEXT DEFAULT 'kick')''')
        c.execute('''CREATE TABLE IF NOT EXISTS strikes (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, count INTEGER DEFAULT 0,
            updated_at INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS xp_boosts (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, mult REAL DEFAULT 1.0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS babka_cfg (
            guild_id TEXT PRIMARY KEY, chat_channel TEXT, ambient_on INTEGER DEFAULT 1)''')
        c.execute('''CREATE TABLE IF NOT EXISTS clown_points (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, count INTEGER DEFAULT 0,
            last_reason TEXT DEFAULT '',
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS clown_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL, giver_id TEXT NOT NULL,
            reason TEXT DEFAULT '', created_at INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS clown_cfg (
            guild_id TEXT PRIMARY KEY, channel_id TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS langroles_cfg (
            guild_id TEXT PRIMARY KEY, channel_id TEXT, message_id TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS stats_cfg (
            guild_id TEXT PRIMARY KEY, category_id TEXT,
            members_ch TEXT, boosts_ch TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS youtube_watch (
            guild_id TEXT NOT NULL, yt_channel_id TEXT NOT NULL, label TEXT DEFAULT '',
            channel_id TEXT NOT NULL, last_video_id TEXT, ping_role TEXT,
            PRIMARY KEY (guild_id, yt_channel_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS msg_stats (
            guild_id TEXT NOT NULL, day TEXT NOT NULL, user_id TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, day, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS day_events (
            guild_id TEXT NOT NULL, day TEXT NOT NULL,
            joins INTEGER DEFAULT 0, leaves INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, day))''')
        try:
            c.execute('ALTER TABLE eco ADD COLUMN last_rob INTEGER DEFAULT 0')
        except Exception:
            pass  # already there
        c.execute('''CREATE TABLE IF NOT EXISTS ships (
            guild_id TEXT NOT NULL, u1 TEXT NOT NULL, u2 TEXT NOT NULL,
            pct INTEGER DEFAULT 0, at INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, u1, u2))''')
        c.execute('''CREATE TABLE IF NOT EXISTS counting_cfg (
            guild_id TEXT PRIMARY KEY, channel_id TEXT,
            current INTEGER DEFAULT 0, record INTEGER DEFAULT 0, last_user TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS wordle (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, day TEXT NOT NULL,
            word TEXT DEFAULT '', guesses TEXT DEFAULT '[]', done INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, day))''')
        c.execute('''CREATE TABLE IF NOT EXISTS wordle_cfg (
            guild_id TEXT PRIMARY KEY, channel_id TEXT,
            minutes INTEGER DEFAULT 30, next_at INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS wordle_wins (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, wins INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS jobs (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, job TEXT DEFAULT '',
            fans INTEGER DEFAULT 0, tier INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS jail (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, until INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS inventory (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, item TEXT NOT NULL,
            qty INTEGER DEFAULT 0, expires INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, item))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bounties (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            target_id TEXT NOT NULL, amount INTEGER DEFAULT 0, by_id TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS heists (
            guild_id TEXT PRIMARY KEY, target TEXT DEFAULT '', stake INTEGER DEFAULT 0,
            crew TEXT DEFAULT '[]', ends_at INTEGER DEFAULT 0, channel_id TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS fit_links (
            guild_id TEXT NOT NULL, u1 TEXT NOT NULL, u2 TEXT NOT NULL,
            PRIMARY KEY (guild_id, u1))''')
        c.execute('''CREATE TABLE IF NOT EXISTS achievements (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, akey TEXT NOT NULL,
            unlocked_at INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, akey))''')
        c.execute('''CREATE TABLE IF NOT EXISTS stocks (
            guild_id TEXT NOT NULL, symbol TEXT NOT NULL, price INTEGER DEFAULT 100,
            updated_at INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, symbol))''')
        c.execute('''CREATE TABLE IF NOT EXISTS stock_hist (
            guild_id TEXT NOT NULL, symbol TEXT NOT NULL, price INTEGER DEFAULT 0,
            ts INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS portfolio (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, symbol TEXT NOT NULL,
            qty INTEGER DEFAULT 0, spent INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, symbol))''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_dex (
            dex INTEGER PRIMARY KEY, name TEXT DEFAULT '', types TEXT DEFAULT '[]',
            hp INTEGER DEFAULT 50, atk INTEGER DEFAULT 50, dfn INTEGER DEFAULT 50,
            spa INTEGER DEFAULT 50, spd INTEGER DEFAULT 50, spe INTEGER DEFAULT 50,
            sprite TEXT DEFAULT '', rate INTEGER DEFAULT 45,
            legendary INTEGER DEFAULT 0, evo_to INTEGER DEFAULT 0, evo_level INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_moves (
            name TEXT PRIMARY KEY, power INTEGER DEFAULT 40,
            ptype TEXT DEFAULT 'normal', acc INTEGER DEFAULT 100)''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_mons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, dex INTEGER DEFAULT 1, level INTEGER DEFAULT 5,
            xp INTEGER DEFAULT 0, shiny INTEGER DEFAULT 0, nick TEXT DEFAULT '',
            active INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_balls (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, ball TEXT NOT NULL,
            qty INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, ball))''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_dexcount (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, dex INTEGER NOT NULL,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, dex))''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_quested (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, track TEXT NOT NULL,
            tier INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id, track))''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_hunt (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, target INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS pk_market (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL,
            seller_id TEXT NOT NULL, seller_name TEXT DEFAULT '',
            dex INTEGER DEFAULT 1, level INTEGER DEFAULT 5,
            xp INTEGER DEFAULT 0, shiny INTEGER DEFAULT 0, nick TEXT DEFAULT '',
            price INTEGER DEFAULT 0, created INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS suggest_cfg (
            guild_id TEXT PRIMARY KEY, panel_channel TEXT,
            panel_message TEXT, inbox_channel TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS tributes (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, total INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS bank_links (
            guild_id TEXT NOT NULL, u1 TEXT NOT NULL, u2 TEXT NOT NULL,
            PRIMARY KEY (guild_id, u1))''')
        c.execute('''CREATE TABLE IF NOT EXISTS voice_vaults (
            guild_id TEXT PRIMARY KEY, channel_id TEXT)''')
        try:
            c.execute('ALTER TABLE eco ADD COLUMN daily_streak INTEGER DEFAULT 0')
        except Exception:
            pass  # already there
        try:
            c.execute('ALTER TABLE eco ADD COLUMN last_work INTEGER DEFAULT 0')
        except Exception:
            pass  # already there
        for col in ('ALTER TABLE eco ADD COLUMN energy INTEGER DEFAULT 100',
                    'ALTER TABLE eco ADD COLUMN energy_at INTEGER DEFAULT 0',
                    'ALTER TABLE jobs ADD COLUMN shifts INTEGER DEFAULT 0',
                    'ALTER TABLE eco ADD COLUMN bank INTEGER DEFAULT 0',
                    'ALTER TABLE eco ADD COLUMN bank_at INTEGER DEFAULT 0',
                    'ALTER TABLE eco ADD COLUMN gamble_n INTEGER DEFAULT 0',
                    'ALTER TABLE eco ADD COLUMN gamble_hr INTEGER DEFAULT 0',
                    'ALTER TABLE eco ADD COLUMN cookie_n INTEGER DEFAULT 0',
                    'ALTER TABLE eco ADD COLUMN cookie_day INTEGER DEFAULT 0'):
            try:
                c.execute(col)
            except Exception:
                pass  # already there


def get_settings(guild_id: str) -> dict:
    with conn_ctx() as conn:
        row = conn.execute('SELECT * FROM guild_settings WHERE guild_id = ?', (str(guild_id),)).fetchone()
        if not row:
            conn.execute('INSERT INTO guild_settings (guild_id) VALUES (?)', (str(guild_id),))
            row = conn.execute('SELECT * FROM guild_settings WHERE guild_id = ?', (str(guild_id),)).fetchone()
        return dict(row)


def get_prefix(guild_id) -> str:
    with conn_ctx() as conn:
        row = conn.execute('SELECT prefix FROM prefixes WHERE guild_id = ?', (str(guild_id),)).fetchone()
        return row['prefix'] if row else '.'


def get_antiraid(guild_id) -> dict:
    with conn_ctx() as conn:
        row = conn.execute('SELECT * FROM antiraid WHERE guild_id = ?', (str(guild_id),)).fetchone()
        if not row:
            conn.execute('INSERT INTO antiraid (guild_id) VALUES (?)', (str(guild_id),))
            row = conn.execute('SELECT * FROM antiraid WHERE guild_id = ?', (str(guild_id),)).fetchone()
        return dict(row)
