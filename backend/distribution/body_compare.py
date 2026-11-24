"""Compare the identified Easy WeChat body with the rendered reprint draft."""
from collections import Counter
from copy import copy
from html.parser import HTMLParser
import re

from markdown_it.token import Token

from .markdown_media import MARKDOWN

VOID = set("area base br col embed hr img input link meta param source track wbr".split())
STRUCTURE = set("h1 h2 h3 h4 h5 h6 ol ul li blockquote table tr th td sub sup pre".split())
HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)",re.I)
SPACING = re.compile(r"[\s\u200b\ufeff]+")
STARS = re.compile(r"(?<!\*)\*\*([^*\n]+)\*\*(?!\*)")


class Body(HTMLParser):
    def __init__(self,require_source):
        super().__init__()
        self.require_source=require_source
        self.roots=0
        self.stack=[]
        self.open_tags=Counter()
        self.fragments=[]
        self.structure=[]
        self.length=0
        self.bold=set()
        self.repairs=set()

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        selected,hidden=self.stack[-1][1:3] if self.stack else (not self.require_source,False)
        if self.require_source and attrs.get("id")=="js_content":
            self.roots+=1
            selected=True
        hidden=(hidden or tag in ("style","script","template","noscript","svg") or "hidden" in attrs
                or HIDDEN_STYLE.search(attrs.get("style") or "") is not None)
        if selected and not hidden and tag in STRUCTURE:
            self.structure.append(tag)
        if tag not in VOID:
            self.stack.append((tag,selected,hidden,self.length,"data-source-bold" in attrs))
            self.open_tags[tag]+=1

    def handle_endtag(self,tag):
        if not self.open_tags[tag]:
            return
        while self.stack:
            name,selected,hidden,start,repair=self.stack.pop()
            self.open_tags[name]-=1
            if selected and not hidden and name in ("b","strong"):
                self.bold.add((start,self.length))
                if repair:
                    self.repairs.add((start,self.length))
            if name==tag:
                break

    def handle_data(self,data):
        if self.stack and self.stack[-1][1] and not self.stack[-1][2]:
            text=SPACING.sub("",data)
            self.fragments.append(text)
            self.length+=len(text)


def marked_render(tokens):
    blocks=[]
    changed=False
    for block in tokens:
        if block.type=="inline" and "**" in block.content:
            block=copy(block)
            children=[]
            for token in block.children or ():
                matches=list(STARS.finditer(token.content)) if token.type=="text" else []
                if not matches:
                    children.append(token)
                    continue
                offset=0
                for match in matches:
                    children.extend([Token("text","",0,content=token.content[offset:match.start()]),
                                     Token("strong_open","strong",1,attrs={"data-source-bold":"1"}),
                                     Token("text","",0,content=match[1]),Token("strong_close","strong",-1)])
                    offset=match.end()
                children.append(Token("text","",0,content=token.content[offset:]))
                changed=True
            block.children=children
        blocks.append(block)
    return MARKDOWN.renderer.render(blocks,MARKDOWN.options,{}) if changed else None


def checked_render(source,tokens):
    rendered=MARKDOWN.renderer.render(tokens,MARKDOWN.options,{})
    if not source.strip():
        return rendered,{"status":"no_html"}
    original=Body(True)
    original.feed(source)
    original.close()
    if original.roots!=1:
        return rendered,{"status":"no_body"}
    draft=Body(False)
    draft.feed(rendered)
    draft.close()
    left,right=("".join(body.fragments) for body in (original,draft))
    restored=0
    if original.bold and left!=right and "**" in right:
        # Some Easy WeChat exports wrap Chinese punctuation in Markdown emphasis
        # that CommonMark reads literally. Verify every proposed range against
        # the original emphasis and the entire visible body before using it.
        marked=marked_render(tokens)
        if marked:
            candidate=Body(False)
            candidate.feed(marked)
            candidate.close()
            if candidate.repairs and candidate.repairs<=original.bold and "".join(candidate.fragments)==left:
                restored=len(candidate.repairs)
                rendered=marked.replace('<strong data-source-bold="1">',"<strong>")
                draft,right=candidate,left
    result=dict(status="compared",source_chars=len(left),draft_chars=len(right),
                structure_matches=original.structure==draft.structure,restored_bold=restored)
    if left!=right:
        offset=next((i for i,(a,b) in enumerate(zip(left,right)) if a!=b),min(len(left),len(right)))
        result["difference"]=dict(position=offset+1,context=left[max(0,offset-18):offset],
                                  source=left[offset:offset+48],draft=right[offset:offset+48])
    if not result["structure_matches"]:
        before,after=Counter(original.structure),Counter(draft.structure)
        result["structure_counts"]={tag:[before[tag],after[tag]] for tag in sorted(before.keys()|after.keys())
                                    if before[tag]!=after[tag]}
    return rendered,result
