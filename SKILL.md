---
name: image-compress
license: MIT
description: "批量把图片压缩到用户指定的体积上限以内（--limit-mb 必填，无默认值），原地覆盖不留备份。先保尺寸再降质量（质量跌破 75 才缩一档），编码后端在 jpegli/mozjpeg/pillow 间自动选优，色度子采样按内容自适应，含二维码的图自动做保码校验（压缩后仍能解出原码），像素转正后清除 Orientation 标签。触发：压缩图片、图片太大、压到1MB、压到指定大小、批量压图、图片瘦身、减小图片体积、图片压缩、jpg 太大、照片太大、compress images、reduce image size、image compression、image optimizer。English keywords: batch image compression, resize images under size limit, compress photos to target size, image optimizer, batch resize with quality search, mozjpeg jpegli encoder."
---

# 图片批量压缩到指定体积

把一批图片压到指定体积上限以内，用于网页上传、微信传递、档案归档等有体积限制的场景。

## 什么时候用

用户提到以下任一情况：

- 图片太大、需要压到 1MB / 指定 KB 或 MB 以内
- 批量压缩图片、图片瘦身、减小图片体积
- 上传有体积限制，需要批量处理
- 电商面料图、色卡图、产品图需要归档或发给客户

不适用：需要转格式（PNG→WebP）、需要裁剪、需要加水印、需要生成图片。

## 安装

把 `SKILL.md` 放到你的技能目录，`scripts/img_shrink.py` 放到任意可执行位置：

```bash
mkdir -p ~/.agents/skills/image-compress
cp SKILL.md ~/.agents/skills/image-compress/
```

下文命令中的 `scripts/img_shrink.py` 指本仓库内脚本路径，按你放置的位置替换。

## 依赖

四个 Python 包，用 uv 可免安装直接运行：

```bash
uv run --with zxing-cpp --with pillow --with numpy --with imagecodecs \
  python scripts/img_shrink.py --help
```

- `pillow`：图片读写与 libjpeg-turbo 编码
- `numpy`：画质评估
- `zxing-cpp`：二维码检测（不用 `--no-qr` 时需要）
- `imagecodecs`：提供 mozjpeg 与 jpegli 两个额外编码后端（Windows x64 有 cp312 abi3 wheel，约 19MB，依赖 numpy>=2.1）。不装也能跑，只是 `--codec auto` 退化为只用 pillow。

## 标准流程

先预演，确认参数与保码情况，再正式执行：

```bash
# 1. 预演（不写盘），核对尺寸、质量、SSIM、检出码数
uv run --with zxing-cpp --with pillow --with numpy --with imagecodecs \
  python scripts/img_shrink.py --limit-mb 1 --dry-run "目录路径"

# 2. 正式执行（原地覆盖，不留备份）
uv run --with zxing-cpp --with pillow --with numpy --with imagecodecs \
  python scripts/img_shrink.py --limit-mb 1 "目录路径1" "目录路径2"
```

`--limit-mb` 是必填项，压缩到多少由使用者按目标场景决定，没有默认值。多个目录可一次传入。已达标（≤ 上限）的文件自动跳过，可安全重复执行。

## 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit-mb` | **必填** | 体积上限。无默认值，压缩到多少由使用者按目标场景定 |
| `--max-edge` | 0 | 长边硬上限，0=跟随原图。确有平台限制（如某站限 3000）再传 |
| `--min-edge` | 1500 | 最小长边，撑不住就报「无可行组合」 |
| `--qr-min-edge` | 1800 | 含二维码图的最小长边，保扫码可用 |
| `--codec` | auto | 编码后端：auto / pillow / mozjpeg / jpegli |
| `--no-qr` | 关 | 不做二维码检测（图里确定没码时更快） |
| `--dry-run` | 关 | 只报告不写盘 |
| `--report` | 空 | 结果写 JSON 的路径 |

## 编码器选择（2026-11-24 实测重写）

装了 `imagecodecs` 时 `--codec auto` 会依次试 jpegli / mozjpeg / pillow，按同尺寸下的 SSIM + 色度挑最优；没装则只用 pillow。

**三者在真实面料图上的实测（33 张真实面料图，2400px、600KB 预算、4:2:0）：**

| 后端 | 平均 SSIM | 强边保持率 |
|---|---|---|
| mozjpeg | 0.95814 | 0.8395 |
| pillow | 0.95730 | **0.8720** |
| jpegli | 0.95337 | 0.8577 |

结论：**没有单一赢家**。mozjpeg 的 SSIM 略高，pillow 的细节保留最好，jpegli 居中但最快（约 0.1s/次，pillow 约 0.3s，mozjpeg 约 2.1s）。真实面料纹理有大量自相似高频，jpegli 的自适应量化会把它们当噪声抹掉，所以在合成图上的领先优势消失了——这也是 auto 模式存在的理由，不要预设哪个更好。

两个容易踩的坑：

- `imagecodecs` 的 `subsampling` 要传字符串 `'444'/'422'/'420'`，传整数会报 `TypeError: 'int' object is not iterable`。
- jpegli 默认输出 baseline，必须显式传 `progressive=True`，实测再省约 7% 体积。

