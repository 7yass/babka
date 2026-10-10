"""Daily quests + activity tickets (Lumina-style retention loop).

Quests reset on UTC day; completion auto-grants (no claim step to forget).
Passive income folds into the SAME transaction as the XP writes that
already happen per message / per voice minute — zero extra round trips.
"""
import datetime

import database as db

QUESTS = [
    {'id': 'chatter', 'goal': 25, 'reward': ('tickets', 1), 'label': 'Send 25 messages'},
    {'id': 'grinder', 'goal': 100, 'reward': ('tickets', 2), 'label': 'Send 100 messages'},
    {'id': 'vocal', 'goal': 20, 'reward': ('tickets', 1), 'label': '20 min in voice'},
    {'id': 'wisher', 'goal': 3, 'reward': ('tickets', 1), 'label': 'Make 3 wishes'},
    {'id': 'burner', 'goal': 1, 'reward': ('dust', 500), 'label': 'Burn 1 card'},
    {'id': 'dropper', 'goal': 2, 'reward': ('tickets', 1), 'label': 'Claim 2 drops'},
    {'id': 'gambler', 'goal': 3, 'reward': ('coins', 500), 'label': 'Play 3 casino games'},
]
BY_ID = {q['id']: q for q in QUESTS}

MSG_PER_TICKET = 100   # passive: every 100 messages -> 1 ticket
VC_PER_TICKET = 20     # passive: every 20 voice minutes -> 1 ticket


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def _ensure_tickets(conn, gid, uid):
    conn.execute('INSERT OR IGNORE INTO card_tickets (guild_id, user_id) VALUES (?,?)', (gid, uid))


def _grant(conn, gid, uid, reward) -> None:
    kind, amount = reward
    if kind == 'tickets':
        _ensure_tickets(conn, gid, uid)
        conn.execute('UPDATE card_tickets SET tickets=tickets+? WHERE guild_id=? AND user_id=?',
                     (amount, gid, uid))
    elif kind == 'dust':
        conn.execute('INSERT OR IGNORE INTO anime_dust (guild_id, user_id, dust) VALUES (?,?,0)',
                     (gid, uid))
        conn.execute('UPDATE anime_dust SET dust=dust+? WHERE guild_id=? AND user_id=?',
                     (amount, gid, uid))
    elif kind == 'coins':
        conn.execute('INSERT OR IGNORE INTO eco (guild_id, user_id, cash) VALUES (?,?,1000)',
                     (gid, uid))
        conn.execute('UPDATE eco SET cash=cash+? WHERE guild_id=? AND user_id=?',
                     (amount, gid, uid))


def bump(gid, uid, qid: str, n: int = 1) -> list:
    """Advance one quest; returns newly completed quest defs (rewards granted)."""
    gid, uid = str(gid), str(uid)
    q = BY_ID.get(qid)
    if not q:
        return []
    done = []
    with db.conn_ctx() as conn:
        row = conn.execute('SELECT progress, done FROM quest_progress WHERE guild_id=? AND user_id=? '
                           'AND day=? AND quest=?', (gid, uid, _today(), qid)).fetchone()
        prog = int((row['progress'] if row else 0) or 0)
        if row and row['done']:
            return []
        prog = min(q['goal'], prog + max(1, int(n)))
        conn.execute('INSERT OR REPLACE INTO quest_progress (guild_id, user_id, day, quest, progress, done) '
                     'VALUES (?,?,?,?,?,?)', (gid, uid, _today(), qid, prog, 0))
        if prog >= q['goal']:
            conn.execute('UPDATE quest_progress SET done=1 WHERE guild_id=? AND user_id=? AND day=? AND quest=?',
                         (gid, uid, _today(), qid))
            _grant(conn, gid, uid, q['reward'])
            done.append(q)
    return done


