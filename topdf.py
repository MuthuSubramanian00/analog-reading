#!/usr/bin/env python3
"""
topdf — turn an article / essay into a clean, printable PDF.

Usage:
  topdf.py URL [URL ...]        fetch each link and convert it
  topdf.py                      use the clipboard (a URL, copied web content, or plain text)
  topdf.py -c                   same as above, explicitly
  topdf.py page.html | notes.txt  convert a local file
  pbpaste | topdf.py -          read text/HTML from stdin

Options: --title, --paper A4|Letter, --font-size 11.5, --no-images, -o DIR, --open
"""

import argparse
import datetime as dt
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import trafilatura
from lxml import html as lhtml
from readability import Document

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = SCRIPT_DIR / "pdfs"
URL_RE = re.compile(r"^https?://\S+$", re.I)

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "google-chrome", "chromium", "chromium-browser",
]


# ---------------------------------------------------------------- input


def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def read_clipboard():
    """Return (html_or_None, text) from the clipboard. Rich HTML keeps headings and images."""
    if sys.platform == "darwin":
        text = _run(["pbpaste"])
        rich = None
        # Content copied from a browser also carries an HTML flavour.
        out = _run(["osascript", "-e", "the clipboard as «class HTML»"]).strip()
        m = re.match(r"«data HTML([0-9A-Fa-f]*)»", out)
        if m and m.group(1):
            try:
                rich = bytes.fromhex(m.group(1)).decode("utf-8", errors="replace")
            except ValueError:
                rich = None
        return rich, text

    if shutil.which("wl-paste"):  # Wayland
        return (_run(["wl-paste", "-t", "text/html"]) or None), _run(["wl-paste", "-n"])
    if shutil.which("xclip"):  # X11
        return (_run(["xclip", "-selection", "clipboard", "-t", "text/html", "-o"]) or None), \
            _run(["xclip", "-selection", "clipboard", "-o"])
    sys.exit("No clipboard tool found. Install xclip or wl-clipboard, "
             "or pipe the text in: pbpaste | topdf -")


def text_to_html(text):
    """Plain text -> paragraphs. Blank lines separate paragraphs; single newlines are kept."""
    text = text.replace("\r\n", "\n").strip()
    blocks = re.split(r"\n\s*\n", text)
    if len(blocks) == 1 and text.count("\n") > 3:
        blocks = text.split("\n")  # text with one paragraph per line
    out = []
    for b in blocks:
        b = b.strip()
        if not b:
            continue
        esc = html.escape(b).replace("\n", "<br>")
        if re.match(r"^#{1,4}\s", b):  # light markdown heading support
            level = min(len(b) - len(b.lstrip("#")) + 1, 4)
            out.append(f"<h{level}>{html.escape(b.lstrip('#').strip())}</h{level}>")
        else:
            out.append(f"<p>{esc}</p>")
    return "\n".join(out)


# ---------------------------------------------------------------- extraction


def norm_len(s):
    return len(" ".join(s.split()))


def text_len(fragment):
    try:
        return norm_len(lhtml.fromstring(fragment).text_content())
    except (ValueError, lhtml.etree.ParserError):
        return 0


CONTAINER = re.compile(r"(post-body|entry-content|article-body|articlebody|post-content|story-body)", re.I)
FURNITURE = re.compile(
    r"(comment|share|social|related|subscribe|newsletter|footer|nav|sidebar|widget|"
    r"byline|author|label|tag|pager|jump-link|post-meta|entry-meta|utility|permalink|"
    r"breadcrumb|promo|advert)", re.I)


