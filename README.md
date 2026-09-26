# 图片管理器 / Image Manager

像手机相册一样，快速找到电脑里的任何图片。  
Find any image on your PC as easily as using your phone gallery.

## 初衷 / Why

桌面看图软件大多靠翻文件夹，找图麻烦。  
这个程序把手机相册的体验搬到 Windows：自动汇总、缩略图浏览、强大搜索。

## 亮点 / Highlights

- **自动扫描本机（极快）**  
  桌面 / 图片 / 下载 / 文档 + 其他本地磁盘。
- **搜祖先文件夹名，后代图片全出**  
  输入“英伟达录制”，该文件夹下所有层级的图片全部显示。多数看图软件做不到。
- **模糊搜索**  
  多关键词、忽略分隔符、字符子序列、拼写容错。输入“英录”也能找到“英伟达录制”。
- **最近更新排序**  
  可以按 `max(创建时间, 修改时间)` 排，刚下载、刚改过的图排前面。
- **手机相册式网格**  
  虚拟化缩略图，懒加载，几万张图片也不卡。
- **纯本地**  
  不上传、不联网、开源。

## 功能 / Features

- 支持 JPG / JPEG / PNG / WEBP图片格式
- 扫描中增量显示，可停止 / 重新扫描 / 选择文件夹
- 精确搜索 + 模糊搜索
- 排序：最近更新（默认）、创建时间、文件大小
- 单击看详情（文件名，格式，尺寸、大小、时间、完整路径，EXIF）
- 双击大图直接预览
- 打开所在文件夹并选中文件
- 悬停显示完整路径
- 缩略图大小可使用滑块调整（眼睛友好）

## 环境要求
- Python 3.10+（开发环境为 3.14）
- PySide6
- Pillow（用于读取 EXIF，可选；未安装时仅不显示 EXIF，不影响其他功能）

## 快速开始 / Quick Start

```bash
git clone https://github.com/你的用户名/image-manager.git
cd image-manager
py -m pip install PySide6 Pillow
py main.py

### 在 VS Code 中运行
1. 用 VS Code 打开本文件夹；
2. 安装 Python 扩展<img width="1402" height="832" alt="bandicam 2026-09-26 13-18-44-815" src="https://github.com/user-attachments/assets/76123c6c-0121-4e5f-ab93-e891ae9a9691" />
<img width="1402" height="832" alt="bandicam 2026-09-26 13-18-29-706" src="https://github.com/user-attachments/assets/c71cb74b-4a64-4e19-9957-efff59d5b7da" />
；
3. 选择解释器 `.venv`（已在 `.vscode/settings.json` 配置）；
4. 按 `F5` 运行。

## 可调参数（`main.py` 顶部常量区）
| 常量 | 说明 | 默认 |
|------|------|------|
| `MAX_DISPLAY` | 界面最多显示图片数 | 500000 |
| `MAX_CONCURRENT_LOADS` | 缩略图并发加载线程数 | 4 |
| `PIXMAP_CACHE_LIMIT_KB` | 缩略图缓存上限 | 200 MB |
| `THUMB_SIZE` | 缩略图边长（默认） | 160 |
| `THUMB_MIN` / `THUMB_MAX` / `THUMB_STEP` | 缩略图滑块范围与步长 | 64 / 320 / 16 |
| `INFO_PANEL_MARGIN` / `INFO_PANEL_SPACING` | 信息面板边距 / 间距 | 12 / 8 |
| `SEARCH_DEBOUNCE_MS` | 搜索输入防抖延迟 | 250 |
| `SKIP_DIR_NAMES` | 跳过的目录名集合 | 系统/缓存目录 |
