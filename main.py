"""图片管理器 - Phase 2

功能：
    - 启动后自动在后台扫描本机图片（桌面 / 图片 / 下载 / 文档 + 其他本地磁盘）
    - 以虚拟化缩略图网格展示 JPG / PNG / WEBP 图片
    - 扫描过程中增量添加、懒加载缩略图、可停止 / 重新扫描 / 选择文件夹
    - 鼠标悬停显示完整文件路径

运行：
    python main.py
"""

import hashlib
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    Qt,
    QSize,
    QSortFilterProxyModel,
    QModelIndex,
    QUrl,
    QAbstractListModel,
    QThread,
    QThreadPool,
    QRunnable,
    Signal,
    QObject,
    QTimer,
    QRect,
)
from PySide6.QtGui import (
    QPixmap,
    QImage,
    QImageReader,
    QIcon,
    QColor,
    QPixmapCache,
    QDesktopServices,
    QFont,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QFileDialog,
    QListView,
    QToolBar,
    QLabel,
    QWidget,
    QFormLayout,
    QVBoxLayout,
    QDialog,
    QLineEdit,
    QSplitter,
    QSlider,
    QCheckBox,
    QComboBox,
    QStyledItemDelegate,
    QStyle,
)

# ============================================================================
# 常量集中配置
# ============================================================================

# 支持的图片扩展名（统一小写比较）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# 缩略图边长（像素，默认值；可被滑块调整）
THUMB_SIZE = 160

# 缩略图大小滑块范围与步长
THUMB_MIN = 64
THUMB_MAX = 320
THUMB_STEP = 16
# 单行文字高度（用于 gridSize 高度计算，关闭 wordWrap 后只占一行）
TEXT_HEIGHT = 22
# 图标与文字之间的额外间距（防止竖图顶到文件名）
ICON_TEXT_GAP = 16
# 图标与文字之间的间距
ICON_TEXT_GAP = 10

# 信息面板边距 / 间距（统一 UI 风格）
INFO_PANEL_MARGIN = 12
INFO_PANEL_SPACING = 8

# 搜索输入防抖毫秒数（避免每敲一个字都全量过滤大列表）
SEARCH_DEBOUNCE_MS = 250

# 模糊搜索时忽略的常见分隔符（匹配前统一去除，用 str.translate 快表）
SEARCH_SEPARATORS = str.maketrans("", "", " \t-_./\\，,;:()[]{}")

# 界面最多显示的图片数量（避免内存爆炸；超出部分仅计数不显示）
# 缩略图才是内存大头，而缩略图已是懒加载 + QPixmapCache 上限兜底，
# 因此这里放宽到 50 万，保证搜索能覆盖几乎所有图片。
MAX_DISPLAY = 500000

# 缩略图并发加载线程数（限制同时读取的原图数量）
MAX_CONCURRENT_LOADS = 4

# QPixmapCache 缓存上限（KB），缩略图内存由它兜底，超出自动淘汰
PIXMAP_CACHE_LIMIT_KB = 200 * 1024

# 扫描时每隔多少个目录发一次进度信号（降低信号频率）
PROGRESS_EMIT_EVERY = 25

# 需要跳过的目录名（统一小写匹配），避免卡死与权限错误
SKIP_DIR_NAMES = {
    "windows", "windows.old", "winnt",
    "program files", "program files (x86)", "programdata",
    "appdata", "application data",
    "$recycle.bin", "$windows.~bt", "$windows.~ws", "$sysreset",
    "system volume information", "recovery", "perflogs",
    "msocache", "config.msi",
    "node_modules", ".git", ".svn", ".hg", ".idea", ".vscode",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".tox",
}

# Windows 隐藏文件属性位（用于跳过隐藏目录）
FILE_ATTRIBUTE_HIDDEN = 0x2


# ============================================================================
# 工具函数
# ============================================================================

def is_image_filename(name: str) -> bool:
    """判断文件名是否为支持的图片格式。"""
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def should_skip_dir(path: Path) -> bool:
    """判断目录是否应被跳过（系统/缓存/隐藏/版本控制目录）。"""
    name = path.name
    if not name:
        return True
    if name.lower() in SKIP_DIR_NAMES:
        return True
    if name.startswith("."):
        return True
    try:
        st = path.stat()
    except OSError:
        # 无法读取（权限/不存在）直接跳过
        return True
    if hasattr(st, "st_file_attributes") and (st.st_file_attributes & FILE_ATTRIBUTE_HIDDEN):
        return True
    return False


def cache_key(path: str, size: int) -> str:
    """基于路径 + 缩略图尺寸生成缓存键（尺寸不同则缓存不同）。"""
    digest = hashlib.md5(f"{size}:{path}".encode("utf-8", "ignore")).hexdigest()
    return f"thumb:{digest}"


def _local_drives() -> list[Path]:
    """枚举当前存在的本地磁盘根目录（A:-Z:）。"""
    drives = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:\\")
        try:
            if root.is_dir():
                drives.append(root)
        except OSError:
            continue
    return drives


def default_scan_roots() -> list[Path]:
    """默认扫描范围：用户桌面/图片/下载/文档 + 除系统盘外的本地磁盘。"""
    roots: list[Path] = []
    home = Path.home()
    for name in ("Desktop", "Pictures", "Downloads", "Documents"):
        p = home / name
        if p.is_dir():
            roots.append(p)

    # 系统盘整盘扫描风险高（含 Windows 等），其用户目录已覆盖；其余磁盘整盘扫描
    system_drive = os.environ.get("SystemDrive", "C:").rstrip("\\").upper()
    for drive in _local_drives():
        if drive.drive.rstrip("\\").upper() != system_drive:
            roots.append(drive)
    return roots


def format_size(num_bytes: int) -> str:
    """把字节数格式化为易读大小（B/KB/MB/GB）。"""
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def format_time(timestamp: float | None) -> str:
    """把 Unix 时间戳格式化为 YYYY-MM-DD HH:MM:SS。"""
    if not timestamp:
        return "—"
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError, OverflowError):
        return "—"


