"""Shared Markdown parsing and native Tk/Pillow rendering for comments."""
from dataclasses import dataclass, replace
from functools import lru_cache
import re
import webbrowser

from markdown_it import MarkdownIt
from PIL import ImageFont


@dataclass(frozen=True)
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    strike: bool = False
    link: str = ""


@dataclass(frozen=True)
class Block:
    spans: tuple = ()
    heading: int = 0
    indent: int = 0
    prefix: str = ""
    quote: bool = False
    code: bool = False
    rule: bool = False
    preserve_whitespace: bool = False


def inline_spans(tokens):
    styles = {"strong": 0, "em": 0, "s": 0}
    link = ""
    spans = []
    for token in tokens or ():
        name, _, action = token.type.rpartition("_")
        if name in styles and action in ("open", "close"):
            styles[name] += 1 if action == "open" else -1
        elif token.type == "link_open":
            link = token.attrGet("href") or ""
        elif token.type == "link_close":
            link = ""
        else:
            if token.type in ("softbreak", "hardbreak"):
                text = "\n"
            elif token.type == "image":
                # Describe embedded images without fetching remote/local resources.
                text = "".join(span.text for span in inline_spans(token.children)) or token.content
            else:
                text = token.content
            if text:
                spans.append(Span(text, styles["strong"] > 0, styles["em"] > 0,
                                  token.type == "code_inline", styles["s"] > 0, link))
    return tuple(spans)


def caption_spans(spans, raw_lines, preserve_indent):
    """Restore edge spaces removed by CommonMark, retaining inline styling."""
    groups = [[]]
    for span in spans:
        parts = span.text.split("\n")
        for index, text in enumerate(parts):
            if index:
                groups.append([])
            if text:
                groups[-1].append(replace(span, text=text))
    result = []
    for index, group in enumerate(groups):
        if index:
            result.append(Span("\n"))
        if index < len(raw_lines):
            raw = raw_lines[index]
            visible = "".join(span.text for span in group)
            prefix = re.match(r"[ \t]*", raw).group() if preserve_indent else ""
            suffix = re.search(r"[ \t]*$", raw).group()
            current_prefix = re.match(r"[ \t]*", visible).group()
            current_suffix = re.search(r"[ \t]*$", visible).group()
            if len(prefix) > len(current_prefix):
                group.insert(0, Span(prefix[:len(prefix) - len(current_prefix)]))
            if len(suffix) > len(current_suffix):
                group.append(Span(suffix[len(current_suffix):]))
        result.extend(group)
    return tuple(result)


@lru_cache(maxsize=128)
def parse(source, preserve_whitespace=False):
    """CommonMark plus strikethrough; retain soft line breaks used in old comments."""
    parser = MarkdownIt("commonmark", {"html": False}).enable("strikethrough")
    blocks, lists, items = [], [], []
    heading = quote = 0
    raw_lines = str(source).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cursor = 0

    def block(spans=(), **options):
        prefix = ""
        if items and items[-1] is not None:
            prefix, items[-1] = items[-1], None
        return Block(tuple(spans), heading=heading, indent=max(0, len(lists) - 1) + int(bool(lists) and not prefix),
                     prefix=prefix, quote=bool(quote), preserve_whitespace=preserve_whitespace, **options)

    def append_block(value, token):
        nonlocal cursor
        if preserve_whitespace and token.map:
            for line in raw_lines[cursor:token.map[0]]:
                if not line.strip():
                    blocks.append(Block((Span(line),), preserve_whitespace=True))
            cursor = max(cursor, token.map[1])
        blocks.append(value)

    for token in parser.parse(str(source)):
        kind = token.type
        if kind in ("bullet_list_open", "ordered_list_open"):
            lists.append(None if kind == "bullet_list_open" else int(token.attrGet("start") or 1))
        elif kind in ("bullet_list_close", "ordered_list_close"):
            lists.pop()
        elif kind == "list_item_open":
            items.append("•" if lists[-1] is None else f"{lists[-1]}.")
            if lists[-1] is not None:
                lists[-1] += 1
        elif kind == "list_item_close":
            items.pop()
        elif kind == "blockquote_open":
            quote += 1
        elif kind == "blockquote_close":
            quote -= 1
        elif kind == "heading_open":
            heading = int(token.tag[1:])
        elif kind == "heading_close":
            heading = 0
        elif kind == "inline":
            spans = inline_spans(token.children)
            if preserve_whitespace and token.map:
                spans = caption_spans(spans, raw_lines[token.map[0]:token.map[1]],
                                      not (heading or lists or quote))
            append_block(block(spans), token)
        elif kind in ("fence", "code_block"):
            spans = (Span(token.content.removesuffix("\n"), code=True),)
            if preserve_whitespace and kind == "code_block" and token.map:
                spans = caption_spans(spans, raw_lines[token.map[0]:token.map[1]], True)
            append_block(block(spans, code=True), token)
        elif kind == "hr":
            append_block(block(rule=True), token)
    if preserve_whitespace and source:
        for line in raw_lines[cursor:]:
            if not line.strip():
                blocks.append(Block((Span(line),), preserve_whitespace=True))
    return tuple(blocks)


