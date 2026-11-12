from html.parser import HTMLParser


class FormObserver(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms, self.select, self.option = [], None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.forms.append({'action': attrs.get('action', ''), 'method': attrs.get('method', 'get'), 'fields': [], 'selects': []})
        elif tag == 'input' and self.forms:
            self.forms[-1]['fields'].append(attrs)
        elif tag == 'select' and self.forms:
            self.select = {'name': attrs.get('name'), 'options': []}
            self.forms[-1]['selects'].append(self.select)
        elif tag == 'option' and self.select is not None:
            self.option = {'value': attrs.get('value'), 'disabled': 'disabled' in attrs,
                           'selected': 'selected' in attrs, 'sku': attrs.get('data-sku'), 'label': ''}
            self.select['options'].append(self.option)

    def handle_data(self, data):
        if self.option is not None:
            self.option['label'] += data

    def handle_endtag(self, tag):
        if tag == 'option':
            self.option = None
        if tag == 'select':
            self.select = None


def inspect_document(body):
    observer = FormObserver()
    observer.feed(body.decode('utf-8', errors='strict'))
    observer.close()
    return {'forms': observer.forms}