def quest_state(gid, uid) -> dict:
    """qid -> (progress, done) for today."""
    gid, uid = str(gid), str(uid)
    with db.conn_ctx() as conn:
        rows = conn.execute('SELECT quest, progress, done FROM quest_progress WHERE guild_id=? AND user_id=? '
                            'AND day=?', (gid, uid, _today())).fetchall()
    out = {q['id']: (0, False) for q in QUESTS}
    for r in rows:
        if r['quest'] in out:
            out[r['quest']] = (int(r['progress'] or 0), bool(r['done']))
    return out


def quests_done(gid, uid) -> int:
    return sum(1 for _, d in quest_state(gid, uid).values() if d)


def message_tick(gid, uid) -> dict:
    """One counted message: passive ticket progress + chatter/grinder quests.
    Single transaction. Returns {'tickets': n_granted, 'quests': [completed]}."""
    gid, uid = str(gid), str(uid)
    out = {'tickets': 0, 'quests': []}
    with db.conn_ctx() as conn:
        _ensure_tickets(conn, gid, uid)
        row = conn.execute('SELECT msg_prog FROM card_tickets WHERE guild_id=? AND user_id=?',
                           (gid, uid)).fetchone()
        prog = int((row['msg_prog'] if row else 0) or 0) + 1
        while prog >= MSG_PER_TICKET:
            prog -= MSG_PER_TICKET
            out['tickets'] += 1
        conn.execute('UPDATE card_tickets SET msg_prog=?, tickets=tickets+? WHERE guild_id=? AND user_id=?',
                     (prog, out['tickets'], gid, uid))
        for qid in ('chatter', 'grinder'):
            q = BY_ID[qid]
            r = conn.execute('SELECT progress, done FROM quest_progress WHERE guild_id=? AND user_id=? '
                             'AND day=? AND quest=?', (gid, uid, _today(), qid)).fetchone()
            if r and r['done']:
                continue
            p = min(q['goal'], int((r['progress'] if r else 0) or 0) + 1)
            conn.execute('INSERT OR REPLACE INTO quest_progress (guild_id, user_id, day, quest, progress, done) '
                         'VALUES (?,?,?,?,?,?)', (gid, uid, _today(), qid, p, 0))
            if p >= q['goal']:
                conn.execute('UPDATE quest_progress SET done=1 WHERE guild_id=? AND user_id=? AND day=? AND quest=?',
                             (gid, uid, _today(), qid))
                _grant(conn, gid, uid, q['reward'])
                out['quests'].append(q)
    return out


def voice_tick(gid, uid) -> dict:
    """One voice minute: passive ticket progress + vocal quest. Single transaction."""
    gid, uid = str(gid), str(uid)
    out = {'tickets': 0, 'quests': []}
    with db.conn_ctx() as conn:
        _ensure_tickets(conn, gid, uid)
        row = conn.execute('SELECT vc_prog FROM card_tickets WHERE guild_id=? AND user_id=?',
                           (gid, uid)).fetchone()
        prog = int((row['vc_prog'] if row else 0) or 0) + 1
        while prog >= VC_PER_TICKET:
            prog -= VC_PER_TICKET
            out['tickets'] += 1
        conn.execute('UPDATE card_tickets SET vc_prog=?, tickets=tickets+? WHERE guild_id=? AND user_id=?',
                     (prog, out['tickets'], gid, uid))
        q = BY_ID['vocal']
        r = conn.execute('SELECT progress, done FROM quest_progress WHERE guild_id=? AND user_id=? '
                         'AND day=? AND quest=?', (gid, uid, _today(), 'vocal')).fetchone()
        if not (r and r['done']):
            p = min(q['goal'], int((r['progress'] if r else 0) or 0) + 1)
            conn.execute('INSERT OR REPLACE INTO quest_progress (guild_id, user_id, day, quest, progress, done) '
                         'VALUES (?,?,?,?,?,?)', (gid, uid, _today(), 'vocal', p, 0))
            if p >= q['goal']:
                conn.execute('UPDATE quest_progress SET done=1 WHERE guild_id=? AND user_id=? AND day=? AND quest=?',
                             (gid, uid, _today(), 'vocal'))
                _grant(conn, gid, uid, q['reward'])
                out['quests'].append(q)
    return out
