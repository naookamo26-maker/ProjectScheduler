"""
タブ横断で使う共通UI部品。

- CrudSection: 「タイトル + 追加/削除ボタン + 表」の1セクション。
  基本情報設定・ワークフロー設計・ジョブタブで繰り返し使う型。
- make_fk_combo: 外部キー選択用のQComboBox（表示は名前、内部値はDBの整数ID）。
- confirm_or_block_delete: 削除前の参照整合性チェック用ダイアログ。
- ChoiceFilterGroup / CollapsibleSection: チェックボックス一覧による絞り込みと、
  それらをまとめて畳める折りたたみセクション（ジョブ・ガントチャートタブ共通）。
"""

from PySide6.QtCore import QDate, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

ROW_ID_ROLE = Qt.UserRole


# -- ホイールスクロールの誤操作防止 ---------------------------------------------------
#
# QComboBox/QSpinBox/QDateEdit/QListWidgetは、マウスカーソルが乗っているだけで
# （クリックしてフォーカスしていなくても）ホイール操作を自分の値変更/スクロールと
# して奪ってしまう。テーブルや画面全体をホイールでスクロールしようとした際、
# たまたまカーソルがこれらのウィジェット上を通過しただけで値が変わってしまう
# 事故を防ぐため、フォーカスを持っている（＝クリックやTabで明示的に選択した）
# 間だけホイール操作を受け付け、それ以外は無視して親ビュー（テーブルの
# スクロールバーやQScrollArea）側にホイールイベントを渡す。


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


# -- 連続した値編集を1つのUndo単位にまとめる ------------------------------------------
#
# スピンボックスや日付欄は、1回の編集で値が何度も変わる（▲を押すたび、
# 矢印キーを押すたび）。変更のたびにDBへ書き込む方針自体はそのままにしつつ
# （DBが常に最新なら、値を変えた直後に保存しても取りこぼさない）、Undoの単位は
# 「フォーカスを得てから外れるまで」でまとめる。そうしないと「▲を5回押したのに
# 5回Undoしないと戻らない」ことになる。


class _UndoSessionMixin:
    """フォーカスの出入りを ProjectDatabase の begin/end_undo_group に繋ぐ。
    bind_undo_session() を呼んだウィジェットでのみ有効になる。"""

    # (db, ラベル, セッション終了時のコールバック, 確定直前のコールバック) または None
    _undo_session = None

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if self._undo_session is not None:
            db, label, _on_end, _on_before_commit = self._undo_session
            db.begin_undo_group(label)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if self._undo_session is not None:
            db, _label, on_end, on_before_commit = self._undo_session
            # Undo単位がまだ開いているうちに呼ぶ。ここでDBを変更すれば、その
            # 変更はユーザーの編集と同じ1つのUndo単位に合流する（例:
            # gui/tab_basic_info.py のマイルストーン整合性の再調整）。
            if on_before_commit is not None:
                on_before_commit()
            changed = db.end_undo_group()
            # 値が実際には変わっていない（例: setCurrentItem()でプログラム的に
            # フォーカスが素通りしただけ）場合は on_session_end を呼ばない。
            # 呼んでしまうと、並べ替え用の再構築（例: gui/tab_basic_info.py の
            # _resort_team_capacity_changes_later）が編集していないのに走り、
            # 選択やスクロール位置を失わせてしまう。
            if on_end is not None and changed:
                on_end()


def bind_undo_session(widget, db, label, on_session_end=None, on_before_commit=None):
    """widget にフォーカスがある間の連続した変更を、1つのUndo単位にまとめる。

    on_session_end を渡すと、実際に値が変わって編集が終わった（フォーカスが
    外れた）時にのみ呼ばれる。並べ替えを伴う表など、「編集中に作り直すと
    フォーカスが飛んでしまうので、編集が終わってから作り直したい」処理を
    ここに載せる。

    on_before_commit は、Undo単位がまだ開いているうちに呼ばれる。ここでDBを
    変更すると、ユーザーの編集と同じ1つのUndo単位に合流するため、「編集に
    連動してDBの他の箇所も調整するが、Undoは1回で全部戻したい」処理に使う
    （on_session_end は単位を閉じた後に呼ばれるので、そこでDBを変更すると
    別のUndoエントリになってしまう）。"""
    widget._undo_session = (db, label, on_session_end, on_before_commit)
    return widget