def recover_tail(raw_html, body_html, limit=2500):
    """Put back trailing blocks the extractors cut from the end of the article.

    Both trafilatura and readability treat a link-dense block as page furniture, which
    loses things like a closing list of data links. Walk forward from where the extracted
    text ends in the original page and take what follows, stopping at the first thing that
    looks like site furniture.
    """
    try:
        doc = lhtml.fromstring(raw_html)
        kept = " ".join(lhtml.fromstring(body_html).text_content().split())
    except (ValueError, lhtml.etree.ParserError):
        return body_html
    # Anchor on the last real paragraph of the extraction: a fingerprint taken from the
    # very end can straddle two elements and then match nothing in the original page.
    fingerprint = ""
    for el in reversed(list(lhtml.fromstring(body_html).iter())):
        if not isinstance(el.tag, str):
            continue
        txt = " ".join((el.text_content() or "").split())
        if len(txt) >= 60:
            fingerprint = txt[-60:]
            break
    if not fingerprint:
        return body_html

    # The deepest element whose text ends the extraction is where we resume.
    anchor, best = None, None
    for el in doc.iter():
        if not isinstance(el.tag, str) or el.tag in ("script", "style"):
            continue  # comments and processing instructions have no text_content()
        txt = " ".join((el.text_content() or "").split())
        if fingerprint in txt and (best is None or len(txt) < best):
            anchor, best = el, len(txt)
    if anchor is None:
        return body_html

    # Never climb out of the article itself, or the walk can re-append the whole post.
    container = None
    for anc in anchor.iterancestors():
        ident = f"{anc.get('class') or ''} {anc.get('id') or ''}"
        if anc.tag == "article" or CONTAINER.search(ident):
            container = anc
            break

    recovered, total = [], 0
    node = anchor
    while node is not None and node is not container and node.tag != "body" and total < limit:
        for sib in node.itersiblings():
            if not isinstance(sib.tag, str):
                continue  # comments between elements
            ident = f"{sib.get('class') or ''} {sib.get('id') or ''}"
            if sib.tag in ("nav", "footer", "aside", "form", "script", "style") or FURNITURE.search(ident):
                return finish(body_html, recovered)
            txt = " ".join((sib.text_content() or "").split())
            if not txt and not sib.xpath(".//img"):
                continue
            if txt and (txt in kept or txt[:120] in kept):  # already extracted
                continue
            if len(txt) > limit - total:
                break  # too big to be a leftover tail (an index or a sidebar)
            recovered.append(sib)
            total += len(txt)
            if total >= limit:
                break
        node = node.getparent()
    return finish(body_html, recovered)


def finish(body_html, recovered):
    """Append the recovered elements inside the document, not after its closing tag."""
    if not recovered:
        return body_html
    doc = lhtml.fromstring(body_html)
    target = doc.find(".//body")
    if target is None:
        target = doc
    for el in recovered:
        el.tail = None
        target.append(el)
    return lhtml.tostring(doc, encoding="unicode")


def extract(raw_html, url=None):
    """Return dict(title, author, date, site, body_html) or None if nothing usable."""
    body = trafilatura.extract(
        raw_html, url=url, output_format="html",
        include_formatting=True, include_links=True, include_images=True,
        include_tables=True, favor_recall=True,
    )
    # trafilatura drops trailing sections that are written as bare <div>s instead of
    # <p>s — Blogger does this, which cost one post its whole conclusion. Readability
    # keeps them, so run both and take whichever recovered more of the article.
    try:
        alt = Document(raw_html).summary()
    except Exception:
        alt = None
    if alt and text_len(alt) > 1.1 * text_len(body or ""):
        doc = lhtml.fromstring(alt)
        if url:
            doc.make_links_absolute(url)  # readability leaves relative image/link URLs
        body = lhtml.tostring(doc, encoding="unicode")
    if not body:
        return None
    body = recover_tail(raw_html, body)
    if url:
        try:
            d = lhtml.fromstring(body)
            d.make_links_absolute(url)
            body = lhtml.tostring(d, encoding="unicode")
        except (ValueError, lhtml.etree.ParserError):
            pass
    meta = trafilatura.extract_metadata(raw_html, default_url=url)
    title = (meta.title if meta else None) or ""
    author = (meta.author if meta else None) or ""
    date = (meta.date if meta else None) or ""
    if date.endswith("-01-01"):  # usually a year-only guess; don't print a fake date
        date = ""
    site = (meta.sitename if meta else None) or (urlparse(url).netloc if url else "")
    return dict(title=title, author=author, date=date, site=site,
                body_html=clean_body(body, title))


