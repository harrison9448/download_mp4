import ctypes
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel, QProgressBar, QFrame,
    QScrollArea, QFileDialog,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled


@dataclass
class DownloadTask:
    url: str
    task_index: int = 0
    status: str = "pending"
    progress: float = 0.0
    downloaded_file: str = ""
    error: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)
    pause_event: threading.Event = field(default_factory=threading.Event)
    started_at: float = 0.0


class DownloadSignals(QObject):
    progress_update = Signal(int, float, str)
    task_done = Signal(int, str, str)


class DownloadWorker:
    def __init__(self, task: DownloadTask, sem: threading.Semaphore,
                 save_dir: str, signals: DownloadSignals):
        self.task = task
        self.sem = sem
        self.save_dir = save_dir
        self.signals = signals

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _emit(self, progress: float, msg: str):
        self.signals.progress_update.emit(self.task.task_index, progress, msg)

    def _run(self):
        t = self.task
        while True:
            while t.pause_event.is_set():
                if t.cancel_event.is_set():
                    t.status = "cancelled"
                    self._emit(t.progress, "已取消")
                    self.signals.task_done.emit(t.task_index, "cancelled", "")
                    return
                time.sleep(0.5)

            if t.cancel_event.is_set():
                t.status = "cancelled"
                self._emit(t.progress, "已取消")
                self.signals.task_done.emit(t.task_index, "cancelled", "")
                return

            self.sem.acquire()

            if t.cancel_event.is_set():
                self.sem.release()
                t.status = "cancelled"
                self._emit(t.progress, "已取消")
                self.signals.task_done.emit(t.task_index, "cancelled", "")
                return

            try:
                t.status = "downloading"
                t.started_at = time.time()
                self._emit(t.progress, "正在下载...")

                save_path = self.save_dir or os.getcwd()
                outtmpl = os.path.join(save_path, f"{t.task_index:02d}.%(ext)s")

                downloaded = None

                def hook(d):
                    if t.cancel_event.is_set() or t.pause_event.is_set():
                        raise DownloadCancelled()
                    if d["status"] == "downloading":
                        total = d.get("total_bytes") or d.get("total_bytes_estimate")
                        if total:
                            p = d.get("downloaded_bytes", 0) / total
                            pct = int(p * 100)
                            t.progress = p
                            self._emit(p, f"下载中... {pct}%")
                    elif d["status"] == "finished":
                        t.progress = 1.0
                        self._emit(1.0, "下载完成，准备转换...")

                try:
                    with YoutubeDL({
                        "outtmpl": outtmpl,
                        "quiet": True,
                        "no_warnings": True,
                        "progress_hooks": [hook],
                    }) as ydl:
                        info = ydl.extract_info(t.url, download=True)
                        fname = ydl.prepare_filename(info)
                        if not os.path.exists(fname):
                            base = os.path.splitext(fname)[0]
                            parent = os.path.dirname(fname) or "."
                            for f in os.listdir(parent):
                                if f.startswith(os.path.basename(base)):
                                    fname = os.path.join(parent, f)
                                    break
                        downloaded = fname
                except DownloadCancelled:
                    if t.cancel_event.is_set():
                        t.status = "cancelled"
                        self._emit(t.progress, "已取消")
                        self.signals.task_done.emit(t.task_index, "cancelled", "")
                        return
                    elif t.pause_event.is_set():
                        t.status = "paused"
                        self._emit(t.progress, "已暂停")
                        self.signals.task_done.emit(t.task_index, "paused", "")
                        continue
                except Exception as e:
                    t.status = "failed"
                    t.error = str(e)
                    self._emit(0, f"下载失败: {e}")
                    self.signals.task_done.emit(t.task_index, "failed", str(e))
                    return

            finally:
                self.sem.release()

            break

        # --- Convert to MP4 ---
        ext = os.path.splitext(downloaded)[1].lower()
        if ext == ".mp4":
            t.status = "done"
            t.downloaded_file = downloaded
            self._emit(1.0, "完成！(已是 MP4)")
            self.signals.task_done.emit(t.task_index, "done", downloaded)
            return

        if t.cancel_event.is_set():
            t.status = "cancelled"
            self._emit(t.progress, "已取消")
            self.signals.task_done.emit(t.task_index, "cancelled", "")
            return

        t.status = "converting"
        t.started_at = time.time()
        self._emit(0.5, "正在转换为 MP4...")

        out_path = os.path.splitext(downloaded)[0] + ".mp4"
        try:
            proc = subprocess.Popen(
                [
                    os.path.join(os.path.dirname(__file__), "ffmpeg.exe"), "-y",
                    "-i", downloaded,
                    "-c:v", "libx264",
                    "-c:a", "aac",
                    "-progress", "pipe:1",
                    "-nostats",
                    out_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            while proc.poll() is None:
                if t.cancel_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    try:
                        os.remove(downloaded)
                    except OSError:
                        pass
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                    t.status = "cancelled"
                    self._emit(t.progress, "已取消")
                    self.signals.task_done.emit(t.task_index, "cancelled", "")
                    return
                time.sleep(0.5)

            _, stderr = proc.communicate()
            if proc.returncode == 0:
                try:
                    os.remove(downloaded)
                except OSError:
                    pass
                t.status = "done"
                t.downloaded_file = out_path
                self._emit(1.0, "完成！")
                self.signals.task_done.emit(t.task_index, "done", out_path)
            else:
                t.status = "failed"
                t.error = f"FFmpeg: {stderr[-200:] if stderr else 'unknown error'}"
                self._emit(0, f"转换失败: {t.error}")
                self.signals.task_done.emit(t.task_index, "failed", t.error)
        except FileNotFoundError:
            t.status = "failed"
            t.error = "FFmpeg 未安装"
            self._emit(0, "FFmpeg 未安装")
            self.signals.task_done.emit(t.task_index, "failed", "FFmpeg 未安装")


class TaskCard(QFrame):
    delete_clicked = Signal(int)
    pause_clicked = Signal(int)
    resume_clicked = Signal(int)
    retry_clicked = Signal(int)

    def __init__(self, task_index: int, url: str):
        super().__init__()
        self.task_index = task_index
        self.setFrameStyle(QFrame.StyledPanel)
        self.setStyleSheet(
            "TaskCard { border: 1px solid #ccc; border-radius: 8px; "
            "background: #fafafa; padding: 8px; }"
        )
        self._build(url)

    def _build(self, url: str):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(10, 8, 10, 8)

        url_display = url if len(url) <= 70 else url[:67] + "..."
        self.url_label = QLabel(url_display)
        self.url_label.setFont(QFont("", 10))
        self.url_label.setStyleSheet("font-weight: bold; border: none;")

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(18)

        self.status_label = QLabel("等待中...")
        self.status_label.setStyleSheet("color: #888; font-size: 11px; border: none;")

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        self.pause_btn = QPushButton("⏸ 暂停")
        self.pause_btn.setVisible(False)
        self.pause_btn.clicked.connect(lambda: self.pause_clicked.emit(self.task_index))

        self.resume_btn = QPushButton("▶ 继续")
        self.resume_btn.setVisible(False)
        self.resume_btn.clicked.connect(lambda: self.resume_clicked.emit(self.task_index))

        self.delete_btn = QPushButton("🗑 删除")
        self.delete_btn.clicked.connect(lambda: self.delete_clicked.emit(self.task_index))

        self.retry_btn = QPushButton("🔄 重新下载")
        self.retry_btn.setVisible(False)
        self.retry_btn.clicked.connect(lambda: self.retry_clicked.emit(self.task_index))

        for btn in (self.pause_btn, self.resume_btn, self.delete_btn, self.retry_btn):
            btn.setFixedHeight(28)
            btn.setStyleSheet("font-size: 11px;")

        btn_row.addWidget(self.status_label)
        btn_row.addStretch()
        btn_row.addWidget(self.pause_btn)
        btn_row.addWidget(self.resume_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addWidget(self.retry_btn)

        layout.addWidget(self.url_label)
        layout.addWidget(self.progress_bar)
        layout.addLayout(btn_row)

    def update_status(self, status: str, progress: float, msg: str):
        self.progress_bar.setValue(int(progress * 100))
        self.status_label.setText(msg)
        self.pause_btn.setVisible(status == "downloading")
        self.resume_btn.setVisible(status == "paused")
        self.delete_btn.setVisible(status in ("pending", "downloading", "paused", "failed"))
        self.retry_btn.setVisible(status == "failed")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.save_dir = ""
        self.tasks: list[DownloadTask] = []
        self.task_cards: dict[int, TaskCard] = {}
        self._running = False
        self._sem: threading.Semaphore | None = None
        self._signals = DownloadSignals()

        self._signals.progress_update.connect(self._on_progress)
        self._signals.task_done.connect(self._on_done)

        self._timer = QTimer()
        self._timer.timeout.connect(self._on_timer)
        self._timer.setInterval(2000)

        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("视频下载 & MP4 转换")
        self.resize(1000, 800)

        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - 1000) // 2, (screen.height() - 800) // 2)

        cw = QWidget()
        self.setCentralWidget(cw)
        root = QVBoxLayout(cw)
        root.setSpacing(10)
        root.setContentsMargins(20, 20, 20, 20)

        title = QLabel("视频下载 & MP4 转换工具")
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        root.addWidget(title)

        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        root.addWidget(div)

        self.url_input = QTextEdit()
        self.url_input.setPlaceholderText("请粘贴视频链接，每行一个")
        self.url_input.setMaximumHeight(120)
        self.url_input.setMinimumHeight(80)
        root.addWidget(self.url_input)

        row1 = QHBoxLayout()
        self.save_btn = QPushButton("📁 选择保存目录")
        self.save_btn.clicked.connect(self._pick_dir)
        self.save_label = QLabel("未选择（默认保存在当前目录）")
        self.save_label.setStyleSheet("color: #888; font-style: italic; font-size: 12px;")
        row1.addWidget(self.save_btn)
        row1.addWidget(self.save_label)
        row1.addStretch()
        root.addLayout(row1)

        row2 = QHBoxLayout()
        self.dl_btn = QPushButton("⬇ 开始下载全部")
        self.dl_btn.clicked.connect(self._start_all)
        self.overall = QLabel("")
        self.overall.setStyleSheet("font-size: 12px; font-weight: 500;")
        row2.addWidget(self.dl_btn)
        row2.addWidget(self.overall)
        row2.addStretch()
        root.addLayout(row2)

        div2 = QFrame()
        div2.setFrameShape(QFrame.HLine)
        root.addWidget(div2)

        ql = QLabel("下载队列:")
        ql.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        root.addWidget(ql)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.task_container = QWidget()
        self.task_layout = QVBoxLayout(self.task_container)
        self.task_layout.setSpacing(6)
        self.task_layout.setContentsMargins(5, 5, 5, 5)
        self.task_layout.addStretch()
        scroll.setWidget(self.task_container)
        root.addWidget(scroll, 1)

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择保存目录")
        if path:
            self.save_dir = path
            self.save_label.setText(path)

    def _update_overall(self):
        done = sum(1 for t in self.tasks if t.status == "done")
        failed = sum(1 for t in self.tasks if t.status == "failed")
        cancelled = sum(1 for t in self.tasks if t.status == "cancelled")
        active = max(len(self.tasks) - cancelled, 1)
        parts = [f"已完成: {done}/{active}"]
        if failed:
            parts.append(f"失败: {failed}")
        if cancelled:
            parts.append(f"已取消: {cancelled}")
        if active > 0 and done + failed == active:
            parts.append("— 全部结束")
        self.overall.setText("  ".join(parts))

    def _start_all(self):
        if self._running:
            return

        urls = [line.strip() for line in self.url_input.toPlainText().strip().splitlines() if line.strip()]
        if not urls:
            self.overall.setText("错误: 请输入至少一个视频 URL")
            return

        self.tasks.clear()
        self.task_cards.clear()
        while self.task_layout.count() > 1:
            item = self.task_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for idx, url in enumerate(urls, start=1):
            task = DownloadTask(url=url, task_index=idx)
            self.tasks.append(task)
            card = TaskCard(idx, url)
            card.delete_clicked.connect(self._delete_task)
            card.pause_clicked.connect(self._pause_task)
            card.resume_clicked.connect(self._resume_task)
            card.retry_clicked.connect(self._retry_task)
            self.task_cards[idx] = card
            self.task_layout.insertWidget(self.task_layout.count() - 1, card)

        self.dl_btn.setEnabled(False)
        self.overall.setText(f"共 {len(urls)} 个任务，开始下载...")

        self._running = True
        self._sem = threading.Semaphore(2)
        self._timer.start()

        for task in self.tasks:
            DownloadWorker(task, self._sem, self.save_dir, self._signals).start()

        threading.Thread(target=self._monitor, daemon=True).start()

    def _monitor(self):
        while self._running:
            if all(t.status in ("done", "failed", "cancelled") for t in self.tasks):
                break
            time.sleep(1)
        self._running = False
        QTimer.singleShot(0, self._timer.stop)
        self.dl_btn.setEnabled(True)
        QTimer.singleShot(0, self._update_overall)

    def _on_progress(self, task_index: int, progress: float, msg: str):
        for t in self.tasks:
            if t.task_index == task_index:
                t.progress = progress
                break
        card = self.task_cards.get(task_index)
        if card:
            task = next((t for t in self.tasks if t.task_index == task_index), None)
            if task:
                card.update_status(task.status, progress, msg)

    def _on_done(self, task_index: int, status: str, detail: str):
        for t in self.tasks:
            if t.task_index == task_index:
                t.status = status
                if status == "done":
                    t.downloaded_file = detail
                elif status == "failed":
                    t.error = detail
                break
        card = self.task_cards.get(task_index)
        if card:
            task = next((t for t in self.tasks if t.task_index == task_index), None)
            if task:
                card.update_status(status, task.progress, card.status_label.text())
        self._update_overall()

    def _on_timer(self):
        if not self._running:
            return
        now = time.time()
        for t in list(self.tasks):
            card = self.task_cards.get(t.task_index)
            if not card:
                continue
            try:
                if t.status == "downloading" and t.started_at > 0:
                    elapsed = int(now - t.started_at)
                    pct = int(t.progress * 100) if t.progress > 0 else 0
                    card.status_label.setText(f"下载中... {pct}% (已耗时 {elapsed}秒)")
                elif t.status == "converting" and t.started_at > 0:
                    elapsed = int(now - t.started_at)
                    card.status_label.setText(f"正在转换为 MP4... (已耗时 {elapsed}秒)")
                elif t.status == "paused":
                    card.status_label.setText("已暂停")
            except Exception:
                pass
        self._update_overall()

    def _delete_task(self, task_index: int):
        for t in self.tasks:
            if t.task_index == task_index:
                t.cancel_event.set()
                t.pause_event.clear()
                break
        card = self.task_cards.pop(task_index, None)
        if card:
            self.task_layout.removeWidget(card)
            card.deleteLater()
        self._update_overall()

    def _pause_task(self, task_index: int):
        for t in self.tasks:
            if t.task_index == task_index:
                t.pause_event.set()
                t.status = "paused"
                break
        card = self.task_cards.get(task_index)
        if card:
            task = next((t for t in self.tasks if t.task_index == task_index), None)
            if task:
                card.update_status("paused", task.progress, "已暂停")

    def _resume_task(self, task_index: int):
        for t in self.tasks:
            if t.task_index == task_index:
                t.pause_event.clear()
                t.status = "pending"
                break
        card = self.task_cards.get(task_index)
        if card:
            card.update_status("pending", 0, "等待恢复...")

    def _retry_task(self, task_index: int):
        task = next((t for t in self.tasks if t.task_index == task_index), None)
        if not task:
            return
        task.cancel_event.clear()
        task.pause_event.clear()
        task.progress = 0.0
        task.error = ""
        task.status = "pending"
        card = self.task_cards.get(task_index)
        if card:
            card.update_status("pending", 0, "等待中...")
        if self._sem:
            DownloadWorker(task, self._sem, self.save_dir, self._signals).start()

    def closeEvent(self, event):
        self._running = False
        self._timer.stop()
        for t in self.tasks:
            t.cancel_event.set()
            t.pause_event.clear()
        super().closeEvent(event)


_MUTEX_NAME = r'Global\download_mp4_single_instance'

def _check_single_instance():
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(0, '程序已在运行中，不能重复打开。', '提示', 0x30)
        return False
    return True

def main():
    if not _check_single_instance():
        sys.exit(0)
    app = QApplication([])
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