class NoWheelSlider(QSlider):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class NoWheelSpinBox(_UndoSessionMixin, QSpinBox):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 既定のWheelFocus（ホイールを回しただけでフォーカスを奪う）は「選択した
        # 状態でのみ」という意図に反するため、クリック/Tabでのみフォーカスさせる。
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class DefaultAwareSpinBox(NoWheelSpinBox):
    """最小値（0）を「既定値を使用」という特殊値として扱うQSpinBox。
    setSpecialValueTextで見た目は「既定（n日）」のようにできるが、素の0から
    ▲▼で増減すると1やマイナスからしか始まらず使い勝手が悪い。この特殊値の
    状態から増減した場合は、既定値を起点に増減させる（0の状態で▲を押すと
    既定値+1、▼を押すと既定値-1になる）。DB保存時の「0＝既定を使用」という
    意味はそのまま変えない（0に戻ればまた「既定（n日）」の表示に戻る）。

    また、増減の結果として値が既定値とちょうど同じ数値になった場合は、
    自動的に特殊値（0）へ畳み込む（既定+1してから-1すると既定と同じ数値の
    「上書き」が残ってしまい既定表示に戻らない、という問題への対応）。
    逆に、通常の減算で0（既定を使用）へ意図せず落ち込まないよう、増減で
    到達できる実際の日数としての下限は1にする（0日という設定自体に意味が
    無いため）。0に戻すのは既定値と一致した場合の自動畳み込み、または
    直接入力のみとする。"""

    def __init__(self, default_value, parent=None):
        super().__init__(parent)
        self.default_value = default_value
        self.valueChanged.connect(self._collapse_to_default_if_matched)

    def _collapse_to_default_if_matched(self, value):
        if value == self.default_value and value != self.minimum():
            self.setValue(self.minimum())

    def stepBy(self, steps):
        if self.value() == self.minimum() and steps != 0:
            self.setValue(max(self.minimum() + 1, self.default_value + steps))
        elif steps < 0 and self.value() + steps <= self.minimum():
            self.setValue(self.minimum() + 1)
        else:
            super().stepBy(steps)

    def stepEnabled(self):
        # QAbstractSpinBoxは既定で「value == minimum() なら▼を無効化」するため、
        # 特殊値（0＝既定を使用）の状態から▼を押しても何も起きなくなってしまう。
        # 上のstepByで0からの▼を「既定値-1」として扱えるよう、常に両方向を
        # 有効にする（数値としての下限自体は0のままなので範囲外にはならない）。
        if not self.isEnabled():
            return QAbstractSpinBox.StepNone
        return QAbstractSpinBox.StepUpEnabled | QAbstractSpinBox.StepDownEnabled


class OptionalSpinBox(NoWheelSpinBox):
    """最小値の1つ下を「指定なし」（NULL）として扱うQSpinBox。

    DefaultAwareSpinBoxと違い、実際の最小値（例: チームの同時ライン数の0＝
    「その期間は稼働なし」）自体に固有の意味がある場面で使うため、その値への
    自動畳み込みはしない——「指定なし」と「0」は別の値として明示的に区別する。

    QSpinBoxのvalue()/setValue()はQtの内部のスピン制御が使う実際の整数
    （最小値＝指定なし）のまま変えず、NULLとの変換は optional_value()/
    set_optional_value() で明示的に行う。"""

    def __init__(self, maximum, special_value_text, parent=None):
        super().__init__(parent)
        self.setRange(-1, maximum)
        self.setSpecialValueText(special_value_text)

    def optional_value(self):
        value = self.value()
        return None if value == self.minimum() else value

    def set_optional_value(self, value):
        self.setValue(self.minimum() if value is None else value)


class NoWheelDateEdit(_UndoSessionMixin, QDateEdit):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.StrongFocus)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


# QDateEditには「特殊値」の概念（QAbstractSpinBoxのspecialValueText）が使える
# が、DefaultAwareSpinBoxの0のような「実際に届く自然な最小値」が日付には無い。
# 実務でまず使われない過去日（2000-01-01）を「未設定」の特殊値に割り当てる。
_UNSET_DATE = QDate(2000, 1, 1)


