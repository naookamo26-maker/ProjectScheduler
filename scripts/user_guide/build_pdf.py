"""利用者向けドキュメント（docs/user_guide/<言語>/*.md）からPDFを作る。

    python scripts/user_guide/build_pdf.py              # → output/user_guide_ja.pdf
    python scripts/user_guide/build_pdf.py --html       # 確認用にHTMLも残す

章ファイルはファイル名の順（00_…, 01_…）に1冊へまとめ、表紙と目次を付けて、
Chromium の印刷機能でA4のPDFにする。画像は capture_screenshots.py が撮った
docs/user_guide/images/<言語>/ のものを使う（原稿には `images/名前.png` とだけ書く）。

原稿の書き方（Markdown の拡張）:

- 「期間であって工数ではない」の囲み（docs/user_guide/outline.md 執筆方針1）
    !!! kikan "見出し"          … 0章の大きな囲み
    !!! kikan-note "見出し"     … 各章で念押しする小さな囲み（目印「期間≠工数」。日数の話）
    !!! lines-note "見出し"     … 同じ見た目で目印が「ライン数≠人数」（ライン数の話）
  （本文は4字下げで続ける。Python-Markdown の admonition 拡張）
- 表・脚注・見出しのアンカーが使える。図はHTML/SVGを直接書いてもよい
- アプリのアイコンを文中に置くときは <img class="inline-icon" src="app-icon" alt="">
- 画面写真に番号の目印を重ねるときは <div class="annotated"> と <span class="pin">
  （本文から指すときは <span class="pinref">1</span>）

必要なパッケージ: markdown, playwright（requirements-docs.txt）。Chromium は
環境にあるものを探して使い、無ければ `playwright install chromium` のものを使う。
"""

import argparse
import datetime
import glob
import html
import itertools
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION  # noqa: E402  表紙の対象バージョン（唯一の定義から読む）
GUIDE = ROOT / "docs" / "user_guide"
OUTPUT = ROOT / "output"

TITLES = {
    "ja": ("プロジェクトスケジューラー", "利用者ガイド"),
}

# 表紙に載せる版数。内容を改めて配り直すときに上げる（誤字の修正など、配り直さない
# 直しでは上げない）。作成日はビルドした日が入る
EDITION = {
    "ja": "第2版",
}

# 表紙に載せる対象バージョンの書き方（番号は app_version.py から読む）
VERSION_LABEL = {
    "ja": "対象バージョン {version}",
}

# 表紙に載せるアプリのアイコン（assets/icon/。scripts/build_icon.py が生成するプレビュー）
ICON = ROOT / "assets" / "icon" / "app_icon_256.png"

