"""Raw Discord REST for the webpanel (post messages, reactions, roles, lookups)."""
import json
import os
import urllib.error
import urllib.request

BASE = 'https://discord.com/api/v10'


class DiscordError(Exception):
    pass


def _req(method: str, path: str, data=None):
    token = os.getenv('DISCORD_TOKEN', '')
    if not token or token.startswith('paste_'):
        raise DiscordError('DISCORD_TOKEN missing in .env')
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        BASE + path, data=body, method=method,
        headers={'Authorization': f'Bot {token}', 'Content-Type': 'application/json',
                 'User-Agent': 'BabkaDanka-panel/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get('message', '')
        except Exception:
            detail = ''
        raise DiscordError(f'Discord {e.code} {path} {detail}')


def get_channels(gid, kind='text'):
    """kind: text (0,5) | voice (2) | category (4). Returns [{id,name}]."""
    try:
        chs = _req('GET', f'/guilds/{gid}/channels')
    except DiscordError:
        return []
    want = {'text': (0, 5), 'voice': (2,), 'category': (4,)}.get(kind, (0, 5))
    out = [{'id': c['id'], 'name': c.get('name', '?')} for c in chs if c.get('type') in want]
    return sorted(out, key=lambda c: c['name'].lower())


def get_roles(gid):
    try:
        roles = _req('GET', f'/guilds/{gid}/roles')
    except DiscordError:
        return []
    return sorted(roles, key=lambda r: r.get('position', 0), reverse=True)


def post_message(ch_id, content=None, embed=None, components=None, embeds=None):
    payload = {}
    if content:
        payload['content'] = content
    if embeds:
        payload['embeds'] = embeds
    elif embed:
        payload['embeds'] = [embed]
    if components:
        payload['components'] = components
    return _req('POST', f'/channels/{ch_id}/messages', payload)


def patch_message(ch_id, msg_id, components):
    return _req('PATCH', f'/channels/{ch_id}/messages/{msg_id}', {'components': components})


def delete_message(ch_id, msg_id):
    try:
        _req('DELETE', f'/channels/{ch_id}/messages/{msg_id}')
    except DiscordError:
        pass


def add_reaction(ch_id, msg_id, emoji):
    from urllib.parse import quote
    _req('PUT', f'/channels/{ch_id}/messages/{msg_id}/reactions/{quote(emoji)}/@me')


def create_role(gid, name, color=0, mentionable=False, hoist=False):
    return _req('POST', f'/guilds/{gid}/roles',
                {'name': name[:100], 'color': color, 'mentionable': mentionable, 'hoist': hoist})


def delete_role(gid, role_id):
    _req('DELETE', f'/guilds/{gid}/roles/{role_id}')


def edit_role(gid, role_id, name=None, color=None, hoist=None, mentionable=None):
    payload = {}
    if name is not None:
        payload['name'] = name[:100]
    if color is not None:
        payload['color'] = color
    if hoist is not None:
        payload['hoist'] = hoist
    if mentionable is not None:
        payload['mentionable'] = mentionable
    return _req('PATCH', f'/guilds/{gid}/roles/{role_id}', payload)


def get_member(gid, uid):
    return _req('GET', f'/guilds/{gid}/members/{uid}')


def set_member_roles(gid, uid, role_ids):
    return _req('PATCH', f'/guilds/{gid}/members/{uid}', {'roles': role_ids})


def send_webhook(url: str, content: str, username: str = '', avatar_url: str = '',
                 embeds: list = None, components: list = None):
    if not url.startswith('https://discord.com/api/webhooks/'):
        raise DiscordError('Not a Discord webhook URL.')
    body = {'content': (content or '')[:2000]}
    if username:
        body['username'] = username[:80]
    if avatar_url:
        body['avatar_url'] = avatar_url
    if embeds:
        clean = []
        for e in embeds[:10]:
            item = {}
            for k in ('title', 'description', 'color', 'url'):
                if e.get(k):
                    item[k] = e[k]
            for k in ('author', 'footer', 'image', 'thumbnail', 'fields'):
                if e.get(k):
                    item[k] = e[k]
            if item:
                clean.append(item)
        if clean:
            body['embeds'] = clean
    if not body.get('content') and not body.get('embeds'):
        raise DiscordError('Empty message.')
    if components:
        body['components'] = components
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'BabkaDanka-panel/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as e:
        raise DiscordError(f'Webhook HTTP {e.code}')