def read_exif(path: str) -> dict[str, str]:
    """读取图片 EXIF 信息（相机、拍摄时间等），返回 {字段名: 值}。

    依赖 Pillow（可选）：未安装或读取失败时返回空 dict，不影响其他功能。
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS, IFD
    except ImportError:
        return {}

    wanted = {
        "Make", "Model", "LensModel", "Software",
        "DateTimeOriginal", "DateTime",
        "ExposureTime", "FNumber", "ISOSpeedRatings", "FocalLength",
    }
    try:
        with Image.open(path) as img:
            exif = img.getexif()
    except Exception:
        return {}
    if not exif:
        return {}

    info: dict[str, str] = {}
    for tag_id, value in exif.items():
        name = TAGS.get(tag_id)
        if name in wanted:
            info[name] = str(value)
    # 子 IFD（Exif）里的拍摄参数
    try:
        exif_sub = exif.get_ifd(IFD.Exif)
    except Exception:
        exif_sub = {}
    for tag_id, value in exif_sub.items():
        name = TAGS.get(tag_id)
        if name in wanted and name not in info:
            info[name] = str(value)
    return info


def open_in_folder(path: str):
    """打开图片所在文件夹，并尽量选中该文件。

    Windows 用 explorer /select；其他平台回退为打开文件夹。
    """
    p = Path(path)
    if sys.platform == "win32":
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return
        except Exception:
            pass  # 失败则回退到打开文件夹
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))


# ============================================================================
# 模糊搜索匹配（纯函数，无额外依赖）
# ============================================================================

def normalize_search_lo(s: str) -> str:
    """小写并去除常见分隔符，得到用于模糊搜索匹配的归一化字符串。"""
    return s.lower().translate(SEARCH_SEPARATORS)


def _is_subsequence(needle: str, haystack: str) -> bool:
    """needle 的字符是否按顺序出现在 haystack 中（子序列匹配）。"""
    if not needle:
        return True
    i = 0
    nl = len(needle)
    for ch in haystack:
        if ch == needle[i]:
            i += 1
            if i == nl:
                return True
    return False


def _fuzzy_substring_le1(token: str, text: str) -> bool:
    """token 是否作为连续窗口出现在 text 中，且至多 1 个字符不同（编辑距离 1 的替换容错）。

    仅用于短文件名，窗口按首字符预筛后逐字符比较，早退保证开销可控。
    """
    k = len(token)
    n = len(text)
    if k == 0 or k > n:
        return False
    first = token[0]
    for start in range(n - k + 1):
        if text[start] != first:
            continue
        diff = 0
        for j in range(k):
            if text[start + j] != token[j]:
                diff += 1
                if diff > 1:
                    break
        else:
            return True
    return False


def fuzzy_keyword_match(keyword: str, name_norm: str, path_norm: str) -> bool:
    """判断单个归一化关键词是否匹配目标。

    匹配优先级：连续子串 > 字符子序列 > 仅对文件名做编辑距离 1 容错（关键词长度>=3）。
    """
    if keyword in name_norm or keyword in path_norm:
        return True  # 连续子串（C 级速度，最快路径）
    if len(keyword) <= len(name_norm) and _is_subsequence(keyword, name_norm):
        return True
    if len(keyword) <= len(path_norm) and _is_subsequence(keyword, path_norm):
        return True
    if len(keyword) >= 3 and _fuzzy_substring_le1(keyword, name_norm):
        return True  # 拼写容错：只对短文件名，避免长路径的性能开销
    return False


# ============================================================================
# 后台扫描线程
# ============================================================================

class ScannerThread(QThread):
    """后台线程：遍历目录树，每发现一张图片就发 found 信号（增量）。"""

    found = Signal(str, float, int, float)  # path + mtime + size + ctime
    progress = Signal(str)                 # 当前扫描到的目录（用于进度显示）
    finished = Signal(int)                 # 扫描结束，携带已发现图片总数

    def __init__(self, roots: list[Path], parent=None):
        super().__init__(parent)
        self._roots = roots
        self._stop = threading.Event()

    def request_stop(self):
        """请求停止扫描（线程会在下次检查点退出）。"""
        self._stop.set()

    def run(self):
        count = 0
        dir_counter = 0
        for root in self._roots:
            if self._stop.is_set():
                break
            try:
                for dirpath, dirnames, filenames in os.walk(
                    root, topdown=True, onerror=lambda e: None, followlinks=False,
                ):
                    if self._stop.is_set():
                        break
                    # 剪枝：剔除系统/缓存/隐藏目录，避免深入
                    dirnames[:] = [
                        d for d in dirnames
                        if not should_skip_dir(Path(dirpath) / d)
                    ]
                    dir_counter += 1
                    if dir_counter % PROGRESS_EMIT_EVERY == 0:
                        self.progress.emit(dirpath)
                    for fn in filenames:
                        if self._stop.is_set():
                            break
                        if is_image_filename(fn):
                            full = os.path.join(dirpath, fn)
                            mtime, size, ctime = self._stat_file(full)
                            self.found.emit(full, mtime, size, ctime)
                            count += 1
            except Exception:
                # 权限不足 / 磁盘不可读等：跳过该根目录，继续其他
                continue
        self.finished.emit(count)

    @staticmethod
    def _stat_file(path: str):
        """获取文件修改时间、大小、创建时间（失败时用 0.0 / 0 / 0.0，保证排序仍可运行）。"""
        try:
            st = os.stat(path)
            return float(st.st_mtime), int(st.st_size), float(st.st_ctime)
        except OSError:
            return 0.0, 0, 0.0


# ============================================================================
# 图片数据模型（虚拟化，只存路径字符串）
# ============================================================================

class ImageModel(QAbstractListModel):
    """图片列表模型：只保存路径，缩略图按需懒加载。"""

    PathRole = Qt.UserRole + 1  # 完整路径
    MtimeRole = Qt.UserRole + 2  # 修改时间戳（float）
    SizeRole = Qt.UserRole + 3  # 文件大小（int 字节）
    OrderRole = Qt.UserRole + 4  # 原始加入顺序（用于默认排序回退）
    CtimeRole = Qt.UserRole + 5  # 创建时间戳（float）
    LatestRole = Qt.UserRole + 6  # 最近关联时刻 = max(ctime, mtime)

    def __init__(self, thumb_loader, parent=None):
        super().__init__(parent)
        self._paths: list[str] = []
        self._row_by_path: dict[str, int] = {}
        self._total_found = 0
        # 排序用平行列表（与 _paths 一一对应）
        self._mtimes: list[float] = []
        self._sizes: list[int] = []
        self._ctimes: list[float] = []
        self._latests: list[float] = []  # max(ctime, mtime)，即"最近与我产生关系"的时刻
        self._orders: list[int] = []
        # 小写文件名 / 完整路径缓存（与 _paths 一一对应，供过滤代理直接读取，避免每次 .lower()）
        self._names_lower: list[str] = []
        self._paths_lower: list[str] = []
        # 归一化缓存（小写 + 去分隔符），供模糊搜索直接读取，避免每行即时 normalize
        self._names_norm: list[str] = []
        self._paths_norm: list[str] = []
        self._thumb_loader = thumb_loader
        self._thumb_loader.loaded.connect(self._on_thumb_loaded)
        # 占位图标：未加载完成时显示（尺寸取当前缩略图尺寸）
        self._placeholder_icon = self._make_placeholder(self._thumb_loader.thumb_size)

    # ------------------------------------------------------------- Qt API
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._paths)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._paths)):
            return None
        path = self._paths[index.row()]

        if role == Qt.DisplayRole:
            return Path(path).name
        if role == self.PathRole or role == Qt.ToolTipRole:
            return path
        if role == self.MtimeRole:
            return self._mtimes[index.row()]
        if role == self.SizeRole:
            return self._sizes[index.row()]
        if role == self.OrderRole:
            return self._orders[index.row()]
        if role == self.CtimeRole:
            return self._ctimes[index.row()]
        if role == self.LatestRole:
            return self._latests[index.row()]
        if role == Qt.DecorationRole:
            key = cache_key(path, self._thumb_loader.thumb_size)
            pixmap = QPixmapCache.find(key)  # PySide6 返回 QPixmap 或 None
            if pixmap is not None and not pixmap.isNull():
                return QIcon(pixmap)
            # 未缓存：请求异步加载，先返回占位图标（懒加载）
            self._thumb_loader.request(path)
            return self._placeholder_icon
        return None

    # ------------------------------------------------------------- 数据操作
    def add_path(self, path: str, mtime: float, size: int, ctime: float) -> bool:
        """增量添加一张图片；超出 MAX_DISPLAY 时仅计数不入模型。

        用 _row_by_path 做去重，避免同一路径重复加入。
        mtime/size/ctime 用于排序；_latests = max(ctime, mtime) 作为"最近与我产生关系"的时刻。
        """
        self._total_found += 1
        if path in self._row_by_path:
            return False
        if len(self._paths) >= MAX_DISPLAY:
            return False
        row = len(self._paths)
        self.beginInsertRows(QModelIndex(), row, row)
        self._paths.append(path)
        self._row_by_path[path] = row
        # 排序用平行列表（与 _paths 对齐）
        self._mtimes.append(float(mtime))
        self._sizes.append(int(size))
        self._ctimes.append(float(ctime))
        self._latests.append(max(float(mtime), float(ctime)))
        self._orders.append(row)
        # 同步预缓存小写文件名/路径及归一化版本（与 _paths 对齐）
        name_lo = Path(path).name.lower()
        self._names_lower.append(name_lo)
        self._paths_lower.append(path.lower())
        self._names_norm.append(name_lo.translate(SEARCH_SEPARATORS))
        self._paths_norm.append(path.lower().translate(SEARCH_SEPARATORS))
        self.endInsertRows()
        return True

    def clear(self):
        """清空模型（保留 total_found 由外部重置或在这里重置）。"""
        self.beginResetModel()
        self._paths.clear()
        self._row_by_path.clear()
        self._mtimes.clear()
        self._sizes.clear()
        self._ctimes.clear()
        self._latests.clear()
        self._orders.clear()
        self._names_lower.clear()
        self._paths_lower.clear()
        self._names_norm.clear()
        self._paths_norm.clear()
        self._total_found = 0
        self.endResetModel()

    def sort_by(self, key: str = "default", reverse: bool = False):
        """按指定键重排所有平行列表并通知视图布局变化。

        key 支持：default（加入顺序）、mtime、latest（max(ctime,mtime)）、
        ctime、size、name。
        reverse=False 表示升序。
        """
        n = len(self._paths)
        if n == 0:
            return
        if key == "mtime":
            idx = sorted(range(n), key=lambda i: self._mtimes[i], reverse=reverse)
        elif key == "ctime":
            idx = sorted(range(n), key=lambda i: self._ctimes[i], reverse=reverse)
        elif key == "latest":
            idx = sorted(range(n), key=lambda i: self._latests[i], reverse=reverse)
        elif key == "size":
            idx = sorted(range(n), key=lambda i: self._sizes[i], reverse=reverse)
        elif key == "name":
            idx = sorted(range(n), key=lambda i: self._paths[i].lower(), reverse=reverse)
        else:  # default：保持扫描/加入顺序
            idx = sorted(range(n), key=lambda i: self._orders[i], reverse=reverse)

        self.layoutAboutToBeChanged.emit()
        self._paths = [self._paths[i] for i in idx]
        self._mtimes = [self._mtimes[i] for i in idx]
        self._sizes = [self._sizes[i] for i in idx]
        self._ctimes = [self._ctimes[i] for i in idx]
        self._latests = [self._latests[i] for i in idx]
        self._orders = [self._orders[i] for i in idx]
        self._names_lower = [self._names_lower[i] for i in idx]
        self._paths_lower = [self._paths_lower[i] for i in idx]
        self._names_norm = [self._names_norm[i] for i in idx]
        self._paths_norm = [self._paths_norm[i] for i in idx]
        # 重建反向索引
        self._row_by_path = {p: r for r, p in enumerate(self._paths)}
        self.layoutChanged.emit()

    @property
    def total_found(self) -> int:
        return self._total_found

    # ------------------------------------------------------------- 内部
    def _on_thumb_loaded(self, path: str, pixmap: QPixmap):
        row = self._row_by_path.get(path)
        if row is None:
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.DecorationRole])

    def set_thumb_size(self, size: int):
        """缩略图尺寸变化后：重建占位图标并触发整表重绘（重新懒加载）。"""
        self._placeholder_icon = self._make_placeholder(size)
        if self._paths:
            top = self.index(0, 0)
            bottom = self.index(len(self._paths) - 1, 0)
            self.dataChanged.emit(top, bottom, [Qt.DecorationRole])

    @staticmethod
    def _make_placeholder(size: int) -> QIcon:
        pix = QPixmap(size, size)
        pix.fill(QColor("#e0e0e0"))
        return QIcon(pix)


# ============================================================================
# 图片过滤代理（按文件名 / 路径实时过滤内存中的列表，不扫磁盘）
# ============================================================================

class ImageFilterProxy(QSortFilterProxyModel):
    """按文件名 / 完整路径过滤内存中的图片列表（不扫磁盘）。

    默认精确子串匹配；开启模糊搜索后：
        - 忽略大小写，按空格拆多个关键词，所有关键词必须命中（AND）；
        - 每个关键词可匹配文件名或完整路径；
        - 优先连续子串，否则允许字符子序列，并忽略常见分隔符；
        - 读取 ImageModel 预缓存的 _names_lower/_paths_lower/_names_norm/_paths_norm，
          避免每行即时 lower/normalize，几十万条也能快速过滤。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._filter_text = ""
        self._fuzzy = False
        self._raw_lower = ""      # 精确模式用的整串（小写）
        self._keywords = []       # 模糊模式：归一化关键词列表

    def set_fuzzy(self, enabled: bool):
        """切换模糊搜索开/关（保持当前文本，立即重过滤）。"""
        self._fuzzy = bool(enabled)
        self._recompute_keywords()
        self.invalidateFilter()

    def set_filter_text(self, text: str):
        """设置搜索文本（空串表示不过滤）。"""
        self._filter_text = (text or "").strip()
        self._recompute_keywords()
        self.invalidateFilter()  # 触发重新过滤

    def is_fuzzy(self) -> bool:
        return self._fuzzy

    def _recompute_keywords(self):
        """按当前文本与模糊开关预计算匹配所需的数据。"""
        text = self._filter_text
        self._raw_lower = text.lower()
        if not text:
            self._keywords = []
            return
        parts = [p for p in text.split() if p]
        # 小写 + 去分隔符，只算一次
        self._keywords = [normalize_search_lo(p) for p in parts]

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        src = self.sourceModel()
        if src is None:
            return False
        if not self._filter_text:
            return True
        try:
            name_lo = src._names_lower[source_row]
            path_lo = src._paths_lower[source_row]
        except IndexError:
            return False
        if not self._fuzzy:
            # 精确模式：整串子串匹配（保留原有默认行为）
            return self._raw_lower in name_lo or self._raw_lower in path_lo
        # 模糊模式：所有关键词都必须命中（AND）；有命中即提前接受
        name_norm = src._names_norm[source_row]
        path_norm = src._paths_norm[source_row]
        for kw in self._keywords:
            if not fuzzy_keyword_match(kw, name_norm, path_norm):
                return False
        return True


