"""利用者向けドキュメント（docs/user_guide/<言語>/*.md）からPDFを作る。

    python scripts/user_guide/build_pdf.py              # → output/user_guide_ja.pdf
    python scripts/user_guide/build_pdf.py --html       # 確認用にHTMLも残す

章ファイルはファイル名の順（00_…, 01_…）に1冊へまとめ、表紙と目次を付けて、
Chromium の印刷機能でA4のPDFにする。画像は capture_screenshots.py が撮った
docs/user_guide/images/<言語>/ のものを使う（原稿には `images/名前.png` とだけ書く）。

原稿の書き方（Markdown の拡張）:

- 「期間であって工数ではない」の囲み（docs/user_guide/outline.md 執筆方針1）
    !!! kikan "見出し"          … 0章の大きな囲み
    !!! kikan-note "見出し"     … 各章で念押しする小さな囲み
  （本文は4字下げで続ける。Python-Markdown の admonition 拡張）
- 表・脚注・見出しのアンカーが使える。図はHTML/SVGを直接書いてもよい

必要なパッケージ: markdown, playwright（requirements-docs.txt）。Chromium は
環境にあるものを探して使い、無ければ `playwright install chromium` のものを使う。
"""

import argparse
import datetime
import glob
import html
import re
import sys
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "user_guide"
OUTPUT = ROOT / "output"

TITLES = {
    "ja": ("プロジェクトスケジューラー", "利用者ガイド"),
}

CSS = """
@page { size: A4; margin: 18mm 17mm 20mm 17mm; }
:root {
  --ink: #1f2933; --muted: #5b6770; --rule: #d5dbe0; --accent: #1d5f8c;
  --kikan: #b3471d; --kikan-bg: #fff4ec;
}
html { font-family: "Noto Sans CJK JP", "Yu Gothic UI", "Meiryo", sans-serif;
       font-size: 10.5pt; line-height: 1.75; color: var(--ink); }
body { margin: 0; }
h1, h2, h3 { line-height: 1.4; color: var(--ink); break-after: avoid; }
h1 { font-size: 20pt; margin: 0 0 6mm; padding-bottom: 2mm; border-bottom: 2px solid var(--accent); }
h2 { font-size: 14pt; margin: 9mm 0 3mm; padding-left: 3mm; border-left: 4px solid var(--accent); }
h3 { font-size: 11.5pt; margin: 6mm 0 2mm; }
p, ul, ol { margin: 0 0 3mm; }
li { margin: 0.5mm 0; }
strong { color: #0f1a22; }
.chapter { break-before: page; }
table { border-collapse: collapse; width: 100%; margin: 3mm 0 5mm; font-size: 9.5pt; break-inside: auto; }
tr { break-inside: avoid; }
th, td { border: 1px solid var(--rule); padding: 1.8mm 2.5mm; vertical-align: top; text-align: left; }
th { background: #eef2f5; font-weight: 700; }
img { max-width: 100%; border: 1px solid var(--rule); }
figure { margin: 4mm 0 6mm; text-align: center; break-inside: avoid; }
figcaption { font-size: 9pt; color: var(--muted); margin-top: 1.5mm; }
code { font-family: "Noto Sans Mono CJK JP", monospace; font-size: 9.5pt;
       background: #f1f3f5; padding: 0 1mm; border-radius: 2px; }

/* 期間≠工数の囲み */
.admonition { break-inside: avoid; }
.admonition.kikan { border: 2px solid var(--kikan); background: var(--kikan-bg);
  border-radius: 3mm; padding: 4mm 6mm 2mm; margin: 5mm 0 7mm; }
.admonition.kikan > .admonition-title { font-size: 13pt; font-weight: 700; color: var(--kikan);
  margin: 0 0 2.5mm; }
.admonition.kikan-note { border-left: 4px solid var(--kikan); background: var(--kikan-bg);
  padding: 2mm 4mm 0.5mm; margin: 3mm 0 4mm; font-size: 9.8pt; }
.admonition.kikan-note > .admonition-title { font-weight: 700; color: var(--kikan); margin: 0 0 1mm; }
.admonition.kikan > .admonition-title::before,
.admonition.kikan-note > .admonition-title::before {
  content: "期間≠工数"; display: inline-block; font-size: 8pt; font-weight: 700; color: #fff;
  background: var(--kikan); border-radius: 1mm; padding: 0 1.8mm; margin-right: 2.5mm;
  vertical-align: 0.15em; line-height: 1.7; }

/* 表紙・目次 */
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover .product { font-size: 13pt; color: var(--muted); letter-spacing: 0.1em; }
.cover .title { font-size: 30pt; font-weight: 700; margin: 3mm 0 10mm; }
.cover .date { font-size: 10pt; color: var(--muted); }
.toc { break-before: page; }
.toc h1 { border-bottom-color: var(--rule); }
.toc ol { list-style: none; padding: 0; }
.toc li.ch { font-weight: 700; margin-top: 3mm; }
.toc li.sec { padding-left: 6mm; color: var(--muted); font-size: 9.8pt; }
.toc a { color: inherit; text-decoration: none; }
"""