CSS = """
@page { size: A4; margin: 18mm 17mm 20mm 17mm; }
:root {
  --ink: #1f2933; --muted: #5b6770; --rule: #d5dbe0; --accent: #1d5f8c;
  --kikan: #b3471d; --kikan-bg: #fff4ec;
}
html { font-family: "Noto Sans CJK JP", "Yu Gothic UI", "Meiryo", sans-serif;
       font-size: 10.5pt; line-height: 1.75; color: var(--ink); }
body { margin: 0; }
h1, h2, h3 { line-height: 1.4; color: var(--ink); break-after: avoid; break-inside: avoid; }
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
/* 画面全体のスクリーンショットは幅いっぱいだと高さが約106mmになり、ページの残りに
   収まらずに次のページへ送られて大きな余白を残しやすい。高さに上限を設けて収まりやすくする */
figure img { max-height: 92mm; }
figcaption { font-size: 9pt; color: var(--muted); margin-top: 1.5mm; }
code { font-family: "Noto Sans Mono CJK JP", monospace; font-size: 9.5pt;
       background: #f1f3f5; padding: 0 1mm; border-radius: 2px; }

/* 期間≠工数・ライン数≠人数の囲み */
.admonition { break-inside: avoid; }
.admonition.kikan { border: 2px solid var(--kikan); background: var(--kikan-bg);
  border-radius: 3mm; padding: 4mm 6mm 2mm; margin: 5mm 0 7mm; }
.admonition.kikan > .admonition-title { font-size: 13pt; font-weight: 700; color: var(--kikan);
  margin: 0 0 2.5mm; }
.admonition.kikan-note, .admonition.lines-note { border-left: 4px solid var(--kikan); background: var(--kikan-bg);
  padding: 2mm 4mm 0.5mm; margin: 3mm 0 4mm; font-size: 9.8pt; }
.admonition.kikan-note > .admonition-title,
.admonition.lines-note > .admonition-title { font-weight: 700; color: var(--kikan); margin: 0 0 1mm; }
.admonition.kikan > .admonition-title::before,
.admonition.kikan-note > .admonition-title::before,
.admonition.lines-note > .admonition-title::before {
  content: "期間≠工数"; display: inline-block; font-size: 8pt; font-weight: 700; color: #fff;
  background: var(--kikan); border-radius: 1mm; padding: 0 1.8mm; margin-right: 2.5mm;
  vertical-align: 0.15em; line-height: 1.7; }
.admonition.lines-note > .admonition-title::before { content: "ライン数≠人数"; }

/* 画面写真に番号の目印を重ねる図（4章）。位置は画像に対する % で指定する:
   <div class="annotated"><img …><span class="pin" style="left:3%;top:1%">1</span></div> */
.annotated { position: relative; display: inline-block; max-width: 100%; }
.annotated img { display: block; }
.annotated .pin { position: absolute; transform: translate(-50%, -50%);
  width: 6mm; height: 6mm; border-radius: 50%; background: #c0392b; color: #fff;
  font-size: 9pt; font-weight: 700; line-height: 6mm; text-align: center;
  box-shadow: 0 0 0 0.6mm #fff; }
.pinref { display: inline-block; width: 4.6mm; height: 4.6mm; border-radius: 50%;
  background: #c0392b; color: #fff; font-size: 7.5pt; font-weight: 700;
  line-height: 4.6mm; text-align: center; vertical-align: 0.1em; }

/* 表の中に置く小さな見本の画像（5章のガントチャートの見た目の一覧） */
.swatch img { max-height: 13mm; max-width: 42mm; border: none; margin-bottom: 1mm; }
.swatches td:first-child, .swatches th:first-child { width: 46mm; }

/* 表紙・目次 */
.cover { height: 250mm; display: flex; flex-direction: column; justify-content: center; }
.cover .product { font-size: 13pt; color: var(--muted); letter-spacing: 0.1em; }
.cover .title { font-size: 30pt; font-weight: 700; margin: 3mm 0 10mm; }
.cover .icon { width: 30mm; height: 30mm; border: none; margin-bottom: 8mm; }
.cover .version { font-size: 11pt; margin-bottom: 2mm; }
.cover .date { font-size: 10pt; color: var(--muted); }
/* 文中に置くアプリのアイコン（2章「起動する」） */
img.inline-icon { height: 6mm; width: 6mm; border: none; vertical-align: -1.5mm; margin: 0 0.5mm; }
.toc { break-before: page; }
.toc h1 { border-bottom-color: var(--rule); }
.toc ol { list-style: none; padding: 0; }
.toc li.ch { font-weight: 700; margin-top: 3mm; }
.toc li.sec { padding-left: 6mm; color: var(--muted); font-size: 9.8pt; }
.toc a { color: inherit; text-decoration: none; display: flex; align-items: baseline; }
.toc .dots { flex: 1; border-bottom: 1px dotted #9aa5ae; margin: 0 2mm; transform: translateY(-0.3em); }
.toc .pg { min-width: 8mm; text-align: right; font-variant-numeric: tabular-nums; }
"""


def _chapters(language):
    files = sorted((GUIDE / language).glob("[0-9][0-9]_*.md"))
    if not files:
        sys.exit(f"原稿が見つかりません: {GUIDE / language}")
    return files


def _render_chapter(path, index, language):
    # 見出しのIDは「章番号-連番」の英数字にする。日本語のIDはPDFの中でURLエンコードされ、
    # 目次のページ番号を求めるとき（_heading_pages）に扱いにくいため
    serial = itertools.count(1)
    md = markdown.Markdown(
        extensions=["tables", "admonition", "attr_list", "md_in_html", "footnotes", "toc"],
        extension_configs={"toc": {"slugify": lambda value, sep: f"c{index}-{next(serial)}"}},
    )
    body = md.convert(path.read_text(encoding="utf-8"))
    # 原稿の改行はHTMLでは空白になる。日本語の文字どうしの間に空白が入らないよう詰める
    body = re.sub(r"([^\x00-\x7f])\n[ \t]*([^\x00-\x7f])", r"\1\2", body)
    # 原稿の images/xxx.png を言語別のフォルダへ向ける
    body = body.replace('src="images/', f'src="images/{language}/')
    body = body.replace('src="app-icon"', f'src="{ICON.as_uri()}"')
    headings = [(t["level"], t["id"], t["name"]) for t in _flatten(md.toc_tokens)]
    return f'<section class="chapter">{body}</section>', headings


