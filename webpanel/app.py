"""Babka Danka webpanel — configure everything in the browser.
Shares data.db with the bot (no restart needed, changes apply live).

Run:  python webpanel/app.py
Env:  WEBPANEL_PASSWORD (required), WEBPANEL_HOST (default 127.0.0.1), WEBPANEL_PORT (default 8080)
"""
import json
import os
import sys
from functools import wraps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))
except Exception:
    pass

import database as db
from lang import get_lang

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.getenv('WEBPANEL_SECRET', 'babka-danka-local')
PASSWORD = os.getenv('WEBPANEL_PASSWORD', '')


def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('ok'):
            return redirect(url_for('login'))
        return fn(*a, **kw)
    return wrapper


def gid() -> str:
    return str(session.get('gid') or '')


def known_guilds():
    ids = set()
    with db.conn_ctx() as conn:
        for tbl in ('guild_settings', 'antiraid', 'prefixes', 'lang', 'welcome_cfg',
                    'voicemaster', 'verify_cfg', 'ticket_cfg'):
            try:
                for r in conn.execute(f'SELECT guild_id FROM {tbl}').fetchall():
                    ids.add(str(r['guild_id']))
            except Exception:
                pass
    return sorted(ids)


def one(table, where='guild_id=?', args=None, create=True):
    with db.conn_ctx() as conn:
        row = conn.execute(f'SELECT * FROM {table} WHERE {where}', args or (gid(),)).fetchone()
        if not row and create and where == 'guild_id=?':
            cols = {'guild_settings': '', 'antiraid': '', 'welcome_cfg': '',
                    'verify_cfg': '', 'ticket_cfg': ''}.get(table)
            if cols is not None:
                conn.execute(f'INSERT OR IGNORE INTO {table} (guild_id) VALUES (?)', (gid(),))
                row = conn.execute(f'SELECT * FROM {table} WHERE {where}', args or (gid(),)).fetchone()
        return dict(row) if row else {}


def save(table, data: dict, where='guild_id=?'):
    data = {k: (v if v != '' else None) for k, v in data.items()}
    with db.conn_ctx() as conn:
        conn.execute(f'INSERT OR IGNORE INTO {table} (guild_id) VALUES (?)', (gid(),))
        sets = ', '.join(f'{k}=?' for k in data)
        conn.execute(f'UPDATE {table} SET {sets} WHERE {where}', (*data.values(), gid()))


# ---------- auth ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if not PASSWORD:
        return 'Set WEBPANEL_PASSWORD in .env and restart the panel.', 500
    if request.method == 'POST':
        if request.form.get('password') == PASSWORD:
            session['ok'] = True
            gs = known_guilds()
            if gs and not session.get('gid'):
                session['gid'] = gs[0]
            return redirect(url_for('dashboard'))
        flash('Wrong password.')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/guild', methods=['POST'])
@login_required
def set_guild():
    session['gid'] = request.form.get('gid', '').strip()
    return redirect(url_for('dashboard'))


# ---------- dashboard ----------
@app.route('/')
@login_required
def dashboard():
    g = gid()
    s = one('guild_settings')
    raid = one('antiraid')
    with db.conn_ctx() as conn:
        staff = [r['role_id'] for r in
                 conn.execute('SELECT role_id FROM staff_roles WHERE guild_id=?', (g,)).fetchall()]
    with db.conn_ctx() as conn:
        def count(tbl, extra=''):
            try:
                return conn.execute(f'SELECT COUNT(*) c FROM {tbl} WHERE guild_id=? {extra}', (g,)).fetchone()['c']
            except Exception:
                return 0
        rows = [
            ('Prefix', db.get_prefix(g) if g else '—'),
            ('Language', ('polski' if get_lang(g) == 'pl' else 'english') if g else '—'),
            ('Staff roles', len(staff)),
            ('Mod log', f"<#{s.get('modlog_channel')}>" if s.get('modlog_channel') else 'missing'),
            ('Level-ups', f"<#{s.get('levelup_channel')}>" if s.get('levelup_channel') else 'same channel'),
            ('Age gate', f"{raid.get('min_age_days')}d → {raid.get('action')}" if raid.get('enabled') else 'off'),
            ('Honeypot', f"<#{raid.get('honeypot_channel')}>" if raid.get('honeypot_channel') else 'missing'),
            ('TikTok watches', count('tiktok_watch')),
            ('Level rewards', count('level_rewards')),
            ('Info panels', count('info_panels')),
            ('Giveaways open', count('giveaways', 'AND closed=0')),
            ('Open tickets', count('tickets', 'AND closed=0')),
        ]
        w = conn.execute('SELECT channel_id FROM welcome_cfg WHERE guild_id=?', (g,)).fetchone()
        tc = conn.execute('SELECT panel_message FROM ticket_cfg WHERE guild_id=?', (g,)).fetchone()
        vc = conn.execute('SELECT master_channel_id FROM voicemaster WHERE guild_id=?', (g,)).fetchone()
        vf = conn.execute('SELECT role_id FROM verify_cfg WHERE guild_id=?', (g,)).fetchone()
        checks = [
            ('Staff roles picked', bool(staff), '/staff'),
            ('Mod log channel', bool(s.get('modlog_channel')), '/general'),
            ('Welcome channel', bool(w and w['channel_id']), '/welcome'),
            ('Ticket panel posted', bool(tc and tc['panel_message']), '/tickets'),
            ('Verification role', bool(vf and vf['role_id']), '/verify'),
            ('Voice lobby', bool(vc and vc['master_channel_id']), '/voice'),
            ('Level rewards', count('level_rewards') > 0, '/levels'),
        ]
    return render_template('dashboard.html', rows=rows, guilds=known_guilds(), gid=g, checks=checks)


# ---------- generic helpers ----------
def _bool(form, name):
    return 1 if form.get(name) == 'on' else 0


# ---------- general ----------
@app.route('/general', methods=['GET', 'POST'])
@login_required
def general():
    g = gid()
    if request.method == 'POST':
        with db.conn_ctx() as conn:
            conn.execute('INSERT OR REPLACE INTO prefixes (guild_id, prefix) VALUES (?,?)',
                         (g, request.form.get('prefix', '.').strip() or '.'))
            conn.execute('INSERT OR REPLACE INTO lang (guild_id, lang) VALUES (?,?)',
                         (g, request.form.get('lang', 'en')))
        s = one('guild_settings')
        save('guild_settings', {
            'modlog_channel': request.form.get('modlog') or None,
            'levelup_channel': request.form.get('levelup') or None,
            'xp_multiplier': float(request.form.get('multiplier') or 1.0),
        })
        flash('Saved.')
        return redirect(url_for('general'))
    from lang import get_lang
    from webpanel.discord_api import get_channels
    chs = [[c['id'], '#' + c['name']] for c in get_channels(g)]
    chs.insert(0, ['', '—'])
    return render_template('form.html', title='General', action='/general', fields=[
        {'k': 'prefix', 'label': 'Prefix', 'v': db.get_prefix(g)},
        {'k': 'lang', 'label': 'Language', 'type': 'select', 'opts': ['en', 'pl'], 'v': get_lang(g)},
        {'k': 'modlog', 'label': 'Mod-log channel', 'type': 'select', 'opts': chs,
         'v': one('guild_settings').get('modlog_channel') or ''},
        {'k': 'levelup', 'label': 'Level-up channel (— = same)', 'type': 'select', 'opts': chs,
         'v': one('guild_settings').get('levelup_channel') or ''},
        {'k': 'multiplier', 'label': 'XP multiplier', 'type': 'number', 'v': one('guild_settings').get('xp_multiplier') or 1.0},
    ])


