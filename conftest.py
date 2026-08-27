"""pytest共通設定。

- リポジトリルートを`sys.path`に入れ、`gui`/`project_scheduler`をテストから
  importできるようにする。
- Qtを使うテストがヘッドレス環境でも動くよう`QT_QPA_PLATFORM=offscreen`を
  既定にする（環境変数で明示済みならそれを尊重する）。ここで設定しておくと
  実行のたびに手で`export`する必要がなくなる。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent))