# ============================================================================
# 缩略图异步加载（限量并发 + QPixmapCache 缓存）
# ============================================================================

class _LoadSignals(QObject):
    """线程间传递缩略图加载结果的信号容器。"""
    done = Signal(str, QImage, int)  # 路径 + 解码图（QImage 线程安全） + 所用尺寸


class _ThumbWorker(QRunnable):
    """后台加载单张缩略图（只产出 QImage，QPixmap 由主线程转换）。"""

    def __init__(self, signals: _LoadSignals, path: str, size: int):
        super().__init__()
        self.signals = signals
        self.path = path
        self.size = size
        self.setAutoDelete(True)

    def run(self):
        reader = QImageReader(self.path)
        reader.setAutoTransform(True)  # 依据 EXIF 方向自动旋转
        orig = reader.size()
        if orig.isValid() and (orig.width() > self.size or orig.height() > self.size):
            reader.setScaledSize(orig.scaled(self.size, self.size, Qt.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            # 读取失败/损坏：发空图，主线程直接忽略
            self.signals.done.emit(self.path, QImage(), self.size)
        else:
            self.signals.done.emit(self.path, image, self.size)


class ThumbnailLoader(QObject):
    """缩略图加载器：限量并发，结果写入 QPixmapCache 并通知模型。"""

    loaded = Signal(str, QPixmap)  # 路径 + 缩略图

    def __init__(self, parent=None):
        super().__init__(parent)
        self.thumb_size = THUMB_SIZE  # 当前缩略图尺寸（可被滑块修改）
        self._signals = _LoadSignals()
        self._signals.done.connect(self._on_done)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_CONCURRENT_LOADS)
        self._in_flight: set[str] = set()

    def request(self, path: str):
        """请求加载某路径缩略图（去重：已缓存或已在加载中的直接返回）。"""
        if path in self._in_flight:
            return
        pixmap = QPixmapCache.find(cache_key(path, self.thumb_size))
        if pixmap is not None and not pixmap.isNull():
            return
        self._in_flight.add(path)
        self._pool.start(_ThumbWorker(self._signals, path, self.thumb_size))

    def _on_done(self, path: str, image: QImage, size: int):
        self._in_flight.discard(path)
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)  # 主线程转换（QPixmap 非线程安全）
        if pixmap.width() > size or pixmap.height() > size:
            pixmap = pixmap.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        QPixmapCache.insert(cache_key(path, size), pixmap)
        self.loaded.emit(path, pixmap)


