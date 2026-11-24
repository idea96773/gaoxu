"""Compare the identified Easy WeChat body with the rendered reprint draft."""
from collections import Counter
from copy import copy
from bisect import bisect_right, bisect_left
from html import escape, unescape
import re

from markdown_it.token import Token

from .markdown_media import MARKDOWN
from .article_html import ArticleHTML

STRUCTURE = set("h1 h2 h3 h4 h5 h6 ol ul li blockquote table tr th td sub sup pre".split())
BLOCKS = set("p div section article header footer aside figure figcaption h1 h2 h3 h4 h5 h6 li blockquote tr th td pre".split())
INLINE_STYLE = re.compile(r"(?:^|;)\s*display\s*:\s*(?:inline(?:-[\w-]+)?|contents)\s*(?:!important\s*)?(?:;|$)",re.I)
SPACING = re.compile(r"[\s\u200b\ufeff]+")
STARS = re.compile(r"(?<!\*)\*\*([^*\n]+)\*\*(?!\*)")
PLAIN_PARAGRAPH = re.compile(r"<p>([^<>]+)</p>")


class Body(ArticleHTML):
    def __init__(self,require_source):
        super().__init__()
        self.require_source=require_source
        self.fragments=[]
        self.structure=[]
        self.length=0
        self.bold=set()
        self.repairs=set()
        self.breaks={}
        self.paragraphs=[]
        self.image_offsets=[]
        self.links=[]

    def start_visible(self,tag,attrs,inside):
        if self.require_source and not inside:
            return None
        block=tag in BLOCKS and not INLINE_STYLE.search(attrs.get("style") or "")
        if tag in STRUCTURE:
            self.structure.append(tag)
        if block:
            self.breaks[self.length]="paragraph"
            if tag=="p" and not self.require_source:
                self.paragraphs.append((self.length,self.getpos()))
        elif tag=="br":
            self.breaks.setdefault(self.length,"line")
        elif tag=="img":
            self.image_offsets.append(self.length)
        elif tag=="a":
            href=(attrs.get("href") or "").strip()
            if href.startswith("//"):
                href="https:"+href
            if href.lower().startswith(("http://","https://")):
                # Keep opening-tag order and visible offsets, including image-only anchors.
                link=[self.length,self.length,len(self.image_offsets),MARKDOWN.normalizeLink(href)]
                self.links.append(link)
                return link
        return self.length,"data-source-bold" in attrs,block

    def end_visible(self,tag,context):
        if tag=="a" and isinstance(context,list):
            context[1]=self.length
            return
        start,repair,block=context
        if block:
            self.breaks[self.length]="paragraph"
        if tag in ("b","strong"):
            self.bold.add((start,self.length))
            if repair:
                self.repairs.add((start,self.length))

    def handle_data(self,data):
        if self.stack and self.stack[-1][2] and (self.stack[-1][1] or not self.require_source):
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


def compare_links(original,draft,left,right):
    aligned=left==right
    def signature(link):
        start,end,image,url=link
        if aligned:
            return (url,start,end,image if start==end else None)
        return (url,)
    before=[signature(link) for link in original.links]
    after=[signature(link) for link in draft.links]
    result=dict(source_count=len(before),draft_count=len(after),positions_compared=aligned)
    if before==after:
        return result

    def sample(link,text):
        start,end,_,url=link
        return dict(label=text[start:min(end,start+60)],position=start+1,url=url)
    def unmatched(keys,links,text,other):
        remaining=Counter(other)
        examples=[]
        count=0
        for key,link in zip(keys,links):
            if remaining[key]:
                remaining[key]-=1
            else:
                count+=1
                if len(examples)<5:
                    examples.append(sample(link,text))
        return dict(count=count,examples=examples)
    result["source_only"]=unmatched(before,original.links,left,after)
    result["draft_only"]=unmatched(after,draft.links,right,before)
    if not (result["source_only"]["count"] or result["draft_only"]["count"]):
        first=next(i for i,(a,b) in enumerate(zip(before,after)) if a!=b)
        result["order"]=dict(position=first+1,source=sample(original.links[first],left),draft=sample(draft.links[first],right))
    return result


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
    result["web_links"]=compare_links(original,draft,left,right)
    if left!=right:
        offset=next((i for i,(a,b) in enumerate(zip(left,right)) if a!=b),min(len(left),len(right)))
        result["difference"]=dict(position=offset+1,context=left[max(0,offset-18):offset],
                                  source=left[offset:offset+48],draft=right[offset:offset+48])
    if not result["structure_matches"]:
        before,after=Counter(original.structure),Counter(draft.structure)
        result["structure_counts"]={tag:[before[tag],after[tag]] for tag in sorted(before.keys()|after.keys())
                                    if before[tag]!=after[tag]}
    if left==right:
        # Reprint admission independently checks the ordered local media references.
        # Extra Markdown illustrations make the occurrence mapping ambiguous.
        if len(original.image_offsets)==len(draft.image_offsets):
            moved=[i for i,(before,after) in enumerate(zip(original.image_offsets,draft.image_offsets))
                   if before!=after]
            if moved:
                first=moved[0]
                def context(at):
                    return {"before":left[max(0,at-18):at],"after":left[at:at+18]}
                result["image_placement"]=dict(count=len(moved),position=first+1,
                    source=context(original.image_offsets[first]),draft=context(draft.image_offsets[first]))
                result["structure_matches"]=False
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