# ---------- staff ----------
@app.route('/staff', methods=['GET', 'POST'])
@login_required
def staff():
    g = gid()
    with db.conn_ctx() as conn:
        if request.method == 'POST':
            if request.form.get('add'):
                conn.execute('INSERT OR IGNORE INTO staff_roles (guild_id, role_id) VALUES (?,?)',
                             (g, request.form['add'].strip()))
            if request.form.get('del'):
                conn.execute('DELETE FROM staff_roles WHERE guild_id=? AND role_id=?',
                             (g, request.form['del'].strip()))
            flash('Saved.')
            return redirect(url_for('staff'))
        roles = [r['role_id'] for r in
                 conn.execute('SELECT role_id FROM staff_roles WHERE guild_id=?', (g,)).fetchall()]
    from webpanel.discord_api import get_roles as _gr3
    all_roles = [[r['id'], '@' + r['name']] for r in _gr3(g) if r['name'] != '@everyone']
    return render_template('list.html', title='Staff roles (pass every gate)', action='/staff',
                           cols=['Role ID'], rows=[[r] for r in roles], add_label='Add role ID',
                           add_opts=all_roles)


# ---------- protection ----------
@app.route('/protection', methods=['GET', 'POST'])
@login_required
def protection():
    g = gid()
    if request.method == 'POST':
        save('antiraid', {
            'enabled': _bool(request.form, 'ar_on'),
            'min_age_days': int(request.form.get('age') or 60),
            'action': request.form.get('action', 'kick'),
            'burst_count': int(request.form.get('burst_n') or 5),
            'burst_seconds': int(request.form.get('burst_s') or 10),
            'honeypot_channel': request.form.get('honeypot') or None,
        })
        with db.conn_ctx() as conn:
            words = sorted({w.strip().lower() for w in (request.form.get('badwords') or '').replace(',', '\n').split('\n') if w.strip()})
            conn.execute('UPDATE automod SET enabled=?, anti_invite=?, anti_link=?, badwords=? WHERE guild_id=?',
                         (_bool(request.form, 'am_on'), _bool(request.form, 'inv'), _bool(request.form, 'links'),
                          json.dumps(words), g))
            conn.execute('INSERT OR IGNORE INTO strike_cfg (guild_id) VALUES (?)', (g,))
            conn.execute('UPDATE strike_cfg SET s1_min=?, s2_min=?, s3_action=? WHERE guild_id=?',
                         (max(1, int(request.form.get('s1') or 5)), max(1, int(request.form.get('s2') or 10)),
                          request.form.get('s3') if request.form.get('s3') in ('kick', 'ban', 'timeout') else 'kick', g))
        flash('Saved.')
        return redirect(url_for('protection'))
    raid = one('antiraid')
    with db.conn_ctx() as conn:
        am = conn.execute('SELECT * FROM automod WHERE guild_id=?', (g,)).fetchone()
        am = dict(am) if am else {}
        sc = conn.execute('SELECT * FROM strike_cfg WHERE guild_id=?', (g,)).fetchone()
        sc = dict(sc) if sc else {'s1_min': 5, 's2_min': 10, 's3_action': 'kick'}
    return render_template('form.html', title='AntiRaid + AutoMod', action='/protection', fields=[
        {'k': 'ar_on', 'label': 'AntiRaid enabled', 'type': 'check', 'v': raid.get('enabled')},
        {'k': 'age', 'label': 'Min account age (days)', 'type': 'number', 'v': raid.get('min_age_days', 60)},
        {'k': 'action', 'label': 'Too-new action', 'type': 'select', 'opts': ['kick', 'ban', 'log'], 'v': raid.get('action', 'kick')},
        {'k': 'burst_n', 'label': 'Burst joins', 'type': 'number', 'v': raid.get('burst_count', 5)},
        {'k': 'burst_s', 'label': 'Burst seconds', 'type': 'number', 'v': raid.get('burst_seconds', 10)},
        {'k': 'honeypot', 'label': 'Honeypot channel ID (empty = off)', 'v': raid.get('honeypot_channel') or ''},
        {'k': 'am_on', 'label': 'AutoMod enabled', 'type': 'check', 'v': am.get('enabled', 1)},
        {'k': 'inv', 'label': 'Invite filter', 'type': 'check', 'v': am.get('anti_invite', 1)},
        {'k': 'links', 'label': 'Link filter', 'type': 'check', 'v': am.get('anti_link', 0)},
        {'k': 'badwords', 'label': 'Blocked words (one per line)', 'type': 'area',
         'v': '\n'.join(json.loads(am.get('badwords') or '[]'))},
        {'k': 's1', 'label': 'Strike 1 timeout (minutes)', 'type': 'number', 'v': sc.get('s1_min', 5)},
        {'k': 's2', 'label': 'Strike 2 timeout (minutes)', 'type': 'number', 'v': sc.get('s2_min', 10)},
        {'k': 's3', 'label': 'Strike 3 suggested action (staff picks in mod-log)', 'type': 'select',
         'opts': ['kick', 'ban', 'timeout'], 'v': sc.get('s3_action', 'kick')},
    ])


# ---------- levels ----------
@app.route('/api/member')
@login_required
def api_member():
    from webpanel.discord_api import _req
    gid = request.args.get('gid', '')
    uid = request.args.get('uid', '')
    try:
        m = _req('GET', f'/guilds/{gid}/members/{uid}')
        name = m.get('nick') or (m.get('user') or {}).get('global_name') or (m.get('user') or {}).get('username', uid)
    except Exception:
        name = uid
    return {'name': name}


