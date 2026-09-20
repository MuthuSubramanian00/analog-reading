# topdf

Turn an essay or article into a clean, printable PDF — no sidebars, no cookie banners, no
navigation. Made for reading long pieces on paper instead of on a screen.

Give it a link, or just copy something and run it with no arguments.

```
$ topdf https://paulgraham.com/greatwork.html
✓ How to Do Great Work
  → pdfs/How-to-Do-Great-Work.pdf
```

The PDF is set in a serif face at a comfortable measure, with page numbers and the source
link under the title. Images and tables are kept; page breaks avoid splitting them.

## Install

Requires Python 3.9+ and a Chromium-based browser (Google Chrome, Chromium, Edge or Brave)
for rendering.

```bash
git clone https://github.com/MuthuSubramanian00/analog-reading.git
cd analog-reading
./topdf --help
```

The first run creates a virtualenv in `.venv/` and installs the dependencies. To put it on
your `PATH`:

```bash
ln -s "$PWD/topdf" /usr/local/bin/topdf
```

## Usage

```bash
topdf URL [URL ...]      # fetch each link and convert it
topdf                    # use the clipboard
topdf -c                 # same, explicitly
topdf page.html          # convert a local HTML file
topdf notes.md           # or a text / Markdown file
pbpaste | topdf -        # read from stdin
```

With no arguments, the clipboard can hold any of these:

- **One or more links** — each is fetched and converted.
- **Content copied from a web page** — headings, bold text, links and images are kept.
  Useful for pages behind a login that can't be fetched by URL.
- **Plain text** — blank lines separate paragraphs, and a short first line becomes the title.

### Options

| Option | Meaning |
| --- | --- |
| `-o DIR` | Output folder (default: `pdfs/` next to the script) |
| `--open` | Open the PDF when it's done |
| `--paper A4\|Letter\|A5` | Page size (default `A4`) |
| `--font-size 12` | Body size in points (default `11.5`) |
| `--no-images` | Leave images out |
| `--raw` | Keep everything you copied; don't let the extractor trim it |
| `-t TITLE` | Override the title |

Files are named after the article title, e.g. `How-to-Do-Great-Work.pdf`.

## How it works

1. **Extraction** — [trafilatura](https://trafilatura.readthedocs.io/) pulls the main body
   out of the page and discards the furniture around it.
   [readability-lxml](https://github.com/buriy/python-readability) runs alongside it as a
   safety net: trafilatura drops trailing sections written as bare `<div>`s (which is how
   Blogger writes paragraphs, and it cost one post its entire conclusion), so whichever
   extractor recovered more of the article wins. Content you selected and copied yourself
   isn't run through the extractor at all — there's no page furniture in a selection, so
   it's kept whole.
2. **Cleanup** — embeds that can't print (a YouTube iframe prints as an error box) are
   removed, old-style layout tables are unwrapped, spacer GIFs dropped, footnote
   markers that the extractor splits off are folded back into their paragraph, and Blogspot
   thumbnails are swapped for full-size images so charts stay legible in print. A
   publication date that looks like a year-only guess is left out rather than printed wrong.
3. **Rendering** — the cleaned article is styled with print CSS and rendered to PDF by
   headless Chrome.

Tested on [Paul Graham's essays](https://paulgraham.com/articles.html) and
[Aswath Damodaran's blog](https://aswathdamodaran.blogspot.com/), which between them cover
both ends of the web's formatting history.

## Limitations

- Needs a Chromium-based browser installed; there's no pure-Python rendering fallback.
- Pages that require a login or assemble themselves with JavaScript may not download. Copy
  the article in your browser and run `topdf` with no arguments instead.
- Bold and italic runs from clipboard HTML sometimes come through as plain text.
- If a PDF ever holds less text than you copied, the script says so on stderr and suggests
  `--raw`; please open an issue with the source so the extraction can be fixed.
- Clipboard support: macOS uses `pbpaste`; Linux needs `xclip` or `wl-clipboard`. Windows is
  untested — piping via `topdf -` should still work.

## License

MIT — see [LICENSE](LICENSE).
