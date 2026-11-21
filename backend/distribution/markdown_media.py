"""Markdown and local media handling shared by import, review and delivery."""
import html
import posixpath
import re
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt


MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable("table")


def inline_tokens(blocks):
    for block in blocks:
        yield from block.children or ()


def local_path(ref):
    try:
        parsed=urlsplit(ref)
    except ValueError:
        return None
    path=unquote(parsed.path).replace("\\","/")
    if parsed.scheme or parsed.netloc or not path or path.startswith("/") or ".." in path.split("/"):
        return None
    return posixpath.normpath(path)


def replace_media_urls(markup,replacements):
    """Replace image and anchor targets in renderer output, escaping URLs once."""
    def replace(match):
        url=replacements.get(match[2])
        return match[0] if url is None else match[1]+html.escape(url,quote=True)+'"'
    return re.sub(r'(<(?:img|a)\b[^>]*?\s(?:src|href)=")([^"]*)"',replace,markup)