@app.route('/levels', methods=['GET', 'POST'])
@login_required
def levels():
    g = gid()
    with db.conn_ctx() as conn:
        if request.method == 'POST':
            if request.form.get('add_level') and request.form.get('add_role'):
                conn.execute('INSERT OR REPLACE INTO level_rewards (guild_id, level, role_id) VALUES (?,?,?)',
                             (g, int(request.form['add_level']), request.form['add_role'].strip()))
            if request.form.get('del_level'):
                conn.execute('DELETE FROM level_rewards WHERE guild_id=? AND level=?',
                             (g, int(request.form['del_level'])))
            if request.form.get('save_settings'):
                try:
                    mult = float(request.form.get('mult') or 1)
                except Exception:
                    mult = 1
                conn.execute('''INSERT INTO guild_settings (guild_id) VALUES (?)
                    ON CONFLICT(guild_id) DO NOTHING''', (g,))
                conn.execute('''UPDATE guild_settings SET stack_rewards=?, noxp_channels=?,
                    xp_multiplier=?, levelup_channel=?, levelup_dm=? WHERE guild_id=?''',
                             (1 if request.form.get('stack') == 'on' else 0,
                              json.dumps([c.strip() for c in (request.form.get('noxp') or '').split(',') if c.strip()]),
                              max(0, min(mult, 100)),
                              request.form.get('lvl_ch') or None,
                              1 if request.form.get('lvl_dm') == 'on' else 0, g))
            if request.form.get('uid'):
                uid = request.form['uid'].strip()
                try:
                    nl, nx = int(request.form.get('set_level') or 0), int(request.form.get('set_xp') or 0)
                except Exception:
                    nl, nx = 0, 0
                conn.execute('''INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?,?,?,?)
                    ON CONFLICT(guild_id, user_id) DO UPDATE SET xp=excluded.xp, level=excluded.level''',
                             (g, uid, max(0, nx), max(0, nl)))
            if request.form.get('del_uid'):
                conn.execute('DELETE FROM levels WHERE guild_id=? AND user_id=?', (g, request.form['del_uid'].strip()))
            if request.form.get('reset_all') == 'yes':
                conn.execute('DELETE FROM levels WHERE guild_id=?', (g,))
            flash('Saved.')
            return redirect(url_for('levels'))
        rewards = conn.execute('SELECT level, role_id FROM level_rewards WHERE guild_id=? ORDER BY level ASC',
                               (g,)).fetchall()
        s = conn.execute('SELECT stack_rewards, noxp_channels, xp_multiplier, levelup_channel, levelup_dm FROM guild_settings WHERE guild_id=?', (g,)).fetchone()
        top = conn.execute('SELECT user_id, level, xp FROM levels WHERE guild_id=? ORDER BY level DESC, xp DESC LIMIT 20',
                           (g,)).fetchall()
    from webpanel.discord_api import get_roles as _gr2
    from webpanel.discord_api import get_channels as _gc5
    roles = [[r['id'], '@' + r['name']] for r in _gr2(g) if r['name'] != '@everyone']
    chs = [['', 'same channel']] + [[c['id'], '#' + c['name']] for c in _gc5(g)]
    rnames = {r[0]: r[1] for r in roles}
    return render_template('levels.html', rewards=[dict(r) for r in rewards],
                           stack=bool(s and s['stack_rewards']),
                           noxp=', '.join(json.loads((s and s['noxp_channels']) or '[]')),
                           mult=(s and s['xp_multiplier']) or 1,
                           lvl_ch=(s and s['levelup_channel']) or '',
                           lvl_dm=bool(s and s['levelup_dm']),
                           roles=roles, rnames=rnames, chs=chs, gid=g,
                           top=[dict(r) for r in top])


# ---------- welcome ----------
@app.route('/welcome', methods=['GET', 'POST'])
@login_required
def welcome():
    g = gid()
    if request.method == 'POST':
        save('welcome_cfg', {
            'channel_id': request.form.get('channel') or None,
            'join_text': request.form.get('join') or 'default:join',
            'leave_channel_id': request.form.get('leave_ch') or None,
            'leave_text': request.form.get('leave') or 'default:leave',
            'enabled': _bool(request.form, 'on'),
            'join_card': _bool(request.form, 'join_card'),
            'leave_card': _bool(request.form, 'leave_card'),
            'boost_channel': request.form.get('boost_ch') or None,
            'boost_text': request.form.get('boost_tx') or None,
            'pingjoin_channel': request.form.get('ping_ch') or None,
            'pingjoin_text': request.form.get('ping_tx') or None,
            'pingjoin_delete': int(request.form.get('ping_del') or 10),
        })
        flash('Saved.')
        return redirect(url_for('welcome'))
    w = one('welcome_cfg')
    from webpanel.discord_api import get_channels as _gc
    chs = [[c['id'], '#' + c['name']] for c in _gc(g)]
    chs.insert(0, ['', '—'])
    return render_template('form.html', title='Welcome + goodbye + extras (tags: {user} {name} {server} {count})',
                           action='/welcome', fields=[
        {'k': 'on', 'label': 'Enabled', 'type': 'check', 'v': w.get('enabled', 1)},
        {'k': 'channel', 'label': 'Welcome channel', 'type': 'select', 'opts': chs, 'v': w.get('channel_id') or ''},
        {'k': 'join', 'label': 'Join text', 'type': 'area', 'v': w.get('join_text') or ''},
        {'k': 'join_card', 'label': 'Attach welcome card image (styled in Cards)', 'type': 'check',
         'v': w.get('join_card')},
        {'k': 'leave_ch', 'label': 'Leave channel (— = same as welcome)', 'type': 'select', 'opts': chs,
         'v': w.get('leave_channel_id') or ''},
        {'k': 'leave', 'label': 'Leave text', 'type': 'area', 'v': w.get('leave_text') or ''},
        {'k': 'leave_card', 'label': 'Attach leaving card image (styled in Cards)', 'type': 'check',
         'v': w.get('leave_card')},
        {'k': 'boost_ch', 'label': 'Boost channel', 'type': 'select', 'opts': chs, 'v': w.get('boost_channel') or ''},
        {'k': 'boost_tx', 'label': 'Boost text', 'type': 'area', 'v': w.get('boost_text') or ''},
        {'k': 'ping_ch', 'label': 'Ping-on-join channel', 'type': 'select', 'opts': chs,
         'v': w.get('pingjoin_channel') or ''},
        {'k': 'ping_tx', 'label': 'Ping text', 'v': w.get('pingjoin_text') or ''},
        {'k': 'ping_del', 'label': 'Ping delete after (sec)', 'type': 'number', 'v': w.get('pingjoin_delete') or 10},
    ])


# ---------- tiktok ----------
@app.route('/tiktok', methods=['GET', 'POST'])
@login_required
def tiktok():
    g = gid()
    with db.conn_ctx() as conn:
        if request.method == 'POST':
            if request.form.get('add_user') and request.form.get('add_ch'):
                conn.execute('INSERT OR REPLACE INTO tiktok_watch (guild_id, tiktok_username, channel_id) VALUES (?,?,?)',
                             (g, request.form['add_user'].strip().lower().lstrip('@'), request.form['add_ch'].strip()))
            if request.form.get('del'):
                conn.execute('DELETE FROM tiktok_watch WHERE guild_id=? AND tiktok_username=?',
                             (g, request.form['del'].strip().lower()))
            flash('Saved.')
            return redirect(url_for('tiktok'))
        rows = conn.execute('SELECT tiktok_username, channel_id FROM tiktok_watch WHERE guild_id=?', (g,)).fetchall()
    from webpanel.discord_api import get_channels as _gc2
    chs = [[c['id'], '#' + c['name']] for c in _gc2(g)]
    return render_template('tiktok.html', rows=[dict(r) for r in rows], channels=chs)


# ---------- voice ----------
@app.route('/voice', methods=['GET', 'POST'])
@login_required
def voice():
    g = gid()
    with db.conn_ctx() as conn:
        if request.method == 'POST':
            conn.execute('''INSERT INTO voicemaster (guild_id, master_channel_id, category_id, default_name, default_limit)
                VALUES (?,?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET master_channel_id=excluded.master_channel_id,
                category_id=excluded.category_id, default_name=excluded.default_name, default_limit=excluded.default_limit''',
                         (g, request.form.get('lobby') or None, request.form.get('category') or None,
                          request.form.get('name') or "{user}'s pv", int(request.form.get('limit') or 0)))
            flash('Saved.')
            return redirect(url_for('voice'))
        vm = conn.execute('SELECT * FROM voicemaster WHERE guild_id=?', (g,)).fetchone()
        vm = dict(vm) if vm else {}
    from webpanel.discord_api import get_channels as _gc3
    vchs = [['', '—']] + [[c['id'], c['name']] for c in _gc3(g, 'voice')]
    cats = [['', '—']] + [[c['id'], c['name']] for c in _gc3(g, 'category')]
    return render_template('form.html', title='Voice master', action='/voice', fields=[
        {'k': 'lobby', 'label': 'Lobby voice channel', 'type': 'select', 'opts': vchs,
         'v': vm.get('master_channel_id') or ''},
        {'k': 'category', 'label': 'Category (— = same)', 'type': 'select', 'opts': cats,
         'v': vm.get('category_id') or ''},
        {'k': 'name', 'label': 'Room name ({user} = name)', 'v': vm.get('default_name') or "{user}'s pv"},
        {'k': 'limit', 'label': 'Default user limit (0 = none)', 'type': 'number', 'v': vm.get('default_limit') or 0},
    ])


