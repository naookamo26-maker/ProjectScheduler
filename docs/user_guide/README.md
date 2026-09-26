# 利用者向けドキュメント

利用者向けドキュメント（利用者ガイド）の原稿と、PDFを作る仕組み。
内容・執筆方針は [`outline.md`](outline.md) を正とする。

| 場所 | 内容 |
| --- | --- |
| `outline.md` | 目次・各章の要点・執筆方針 |
| `ja/NN_*.md` | 原稿（日本語）。ファイル名の番号順に1冊へまとめる |
| `images/<言語>/*.png` | 画面の画像。`scripts/user_guide/capture_screenshots.py` が撮る（手で編集しない） |
| `data/Guide_Sample_*.pschedule` | 撮影に使うサンプル。`scripts/user_guide/generate_guide_samples.py` が作る（ゲーム開発の題材: 新作タイトルの制作／アップデートの制作） |
| `scripts/user_guide/build_pdf.py` | 原稿＋画像から `output/user_guide_<言語>.pdf` を作る |

## 手順

```bash
pip install -r requirements.txt -r requirements-docs.txt

# 0. 撮影用のサンプルを作る（サンプルの内容を変えたときだけ）
python scripts/user_guide/generate_guide_samples.py

# 1. 画面を撮る（撮影用サンプルを開いて撮影。画面を変えたら撮り直す）
python scripts/user_guide/capture_screenshots.py

# 2. PDFを作る（--html を付けると確認用のHTMLも output/ に残る）
python scripts/user_guide/build_pdf.py
```

- 撮影は画面を表示せずに行う（offscreen）。Linuxでは PySide6 用のシステムライブラリ
  （CLAUDE.md「環境セットアップ」）と日本語フォント（`fonts-noto-cjk`）が要る。
- PDFは Chromium の印刷機能で作る。環境に Chromium が無ければ
  `playwright install chromium` で入れる。
- 目次のページ番号は、1回目に印刷したPDFから各見出しのページを読み取って入れ、2回目を
  印刷する。読み取りに poppler の `pdfinfo` を使う（Linux では `poppler-utils`、Windows では
  poppler を入れて `pdfinfo` にパスを通す）。
- 撮る場面を増やすときは、`capture_screenshots.py` の `SHOTS` に1行足す。

## 原稿の書き方

- 画像は `![説明](images/名前.png)`、キャプションを付けるときは
  `<figure><img src="images/名前.png"><figcaption>…</figcaption></figure>`。
  言語別のフォルダはビルド時に補う。
- 「期間であって工数ではない」の囲み（執筆方針1）:

  ```markdown
  !!! kikan "見出し"
      0章の大きな囲み。本文は4字下げで続ける。

  !!! kikan-note "期間であって工数ではありません"
      各章で念押しする小さな囲み（日数の話。目印は「期間≠工数」）。

  !!! lines-note "ライン数は人数ではありません"
      同じ見た目で、目印が「ライン数≠人数」になる（ライン数の話）。
  ```

- 用語は `docs/i18n_glossary.md` の日本語に揃える。