def _flatten(tokens):
    for t in tokens:
        yield t
        yield from _flatten(t.get("children", []))


def build_html(language, page_numbers=None):
    """page_numbers（{見出しのID: ページ}）を渡すと、目次にページ番号を入れる。"""
    product, title = TITLES.get(language, TITLES["ja"])
    page_numbers = page_numbers or {}
    sections, toc = [], []
    for i, path in enumerate(_chapters(language)):
        section, headings = _render_chapter(path, i, language)
        sections.append(section)
        for level, anchor, name in headings:
            if level <= 2:
                cls = "ch" if level == 1 else "sec"
                page = page_numbers.get(anchor, "")
                toc.append(
                    f'<li class="{cls}"><a href="#{anchor}"><span class="t">{name}</span>'
                    f'<span class="dots"></span><span class="pg">{page}</span></a></li>'
                )
    today = datetime.date.today().isoformat()
    edition = EDITION.get(language, EDITION["ja"])
    target = VERSION_LABEL.get(language, VERSION_LABEL["ja"]).format(version=APP_VERSION)
    return f"""<!doctype html>
<html lang="{language}"><head><meta charset="utf-8">
<base href="{GUIDE.as_uri()}/">
<title>{html.escape(product)} {html.escape(title)}</title>
<style>{CSS}</style></head><body>
<div class="cover"><img class="icon" src="{ICON.as_uri()}" alt="">
<div class="product">{html.escape(product)}</div>
<div class="title">{html.escape(title)}</div>
<div class="version">{html.escape(target)}</div>
<div class="date">{html.escape(edition)}　{today}</div></div>
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


def _heading_pages(pdf_path):
    """PDFのリンク先（目次から飛ぶ見出し）ごとのページ番号を返す。poppler の
    `pdfinfo -dests` を使う（Chromium は目次のリンク先を名前付きの位置として書き出す）。"""
    out = subprocess.run(["pdfinfo", "-dests", str(pdf_path)], capture_output=True, text=True, check=True).stdout
    pages = {}
    for line in out.splitlines():
        m = re.match(r'\s*(\d+)\s+\[.*\]\s+"([^"]+)"', line)
        if m:
            pages[m.group(2)] = int(m.group(1))
    return pages


def _print_pdf(page_html, pdf_path, tmp, browser):
    html_path = Path(tmp) / "guide.html"
    html_path.write_text(page_html, encoding="utf-8")
    page = browser.new_page()
    page.goto(html_path.as_uri(), wait_until="networkidle")
    page.pdf(
        path=str(pdf_path), format="A4", print_background=True, prefer_css_page_size=True,
        display_header_footer=True, header_template="<span></span>",
        footer_template='<div style="width:100%;text-align:center;font-size:8pt;color:#777;">'
                        '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
    )
    page.close()


def build_pdf(language, keep_html=False, chromium=None):
    """2回印刷する。1回目で各見出しのページを調べ、目次にページ番号を入れて2回目を印刷する
    （目次の番号は右端の決まった幅に入るので、番号を入れてもページ割りは変わらない）。"""
    from playwright.sync_api import sync_playwright

    OUTPUT.mkdir(exist_ok=True)
    pdf_path = OUTPUT / f"user_guide_{language}.pdf"
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        exe = _find_chromium(chromium)
        browser = p.chromium.launch(**({"executable_path": exe} if exe else {}))
        draft = Path(tmp) / "draft.pdf"
        _print_pdf(build_html(language), draft, tmp, browser)
        pages = _heading_pages(draft)
        page_html = build_html(language, pages)
        _print_pdf(page_html, pdf_path, tmp, browser)
        browser.close()
        if _heading_pages(pdf_path) != pages:
            sys.exit("目次のページ番号を入れたらページ割りが変わりました。目次の組み方を見直してください")
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
