"""Identify visible elements in an Easy WeChat export and its js_content region."""
from collections import Counter
from html.parser import HTMLParser
import re

VOID = set("area base br col embed hr img input link meta param source track wbr".split())
HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)",re.I)


class ArticleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.roots=0
        self.stack=[]
        self.open_tags=Counter()

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        inside,visible=self.stack[-1][1:3] if self.stack else (False,True)
        visible=(visible and tag not in ("style","script","template","noscript","svg") and
                 "hidden" not in attrs and not HIDDEN_STYLE.search(attrs.get("style") or ""))
        if visible and attrs.get("id")=="js_content":
            self.roots+=1
            inside=True
        context=self.start_visible(tag,attrs,inside) if visible else None
        if tag not in VOID:
            self.stack.append((tag,inside,visible,context))
            self.open_tags[tag]+=1

    def handle_endtag(self,tag):
        if not self.open_tags[tag]:
            return
        while self.stack:
            name,_,_,context=self.stack.pop()
            self.open_tags[name]-=1
            if context is not None:
                self.end_visible(name,context)
            if name==tag:
                return

    def start_visible(self,tag,attrs,inside):
        """Optionally return context to receive the corresponding end_visible call."""

    def end_visible(self,tag,context):
        pass