def clean_body(body_html, title):
    doc = lhtml.fromstring(body_html)
    body = doc.find(".//body")
    if body is None:
        body = doc

    # Embeds can't print: a YouTube iframe renders as an error box on paper.
    for el in body.xpath(".//iframe|.//video|.//audio|.//embed|.//object|.//script|.//style"):
        el.drop_tree()

    # Drop tables that hold no text (spacer / decoration tables, e.g. paulgraham.com nav).
    for t in body.xpath(".//table"):
        if not t.text_content().strip():
            t.drop_tree()
    # Unwrap layout tables: tables used to position prose rather than hold data.
    for t in body.xpath(".//table[.//p or .//h1 or .//h2 or .//h3]"):
        for cell in t.xpath(".//td|.//th"):
            cell.tag = "div"
        for el in t.xpath(".//tr|.//tbody|.//thead"):
            el.drop_tag()
        t.tag = "div"

    for img in body.xpath(".//img"):
        src = img.get("src", "")
        alt = (img.get("alt") or "").strip()
        # spacer gifs and image-rendered titles (the title is printed separately)
        if re.search(r"(1x1|spacer|trans_)", src, re.I) or (title and alt.lower() == title.lower()):
            img.drop_tree()
            continue
        # Blogger/Blogspot serves shrunken thumbnails (/w400-h300/); ask for full size.
        img.set("src", re.sub(r"/(w\d+-h\d+|s\d+)(-[a-z0-9-]+)?/", "/s1600/", src)
                if "googleusercontent" in src or "bp.blogspot" in src else src)

    # The extractor sometimes closes a <p> early, leaving inline bits (e.g. footnote
    # links like "[<a>1</a>]") stranded between paragraphs. Fold them back in.
    inline = {"a", "span", "b", "i", "em", "strong", "sup", "sub", "code", "u", "small"}
    for container in [body] + body.xpath(".//div|.//td"):
        prev_p = None
        for child in list(container):
            if child.tag == "p":
                if child.tail and child.tail.strip():
                    child.text = child.text or ""
                    last = child[-1] if len(child) else None
                    if last is not None:
                        last.tail = (last.tail or "") + child.tail
                    else:
                        child.text += child.tail
                    child.tail = None
                prev_p = child
            elif child.tag in inline and prev_p is not None:
                prev_p.append(child)  # moves the element along with its tail text
            else:
                prev_p = None

    # Remove a leading heading that just repeats the title.
    first = next(iter(body), None)
    if first is not None and first.tag in ("h1", "h2") and title and \
            first.text_content().strip().lower() == title.strip().lower():
        first.drop_tree()

    return "".join(lhtml.tostring(c, encoding="unicode") for c in body) \
        if body.tag == "body" else lhtml.tostring(body, encoding="unicode")


def from_url(url):
    raw = trafilatura.fetch_url(url)
    if not raw:
        sys.exit(f"Could not download {url}")
    art = extract(raw, url)
    if not art:
        sys.exit(f"Could not find the main content of {url}")
    art["url"] = url
    return art


def looks_like_full_page(rich):
    """True for a whole web page (which has furniture to strip), false for a copied selection."""
    low = rich.lower()
    return ("<html" in low or "<head" in low or "<nav" in low or "<footer" in low
            or low.count("<script") >= 2)


def sanitize_fragment(rich):
    """Keep everything that was copied, minus what can't be printed."""
    doc = lhtml.fromstring(rich)
    for bad in doc.xpath("//script|//style|//noscript|//iframe|//form|//button|//input"):
        bad.drop_tree()
    return clean_body(lhtml.tostring(doc, encoding="unicode"), "")


