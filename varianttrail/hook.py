import html
import urllib.parse
import uuid


def handle(server, path, headers):
    parsed = urllib.parse.urlsplit(path)
    if parsed.path not in {'/product', '/select', '/purchase'}:
        return None
    if parsed.path == '/purchase':
        return 403, 'text/plain', b'write action prohibited'
    with server.phase_lock:
        if not hasattr(server, 'sessions'):
            server.sessions = {}
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == '/product':
            selection = {}
        else:
            previous = server.sessions.pop(query.get('token', [''])[0], None)
            if previous is None or previous['revision'] != server.phase:
                return 409, 'text/plain', b'expired page'
            selection = previous['selection'].copy()
            field, value = query.get('field', [''])[0], query.get('value', [''])[0]
            choices = available(selection, field, server.phase)
            if value not in choices or choices[value][1] == 'condition':
                return 422, 'text/plain', b'invalid option'
            order = ['material', 'color', 'pack']
            selection = {name: entry for name, entry in selection.items() if order.index(name) < order.index(field)}
            selection[field] = value
        token = uuid.uuid4().hex
        server.sessions[token] = {'revision': server.phase, 'selection': selection}
        parts = ['<html><body><div id="revision">' + server.phase + '</div>', '<pre id="selection">' + html.escape(str(selection)) + '</pre>']
        for field in ['material', 'color', 'pack']:
            choices = available(selection, field, server.phase)
            if not choices:
                continue
            parts.append('<form action="/select"><input type="hidden" name="token" value="' + token + '"><input type="hidden" name="field" value="' + field + '"><select name="value">')
            for value, (label, state) in choices.items():
                disabled = ' disabled' if state == 'condition' else ''
                selected = ' selected' if selection.get(field) == value else ''
                parts.append('<option value="' + value + '" data-key="' + field + ':' + value + '" data-reason="' + state + '"' + disabled + selected + '>' + label + '</option>')
            parts.append('</select><button>查看</button></form>')
        if set(selection) == {'material', 'color', 'pack'}:
            sku = selection['material'] + '-' + selection['color'] + '-' + ('large' if selection['pack'] == 'large' else 'small')
            state = 'soldout' if selection.get('pack') == 'large' and selection.get('color') == 'white' else 'available'
            parts.append('<div id="sku" data-id="' + sku + '" data-price="12.30" data-stock="' + state + '"></div>')
        parts.append('<form action="/purchase"><button>订货</button></form></body></html>')
        return 200, 'text/html', ''.join(parts).encode()


def available(selection, field, phase):
    if field == 'material':
        return {'cotton': ('棉', 'available'), 'linen': ('麻', 'available')}
    if field == 'color' and 'material' in selection:
        return {'white': ('白', 'available'), 'black': ('黑', 'available')} if selection['material'] == 'cotton' else {'natural': ('原色', 'available'), 'black': ('黑', 'condition')}
    if field == 'pack' and 'color' in selection:
        values = {'single': ('单件', 'available'), 'gift': ('礼装', 'available'), 'large': ('大包装', 'soldout' if selection['color'] == 'white' else 'available')}
        if phase == 'updated':
            values.pop('gift')
            values = dict(reversed(list(values.items())))
        return values
    return {}