class OptionalDateEdit(NoWheelDateEdit):
    """「日付を指定しない」を1列で表せるQDateEdit。DefaultAwareSpinBoxの
    日付版——0の代わりに_UNSET_DATE（2000-01-01）を「未設定」の特殊値として
    扱い、setSpecialValueTextで見た目を「（固定なし）」にする。

    カレンダーポップアップで_UNSET_DATEまで戻るのは非現実的なので、
    Delete/Backspaceキーで直接「未設定」に戻せるようにする（値を持つ入力欄で
    Deleteが「クリア」を意味するのは一般的な操作感のため、追加のボタンを
    UIに増やさずに済む）。

    逆に「未設定から日付を入れ始める」側も、素のQDateEditでは破綻する。
    特殊値（_UNSET_DATE）は最小値でもあるため、Qtの既定動作では:

    - ▲/▼キー・スピンの矢印・ホイールは、最小値の**年セクション**を1つ
      動かして 2001-01-01 にしてしまう（実務で使う日付から20年以上離れる）。
    - 表示が「（固定なし）」という特殊テキストなので、そこへ数字を打っても
      Qtはセクションを更新できず、'（固定なし）0260415' のような壊れた表示に
      なるだけで値が入らない。

    どちらも「未設定から入力を始めた瞬間に今日を起点として置く」ことで解決する
    （DefaultAwareSpinBoxが0から既定値を起点に増減するのと同じ考え方）。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumDate(_UNSET_DATE)
        self.setSpecialValueText("（固定なし）")
        self.setCalendarPopup(True)
        self.setDisplayFormat("yyyy-MM-dd")
        self.setDate(_UNSET_DATE)
        self.setToolTip("クリックしてカレンダーから日付を選択、または"
                         "Deleteキーで固定を解除できます。")

    def value(self):
        """設定されていれば 'YYYY-MM-DD'、未設定なら None。"""
        d = self.date()
        return None if d == _UNSET_DATE else d.toString("yyyy-MM-dd")

    def set_value(self, iso_str):
        self.setDate(_to_qdate_or_unset(iso_str))

    def _sync_calendar_page_to_today(self):
        """未設定（_UNSET_DATE=2000-01-01）のままカレンダーを開くと表示月が
        2000年になってしまい、現在の年まで大きくスクロールする必要がある。
        実際の値（＝未設定という状態）は変えず、カレンダーの表示ページだけ
        今日の月に合わせておく。

        ポップアップを開く操作（mousePressEvent/keyPressEvent）の直後に
        呼ぶだけでは効果が無い——QDateTimeEdit側がポップアップを開く際、
        自分自身の`date()`（＝未設定の2000-01-01）に合わせてカレンダーの
        表示ページを自動的に上書きするため、そちらが後から効いて2000年に
        戻ってしまう（実際に確認済み）。QDateTimeEdit自身の処理が終わった
        「後」に上書きし直す必要があるため、呼び出し側で
        `QTimer.singleShot(0, ...)` 経由で次のイベントループへ回してから
        呼ぶ。"""
        if self.date() == _UNSET_DATE:
            today = QDate.currentDate()
            self.calendarWidget().setCurrentPage(today.year(), today.month())

    def _seed_from_unset(self):
        """未設定の状態から日付の入力を始める際、今日を起点として置く
        （クラスのdocstring参照）。実際に置き換えたらTrueを返す。"""
        if self.date() == _UNSET_DATE:
            self.setDate(QDate.currentDate())
            return True
        return False

    def stepBy(self, steps):
        # 未設定から▲▼・ホイールで動かしたときの1歩目は、最小値(2000-01-01)の
        # 年セクションを動かす既定動作ではなく「今日」にする。2歩目以降は
        # 通常どおりカーソル位置のセクションを増減する。
        if self._seed_from_unset():
            return
        super().stepBy(steps)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        QTimer.singleShot(0, self._sync_calendar_page_to_today)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.setDate(_UNSET_DATE)
            event.accept()
            return
        # 数字の打鍵で入力を始めた場合は、先に今日を入れて通常の日付表示に
        # してから、その打鍵をQtに渡してセクションへ適用させる（特殊テキストの
        # ままでは打鍵がセクションに入らない）。
        if event.text().isdigit():
            self._seed_from_unset()
        super().keyPressEvent(event)
        QTimer.singleShot(0, self._sync_calendar_page_to_today)


def _to_qdate_or_unset(iso_str):
    if not iso_str:
        return _UNSET_DATE
    d = QDate.fromString(str(iso_str), "yyyy-MM-dd")
    return d if d.isValid() else _UNSET_DATE


class NoWheelListWidget(QListWidget):
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


def keep_selection_visible(view):
    """フォーカスが他のウィジェット（別テーブルのセルウィジェット等）に移っても、
    選択中の行がグレーアウトして見えなくならないようにする。

    Qtの既定パレットは、ウィジェットが非アクティブ（フォーカスを持たない）な
    間、選択色（Highlight）を薄いグレーに変える。タスク上書き欄のコンボ等を
    操作するとジョブ一覧からフォーカスが離れるため、この既定動作のせいで
    「選択していたジョブが分からなくなった（選択が解除された）」ように見える
    ことがある。アクティブ時と同じ色を非アクティブ時にも使うことで、選択状態を
    常に視認できるようにする。"""
    palette = view.palette()
    palette.setColor(QPalette.Inactive, QPalette.Highlight, palette.color(QPalette.Active, QPalette.Highlight))
    palette.setColor(
        QPalette.Inactive, QPalette.HighlightedText, palette.color(QPalette.Active, QPalette.HighlightedText)
    )
    view.setPalette(palette)


def row_id(table, row):
    """table の row 行に紐づくDB上のID（先頭列のQt.UserRoleに格納）を返す。"""
    item = table.item(row, 0)
    return item.data(ROW_ID_ROLE) if item else None


def capture_table_state(table):
    """QTableWidgetの選択位置を、Undo/Redo後に復元できる形で取り出す
    （選択行はDB上の実体IDで、列は番号で覚える）。復元は restore_table_state。

    フォーカスは意図的に記録しない。スピンボックス・日付欄・テキスト欄は
    いずれもCtrl+Zを自分のものとして横取りする（QAbstractSpinBoxやQLineEditが
    ShortcutOverrideを受け取る）ため、Undoのたびにフォーカスをそれらへ戻すと、
    次のCtrl+Zがメニューまで届かず「Undoが効かなくなった」ように見える。"""
    row = table.currentRow()
    if row < 0:
        return {"entity_id": None, "column": None}
    return {"entity_id": row_id(table, row), "column": table.currentColumn()}


def restore_table_state(table, state):
    """capture_table_state() の戻り値から選択位置を復元する。対象の entity_id が
    （Undo/Redoの結果）もう存在しない場合は何もしない。

    現在セルの移動は、その列にセルウィジェット（スピンボックスや日付欄）が
    置かれていると、そのウィジェットへフォーカスを移してしまう。これらは
    Ctrl+Zを自分のものとして横取りするため、Undoのたびにフォーカスが入ると
    次のCtrl+Zがメニューまで届かなくなる。選択だけを復元してフォーカスは
    元の位置に留めるため、移ってしまった場合は戻す。"""
    if not state or state.get("entity_id") is None:
        return
    previous_focus = QApplication.focusWidget()
    select_row_by_id(table, state["entity_id"])
    row = table.currentRow()
    column = state.get("column")
    if row >= 0 and column is not None and 0 <= column < table.columnCount():
        table.setCurrentCell(row, column)

    moved_focus = QApplication.focusWidget()
    if moved_focus is not previous_focus:
        if previous_focus is not None:
            previous_focus.setFocus()
        elif moved_focus is not None:
            moved_focus.clearFocus()


def set_current_tree_item_keeping_focus(tree, item):
    """QTreeWidget.setCurrentItem() は、その項目の列にセルウィジェット
    （スピンボックスや日付欄）が置かれていると、そのウィジェットへフォーカスを
    移してしまうことがある（restore_table_state と同じ理由）。選択（現在項目）
    だけを変更し、フォーカスは元の位置に留める（プログラムからの選択変更で
    ユーザーの入力中の欄からフォーカスを奪わないようにするため）。"""
    previous_focus = QApplication.focusWidget()
    tree.setCurrentItem(item)
    moved_focus = QApplication.focusWidget()
    if moved_focus is not previous_focus:
        if previous_focus is not None:
            previous_focus.setFocus()
        elif moved_focus is not None:
            moved_focus.clearFocus()


def select_row_by_id(table, entity_id):
    """entity_id に対応する行を選択状態にする（新規追加直後の行を選ぶ用途）。
    見つからない場合は何もしない。"""
    for row in range(table.rowCount()):
        if row_id(table, row) == entity_id:
            table.setCurrentCell(row, 0)
            return


def set_row_id(table, row, entity_id):
    item = table.item(row, 0)
    if item is None:
        item = QTableWidgetItem()
        table.setItem(row, 0, item)
    item.setData(ROW_ID_ROLE, entity_id)


def unique_default_name(existing_names, base):
    """「追加」ボタン連打で名前が衝突しエラーダイアログが出る事態を避けるため、
    既存名と衝突しない既定名を自動生成する（例: 新しいジョブ, 新しいジョブ (2), ...）。"""
    if base not in existing_names:
        return base
    n = 2
    while f"{base} ({n})" in existing_names:
        n += 1
    return f"{base} ({n})"


def make_fk_combo(options, current_id=None, allow_blank=False, blank_label="（未設定）"):
    """options: [(id, display_name), ...]。選択中の値はコンボの currentData() で
    取得できる（未設定/空欄の場合は None）。"""
    combo = NoWheelComboBox()
    if allow_blank:
        combo.addItem(blank_label, None)
    for entity_id, name in options:
        combo.addItem(name, entity_id)
    if current_id is not None:
        idx = combo.findData(current_id)
        if idx >= 0:
            combo.setCurrentIndex(idx)
    return combo


def auto_size_columns(table, min_width=90, stretch_last=False):
    """データ読み込み後に呼び出し、列幅を内容に合わせて自動調整する。
    列が狭くなりすぎないよう min_width で下限を設ける。

    stretch_last=True の場合、最後の列は内容幅を無視し、表の右側に残る
    余白をすべて使うよう広げる（備考欄のような自由記述の列を、内容の
    有無にかかわらず広く使いたい場合向け）。"""
    table.resizeColumnsToContents()
    for col in range(table.columnCount()):
        if table.columnWidth(col) < min_width:
            table.setColumnWidth(col, min_width)
    if stretch_last:
        table.horizontalHeader().setStretchLastSection(True)


def confirm_or_block_delete(parent, usage_count, entity_label, hard_block):
    """削除前の確認/ブロックダイアログ。

    usage_count が0なら何も表示せず True（そのまま削除してよい）。
    hard_block=True の場合、usage_count>0 なら削除不可を通知して False。
    hard_block=False の場合、usage_count>0 なら確認ダイアログを出し、
    ユーザーが続行を選べば True。
    """
    if usage_count <= 0:
        return True
    if hard_block:
        QMessageBox.warning(
            parent, "削除できません",
            f"{entity_label}は {usage_count} 件から参照されているため削除できません。",
        )
        return False
    reply = QMessageBox.question(
        parent, "削除の確認",
        f"{entity_label}は {usage_count} 件から参照されています。"
        "削除すると、それらの参照が解除されます。続行しますか？",
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
    )
    return reply == QMessageBox.Yes


class MilestoneRepairConfirmDialog(QDialog):
    """マイルストーンの前後関係が変わった結果、ジョブ側のタスク上書きが
    「先行タスクより早い締切」になってしまう場合に、何をどう調整するかを
    提示して実行の可否を確認するダイアログ。

    自動で黙って書き換えると、ジョブタブを開くまで変更に気付けないため、
    必ずこの確認を挟む（キャンセルすれば、きっかけになった編集ごと取り消す）。"""

    def __init__(self, plan, trigger_label, parent=None):
        super().__init__(parent)
        self.setWindowTitle("マイルストーンの整合性を調整")
        self.resize(560, 380)

        layout = QVBoxLayout(self)
        info = QLabel(
            f"{trigger_label}により、以下のタスクが「先行タスクより早い締切」に"
            "なってしまいます。\n"
            "先行タスクに合わせてマイルストーンを引き上げますか？\n"
            "（マイルストーン自体が先行タスクのものに差し替わります。"
            "キャンセルすると、この変更自体を取り消します）"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        def label_of(name, end_date):
            if name is None and end_date is None:
                return "（未設定）"
            return f"{name}（{end_date}）"

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["ジョブ", "タスク", "現在", "調整後"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        for item in plan:
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(item["job_name"]))
            table.setItem(row, 1, QTableWidgetItem(item["task_name"]))
            table.setItem(row, 2, QTableWidgetItem(
                label_of(item["from_milestone_name"], item["from_end_date"])))
            table.setItem(row, 3, QTableWidgetItem(
                label_of(item["to_milestone_name"], item["to_end_date"])))
        auto_size_columns(table, min_width=70, stretch_last=True)
        layout.addWidget(table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("調整して変更")
        buttons.button(QDialogButtonBox.Cancel).setText("変更を取り消す")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def confirm_and_repair_milestone_consistency(db, parent, trigger_label):
    """マイルストーンの前後関係・依存グラフが変わった直後に呼ぶ。整合性が
    崩れていれば確認ダイアログを出し、了承されれば再調整を適用する。

    呼び出し側がまだ開いているUndo単位（`db.undo_group`）の中から呼ぶこと。
    そうすれば、きっかけになった編集と再調整が1回のUndoでまとめて戻る。

    Returns: 変更をそのまま確定してよければ True、ユーザーがキャンセルしたので
    呼び出し側が変更を取り消すべきなら False（調整不要だった場合も True）。"""
    plan = db.plan_milestone_consistency_repair()
    if not plan:
        return True
    if MilestoneRepairConfirmDialog(plan, trigger_label, parent).exec() != QDialog.Accepted:
        return False
    db.apply_milestone_consistency_repair(plan)
    return True


class CrudSection(QGroupBox):
    """タイトル付きグループボックスの中に「追加/削除」ボタンと表を持つ、
    タブ内の1セクション。表本体（QTableWidget）は呼び出し側が組み立て、
    列やセルウィジェットの構成は各タブ側の責務とする（追加/削除の導線と
    見た目の一貫性だけをここで共通化する）。"""

    def __init__(
        self, title, column_labels, on_add, on_delete, on_edit=None,
        edit_dblclick_columns=None, on_duplicate=None, parent=None,
    ):
        super().__init__(title, parent)
        self.on_add = on_add
        self.on_delete = on_delete
        self.on_edit = on_edit
        self.on_duplicate = on_duplicate
        # on_edit をダブルクリックで開く対象列を限定したい場合に指定する
        # （例: 列0がインライン編集可能なテキストで、別の列だけダイアログを
        # 開かせたいケース）。Noneなら全列で発火（従来互換の挙動）。
        self.edit_dblclick_columns = edit_dblclick_columns

        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        add_btn = QPushButton("＋ 追加")
        add_btn.clicked.connect(self._handle_add)
        toolbar.addWidget(add_btn)
        if on_edit is not None:
            edit_btn = QPushButton("編集...")
            edit_btn.clicked.connect(self._handle_edit)
            toolbar.addWidget(edit_btn)
        if on_duplicate is not None:
            duplicate_btn = QPushButton("複製")
            duplicate_btn.clicked.connect(self._handle_duplicate)
            toolbar.addWidget(duplicate_btn)
        del_btn = QPushButton("－ 削除")
        del_btn.clicked.connect(self._handle_delete)
        toolbar.addWidget(del_btn)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, len(column_labels))
        self.table.setHorizontalHeaderLabels(column_labels)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        keep_selection_visible(self.table)
        if on_edit is not None:
            self.table.itemDoubleClicked.connect(self._handle_item_double_clicked)
        layout.addWidget(self.table)

    def _handle_item_double_clicked(self, item):
        if self.edit_dblclick_columns is not None and item.column() not in self.edit_dblclick_columns:
            return
        self._handle_edit()

    def _handle_add(self):
        self.on_add()

    def _handle_edit(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "編集", "編集する行を選択してください。")
            return
        self.on_edit(row)

    def _handle_delete(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "削除", "削除する行を選択してください。")
            return
        self.on_delete(row)

    def _handle_duplicate(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "複製", "複製する行を選択してください。")
            return
        self.on_duplicate(row)

    def current_row_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return row_id(self.table, row)


# -- 絞り込み用チェックボックス一覧・折りたたみセクション ------------------------------
#
# ジョブタブ・ガントチャートタブの両方で、「ワークフロー／チーム／タグ等の
# チェックボックスをOR条件で絞り込み、複数の絞り込みをAND条件で組み合わせる」
# という同じ構造を使うため、ここに共通化してある。


class ChoiceFilterGroup(QGroupBox):
    """チェックボックス一覧による絞り込み（OR条件）。「すべて表示」「すべて解除」
    ボタンと、rebuild()で渡した項目ぶんのチェックボックスを横並びで持つ。"""

    changed = Signal()

    def __init__(self, title, parent=None):
        super().__init__(title, parent)
        self._checks = {}  # key -> QCheckBox
        layout = QHBoxLayout(self)
        select_all_btn = QPushButton("すべて表示")
        select_all_btn.clicked.connect(lambda: self.set_all(True))
        select_none_btn = QPushButton("すべて解除")
        select_none_btn.clicked.connect(lambda: self.set_all(False))
        layout.addWidget(select_all_btn)
        layout.addWidget(select_none_btn)
        layout.addSpacing(16)
        self._checks_layout = QHBoxLayout()
        layout.addLayout(self._checks_layout)
        layout.addStretch(1)

    def rebuild(self, items, colors=None):
        """items: [(key, label), ...]。既存のチェック状態はキーで可能な限り
        維持し、新規キーは既定でチェック済み（＝表示）にする。

        colors: {key: 16進色, ...}（省略可）。渡すと、そのキーのチェックボックスの
        左に色スペースを添える（ワークフロー／チームのように色分けがある軸で、
        チャート本体の色と対応付けられるようにするため）。"""
        colors = colors or {}
        previous_checked = {k for k, cb in self._checks.items() if cb.isChecked()}
        previous_unchecked = {k for k, cb in self._checks.items() if not cb.isChecked()}
        while self._checks_layout.count():
            item = self._checks_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._checks = {}
        for key, label in items:
            checked = key not in previous_unchecked or key in previous_checked
            if key in colors:
                swatch = QLabel("　")
                swatch.setFixedWidth(14)
                swatch.setStyleSheet(f"background-color: {colors[key]}; border: 1px solid #0b0b0b;")
                self._checks_layout.addWidget(swatch)
            checkbox = QCheckBox(label)
            checkbox.setChecked(checked)  # connect前に設定し、構築時のstateChangedを発火させない
            checkbox.stateChanged.connect(lambda _state: self.changed.emit())
            self._checks_layout.addWidget(checkbox)
            self._checks[key] = checkbox

    def set_all(self, checked):
        # 一括変更中に途中でchanged経由のrebuildが走るとループ中のウィジェットが
        # 差し替わってしまうため、シグナルを止めてから最後にまとめて一度だけ発火する。
        for checkbox in self._checks.values():
            checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(False)
        self.changed.emit()

    def visible_keys(self):
        return {k for k, cb in self._checks.items() if cb.isChecked()}


class CollapsibleSection(QWidget):
    """折りたたみ可能なセクション。見出しをクリックすると中身の表示/非表示が
    切り替わる。複数の絞り込み（ChoiceFilterGroup等）が常時展開だと縦幅を
    取りすぎる画面で、まとめて1つに畳めるようにする。"""

    def __init__(self, title, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._toggle_btn = QToolButton()
        self._toggle_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle_btn.setArrowType(Qt.RightArrow)
        self._toggle_btn.setText(title)
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setChecked(False)
        self._toggle_btn.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self._toggle_btn.clicked.connect(self._on_toggled)
        outer.addWidget(self._toggle_btn)

        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content.setVisible(False)  # 既定は折りたたんだ状態
        outer.addWidget(self.content)

    def _on_toggled(self, checked):
        self._toggle_btn.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.content.setVisible(checked)