# ---------- verify (rules + gate) ----------
@app.route('/verify', methods=['GET', 'POST'])
@login_required
def verify():
    g = gid()
    if request.method == 'POST':
        save('verify_panels', {'title': request.form.get('r_title') or 'Rules',
                               'description': request.form.get('r_text') or '',
                               'label': request.form.get('r_label') or 'Verify',
                               'role_id': request.form.get('r_role') or None})
        save('verify_cfg', {'title': request.form.get('v_title') or 'Verification',
                            'description': request.form.get('v_desc') or '',
                            'label': request.form.get('v_label') or 'Verify',
                            'role_id': request.form.get('v_role') or None,
                            'unverified_role': request.form.get('v_unver') or None,
                            'method': request.form.get('v_method', 'button'),
                            'kick_minutes': int(request.form.get('v_kick') or 0)})
        flash('Saved. (Panels themselves are posted from Discord: /rules panel, /verify setup.)')
        return redirect(url_for('verify'))
    rules = one('verify_panels', create=False)
    gate = one('verify_cfg', create=False)
    from webpanel.discord_api import get_roles as _gr
    roles = [['', '—']] + [[r['id'], '@' + r['name']] for r in _gr(g) if r['name'] != '@everyone']
    return render_template('form.html', title='Rules panel + verification gate texts/roles', action='/verify', fields=[
        {'k': 'r_title', 'label': '[Rules] Title', 'v': rules.get('title') or ''},
        {'k': 'r_text', 'label': '[Rules] Text', 'type': 'area', 'v': rules.get('description') or ''},
        {'k': 'r_label', 'label': '[Rules] Button', 'v': rules.get('label') or ''},
        {'k': 'r_role', 'label': '[Rules] Role', 'type': 'select', 'opts': roles, 'v': rules.get('role_id') or ''},
        {'k': 'v_title', 'label': '[Gate] Title', 'v': gate.get('title') or ''},
        {'k': 'v_desc', 'label': '[Gate] Text', 'type': 'area', 'v': gate.get('description') or ''},
        {'k': 'v_label', 'label': '[Gate] Button', 'v': gate.get('label') or ''},
        {'k': 'v_role', 'label': '[Gate] Verified role', 'type': 'select', 'opts': roles,
         'v': gate.get('role_id') or ''},
        {'k': 'v_unver', 'label': '[Gate] Unverified role', 'type': 'select', 'opts': roles,
         'v': gate.get('unverified_role') or ''},
        {'k': 'v_method', 'label': '[Gate] Method', 'type': 'select', 'opts': ['button', 'captcha'],
         'v': gate.get('method') or 'button'},
        {'k': 'v_kick', 'label': '[Gate] Kick after (min, 0 = off)', 'type': 'number',
         'v': gate.get('kick_minutes') or 0},
    ])


# ---------- info panels ----------
@app.route('/panels', methods=['GET', 'POST'])
@login_required
def panels():
    from webpanel.discord_api import (DiscordError, delete_message, get_channels,
                                      patch_message, post_message)
    g = gid()
    channels = get_channels(g)
    with db.conn_ctx() as conn:
        if request.method == 'POST':
            if request.form.get('del'):
                pid = int(request.form['del'])
                prow = conn.execute('SELECT channel_id, message_id FROM info_panels WHERE id=? AND guild_id=?',
                                    (pid, g)).fetchone()
                if prow and prow['message_id']:
                    delete_message(prow['channel_id'], prow['message_id'])
                conn.execute('DELETE FROM info_buttons WHERE panel_id=?', (pid,))
                conn.execute('DELETE FROM info_panels WHERE id=? AND guild_id=?', (pid, g))
            if request.form.get('create_ch') and request.form.get('create_title'):
                try:
                    msg = post_message(request.form['create_ch'],
                                       embed={'title': request.form['create_title'].strip(),
                                              'description': request.form.get('create_desc', ''),
                                              'color': 0xFFFFFF})
                    conn.execute('INSERT INTO info_panels (guild_id, channel_id, message_id, title, description) VALUES (?,?,?,?,?)',
                                 (g, request.form['create_ch'], str(msg['id']),
                                  request.form['create_title'].strip(), request.form.get('create_desc', '')))
                except DiscordError as e:
                    flash(str(e))
                    return redirect(url_for('panels'))
            if request.form.get('btn_panel') and request.form.get('btn_label') and request.form.get('btn_text'):
                pid = int(request.form['btn_panel'])
                style = {'grey': 2, 'green': 3, 'red': 4}.get(request.form.get('btn_color', 'grey'), 2)
                cur = conn.execute('INSERT INTO info_buttons (panel_id, label, text, style) VALUES (?,?,?,?)',
                                   (pid, request.form['btn_label'].strip()[:80], request.form['btn_text'],
                                    request.form.get('btn_color', 'grey'))).lastrowid
                prow = conn.execute('SELECT channel_id, message_id FROM info_panels WHERE id=? AND guild_id=?',
                                    (pid, g)).fetchone()
                btns = conn.execute('SELECT id, label, style FROM info_buttons WHERE panel_id=? ORDER BY id ASC',
                                    (pid,)).fetchall()
                smap = {'grey': 2, 'green': 3, 'red': 4}
                comps = []
                for i in range(0, len(btns), 5):
                    comps.append({'type': 1, 'components': [
                        {'type': 2, 'style': smap.get(b['style'], 2),
                         'label': b['label'][:80], 'custom_id': f"info:{b['id']}"}
                        for b in btns[i:i + 5]]})
                try:
                    patch_message(prow['channel_id'], prow['message_id'], comps)
                except DiscordError as e:
                    conn.execute('DELETE FROM info_buttons WHERE id=?', (cur,))
                    flash(str(e))
                    return redirect(url_for('panels'))
            flash('Saved.')
            return redirect(url_for('panels'))
        rows = conn.execute('SELECT id, title, channel_id FROM info_panels WHERE guild_id=?', (g,)).fetchall()
    return render_template('panels.html', rows=[dict(r) for r in rows], channels=channels)


