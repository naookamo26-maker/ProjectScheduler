#!/usr/bin/env python3
"""起動用アイコンの原画（SVG）から、.exe・ウィンドウ用のアイコンファイルを生成する。

使い方:
    python scripts/build_icon.py

入力:
    assets/icon/app_icon.svg        48px以上で使う原画
    assets/icon/app_icon_small.svg  32px以下で使う簡略版（細い線が潰れないようにしたもの）
出力:
    assets/icon/app_icon.ico        16〜256pxを1ファイルにまとめたもの（spec の icon= と
                                    ウィンドウアイコンの両方で使う）
    assets/icon/app_icon_256.png    ドキュメント等に貼るためのプレビュー

PNGを埋め込んだICO（Windows Vista以降が対応）を自前で書き出すので、PySide6以外の
依存は要らない。原画を直したらこのスクリプトを実行し、生成物もコミットする。
"""

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icon"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
SMALL_MAX = 32


def render_png(svg_path, size):
    """SVGを size×size のPNGバイト列にする。"""
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise SystemExit(f"SVGを読み込めません: {svg_path}")
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def write_ico(path, pngs):
    """{サイズ: PNGバイト列} をPNG埋め込み形式のICOとして書き出す。"""
    header = struct.pack("<HHH", 0, 1, len(pngs))
    offset = len(header) + 16 * len(pngs)
    entries, blobs = b"", b""
    for size, png in sorted(pngs.items()):
        # 幅・高さの 0 は 256 を表す
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    path.write_bytes(header + entries + blobs)


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841  QSvgRenderer/QImage に必要
    pngs = {}
    for size in SIZES:
        svg = ICON_DIR / ("app_icon_small.svg" if size <= SMALL_MAX else "app_icon.svg")
        pngs[size] = render_png(svg, size)
    write_ico(ICON_DIR / "app_icon.ico", pngs)
    (ICON_DIR / "app_icon_256.png").write_bytes(pngs[256])
    print(f"生成しました: {ICON_DIR / 'app_icon.ico'}（{', '.join(map(str, SIZES))}px）")


if __name__ == "__main__":
    main()