# ============================================================================
# 图片详细信息异步读取（尺寸 / 文件信息 / EXIF，不阻塞 UI）
# ============================================================================

class _DetailSignals(QObject):
    """线程间传递详情读取结果的信号容器。"""
    done = Signal(str, dict)  # 路径 + 信息字典


class _DetailWorker(QRunnable):
    """后台读取单张图片的详细信息（尺寸、文件信息、EXIF）。"""

    def __init__(self, signals: _DetailSignals, path: str):
        super().__init__()
        self.signals = signals
        self.path = path
        self.setAutoDelete(True)

    def run(self):
        p = Path(self.path)
        info = {"path": self.path, "name": p.name}
        info["suffix"] = p.suffix.lower().lstrip(".") or "未知"

        # 图片尺寸（QImageReader.size 只读文件头，不解码全图）
        reader = QImageReader(self.path)
        size = reader.size()
        info["width"] = size.width() if size.isValid() else 0
        info["height"] = size.height() if size.isValid() else 0

        # 文件大小 / 修改时间 / 创建时间
        try:
            st = os.stat(self.path)
            info["size"] = st.st_size
            info["mtime"] = st.st_mtime
            info["ctime"] = st.st_ctime
        except OSError:
            info["size"] = 0
            info["mtime"] = None
            info["ctime"] = None

        # EXIF（可选，读取失败返回空）
        info["exif"] = read_exif(self.path)

        self.signals.done.emit(self.path, info)