def from_text_or_html(rich, text, title=None, raw=False):
    """Build an article from pasted content: rich HTML if usable, else plain text."""
    art = None
    plain_len = norm_len(text)
    if rich:
        if raw or not looks_like_full_page(rich):
            # A selection you made by hand: nothing here is page furniture, so keep all of it.
            art = dict(title="", author="", date="", site="", body_html=sanitize_fragment(rich))
        else:
            art = extract(rich)  # a whole page: strip navigation, sidebars, footers
        if art and plain_len and norm_len(lhtml.fromstring(art["body_html"]).text_content()) < 0.5 * plain_len:
            art = None  # extraction went badly wrong; the plain text is safer
        if art is None and not text.strip():
            art = dict(title="", author="", date="", site="",
                       body_html=sanitize_fragment(rich))
    if art is None:
        if not text.strip():
            sys.exit("Nothing to convert: the input is empty.")
        body = text.strip()
        guessed = ""
        first_line, _, rest = body.partition("\n")
        if not title and len(first_line) < 120 and rest.strip():
            guessed, body = first_line.strip().lstrip("# "), rest
        art = dict(title=guessed, author="", date="", site="", body_html=text_to_html(body))
    art["url"] = ""
    if title:
        art["title"] = title
    if not art["title"]:
        art["title"] = "Untitled " + dt.datetime.now().strftime("%Y-%m-%d %H%M")
    # Say so when the PDF holds less than what was copied, instead of losing it quietly.
    kept = norm_len(lhtml.fromstring(art["body_html"]).text_content()) + norm_len(art["title"])
    if plain_len and kept < 0.95 * plain_len:
        print(f"! kept {kept:,} of {plain_len:,} characters of the copied text "
              f"({kept / plain_len:.0%}); re-run with --raw to keep all of it", file=sys.stderr)
    return art


# ---------------------------------------------------------------- rendering


CSS = """
@page {
  size: %(paper)s;
  margin: 20mm 22mm 20mm 22mm;
  @bottom-center { content: counter(page) " / " counter(pages); font: 8.5pt %(sans)s; color: #777; }
}
html { -webkit-print-color-adjust: exact; }
body {
  font-family: "Charter", "Iowan Old Style", "Georgia", serif;
  font-size: %(size)spt; line-height: 1.5; color: #111; margin: 0;
  hyphens: auto; text-rendering: optimizeLegibility;
}
header { margin-bottom: 1.6em; padding-bottom: .9em; border-bottom: 0.6pt solid #bbb; }
header h1 { font-size: 1.9em; line-height: 1.15; margin: 0 0 .35em; font-weight: 700; }
header .meta { font: 8.5pt %(sans)s; color: #555; line-height: 1.4; }
header .meta a { color: #555; text-decoration: none; word-break: break-all; }
p { margin: 0 0 .8em; text-align: justify; orphans: 3; widows: 3; }
h1, h2, h3, h4 { line-height: 1.25; margin: 1.3em 0 .5em; break-after: avoid; }
h1 { font-size: 1.45em; } h2 { font-size: 1.3em; } h3 { font-size: 1.12em; } h4 { font-size: 1em; }
a { color: inherit; text-decoration: underline; text-decoration-color: #aaa; }
img { max-width: 100%%; max-height: 190mm; height: auto; display: block; margin: .8em auto;
      break-inside: avoid; }
figure { margin: 1em 0; break-inside: avoid; }
blockquote { margin: 1em 0; padding-left: 1em; border-left: 2pt solid #ccc; color: #333; }
pre, code { font-family: "SF Mono", Menlo, monospace; font-size: .85em; }
pre { white-space: pre-wrap; background: #f5f5f5; padding: .6em .8em; break-inside: avoid; }
table { border-collapse: collapse; margin: 1em 0; font-size: .85em; break-inside: avoid; }
td, th { border: 0.5pt solid #ccc; padding: .25em .5em; vertical-align: top; }
ul, ol { padding-left: 1.4em; margin: 0 0 .8em; } li { margin-bottom: .25em; }
hr { border: 0; border-top: 0.6pt solid #ccc; margin: 1.5em 0; }
"""
SANS = '-apple-system, "Helvetica Neue", Arial, sans-serif'


def build_html(art, paper, size, images):
    meta_bits = [b for b in (art.get("author"), art.get("site"), art.get("date")) if b]
    meta = " · ".join(html.escape(b) for b in meta_bits)
    if art.get("url"):
        u = html.escape(art["url"])
        meta += (f"<br>" if meta else "") + f'<a href="{u}">{u}</a>'
    body = art["body_html"]
    if not images:
        body = re.sub(r"<img\b[^>]*>", "", body)
    css = CSS % dict(paper=paper, size=size, sans=SANS)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{html.escape(art['title'])}</title><style>{css}</style></head><body>