# ---------- roles ----------
@app.route('/roles', methods=['GET', 'POST'])
@login_required
def roles():
    from webpanel.discord_api import DiscordError, create_role, delete_role, get_roles
    g = gid()
    if request.method == 'POST':
        try:
            if request.form.get('add_name'):
                color = int((request.form.get('add_color') or '000000').lstrip('#') or '0', 16)
                create_role(g, request.form['add_name'].strip(),
                            color=color,
                            mentionable=bool(request.form.get('add_mention')),
                            hoist=bool(request.form.get('add_hoist')))
            if request.form.get('del'):
                delete_role(g, request.form['del'].strip())
        except DiscordError as e:
            flash(str(e))
            return redirect(url_for('roles'))
        except ValueError:
            flash('Bad color — use hex like ff0000.')
            return redirect(url_for('roles'))
        flash('Saved.')
        return redirect(url_for('roles'))
    try:
        droles = get_roles(g)
    except DiscordError as e:
        flash(str(e))
        droles = []
    return render_template('roles.html',
                           roles=[{'id': r['id'], 'name': r['name'],
                                   'members': len(r.get('members', [])) if isinstance(r.get('members'), list) else '—',
                                   'managed': r.get('managed')}
                                  for r in droles])


# ---------- webhook sender ----------
@app.route('/webhooks', methods=['GET', 'POST'])
@login_required
def webhooks():
    from webpanel.discord_api import DiscordError, send_webhook
    if request.method == 'POST':
        try:
            send_webhook(request.form.get('url', '').strip(),
                         request.form.get('content', ''),
                         request.form.get('username', '').strip())
            flash('Sent.')
        except DiscordError as e:
            flash(str(e))
        return redirect(url_for('webhooks'))
    return render_template('webhooks.html')


# ---------- studio: discohook-style composer ----------
@app.route('/api/roles')
@login_required
def api_roles():
    from webpanel.discord_api import get_roles, _req
    gid = request.args.get('gid', '') or (known_guilds()[0] if known_guilds() else '')
    try:
        roles = [{'id': r['id'], 'name': r['name']}
                 for r in get_roles(gid) if r['name'] != '@everyone']
    except Exception:
        roles = []
    guilds = []
    for g in known_guilds():
        try:
            info = _req('GET', f'/guilds/{g}')
            guilds.append({'id': g, 'name': info.get('name', g)})
        except Exception:
            guilds.append({'id': g, 'name': g})
    return {'roles': roles, 'guilds': guilds}


@app.route('/api/channels')
@login_required
def api_channels():
    from webpanel.discord_api import get_channels
    gid = request.args.get('gid', '') or (known_guilds()[0] if known_guilds() else '')
    try:
        channels = get_channels(gid, 'text')
    except Exception:
        channels = []
    return {'channels': channels}


@app.route('/studio', methods=['GET', 'POST'])
@login_required
def studio():
    from webpanel.discord_api import DiscordError, send_webhook
    if request.method == 'POST':
        session['wh_url'] = request.form.get('url', '').strip()
        session['wh_user'] = request.form.get('username', '').strip()
        session['wh_avatar'] = request.form.get('avatar', '').strip()
        titles = request.form.getlist('e_title')
        descs = request.form.getlist('e_desc')
        colors = request.form.getlist('e_color')
        images = request.form.getlist('e_image')
        thumbs = request.form.getlist('e_thumb')
        footers = request.form.getlist('e_footer')
        foot_icons = request.form.getlist('e_footicon')
        authors = request.form.getlist('e_author')
        author_icons = request.form.getlist('e_authoricon')
        title_urls = request.form.getlist('e_titleurl')
        embeds = []
        for i in range(len(titles)):
            try:
                color = int((colors[i] or '').lstrip('#') or 'ffffff', 16)
            except ValueError:
                color = 0xFFFFFF
            e = {'title': titles[i][:256], 'description': descs[i][:4000], 'color': color}
            if title_urls[i].strip():
                e['url'] = title_urls[i].strip()
            if authors[i].strip():
                e['author'] = {'name': authors[i].strip()[:256]}
                if author_icons[i].strip():
                    e['author']['icon_url'] = author_icons[i].strip()
            if images[i].strip():
                e['image'] = {'url': images[i].strip()}
            if thumbs[i].strip():
                e['thumbnail'] = {'url': thumbs[i].strip()}
            if footers[i].strip():
                e['footer'] = {'text': footers[i].strip()[:2048]}
                if foot_icons[i].strip():
                    e['footer']['icon_url'] = foot_icons[i].strip()
            if request.form.get(f'e_timestamp_{i}') == 'on':
                import datetime
                e['timestamp'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            names = request.form.getlist(f'f_name_{i}')
            values = request.form.getlist(f'f_value_{i}')
            inlines = request.form.getlist(f'f_inline_{i}')
            fields = []
            for n, v in zip(names, values):
                if n.strip() or v.strip():
                    fields.append({'name': n.strip()[:256] or '​',
                                   'value': v.strip()[:1024] or '​',
                                   'inline': False})
            for k in [int(x) for x in inlines if x.isdigit()]:
                if k < len(fields):
                    fields[k]['inline'] = True
            if fields:
                e['fields'] = fields[:25]
            if e['title'] or e['description'] or fields:
                embeds.append(e)
        b_labels = request.form.getlist('b_label')
        b_colors = request.form.getlist('b_color')
        b_actions = request.form.getlist('b_action')
        b_roles = request.form.getlist('b_role')
        b_urls = request.form.getlist('b_url')
        stylemap = {'grey': 2, 'green': 3, 'red': 4}
        comps = []
        skipped = []
        for lab, col, act, role, url in zip(b_labels, b_colors, b_actions, b_roles, b_urls):
            if not lab.strip():
                continue
            if act == 'link':
                if not url.strip():
                    skipped.append(f"'{lab.strip()}': link needs a URL")
                    continue
                comps.append({'type': 2, 'style': 5, 'label': lab.strip()[:80], 'url': url.strip()})
            elif act == 'verify':
                comps.append({'type': 2, 'style': stylemap.get(col, 2), 'label': lab.strip()[:80],
                              'custom_id': 'verify:studio'})
            else:
                if not role:
                    skipped.append(f"'{lab.strip()}': pick a role (if the list is empty, switch Server above)")
                    continue
                comps.append({'type': 2, 'style': stylemap.get(col, 2), 'label': lab.strip()[:80],
                              'custom_id': f'sr:{role}'})
            if len(comps) >= 5:
                break
        components = [{'type': 1, 'components': comps}] if comps else None
        needs_bot = any('custom_id' in c for c in comps)
        try:
            if needs_bot:
                from webpanel.discord_api import post_message
                ch = request.form.get('channel', '').strip()
                if not ch:
                    flash('Role/verify buttons must be sent via the bot: pick a channel first.')
                    return redirect(url_for('studio'))
                post_message(ch, request.form.get('content', '') or None,
                             components=components, embeds=embeds or None)
                flash(f'Sent via bot ({len(embeds)} embed(s), {len(comps)} button(s)).')
            else:
                send_webhook(session.get('wh_url', ''), request.form.get('content', ''),
                             session.get('wh_user', ''), session.get('wh_avatar', ''), embeds,
                             components=components)
                flash(f'Sent ({len(embeds)} embed(s), {len(comps)} button(s)).')
            for s in skipped:
                flash(f'Skipped button {s}.')
        except DiscordError as e:
            flash(str(e))
        return redirect(url_for('studio'))
    return render_template('studio.html', wh_url=session.get('wh_url', ''),
                           wh_user=session.get('wh_user', ''), wh_avatar=session.get('wh_avatar', ''))


# ---------- cards ----------
_CARD_BG_CACHE = {}


def _dl(url: str):
    """Sync image download for previews. Cached 10 min. None on failure."""
    import time
    import urllib.request
    url = (url or '').strip()
    if not url.lower().startswith(('http://', 'https://')):
        return None
    hit = _CARD_BG_CACHE.get(url)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=12) as r:
            data = r.read(8 << 20)
        from PIL import Image as _Img
        import io as _io
        _Img.open(_io.BytesIO(data)).verify()
        _CARD_BG_CACHE[url] = (time.time(), data)
        return data
    except Exception:
        return None