class DetailLoader(QObject):
    """图片详情加载器：后台读取 + 结果缓存，避免重复读取。"""

    loaded = Signal(str, dict)  # 路径 + 信息字典

    def __init__(self, parent=None):
        super().__init__(parent)
        self._signals = _DetailSignals()
        self._signals.done.connect(self._on_done)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)  # 详情读取并发量低即可
        self._in_flight: set[str] = set()
        self._cache: dict[str, dict] = {}

    def request(self, path: str):
        """请求读取某路径详情（缓存命中直接回发，加载中则去重）。"""
        if path in self._cache:
            self.loaded.emit(path, self._cache[path])
            return
        if path in self._in_flight:
            return
        self._in_flight.add(path)
        self._pool.start(_DetailWorker(self._signals, path))

    def clear_cache(self):
        self._cache.clear()

    def _on_done(self, path: str, info: dict):
        self._in_flight.discard(path)
        self._cache[path] = info
        self.loaded.emit(path, info)


# ============================================================================
# 大图预览对话框（双击打开）
# ============================================================================

class PreviewDialog(QDialog):
    """双击缩略图后弹出的大图预览，缩放自适应窗口。"""

    MAX_PREVIEW_EDGE = 2048  # 解码上限边长，避免超大图占用内存

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(Path(path).name)
        self.resize(900, 700)
        self._original: QPixmap | None = None

        layout = QVBoxLayout(self)
        self._label = QLabel("加载中…", self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setMinimumSize(400, 300)
        layout.addWidget(self._label)

        self._load(path)

    def _load(self, path: str):
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (
            size.width() > self.MAX_PREVIEW_EDGE or size.height() > self.MAX_PREVIEW_EDGE
        ):
            reader.setScaledSize(size.scaled(
                self.MAX_PREVIEW_EDGE, self.MAX_PREVIEW_EDGE, Qt.KeepAspectRatio,
            ))
        image = reader.read()
        if image.isNull():
            self._label.setText("无法加载图片")
            return
        self._original = QPixmap.fromImage(image)

    def showEvent(self, event):
        super().showEvent(event)
        self._update_preview()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_preview()

    def _update_preview(self):
        if self._original is None:
            return
        avail = self._label.size()
        if avail.width() <= 0 or avail.height() <= 0:
            return
        scaled = self._original.scaled(avail, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._label.setPixmap(scaled)


# ============================================================================
# 图片视图（网格 + 鼠标移出信号）
# ============================================================================

class ImageView(QListView):
    """图片网格视图：鼠标移出时发出信号，用于恢复状态栏。"""

    left = Signal()

    def leaveEvent(self, event):
        self.left.emit()
        super().leaveEvent(event)

# ============================================================================
# 自定义缩略图 delegate（固定布局：图标在上，文件名在下，竖图不会压住文字）
# ============================================================================

class ThumbDelegate(QStyledItemDelegate):
    """自绘缩略图和单行文件名，布局完全可控，避免 Qt 默认 delegate 的竖图遮挡问题。"""

    PADDING = 8       # 图标区域上下留白
    TEXT_GAP = 6      # 图标底部到文字顶部的间距
    TEXT_MARGIN = 4   # 文字左右留白

    def __init__(self, icon_size: int = THUMB_SIZE, parent=None):
        super().__init__(parent)
        self._icon_size = int(icon_size)

    def set_icon_size(self, size: int):
        self._icon_size = int(size)

    def icon_size(self) -> int:
        return self._icon_size

    def sizeHint(self, option, index):
        """每个格子的固定尺寸，view 会用它来决定 item 大小。"""
        return QSize(
            self._icon_size + 40,
            self.PADDING + self._icon_size + self.TEXT_GAP + TEXT_HEIGHT + self.PADDING,
        )

    def paint(self, painter, option, index):
        painter.save()

        rect = option.rect

        # 选中 / 悬停背景
        if option.state & QStyle.State_Selected:
            painter.fillRect(rect, QColor("#43434349"))
        elif option.state & QStyle.State_MouseOver:
            painter.fillRect(rect, QColor("#45454523"))

        # 图标区域：固定在 item 顶部，尺寸 = _icon_size
        icon_left = rect.x() + (rect.width() - self._icon_size) // 2
        icon_top = rect.y() + self.PADDING
        icon_rect = QRect(icon_left, icon_top, self._icon_size, self._icon_size)

        icon = index.data(Qt.DecorationRole)
        if icon is not None:
            pixmap = icon.pixmap(self._icon_size, self._icon_size)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    self._icon_size, self._icon_size,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation,
                )
                # 在 icon_rect 内居中（横图上下留白，竖图左右留白）
                x = icon_rect.x() + (icon_rect.width() - scaled.width()) // 2
                y = icon_rect.y() + (icon_rect.height() - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)

        # 文字区域：紧贴图标下方，固定高度
        text_top = icon_rect.bottom() + self.TEXT_GAP + 1
        text_rect = QRect(
            rect.x() + self.TEXT_MARGIN,
            text_top,
            rect.width() - self.TEXT_MARGIN * 2,
            TEXT_HEIGHT,
        )

        text = index.data(Qt.DisplayRole) or ""
        fm = painter.fontMetrics()
        elided = fm.elidedText(text, Qt.ElideMiddle, text_rect.width())

        # 文件名统一白色
        painter.setPen(QColor("#ffffff"))
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignVCenter, elided)

        painter.restore()