<header><h1>{html.escape(art['title'])}</h1><div class="meta">{meta}</div></header>
<article>{body}</article></body></html>"""


def find_chrome():
    for c in CHROME_CANDIDATES:
        p = c if os.path.isabs(c) else shutil.which(c)
        if p and os.path.exists(p):
            return p
    return None


def render_pdf(doc_html, out_path):
    chrome = find_chrome()
    if not chrome:
        sys.exit("Google Chrome (or Chromium/Edge/Brave) is required to render the PDF.")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "article.html"
        src.write_text(doc_html, encoding="utf-8")
        tmp_pdf = Path(tmp) / "out.pdf"
        cmd = [chrome, "--headless", "--disable-gpu", "--no-first-run",
               "--no-default-browser-check", "--disable-background-networking",
               "--disable-component-update", "--disable-sync",
               f"--user-data-dir={tmp}/profile", "--no-pdf-header-footer",
               "--run-all-compositor-stages-before-draw",
               f"--print-to-pdf={tmp_pdf}", src.as_uri()]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Chrome sometimes lingers after writing the PDF, so wait for the file to
        # appear and stop growing rather than waiting for the process to exit.
        deadline, last_size, stable = time.time() + 180, -1, 0
        try:
            while time.time() < deadline:
                if tmp_pdf.exists():
                    size = tmp_pdf.stat().st_size
                    stable = stable + 1 if size == last_size and size > 0 else 0
                    last_size = size
                    if stable >= 3:
                        break
                elif proc.poll() is not None:
                    break
                time.sleep(0.5)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if not tmp_pdf.exists() or tmp_pdf.stat().st_size == 0:
            sys.exit("PDF rendering failed (Chrome produced no output).")
        shutil.move(str(tmp_pdf), out_path)


def slugify(s, maxlen=80):
    s = re.sub(r"[^\w\s-]", "", s).strip()
    s = re.sub(r"[\s_-]+", "-", s)
    return (s[:maxlen].rstrip("-") or "article")


# ---------------------------------------------------------------- main


def convert(art, args):
    args.out = args.out.expanduser().resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out / f"{slugify(art['title'])}.pdf"
    render_pdf(build_html(art, args.paper, args.font_size, not args.no_images), out)
    print(f"✓ {art['title']}\n  → {out}")
    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(out)])
    return out


def main():
    ap = argparse.ArgumentParser(description="Turn articles/essays into printable PDFs.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    ap.add_argument("inputs", nargs="*", help="URL(s), a local .html/.txt/.md file, or '-' for stdin. "
                                              "Omit to use the clipboard.")
    ap.add_argument("-c", "--clipboard", action="store_true", help="read from the clipboard")
    ap.add_argument("-t", "--title", help="override the title")
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT})")
    ap.add_argument("--paper", default="A4", choices=["A4", "Letter", "A5"], help="page size (default A4)")
    ap.add_argument("--font-size", type=float, default=11.5, help="body font size in pt (default 11.5)")
    ap.add_argument("--no-images", action="store_true", help="leave images out")
    ap.add_argument("--raw", action="store_true",
                    help="keep everything that was copied; don't let the extractor trim it")
    ap.add_argument("--open", action="store_true", help="open the PDF when done")
    args = ap.parse_args()

    if args.clipboard or not args.inputs:
        rich, text = read_clipboard()
        stripped = text.strip()
        urls = stripped.split()
        if stripped and all(URL_RE.match(u) for u in urls):
            for u in urls:  # clipboard holds one or more links
                convert(from_url(u), args)
        else:
            convert(from_text_or_html(rich, text, args.title, args.raw), args)
        return

    for item in args.inputs:
        if URL_RE.match(item):
            art = from_url(item)
            if args.title:
                art["title"] = args.title
        elif item == "-":
            data = sys.stdin.read()
            is_html = bool(re.search(r"<(p|div|html|body|article)\b", data, re.I))
            art = from_text_or_html(data if is_html else None, "" if is_html else data,
                                    args.title, args.raw)
        elif Path(item).is_file():
            data = Path(item).read_text(encoding="utf-8", errors="replace")
            if Path(item).suffix.lower() in (".html", ".htm"):
                art = from_text_or_html(data, "", args.title or None, args.raw)
            else:
                art = from_text_or_html(None, data, args.title or Path(item).stem, args.raw)
        else:
            sys.exit(f"Not a URL or file: {item}")
        convert(art, args)


if __name__ == "__main__":
    main()