def _guild_icon(gid: str):
    try:
        from webpanel.discord_api import _req
        import urllib.request
        info = _req('GET', f'/guilds/{gid}')
        if info.get('icon'):
            url = f"https://cdn.discordapp.com/icons/{gid}/{info['icon']}.png?size=256"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=12) as r:
                return r.read(4 << 20)
    except Exception:
        pass
    return None


@app.route('/cards', methods=['GET', 'POST'])
@login_required
def cards():
    from utils.cards import get_style, save_style, DEFAULTS, TIER_TABLE, get_tier_names, save_tier_name
    g = gid()
    if request.method == 'POST':
        action = request.form.get('action', 'style')
        if action == 'tiers':
            for _, key, _default, _, _ in TIER_TABLE:
                save_tier_name(g, key, request.form.get('tier_' + key) or '')
            flash('Tier names saved.')
        elif action == 'skin_add':
            try:
                ml = max(0, int(request.form.get('skin_level') or 0))
            except Exception:
                ml = 0
            with db.conn_ctx() as conn:
                conn.execute('''INSERT INTO card_skins (guild_id, min_level, bg_url, bg_color, accent, layout)
                    VALUES (?,?,?,?,?,?)''', (g, ml, (request.form.get('skin_bg') or '').strip(),
                    (request.form.get('skin_color') or '').strip(), (request.form.get('skin_accent') or '').strip(),
                    request.form.get('skin_layout') or ''))
            flash(f'Skin for level {ml}+ saved.')
        elif action == 'skin_del':
            with db.conn_ctx() as conn:
                conn.execute('DELETE FROM card_skins WHERE guild_id=? AND id=?', (g, int(request.form.get('skin_del'))))
            flash('Skin removed.')
        else:
            kind = request.form.get('kind', 'rank')
            if kind not in DEFAULTS:
                kind = 'rank'
            try:
                blur = max(0, min(int(request.form.get('blur') or 0), 60))
            except Exception:
                blur = 25
            try:
                dim = max(0, min(int(request.form.get('dim') or 0), 90)) / 100
            except Exception:
                dim = 0.45
            save_style(g, kind, {
                'title': (request.form.get('title') or '').strip()[:24],
                'bg_url': (request.form.get('bg_url') or '').strip(),
                'bg_color': (request.form.get('bg_color') or '').strip(),
                'blur': blur, 'dim': dim,
                'layout': request.form.get('layout') if request.form.get('layout') in ('banner', 'center') else 'banner',
                'accent': (request.form.get('accent') or '').strip(),
                'show_avatar': 1 if request.form.get('show_avatar') == 'on' else 0,
                'show_tier': 1 if request.form.get('show_tier') == 'on' else 0,
                'show_bar': 1 if request.form.get('show_bar') == 'on' else 0,
                'show_xptext': 1 if request.form.get('show_xptext') == 'on' else 0,
                'show_stat': 1 if request.form.get('show_stat') == 'on' else 0,
            })
            flash(f'{kind} card saved.')
        return redirect(url_for('cards'))
    styles = {k: get_style(g, k) for k in ('rank', 'welcome', 'leave')}
    for k, s in styles.items():
        s['dim_pct'] = round((s.get('dim') or 0) * 100)
    with db.conn_ctx() as conn:
        skins = [dict(r) for r in conn.execute(
            'SELECT * FROM card_skins WHERE guild_id=? ORDER BY min_level ASC', (g,)).fetchall()]
    return render_template('cards.html', styles=styles, tiers=get_tier_names(g),
                           tier_table=TIER_TABLE, skins=skins)