def relative_size(heading):
    return (1, 1.55, 1.35, 1.2, 1.1, 1, 1)[heading]


@lru_cache(maxsize=256)
def font(size, bold=False, italic=False, code=False):
    family = "DejaVuSansMono" if code else "DejaVuSans"
    suffix = "-BoldOblique" if bold and italic else "-Bold" if bold else "-Oblique" if italic else ""
    try:
        return ImageFont.truetype(f"{family}{suffix}.ttf", size)
    except OSError as error:
        raise ValueError("Markdown fonts unavailable. Install fonts-dejavu-core.") from error


@dataclass
class Run:
    span: Span
    font: object

    @property
    def width(self):
        return self.font.getlength(self.span.text)


@dataclass
class Line:
    runs: list
    height: int
    ascent: int
    indent: float
    prefix: str
    prefix_font: object
    gap: int
    block: Block

    @property
    def text(self):
        return "".join(run.span.text for run in self.runs)


@dataclass
class Layout:
    lines: list

    @property
    def height(self):
        return sum(line.height + line.gap for line in self.lines)

    @property
    def plain_lines(self):
        return [line.text for line in self.lines]


def tokens(spans):
    """Keep a word together even when bold/italic styling changes within it."""
    previous, parts = None, []
    for span in spans:
        for text in re.findall(r"\n|[^\S\n]+|[^\s]+", span.text.expandtabs(4)):
            kind = "newline" if text == "\n" else "space" if text.isspace() else "word"
            if kind != previous or kind == "newline":
                if parts:
                    yield previous, parts
                previous, parts = kind, []
            parts.append(replace(span, text=text))
    if parts:
        yield previous, parts


def layout(blocks, width, size):
    lines = []
    for number, block in enumerate(blocks):
        base = font(max(1, round(size * relative_size(block.heading))), bool(block.heading), code=block.code)
        prefix_width = base.getlength(block.prefix + " ") if block.prefix else 0
        indent = block.indent * size * 1.2 + (size * .8 if block.quote else 0) + prefix_width
        available = width - indent
        if available <= 0:
            raise ValueError("Poster text area is too narrow.")
        runs, pending = [], []
        first = True

        def emit():
            nonlocal runs, first
            fonts = [run.font for run in runs] or [base]
            ascent = max(item.getmetrics()[0] for item in fonts)
            descent = max(item.getmetrics()[1] for item in fonts)
            lines.append(Line(runs, ascent + descent, ascent, indent,
                              block.prefix if first else "", base,
                              max(1, round(size * .35)) if first and number and not block.preserve_whitespace else 0, block))
            runs, first = [], False

        def append(parts):
            for part in parts:
                chosen = font(base.size, part.bold or bool(block.heading), part.italic, part.code or block.code)
                if runs and runs[-1].font is chosen and replace(runs[-1].span, text=part.text) == part:
                    runs[-1].span = replace(part, text=runs[-1].span.text + part.text)
                else:
                    runs.append(Run(part, chosen))

        def measure(parts):
            return sum(font(base.size, part.bold or bool(block.heading), part.italic,
                            part.code or block.code).getlength(part.text) for part in parts)

        def append_whitespace(parts):
            for part in parts:
                for character in part.text:
                    piece = replace(part, text=character)
                    if sum(run.width for run in runs) + measure([piece]) > available:
                        if not runs or measure([piece]) > available:
                            raise ValueError("Poster text area is too narrow.")
                        emit()
                    append([piece])

        for kind, parts in tokens(block.spans):
            if kind == "newline":
                if block.preserve_whitespace:
                    append_whitespace(pending)
                elif block.code:
                    append(pending)
                emit()
                pending = []
            elif kind == "space":
                pending = parts if block.code or block.preserve_whitespace else [replace(parts[0], text=" ")]
            else:
                spacing = pending if runs or block.code or block.preserve_whitespace else []
                if sum(run.width for run in runs) + measure(spacing + parts) > available and runs:
                    emit()
                    if not block.preserve_whitespace:
                        spacing = []
                if measure(spacing + parts) <= available:
                    append(spacing + parts)
                else:
                    # Long URLs/code words wrap at characters instead of being clipped.
                    for part in spacing + parts:
                        for character in part.text:
                            piece = replace(part, text=character)
                            if sum(run.width for run in runs) + measure([piece]) > available:
                                if not runs:
                                    raise ValueError("Poster text area is too narrow.")
                                emit()
                            if measure([piece]) > available:
                                raise ValueError("Poster text area is too narrow.")
                            append([piece])
                pending = []
        if block.preserve_whitespace:
            append_whitespace(pending)
        if runs or first:
            emit()
    return Layout(lines)