**横向对比编码器时必须固定同一个轴**（同体积比画质，或同画质比体积），不能直接比 quality 数字。

## 尺寸策略（实测重写）

策略是**先保尺寸，再优化质量**：从原尺寸起用预算能撑住的最高质量，只有质量跌破 75 才按 0.85 步长缩一档，最多缩到 `--min-edge`。

不要改成「扫描尺寸空间按画质选优」。试过，失败：任何把候选放大回原尺寸再比清晰度的指标，都会让最大尺寸几乎必胜（实测同一张图 3600px 强边保持率 0.918、1700px 只有 0.629），尺寸选择就失去意义。而且真实图上会选出「3600px + q13」这种块效应明显的结果。

同理，`--max-edge` 默认改成 0（跟随原图）。旧默认硬砍到 3000 会浪费预算：实测 3600px 的图缩到 3000 只用掉 59% 预算，放宽回原尺寸后画质更高且体积仍在上限内。

## 内置保护（不要绕过）

1. **长边断言**：缩放一律按 `max(宽,高)` 计算，落盘前后双重校验。横竖图混批不会缩错。
2. **二维码硬约束**：检测到二维码的图，压缩后必须仍能解出原码，否则降质量重试，降到头仍解不出就换更小尺寸重试。检测走「整图粗检 → 按位置裁切放大精检 → 区域盲扫兜底」三段式，只认 QRCode 格式，避免货号文字被误判成条码。
3. **原地写入带回滚**：用 `r+b` 覆盖后截断，失败会把原内容写回，不产生半截文件。
4. **Orientation 一律清除**：像素经 `exif_transpose` 转正后，EXIF 里的 Orientation 标签必须删掉。留着会让查看器再转一次；值为 0 这类非法值更会被各种软件解释成随机角度（实测踩到过，源图 orientation=0 一路带到了输出）。

## 交付前必须核验

工具跑完后，不要只看工具输出就交付，还要做这几步：

1. **体积与尺寸**：逐张回读，确认 ≤ 上限、长边符合预期
2. **目视裁切**：对含小字标签、色卡编号、二维码的图，裁切关键区域放大 2–3 倍确认可读
3. **二维码**：工具已验，但关键批次建议再用不同工具复核一次

核验用代码示例：

```python
from PIL import Image, ImageOps
import glob, os

for f in sorted(glob.glob('目录/*.jpg')):
    n = os.path.getsize(f)
    with Image.open(f) as im:
        im.load()
        d = ImageOps.exif_transpose(im)
        print(f'{os.path.basename(f)}: {n/1024/1024:.3f}MiB {d.size} '
              f'长边{max(d.size)} orient={im.getexif().get(0x0112)}')
```

## 已知取舍

- **视觉无损 ≠ 数学无损**：2600 万像素手机照片全分辨率压不进 1MB，必须降分辨率。工具按「先保尺寸再降质量」处理，质量跌破 75 才缩一档。
- **质量档因内容而异**：色卡整页、密集编号的小图在同样体积下只能取更低质量档，这是内容熵决定的，正常现象。
- **面积小的关键元素**（二维码、小字标签）要靠裁切核验，整图 SSIM 会掩盖局部劣化。
- **子采样按内容自适应**：先试 4:4:4 作基准，再试 4:2:0，若色度 PSNR 相对降幅超过 3dB 就弃用 4:2:0。实测面料/文字 4:2:0 只掉 0.8–2.0dB（可用），彩色文字页掉 6.8–15.5dB（必须 4:4:4）。这条判据用相对值而非绝对值，因为高饱和内容（色卡）本身色度信息多，4:4:4 也达不到高 dB。
- **质量下限 40 的惩罚**：低于 q40 的候选会被按比例折损评分。不加这条会在真实图上选出 q13 这类块效应明显的结果。

## 不留备份的口径

用户明确说「不留原图备份」时才原地覆盖。若用户没说明，先确认是保留原图（输出到新目录）还是覆盖。覆盖不可逆，原图无法恢复。

## 常见问题

**工具报「无可行组合」**：目标体积太小或最小长边设太高，放宽 `--min-edge` 或提高 `--limit-mb`。

**二维码报「码=0/0」但图里明明有**：先确认码是否太小。用 `--dry-run` 看检出数，若与肉眼不符，把码区裁切出来单独验证。

**无法覆盖文件（PermissionError）**：多为看图软件或资源管理器持有文件句柄，关闭预览后重试。Windows 下同一进程内未关闭的图片句柄也会锁文件。

**结果里 `codec` 字段始终是 pillow**：检查 imagecodecs 是否装了（`uv run --with imagecodecs python -c "import imagecodecs"`）。未装时 `--codec auto` 会静默退化为只用 pillow。

**单图耗时比预期长**：auto 模式在原尺寸上要跑 3 个后端 × 2 种子采样 × 每次 7 轮二分编码。真实面料图实测约 5–10 秒/张（mozjpeg 单次编码就要 2.1 秒）。只要压得准就用 `--codec pillow` 或 `--codec jpegli` 锁单后端，能快 2–3 倍。
