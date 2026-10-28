"""Extract transcript body text without executing HTML or loading its resources."""

from html.parser import HTMLParser
import re


IGNORED = frozenset({"head", "script", "style", "template"})
BLOCKS = frozenset({
    "address", "article", "aside", "blockquote", "dd", "details", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary",
    "table", "textarea", "tr", "ul",
})


class _TranscriptText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = []
        self.preformatted = 0

    def line_break(self):
        while self.parts and not self.parts[-1].rstrip(" \t"):
            self.parts.pop()
        if self.parts:
            self.parts[-1] = self.parts[-1].rstrip(" \t")
            if not self.parts[-1].endswith("\n"):
                self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in IGNORED:
            self.ignored.append(tag)
            return
        if self.ignored:
            return
        if tag in BLOCKS:
            self.line_break()
        if tag in {"pre", "textarea"}:
            self.preformatted += 1
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag in {"pre", "textarea"}:
            self.preformatted = max(0, self.preformatted - 1)
        if tag in BLOCKS:
            self.line_break()
        if tag in {"td", "th"}:
            self.parts.append(" ")

    def handle_data(self, data):
        if self.ignored:
            return
        if not self.preformatted:
            data = re.sub(r"\s+", " ", data)
            if not self.parts or self.parts[-1].endswith("\n"):
                data = data.lstrip()
        if data:
            self.parts.append(data)


def visible_html_text(content: str) -> str:
    parser = _TranscriptText()
    parser.feed(content)
    parser.close()
    return "".join(parser.parts).strip()