def fit(texts, width, max_height, preferred, minimum, *, preserve_whitespace=False):
    documents = [parse(text, preserve_whitespace=preserve_whitespace) for text in texts]
    for size in range(max(1, preferred), max(1, minimum) - 1, -1):
        try:
            layouts = [layout(blocks, width, size) for blocks in documents]
        except ValueError:
            continue
        height = max((item.height for item in layouts), default=0)
        if height <= max_height:
            return layouts, height
    raise ValueError("Poster text does not fit. Increase POSTER-SIZE/LIMIT, reduce the grid or shorten comments.")


def draw_poster(draw, document, left, top, width):
    for line in document.lines:
        top += line.gap
        block = line.block
        if block.rule:
            draw.line((left, top + line.height / 2, left + width, top + line.height / 2), fill="#64748b")
        else:
            used = sum(run.width for run in line.runs)
            aligned_left = block.prefix or block.indent or block.quote or block.code
            x = left + line.indent if aligned_left else left + (width - used) / 2
            baseline = top + line.ascent
            if block.code:
                draw.rectangle((left, top, left + width, top + line.height), fill="#eef2f6")
            if block.quote:
                draw.line((left + line.indent - line.prefix_font.size * .5, top,
                           left + line.indent - line.prefix_font.size * .5, top + line.height), fill="#94a3b8", width=2)
            if line.prefix:
                draw.text((x - line.prefix_font.getlength(line.prefix + " "), baseline), line.prefix,
                          font=line.prefix_font, fill="#202020", anchor="ls")
            for run in line.runs:
                color = "#2563eb" if run.span.link else "#202020"
                if run.span.code:
                    draw.rectangle((x, top, x + run.width, top + line.height), fill="#eef2f6")
                draw.text((x, baseline), run.span.text, font=run.font, fill=color, anchor="ls")
                if run.span.strike or run.span.link:
                    y = baseline - run.font.size * .3 if run.span.strike else baseline + 1
                    draw.line((x, y, x + run.width, y), fill=color)
                x += run.width
        top += line.height


def open_preview_link(event):
    for tag in event.widget.tag_names(f"@{event.x},{event.y}"):
        url = event.widget.markdown_links.get(tag)
        if url:
            webbrowser.open(url)
            return "break"


def render_preview(widget, source):
    """Render native Text tags; never interpret HTML or fetch embedded images."""
    scroll = widget.yview()[0]
    if not hasattr(widget, "markdown_links"):
        widget.bind("<Button-1>", open_preview_link, add="+")
    widget.markdown_links = {}
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    for tag in widget.tag_names():
        if tag.startswith("md_"):
            widget.tag_delete(tag)
    for index, block in enumerate(parse(source)):
        tag = f"md_block_{index}"
        margin = block.indent * 20 + (16 if block.quote else 0)
        widget.tag_configure(tag, lmargin1=margin, lmargin2=margin + (22 if block.prefix else 0),
                             spacing1=5 if index else 0, spacing3=3,
                             background="#eef2f6" if block.code else "#ffffff")
        if block.prefix:
            widget.insert("end", block.prefix + " ", (tag,))
        if block.rule:
            widget.insert("end", "────────────────────────", (tag,))
        for span_index, span in enumerate(block.spans):
            style = f"md_span_{index}_{span_index}"
            face = "DejaVu Sans Mono" if span.code or block.code else "DejaVu Sans"
            weight = "bold" if span.bold or block.heading else "normal"
            slant = "italic" if span.italic else "roman"
            widget.tag_configure(style, font=(face, round(11 * relative_size(block.heading)), weight, slant),
                                 foreground="#2563eb" if span.link else "#475569" if block.quote else "#202020",
                                 background="#eef2f6" if span.code or block.code else "#ffffff",
                                 underline=bool(span.link), overstrike=span.strike)
            if span.link.startswith(("https://", "http://", "mailto:")):
                widget.markdown_links[style] = span.link
            widget.insert("end", span.text, (tag, style))
        widget.insert("end", "\n", (tag,))
    widget.configure(state="disabled")
    widget.yview_moveto(scroll)