@app.route('/cards/preview')
@login_required
def cards_preview():
    from utils.cards import get_style, render_greet, apply_skin, get_skin, get_tier_names
    g = gid() or (known_guilds()[0] if known_guilds() else '')
    kind = request.args.get('kind', 'rank')
    if kind not in ('rank', 'welcome', 'leave'):
        kind = 'rank'
    try:
        sample_level = max(0, min(int(request.args.get('level', 12)), 999))
    except Exception:
        sample_level = 12
    if kind == 'rank':
        style = apply_skin(get_style(g, kind), get_skin(g, sample_level))
    else:
        style = get_style(g, kind)
    for k in ('title', 'bg_url', 'bg_color', 'layout', 'accent'):
        v = request.args.get(k)
        if v is not None and v != '':
            style[k] = v
    try:
        style['blur'] = max(0, min(int(request.args.get('blur', style['blur'])), 60))
    except Exception:
        pass
    try:
        style['dim'] = max(0, min(int(request.args.get('dim', 45)), 90)) / 100
    except Exception:
        pass
    for k, q in (('show_avatar', 'avatar'), ('show_tier', 'tier'), ('show_bar', 'bar'),
                 ('show_xptext', 'xptext'), ('show_stat', 'stat')):
        if request.args.get(q) in ('0', '1'):
            style[k] = int(request.args[q])
    avatar = _guild_icon(g) if style.get('show_avatar') else None
    bg = _dl(style.get('bg_url') or '')
    if kind == 'rank':
        from cogs.levels import rank_card, xp_needed
        from lang import get_lang
        member = type('M', (), {'display_name': 'Preview', 'id': 0})()
        f = rank_card(member, {'level': sample_level, 'xp': xp_needed(sample_level) // 2},
                      3, get_lang(g), avatar, None, None, style, bg, get_tier_names(g))
        png = f.fp.getvalue()
    else:
        png = render_greet(kind, 'Preview',
                           'Member #128' if kind == 'welcome' else '127 members left',
                           avatar, bg, style)
    return Response(png, mimetype='image/png')


# ---------- members & roles ----------
def _hex_to_int(s: str) -> int:
    s = (s or '').strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    try:
        return max(0, min(int(s, 16), 0xFFFFFF)) if len(s) == 6 else 0
    except Exception:
        return 0


@app.route('/members', methods=['GET', 'POST'])
@login_required
def members():
    from webpanel.discord_api import (DiscordError, create_role, delete_role, edit_role,
                                      get_member, get_roles, set_member_roles)
    g = gid()
    if request.method == 'POST':
        uid = (request.form.get('uid') or '').strip()
        try:
            if request.form.get('uid_search'):
                return redirect(url_for('members', uid=uid))
            if request.form.get('add_role') and uid:
                m = get_member(g, uid)
                roles = [r for r in m.get('roles', []) if r]
                if request.form['add_role'] not in roles:
                    roles.append(request.form['add_role'])
                set_member_roles(g, uid, roles)
                flash('Role added.')
            if request.form.get('del_role') and uid:
                m = get_member(g, uid)
                set_member_roles(g, uid, [r for r in m.get('roles', []) if r != request.form['del_role']])
                flash('Role removed.')
            if request.form.get('role_create'):
                create_role(g, request.form['role_create'].strip() or 'new role',
                            _hex_to_int(request.form.get('role_color')),
                            'role_ment' in request.form, 'role_hoist' in request.form)
                flash('Role created.')
            if request.form.get('role_edit'):
                edit_role(g, request.form['role_edit'], request.form.get('role_name') or None,
                          _hex_to_int(request.form.get('role_color_edit')),
                          'role_hoist_edit' in request.form, 'role_ment_edit' in request.form)
                flash('Role updated.')
            if request.form.get('role_del'):
                delete_role(g, request.form['role_del'])
                flash('Role deleted.')
        except DiscordError as e:
            flash(str(e))
        return redirect(url_for('members', uid=uid) if uid else url_for('members'))
    uid = (request.args.get('uid') or '').strip()
    member = None
    if uid:
        try:
            m = get_member(g, uid)
            u = m.get('user', {})
            member = {'id': uid, 'name': m.get('nick') or u.get('global_name') or u.get('username', uid),
                      'roles': m.get('roles', [])}
        except DiscordError as e:
            flash(str(e))
    roles = get_roles(g)
    rnames = {r['id']: '@' + r.get('name', '?') for r in roles}
    return render_template('members.html', uid=uid, member=member, roles=roles, rnames=rnames)


# ---------- say ----------
@app.route('/say', methods=['GET', 'POST'])
@login_required
def say():
    from webpanel.discord_api import DiscordError, get_channels, post_message
    g = gid()
    if request.method == 'POST':
        try:
            post_message(request.form.get('channel', ''), request.form.get('text', '') or '…')
            flash('Sent.')
        except DiscordError as e:
            flash(str(e))
        return redirect(url_for('say'))
    chs = [[c['id'], '#' + c['name']] for c in get_channels(g)]
    return render_template('say.html', chs=chs)


# ---------- activity ----------
@app.route('/activity')
@login_required
def activity():
    import datetime
    g = gid()
    days = [(datetime.date.today() - datetime.timedelta(days=i)).isoformat() for i in range(13, -1, -1)]
    with db.conn_ctx() as conn:
        msgs = {r['day']: r['c'] for r in conn.execute(
            'SELECT day, SUM(count) c FROM msg_stats WHERE guild_id=? GROUP BY day', (g,)).fetchall()}
        evs = {r['day']: dict(r) for r in conn.execute(
            'SELECT * FROM day_events WHERE guild_id=?', (g,)).fetchall()}
        week = (datetime.date.today() - datetime.timedelta(days=6)).isoformat()
        top = [dict(r) for r in conn.execute(
            '''SELECT user_id, SUM(count) c FROM msg_stats WHERE guild_id=? AND day>=?
               GROUP BY user_id ORDER BY c DESC LIMIT 10''', (g, week)).fetchall()]
        voice = [dict(r) for r in conn.execute(
            '''SELECT user_id, voice_minutes FROM levels WHERE guild_id=? AND voice_minutes>0
               ORDER BY voice_minutes DESC LIMIT 10''', (g,)).fetchall()]
    bars = [{'day': d[5:], 'msgs': msgs.get(d, 0),
             'joins': (evs.get(d) or {}).get('joins', 0),
             'leaves': (evs.get(d) or {}).get('leaves', 0)} for d in days]
    peak = max([b['msgs'] for b in bars] + [1])
    try:
        from webpanel.discord_api import get_member as _gm
        names = {}
        for r in top:
            try:
                m = _gm(g, r['user_id'])
                names[r['user_id']] = m.get('nick') or (m.get('user') or {}).get('global_name') \
                    or (m.get('user') or {}).get('username', r['user_id'])
            except Exception:
                names[r['user_id']] = r['user_id']
    except Exception:
        names = {}
    return render_template('activity.html', bars=bars, peak=peak, top=top, names=names, voice=voice)


# ---------- moderation ----------
@app.route('/mod', methods=['GET', 'POST'])
@login_required
def mod():
    g = gid()
    warns, bans = [], []
    lookup = ''
    with db.conn_ctx() as conn:
        if request.method == 'POST':
            if request.form.get('lookup'):
                lookup = request.form['lookup'].strip().strip('<@!>')
            if request.form.get('clear'):
                conn.execute('DELETE FROM warns WHERE guild_id=? AND user_id=?', (g, request.form['clear'].strip()))
            if request.form.get('pban_add'):
                conn.execute('INSERT OR REPLACE INTO permabans (guild_id, user_id, reason) VALUES (?,?,?)',
                             (g, request.form['pban_add'].strip(), request.form.get('pban_reason') or 'webpanel'))
            if request.form.get('pban_del'):
                conn.execute('DELETE FROM permabans WHERE guild_id=? AND user_id=?', (g, request.form['pban_del'].strip()))
            if request.method == 'POST' and not request.form.get('lookup'):
                flash('Saved.')
                return redirect(url_for('mod'))
        if lookup:
            warns = [dict(r) for r in conn.execute(
                'SELECT * FROM warns WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 20', (g, lookup)).fetchall()]
        bans = [dict(r) for r in conn.execute('SELECT user_id, reason FROM permabans WHERE guild_id=?', (g,)).fetchall()]
    return render_template('mod.html', warns=warns, bans=bans, lookup=lookup)


# ---------- tickets ----------
@app.route('/tickets', methods=['GET', 'POST'])
@login_required
def tickets():
    from webpanel.discord_api import (DiscordError, delete_message, get_channels,
                                      get_roles, post_message)
    from lang import t as _t
    g = gid()
    if request.method == 'POST':
        if request.form.get('save_cfg'):
            save('ticket_cfg', {
                'panel_channel': request.form.get('panel_ch') or None,
                'category_id': request.form.get('category') or None,
                'archive_id': request.form.get('archive') or None,
                'log_channel': request.form.get('log') or None,
                'max_open': int(request.form.get('max_open') or 3),
                'auto_close_hours': int(request.form.get('auto_close') or 0),
                'naming': request.form.get('naming') or 'ticket-{user}-{n}',
                'greeting': request.form.get('greeting') or '',
                'btn_label': request.form.get('btn') or 'Open ticket',
                'title': request.form.get('title') or 'Support',
                'description': request.form.get('desc') or '',
            })
            flash('Saved.')
        with db.conn_ctx() as conn:
            if request.form.get('staff_add'):
                try:
                    roles = json.loads((one('ticket_cfg').get('support_roles')) or '[]')
                except Exception:
                    roles = []
                if request.form['staff_add'] not in roles:
                    roles.append(request.form['staff_add'])
                conn.execute('INSERT OR IGNORE INTO ticket_cfg (guild_id) VALUES (?)', (g,))
                conn.execute('UPDATE ticket_cfg SET support_roles=? WHERE guild_id=?',
                             (json.dumps(sorted(roles)), g))
                flash('Support role added.')
            if request.form.get('staff_del'):
                try:
                    roles = json.loads((one('ticket_cfg').get('support_roles')) or '[]')
                except Exception:
                    roles = []
                roles = [r for r in roles if r != request.form['staff_del']]
                conn.execute('UPDATE ticket_cfg SET support_roles=? WHERE guild_id=?',
                             (json.dumps(roles), g))
                flash('Support role removed.')
            if request.form.get('t_add'):
                conn.execute('INSERT INTO ticket_types (guild_id, label, description, support_role) VALUES (?,?,?,?)',
                             (g, request.form['t_add'].strip(), request.form.get('t_desc', ''),
                              request.form.get('t_role') or None))
                flash('Type added.')
            if request.form.get('t_del'):
                conn.execute('DELETE FROM ticket_types WHERE guild_id=? AND id=?', (g, int(request.form['t_del'])))
                flash('Type removed.')
            if request.form.get('post'):
                c = one('ticket_cfg')
                if not c.get('panel_channel') or not c.get('category_id'):
                    flash('Pick a panel channel and a category first.')
                else:
                    try:
                        if c.get('panel_message'):
                            try:
                                delete_message(c['panel_channel'], c['panel_message'])
                            except Exception:
                                pass
                        types = [dict(r) for r in conn.execute(
                            'SELECT * FROM ticket_types WHERE guild_id=? ORDER BY id ASC', (g,)).fetchall()]
                        embed = {'title': (c.get('title') or 'Support')[:256],
                                 'description': ((c.get('description') or _t(g, 'tix.pick'))[:4000]),
                                 'color': 0xFFFFFF}
                        if types:
                            comps = [{'type': 1, 'components': [{
                                'type': 3, 'custom_id': 'tix_open_select',
                                'placeholder': _t(g, 'tix.pick')[:100],
                                'options': [{'label': x['label'][:100],
                                             'value': str(x['id']),
                                             'description': (x.get('description') or '')[:100]}
                                            for x in types[:25]]}]}]
                        else:
                            comps = [{'type': 1, 'components': [{
                                'type': 2, 'style': 2,
                                'label': (c.get('btn_label') or 'Open ticket')[:80],
                                'custom_id': 'tix_open_single'}]}]
                        msg = post_message(c['panel_channel'], None,
                                           embeds=[embed], components=comps)
                        conn.execute('UPDATE ticket_cfg SET panel_message=? WHERE guild_id=?',
                                     (str(msg.get('id')), g))
                        flash('Panel posted.')
                    except DiscordError as e:
                        flash(str(e))
        return redirect(url_for('tickets'))
    c = one('ticket_cfg')
    with db.conn_ctx() as conn:
        types = [dict(r) for r in conn.execute(
            'SELECT id, label, description, support_role FROM ticket_types WHERE guild_id=? ORDER BY id ASC',
            (g,)).fetchall()]
        open_n = conn.execute('SELECT COUNT(*) n FROM tickets WHERE guild_id=? AND closed=0', (g,)).fetchone()['n']
    try:
        support = json.loads(c.get('support_roles') or '[]')
    except Exception:
        support = []
    chs = [['', '—']] + [[x['id'], '#' + x['name']] for x in get_channels(g)]
    cats = [['', '—']] + [[x['id'], x['name']] for x in get_channels(g, 'category')]
    roles = [[r['id'], '@' + r['name']] for r in get_roles(g) if r['name'] != '@everyone']
    rnames = {r[0]: r[1] for r in roles}
    return render_template('tickets.html', c=c, types=types, open_n=open_n, chs=chs, cats=cats,
                           roles=roles, support=[(r, rnames.get(r, r)) for r in support])


# ---------- giveaways ----------
@app.route('/giveaways')
@login_required
def giveaways():
    g = gid()
    with db.conn_ctx() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT prize, channel_id, ends_at, message_id FROM giveaways WHERE guild_id=? AND closed=0', (g,)).fetchall()]
    return render_template('list.html', title='Active giveaways (manage in Discord)',
                           action='/giveaways', cols=['Prize', 'Channel', 'Ends', 'Message'],
                           rows=[[r['prize'], r['channel_id'], r['ends_at'], r['message_id']] for r in rows],
                           add_label=None)


# ---------- economy ----------
def _eco_row(g, uid):
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT * FROM eco WHERE guild_id=? AND user_id=?', (g, uid)).fetchone()
        return dict(row) if row else {}


@app.route('/economy', methods=['GET', 'POST'])
@login_required
def economy():
    g = gid()
    if request.method == 'POST':
        uid = (request.form.get('uid') or '').strip()
        try:
            amt = abs(int(request.form.get('amount') or 0))
            if request.form.get('uid_search'):
                return redirect(url_for('economy', uid=uid))
            with db.conn_ctx() as conn:
                if (request.form.get('give') or request.form.get('take')) and uid and amt > 0:
                    conn.execute('INSERT OR IGNORE INTO eco (guild_id, user_id, cash) VALUES (?,?,0)',
                                 (g, uid))
                    if request.form.get('give'):
                        conn.execute('UPDATE eco SET cash=cash+? WHERE guild_id=? AND user_id=?',
                                     (amt, g, uid))
                        flash(f'Gave {amt}.')
                    else:
                        conn.execute('UPDATE eco SET cash=CASE WHEN cash>? THEN cash-? ELSE 0 END '
                                     'WHERE guild_id=? AND user_id=?', (amt, amt, g, uid))
                        flash(f'Took {amt}.')
                elif request.form.get('reset_limit') and uid:
                    conn.execute('UPDATE eco SET gamble_n=0 WHERE guild_id=? AND user_id=?', (g, uid))
                    flash('Hourly gamble counter reset.')
                elif request.form.get('reset_cookie') and uid:
                    conn.execute('UPDATE eco SET cookie_n=0 WHERE guild_id=? AND user_id=?', (g, uid))
                    flash('Cookie ration reset.')
                elif request.form.get('unjail') and uid:
                    db.unjail(g, uid)
                    flash('Freed from jail.')
                elif request.form.get('clearwarns') and uid:
                    conn.execute('DELETE FROM warns WHERE guild_id=? AND user_id=?', (g, uid))
                    flash('Warns cleared.')
        except Exception as e:
            flash(str(e))
        return redirect(url_for('economy', uid=uid) if uid else url_for('economy'))
    uid = (request.args.get('uid') or '').strip()
    member = None
    if uid:
        e = _eco_row(g, uid)
        with db.conn_ctx() as conn:
            job = conn.execute('SELECT job, fans, shifts FROM jobs WHERE guild_id=? AND user_id=?',
                               (g, uid)).fetchone()
            warns = conn.execute('SELECT COUNT(*) c FROM warns WHERE guild_id=? AND user_id=?',
                                 (g, uid)).fetchone()['c']
        jl = db.jail_left(g, uid)
        member = {'id': uid, 'cash': e.get('cash', 0), 'bank': e.get('bank', 0),
                  'streak': e.get('daily_streak', 0),
                  'gambles': f"{e.get('gamble_n', 0)}/10",
                  'cookies': f"{e.get('cookie_n', 0)}/3",
                  'job': dict(job) if job else {},
                  'warns': warns, 'jailed': max(1, jl // 60) if jl else 0}
    with db.conn_ctx() as conn:
        try:
            top = [dict(r) for r in conn.execute(
                'SELECT user_id, cash, bank FROM eco WHERE guild_id=? '
                'ORDER BY cash+bank DESC LIMIT 10', (g,)).fetchall()]
        except Exception:
            top = []
    try:
        from cogs.shop import ITEMS as SHOP_ITEMS
        prices = sorted(((k, v['price']) for k, v in SHOP_ITEMS.items()),
                        key=lambda x: x[1])
    except Exception:
        prices = []
    return render_template('economy.html', uid=uid, member=member, top=top, prices=prices)


if __name__ == '__main__':
    if not PASSWORD:
        print('Set WEBPANEL_PASSWORD in .env first.')
    else:
        db.init_db()
        app.run(host=os.getenv('WEBPANEL_HOST', '127.0.0.1'), port=int(os.getenv('WEBPANEL_PORT', 8080)))