def _chapters(language):
    files = sorted((GUIDE / language).glob("[0-9][0-9]_*.md"))
    if not files:
        sys.exit(f"原稿が見つかりません: {GUIDE / language}")
    return files


def _render_chapter(path, index, language):
    md = markdown.Markdown(
        extensions=["tables", "admonition", "attr_list", "md_in_html", "footnotes", "toc"],
        extension_configs={"toc": {"slugify": lambda value, sep: f"c{index}-" + re.sub(r"\W+", "-", value).strip("-")}},
    )
    body = md.convert(path.read_text(encoding="utf-8"))
    # 原稿の改行はHTMLでは空白になる。日本語の文字どうしの間に空白が入らないよう詰める
    body = re.sub(r"([^\x00-\x7f])\n[ \t]*([^\x00-\x7f])", r"\1\2", body)
    # 原稿の images/xxx.png を言語別のフォルダへ向ける
    body = body.replace('src="images/', f'src="images/{language}/')
    headings = [(t["level"], t["id"], t["name"]) for t in _flatten(md.toc_tokens)]
    return f'<section class="chapter">{body}</section>', headings


def _flatten(tokens):
    for t in tokens:
        yield t
        yield from _flatten(t.get("children", []))


def build_html(language):
    product, title = TITLES.get(language, TITLES["ja"])
    sections, toc = [], []
    for i, path in enumerate(_chapters(language)):
        section, headings = _render_chapter(path, i, language)
        sections.append(section)
        for level, anchor, name in headings:
            if level <= 2:
                cls = "ch" if level == 1 else "sec"
                toc.append(f'<li class="{cls}"><a href="#{anchor}">{name}</a></li>')
    today = datetime.date.today().isoformat()
    return f"""<!doctype html>
<html lang="{language}"><head><meta charset="utf-8">
<base href="{GUIDE.as_uri()}/">
<title>{html.escape(product)} {html.escape(title)}</title>
<style>{CSS}</style></head><body>
<div class="cover"><div class="product">{html.escape(product)}</div>
<div class="title">{html.escape(title)}</div><div class="date">{today}</div></div>
<nav class="toc"><h1>目次</h1><ol>{''.join(toc)}</ol></nav>
{''.join(sections)}
</body></html>"""


def _find_chromium(explicit):
    if explicit:
        return explicit
    for pattern in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",):
        found = sorted(glob.glob(pattern))
        if found:
            return found[-1]
    return None  # playwright が自分で入れたものを使う


def build_pdf(language, keep_html=False, chromium=None):
    from playwright.sync_api import sync_playwright

    OUTPUT.mkdir(exist_ok=True)
    pdf_path = OUTPUT / f"user_guide_{language}.pdf"
    page_html = build_html(language)
    with tempfile.TemporaryDirectory() as tmp:
        html_path = Path(tmp) / "guide.html"
        html_path.write_text(page_html, encoding="utf-8")
        with sync_playwright() as p:
            exe = _find_chromium(chromium)
            browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
            page = browser.new_page()
            page.goto(html_path.as_uri(), wait_until="networkidle")
            page.pdf(
                path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True,
                display_header_footer=True, header_template="<span></span>",
                footer_template='<div style="width:100%;text-align:center;font-size:8pt;color:#777;">'
                                '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
            )
            browser.close()
    if keep_html:
        (OUTPUT / f"user_guide_{language}.html").write_text(page_html, encoding="utf-8")
    print(f"saved {pdf_path.relative_to(ROOT)}")
    return pdf_path


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lang", default="ja")
    parser.add_argument("--html", action="store_true", help="確認用のHTMLも output/ に残す")
    parser.add_argument("--chromium", help="使う Chromium の実行ファイル")
    args = parser.parse_args()
    build_pdf(args.lang, keep_html=args.html, chromium=args.chromium)


if __name__ == "__main__":
    main()