# ============================================================================
# 主窗口
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("图片管理器 - Phase 2")
        self.resize(1400, 800)   # 工具栏按钮变多，加大初始尺寸

        self._scanner: ScannerThread | None = None
        self._scan_generation = 0
        self._scanning = False
        self._scan_path = ""
        self._hover_path: str | None = None
        self._current_path: str | None = None
        self._search_text = ""

        self._thumb_loader = ThumbnailLoader(self)
        self.model = ImageModel(self._thumb_loader, self)
        self.proxy = ImageFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self._detail_loader = DetailLoader(self)
        self._detail_loader.loaded.connect(self._on_detail_loaded)

        self._build_toolbar()
        self._build_view()  # 内部构建 QSplitter（视图 + 信息面板）
        self._build_statusbar()
        self.setMinimumWidth(1200)   # 防止用户把窗口拖太窄，导致工具栏被裁

        # UI 状态定时刷新（降低高频信号的标签重绘压力）
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_labels)
        self._refresh_timer.start(150)

        # 搜索输入防抖：停止输入 250ms 后才真正过滤大列表
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._search_debounce.timeout.connect(self._apply_search)

        # 启动后自动扫描本机图片
        QTimer.singleShot(0, self.rescan)

    # ------------------------------------------------------------------ UI
    def _build_toolbar(self):
        toolbar = QToolBar("工具栏", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)

        self.choose_action = toolbar.addAction("选择文件夹")
        self.choose_action.triggered.connect(self.choose_folder)

        self.rescan_action = toolbar.addAction("重新扫描")
        self.rescan_action.triggered.connect(self.rescan)

        self.stop_action = toolbar.addAction("停止扫描")
        self.stop_action.triggered.connect(self.stop_scan)

        toolbar.addSeparator()

        self.open_folder_action = toolbar.addAction("打开所在文件夹")
        self.open_folder_action.triggered.connect(self._open_containing_folder)
        self.open_folder_action.setEnabled(False)  # 未选中时禁用

        toolbar.addSeparator()

        # 排序下拉框
        toolbar.addWidget(QLabel(" 排序："))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems([
            "最近更新（新→旧）",   # 默认
            "最近更新（旧→新）",
            "创建时间（新→旧）",
            "创建时间（旧→新）",
            "文件大小（大→小）",
            "文件大小（小→大）",
        ])
        self.sort_combo.setCurrentIndex(0)  # 默认选中"最近更新（新→旧）"
        self.sort_combo.setFixedWidth(160)
        self.sort_combo.setEnabled(True)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        toolbar.addWidget(self.sort_combo)

        toolbar.addSeparator()

        # 搜索框：实时过滤内存中的图片列表（不扫磁盘）
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索文件名或路径…")
        self.search_edit.setClearButtonEnabled(True)  # 内置清空按钮
        self.search_edit.setFixedWidth(200)
        self.search_edit.textChanged.connect(self._on_search_changed)
        toolbar.addWidget(self.search_edit)

        # 模糊搜索开关（默认不勾选，保留精确子串搜索）
        self.fuzzy_checkbox = QCheckBox("模糊搜索")
        self.fuzzy_checkbox.setToolTip("拆分多个关键词、忽略分隔符与大小写、允许字符子序列与拼写容错")
        self.fuzzy_checkbox.toggled.connect(self._on_fuzzy_toggled)
        toolbar.addWidget(self.fuzzy_checkbox)

        toolbar.addSeparator()

        # 缩略图大小滑块
        toolbar.addWidget(QLabel(" 缩略图："))
        self.thumb_slider = QSlider(Qt.Horizontal)
        self.thumb_slider.setRange(THUMB_MIN, THUMB_MAX)
        self.thumb_slider.setValue(THUMB_SIZE)
        self.thumb_slider.setSingleStep(THUMB_STEP)
        self.thumb_slider.setPageStep(THUMB_STEP)
        self.thumb_slider.setFixedWidth(120)
        self.thumb_slider.valueChanged.connect(self._on_thumb_size_changed)
        toolbar.addWidget(self.thumb_slider)
        self.thumb_size_label = QLabel(f"{THUMB_SIZE}px")
        self.thumb_size_label.setFixedWidth(44)
        toolbar.addWidget(self.thumb_size_label)

    def _build_view(self):
        self.view = ImageView(self)
        self.view.setModel(self.proxy)  # 视图绑定代理模型，支持搜索过滤
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setSpacing(10)
        self.view.setUniformItemSizes(True)
        # 用自定义 delegate，item 尺寸由 delegate.sizeHint 决定，不再 setGridSize
        self._thumb_delegate = ThumbDelegate(THUMB_SIZE, self.view)
        self.view.setItemDelegate(self._thumb_delegate)
        self.view.setWordWrap(False)              # 关闭换行，避免文字布局慢
        self.view.setTextElideMode(Qt.ElideMiddle)  # 长文件名中间省略，保留后缀
        self.view.setLayoutMode(QListView.Batched)  # 批量布局，减少一次性压力
        self.view.setBatchSize(100)
        self.view.viewport().setAutoFillBackground(False)  # 避免 viewport 白底
        # entered 信号需开启鼠标追踪
        self.view.setMouseTracking(True)
        self.view.viewport().setMouseTracking(True)
        self.view.entered.connect(self._on_item_hovered)
        self.view.left.connect(self._on_view_leave)
        # 单击显示详情 / 双击预览
        self.view.clicked.connect(self._on_item_clicked)
        self.view.doubleClicked.connect(self._on_item_double_clicked)

        # 信息面板（普通 widget，放入 QSplitter 可拖动调整宽度）
        self.info_widget = self._build_info_widget()

        # QSplitter：缩略图区域 + 信息面板，可拖动调整
        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(self.info_widget)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([700, 260])
        self.setCentralWidget(self.splitter)

    def _build_info_widget(self):
        """信息面板 widget（放入 QSplitter，可拖动调整宽度）。"""
        panel = QWidget()
        panel.setMinimumWidth(220)
        form = QFormLayout(panel)
        form.setContentsMargins(
            INFO_PANEL_MARGIN, INFO_PANEL_MARGIN,
            INFO_PANEL_MARGIN, INFO_PANEL_MARGIN,
        )
        form.setSpacing(INFO_PANEL_SPACING)
        self.info_labels: dict[str, QLabel] = {}
        for key, title in [
            ("name", "文件名"),
            ("format", "格式"),
            ("dimensions", "尺寸"),
            ("size", "文件大小"),
            ("mtime", "修改时间"),
            ("ctime", "创建时间"),
            ("path", "完整路径"),
            ("exif", "EXIF"),
        ]:
            label = QLabel("—")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setWordWrap(True)
            form.addRow(f"{title}：", label)
            self.info_labels[key] = label
        return panel

    def _build_statusbar(self):
        self.total_label = QLabel("共 0 张图片")
        self.selected_label = QLabel("")
        self.scan_label = QLabel("就绪")
        self.statusBar().addWidget(self.total_label)
        self.statusBar().addWidget(self.selected_label, 1)
        self.statusBar().addPermanentWidget(self.scan_label)

    # ------------------------------------------------------------------ 扫描控制
    def rescan(self):
        """重新扫描默认范围。"""
        self.start_scan(default_scan_roots())

    def choose_folder(self):
        """选择单个文件夹进行扫描。"""
        folder = QFileDialog.getExistingDirectory(self, "选择图片文件夹")
        if not folder:
            return
        self.start_scan([Path(folder)])

    def start_scan(self, roots: list[Path]):
        """启动一次新的扫描（会先停止旧扫描并清空模型）。"""
        self._stop_active_scanner()

        self.model.clear()
        QPixmapCache.clear()
        self._detail_loader.clear_cache()
        self._current_path = None
        self.open_folder_action.setEnabled(False)
        self._clear_info_panel()
        self._scan_path = ""
        self._hover_path = None

        self._scan_generation += 1
        gen = self._scan_generation
        self._scanning = True
        self._update_buttons()

        scanner = ScannerThread(roots, self)
        scanner.found.connect(lambda p, m, s, c, g=gen: self._on_found(p, m, s, c, g))
        scanner.progress.connect(lambda p, g=gen: self._on_progress(p, g))
        scanner.finished.connect(lambda c, g=gen: self._on_scan_finished(c, g))
        scanner.finished.connect(scanner.deleteLater)
        self._scanner = scanner
        scanner.start()

    def stop_scan(self):
        """请求停止当前扫描（线程会在检查点安全退出）。"""
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.request_stop()
            self._scanning = False
            self._update_buttons()

    def _stop_active_scanner(self):
        """停止并清理当前扫描线程（若有）。

        先请求停止并等待线程安全退出，再安排删除，避免
        "QThread: Destroyed while thread is still running" 崩溃。
        """
        if self._scanner is not None:
            old = self._scanner
            self._scanner = None
            if old.isRunning():
                old.request_stop()
                old.wait(5000)  # 扫描循环高频检查停止标志，通常毫秒级退出
            old.deleteLater()

    def closeEvent(self, event):
        """关闭窗口前停止扫描线程，避免线程仍在运行时销毁导致崩溃。"""
        self._refresh_timer.stop()
        self._stop_active_scanner()
        super().closeEvent(event)

    def _on_sort_changed(self, index: int):
        """排序下拉框变化：调用模型重排（只在扫描未进行时可选）。"""
        # 下拉选项 -> (排序键, 是否降序)
        spec = [
            ("latest", True),   # 最近更新（新→旧）
            ("latest", False),  # 最近更新（旧→新）
            ("ctime", True),    # 创建时间（新→旧）
            ("ctime", False),   # 创建时间（旧→新）
            ("size", True),     # 文件大小（大→小）
            ("size", False),    # 文件大小（小→大）
        ]
        key, reverse = spec[index]
        self.model.sort_by(key, reverse)

    # ------------------------------------------------------------------ 扫描回调
    def _on_found(self, path: str, mtime: float, size: int, ctime: float, gen: int):
        if gen != self._scan_generation:
            return
        self.model.add_path(path, mtime, size, ctime)  # 增量加入模型，附带排序用属性

    def _on_progress(self, path: str, gen: int):
        if gen != self._scan_generation:
            return
        self._scan_path = path

    def _on_scan_finished(self, count: int, gen: int):
        if gen != self._scan_generation:
            return
        self._scanning = False
        self._update_buttons()
        # 扫描结束、列表完整后，按当前下拉框选择应用一次排序（默认最近更新新→旧）
        self._on_sort_changed(self.sort_combo.currentIndex())
        self._refresh_labels()

    # ------------------------------------------------------------------ 悬停
    def _on_item_hovered(self, index: QModelIndex):
        path = index.data(ImageModel.PathRole)
        if path:
            self._hover_path = path
            self._refresh_labels()

    def _on_view_leave(self):
        self._hover_path = None
        self._refresh_labels()

    # ------------------------------------------------------------------ 选中详情
    def _on_item_clicked(self, index: QModelIndex):
        """单击缩略图：在信息面板显示详情（尺寸/EXIF 后台异步读取）。"""
        if not index.isValid():
            self._current_path = None
            self.open_folder_action.setEnabled(False)
            self._clear_info_panel()
            self._refresh_labels()
            return
        path = index.data(ImageModel.PathRole)
        if not path:
            return
        self._current_path = path
        self.open_folder_action.setEnabled(True)
        self._show_partial_info(path)
        self._detail_loader.request(path)
        self._refresh_labels()

    def _on_item_double_clicked(self, index: QModelIndex):
        """双击缩略图：弹出大图预览。"""
        if not index.isValid():
            return
        path = index.data(ImageModel.PathRole)
        if not path:
            return
        dlg = PreviewDialog(path, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.exec()

    def _show_partial_info(self, path: str):
        """立即显示已知字段，其余显示"读取中…"。"""
        p = Path(path)
        self.info_labels["name"].setText(p.name)
        self.info_labels["format"].setText(p.suffix.lower().lstrip(".") or "未知")
        self.info_labels["path"].setText(path)
        for key in ("dimensions", "size", "mtime", "ctime", "exif"):
            self.info_labels[key].setText("读取中…")

    def _on_detail_loaded(self, path: str, info: dict):
        if path != self._current_path:
            return  # 已切换到别的图片，丢弃过期结果
        width = info.get("width", 0)
        height = info.get("height", 0)
        self.info_labels["dimensions"].setText(
            f"{width} × {height}" if width and height else "未知",
        )
        self.info_labels["size"].setText(format_size(info.get("size", 0)))
        self.info_labels["mtime"].setText(format_time(info.get("mtime")))
        self.info_labels["ctime"].setText(format_time(info.get("ctime")))
        self.info_labels["exif"].setText(self._format_exif(info.get("exif", {})))

    def _clear_info_panel(self):
        for label in self.info_labels.values():
            label.setText("—")

    @staticmethod
    def _format_exif(exif: dict) -> str:
        if not exif:
            return "无 EXIF 信息"
        parts = []
        make = str(exif.get("Make", "")).strip()
        model = str(exif.get("Model", "")).strip()
        if make or model:
            parts.append(f"相机：{' '.join(x for x in (make, model) if x)}")
        dt = exif.get("DateTimeOriginal") or exif.get("DateTime")
        if dt:
            parts.append(f"拍摄时间：{dt}")
        params = []
        for key, label in [
            ("ExposureTime", "曝光"), ("FNumber", "光圈"),
            ("ISOSpeedRatings", "ISO"), ("FocalLength", "焦距"),
        ]:
            if key in exif:
                params.append(f"{label} {exif[key]}")
        if params:
            parts.append(" · ".join(params))
        return "\n".join(parts) if parts else "无 EXIF 信息"

    def _open_containing_folder(self):
        """打开当前选中图片所在文件夹并选中该文件。"""
        if self._current_path:
            open_in_folder(self._current_path)

    # ------------------------------------------------------------------ 搜索
    def _on_search_changed(self, text: str):
        """搜索框内容变化：只过滤内存列表，不重新扫描磁盘。"""
        self._search_text = text.strip()
        if not self._search_text:
            # 清空时立即恢复全部，无需防抖
            self._search_debounce.stop()
            self._apply_search()
        else:
            # 有输入：防抖，停止输入 SEARCH_DEBOUNCE_MS 后才过滤
            self._search_debounce.start()

    def _on_fuzzy_toggled(self, enabled: bool):
        """模糊搜索开关：立即用当前文本重过滤。"""
        checked = self.fuzzy_checkbox.isChecked()
        self.proxy.set_fuzzy(checked)
        self.proxy.set_filter_text(self._search_text)
        self._refresh_labels()

    def _apply_search(self):
        """防抖生效：按当前文本 + 模糊开关执行过滤。"""
        self.proxy.set_fuzzy(self.fuzzy_checkbox.isChecked())
        self.proxy.set_filter_text(self._search_text)
        self._refresh_labels()

    # ------------------------------------------------------------------ UI 刷新
    def _refresh_labels(self):
        self._update_total_label()
        self._update_selected_label()
        self._update_scan_label()

    def _update_total_label(self):
        """状态栏左：图片总数（搜索时显示 显示/总数）。"""
        total = self.model.total_found
        shown = self.proxy.rowCount()
        if self._search_text:
            text = f"显示 {shown} / 共 {total} 张"
        elif total > MAX_DISPLAY:
            text = f"共 {total} 张图片（仅显示前 {MAX_DISPLAY} 张）"
        else:
            text = f"共 {total} 张图片"
        self.total_label.setText(text)

    def _update_selected_label(self):
        """状态栏中：当前选中文件（悬停时优先显示悬停路径）。"""
        if self._hover_path:
            self.selected_label.setText(self._elide(self._hover_path))
        elif self._current_path:
            self.selected_label.setText(self._elide(self._current_path))
        else:
            self.selected_label.setText("")

    def _update_scan_label(self):
        """状态栏右：扫描状态。"""
        if self._scanning:
            if self._scan_path:
                self.scan_label.setText("扫描中：" + self._elide(self._scan_path, 220))
            else:
                self.scan_label.setText("扫描中…")
        else:
            self.scan_label.setText("就绪")

    def _update_buttons(self):
        self.stop_action.setEnabled(self._scanning)
        # 扫描进行中禁用排序下拉框，避免增量加入与重排互相干扰
        self.sort_combo.setEnabled(not self._scanning)

    def _elide(self, text: str, width: int = 460) -> str:
        metrics = self.selected_label.fontMetrics()
        return metrics.elidedText(text, Qt.ElideMiddle, width)

    # ------------------------------------------------------------------ UI 交互
    def _on_thumb_size_changed(self, value: int):
        """缩略图大小滑块变化：更新 delegate 尺寸并触发重新布局。"""
        size = int(value)
        self.thumb_size_label.setText(f"{size}px")
        self._thumb_loader.thumb_size = size
        QPixmapCache.clear()  # 旧尺寸缩略图作废
        self.model.set_thumb_size(size)
        self._thumb_delegate.set_icon_size(size)
        # 让 view 重新询问所有 item 的 sizeHint（delegate 尺寸变了）
        self.view.scheduleDelayedItemsLayout()
        self.view.viewport().update()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("图片管理器")
    app.setFont(QFont("Microsoft YaHei UI", 9))  # 统一基础字体
    QPixmapCache.setCacheLimit(PIXMAP_CACHE_LIMIT_KB)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
