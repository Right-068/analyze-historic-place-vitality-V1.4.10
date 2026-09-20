"""Lossless raw storage is elsewhere; these are bounded, non-evidentiary views."""
from html.parser import HTMLParser
import hashlib
import json
import re
from difflib import SequenceMatcher
from functools import lru_cache


@lru_cache(maxsize=256)
def site_template(domain):
    # Only structural markers, never learned deletions of repeated research text.
    return frozenset({'nav', 'footer', 'aside', 'script', 'style', 'noscript', 'template'})


class BodyView(HTMLParser):
    def __init__(self, domain):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.ignored = site_template(domain)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        style = re.sub(r'\s+', '', values.get('style') or '').lower()
        role = (values.get('role') or '').lower()
        classes = set(re.split(r'\s+', (values.get('class') or '').lower()))
        hidden = (tag in self.ignored or 'hidden' in values or
                  'display:none' in style or 'visibility:hidden' in style or
                  role in {'navigation', 'banner', 'contentinfo', 'complementary'} or
                  bool(classes & {'breadcrumb', 'breadcrumbs', 'advertisement', 'ad-banner', 'related-articles'}))
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.stack.append((tag, hidden or any(item[1] for item in self.stack)))
        if tag in {'p','div','article','section','li','br','h1','h2','h3'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in {'p','div','article','section','li'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not any(item[1] for item in self.stack):
            self.parts.append(data)


def clean_body(raw, body_format, domain=''):
    if body_format == 'text/html':
        parser = BodyView(domain)
        parser.feed(raw)
        raw = ''.join(parser.parts)
    lines = []
    for line in raw.splitlines():
        line = re.sub(r'[ \t\xa0]+', ' ', line).strip()
        if not line or re.fullmatch(r'(登录|注册|返回顶部|上一页|下一页|首页|版权所有|Copyright\s+\d{4}.*)', line, re.I):
            continue
        if not lines or lines[-1] != line:
            lines.append(line)
    return '\n'.join(lines)


def compact_view(body, terms=(), limit=900, offset=0):
    """Return exact slices with offsets; a short view never asserts irrelevance."""
    if offset:
        return {'text': body[offset:offset+limit], 'offset': offset,
                'has_more': offset+limit < len(body)}
    positions = [body.find(t) for t in terms if t and body.find(t) >= 0]
    start = max(0, min(positions)-100) if positions else 0
    return {'text': body[start:start+limit], 'offset': start,
            'has_more': start > 0 or start+limit < len(body)}


def content_hash(body):
    return hashlib.sha256(body.encode('utf8')).hexdigest()


def similarity(left, right):
    # Scheduling hint only; never admission dedup or a scoring rule.
    if left == right:
        return 1.0
    # A matching prefix cannot establish equality of the retained full text.
    # Junk filtering bounds repetitive-template work; this remains only a hint.
    return min(0.999, SequenceMatcher(None, left[:12000], right[:12000], autojunk=True).ratio())


def bounded(items, count, chars, text_key='view'):
    selected = []
    used = 0
    for item in items:
        size = len(json.dumps(item, ensure_ascii=False))
        if not selected and size > chars:
            item=dict(item)
            overflow=size-chars+32
            item[text_key]=str(item.get(text_key,''))[:max(0,len(str(item.get(text_key,'')))-overflow)]
            item['has_more']=True
            size=len(json.dumps(item,ensure_ascii=False))
            if size>chars:raise ValueError('item_metadata_exceeds_context_budget')
        if selected and (len(selected) >= count or used+size > chars):
            break
        selected.append(item)
        used += size
    return selected
