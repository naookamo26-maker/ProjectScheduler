"""pytest共通設定。

- リポジトリルートを`sys.path`に入れ、`gui`/`project_scheduler`をテストから
  importできるようにする。
- Qtを使うテストがヘッドレス環境でも動くよう`QT_QPA_PLATFORM=offscreen`を
  既定にする（環境変数で明示済みならそれを尊重する）。ここで設定しておくと
  実行のたびに手で`export`する必要がなくなる。
- オプション設定（gui/app_settings.py）の保存先を一時フォルダに向ける。
  MainWindowを作るテストが、利用者の実際の設定ファイルを読み書きしないように。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PROJECT_SCHEDULER_SETTINGS"] = str(
    Path(tempfile.mkdtemp(prefix="pschedule_settings_")) / "ProjectScheduler.ini"
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
