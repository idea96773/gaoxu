"""Compare the identified Easy WeChat body with the rendered reprint draft."""
from collections import Counter
from copy import copy
from bisect import bisect_right, bisect_left
from html import escape, unescape
from html.parser import HTMLParser
import re

from markdown_it.token import Token

from .markdown_media import MARKDOWN

VOID = set("area base br col embed hr img input link meta param source track wbr".split())
STRUCTURE = set("h1 h2 h3 h4 h5 h6 ol ul li blockquote table tr th td sub sup pre".split())
BLOCKS = set("p div section article header footer aside figure figcaption h1 h2 h3 h4 h5 h6 li blockquote tr th td pre".split())
HIDDEN_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)",re.I)
INLINE_STYLE = re.compile(r"(?:^|;)\s*display\s*:\s*(?:inline(?:-[\w-]+)?|contents)\s*(?:!important\s*)?(?:;|$)",re.I)
SPACING = re.compile(r"[\s\u200b\ufeff]+")
STARS = re.compile(r"(?<!\*)\*\*([^*\n]+)\*\*(?!\*)")
PLAIN_PARAGRAPH = re.compile(r"<p>([^<>]+)</p>")


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
        self.breaks={}
        self.paragraphs=[]
        self.image_offsets=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        selected,hidden=self.stack[-1][1:3] if self.stack else (not self.require_source,False)
        if self.require_source and attrs.get("id")=="js_content":
            self.roots+=1
            selected=True
        style=attrs.get("style") or ""
        hidden=(hidden or tag in ("style","script","template","noscript","svg") or "hidden" in attrs
                or HIDDEN_STYLE.search(style) is not None)
        block=tag in BLOCKS and not INLINE_STYLE.search(style)
        if selected and not hidden and tag in STRUCTURE:
            self.structure.append(tag)
        if selected and not hidden:
            if block:
                self.breaks[self.length]="paragraph"
                if tag=="p" and not self.require_source:
                    self.paragraphs.append((self.length,self.getpos()))
            elif tag=="br":
                self.breaks.setdefault(self.length,"line")
            elif tag=="img":
                self.image_offsets.append(self.length)
        if tag not in VOID:
            self.stack.append((tag,selected,hidden,self.length,"data-source-bold" in attrs,block))
            self.open_tags[tag]+=1

    def handle_endtag(self,tag):
        if not self.open_tags[tag]:
            return
        while self.stack:
            name,selected,hidden,start,repair,block=self.stack.pop()
            self.open_tags[name]-=1
            if selected and not hidden and block:
                self.breaks[self.length]="paragraph"
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


def restore_plain_breaks(rendered,draft,original,missing):
    """Split only plain rendered paragraphs. All source markup stays outside the draft."""
    line_starts=[0]+[m.end() for m in re.finditer("\n",rendered)]
    wanted=sorted(missing-set(original.image_offsets))
    pieces=[]
    previous=0
    fixed=set()
    for offset,(line,column) in draft.paragraphs:
        start=line_starts[line-1]+column
        paragraph=PLAIN_PARAGRAPH.match(rendered,start)
        if not paragraph:
            continue
        text=unescape(paragraph[1])
        size=len(SPACING.sub("",text))
        cuts=wanted[bisect_right(wanted,offset):bisect_left(wanted,offset+size)]
        if not cuts:
            continue
        pieces.extend((rendered[previous:start],"<p>"))
        indices=iter(cuts)
        boundary=next(indices)
        last=0
        for count,char in enumerate(re.finditer(r"[^\s\u200b\ufeff]",text),offset):
            if count!=boundary:
                continue
            pieces.append(escape(text[last:char.start()],quote=False))
            pieces.append("</p>\n<p>" if original.breaks[count]=="paragraph" else "<br />\n")
            last=char.start()
            fixed.add(count)
            boundary=next(indices,None)
            if boundary is None:
                break
        pieces.extend((escape(text[last:],quote=False),"</p>"))
        previous=paragraph.end()
    return "".join(pieces)+rendered[previous:],fixed


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
    if left==right:
        edges={0,len(left)}
        missing=original.breaks.keys()-draft.breaks.keys()-edges
        extra=draft.breaks.keys()-original.breaks.keys()-edges
        if missing and result["structure_matches"] and original.image_offsets==draft.image_offsets:
            rendered,fixed=restore_plain_breaks(rendered,draft,original,missing)
            missing-=fixed
            result["restored_breaks"]=len(fixed)
        if missing or extra:
            result.update(structure_matches=False,missing_breaks=len(missing),extra_breaks=len(extra))
            first=min(missing|extra)
            result["break_example"]=dict(before=left[max(0,first-18):first],after=left[first:first+18],missing=first in missing)
    return rendered,result
