---
name: image-compress
license: MIT
description: "批量把图片压缩到指定体积上限（默认 1MB）以内，原地覆盖不留备份。分辨率按长边统一规划，质量用二分搜索取达标最高档，含二维码的图自动做保码校验（压缩后仍能解出原码），转正后清理非法 Orientation。触发：压缩图片、图片太大、压到1MB、批量压图、图片瘦身、减小图片体积、图片压缩、compress images、reduce image size、image compression。English keywords: batch image compression, resize images under size limit, compress photos to 1MB, image optimizer, batch resize with quality search."
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

三个 Python 包，用 uv 可免安装直接运行：

```bash
uv run --with zxing-cpp --with pillow --with numpy python scripts/img_shrink.py --help
```

- `pillow`：图片读写与 JPEG 编码
- `numpy`：画质评估
- `zxing-cpp`：二维码检测（不用 `--no-qr` 时需要）

可选：`pyjpegli`（Google jpegli 编码器，见下方编码器选择）。

## 标准流程

先预演，确认参数与保码情况，再正式执行：

```bash
# 1. 预演（不写盘），核对尺寸、质量、SSIM、检出码数
uv run --with zxing-cpp --with pillow --with numpy \
  python scripts/img_shrink.py --dry-run "目录路径"

# 2. 正式执行（原地覆盖，不留备份）
uv run --with zxing-cpp --with pillow --with numpy \
  python scripts/img_shrink.py "目录路径1" "目录路径2"
```

多个目录可一次传入。已达标（≤ 上限）的文件自动跳过，可安全重复执行。

## 关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit-mb` | 1.0 | 体积上限 |
| `--max-edge` | 3000 | 最大长边，避免无意义超采样 |
| `--min-edge` | 1500 | 最小长边 |
| `--qr-min-edge` | 1800 | 含二维码图的最小长边，保扫码可用 |
| `--codec` | pillow | 编码器，见下方编码器选择 |
| `--no-qr` | 关 | 不做二维码检测（图里确定没码时更快） |
| `--dry-run` | 关 | 只报告不写盘 |
| `--report` | 空 | 结果写 JSON 的路径 |

## 编码器选择（实测结论）

默认用 `pillow`（libjpeg-turbo 4:4:4 + 渐进式 + optimize）。

**不要因为「jpegli 是更新的编码器」就切过去。** 在 12 张真实面料图上按同体积对比，jpegli 画质略差：

| 指标 | jpegli vs pillow |
|---|---|
| 亮度 SSIM @1300px | jpegli 胜 0/12，中位差 −0.00133 |
| 色度 PSNR | jpegli 胜 0/12，中位差 −1.29dB |

原因是 jpegli 的质量参数刻度与 Pillow 不同，同样数字不对应同样画质。网上「jpegli 同画质省 20% 体积」的说法在手机照片语料上成立，但换成「塞进固定体积」的用法、且内容是面料色卡时，结论会反过来。

`--codec jpegli` 保留作备选：它的质量档范围更宽（q70–96），当 Pillow 在某些图上压不进目标体积时可以试。装了 pyjpegli 才生效，没装自动回落。

**横向对比编码器时必须固定同一个轴**（同体积比画质，或同画质比体积），不能直接比 quality 数字。

## 内置保护（不要绕过）

1. **长边断言**：缩放一律按 `max(宽,高)` 计算，落盘前后双重校验。横竖图混批不会缩错。
2. **二维码硬约束**：检测到二维码的图，压缩后必须仍能解出原码，否则降质量重试。检测走「整图粗检 → 按位置裁切放大精检 → 区域盲扫兜底」三段式，只认 QRCode 格式，避免货号文字被误判成条码。
3. **原地写入带回滚**：用 `r+b` 覆盖后截断，失败会把原内容写回，不产生半截文件。
4. **Orientation 修正**：转正后把非法值（0 等）字节级改为 1，不重编码像素。

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

- **视觉无损 ≠ 数学无损**：2600 万像素手机照片全分辨率压不进 1MB，必须降分辨率。工具会实测选 SSIM 最优组合。
- **质量档因内容而异**：色卡整页、密集编号的小图在同样体积下只能取更低质量档，这是内容熵决定的，正常现象。
- **面积小的关键元素**（二维码、小字标签）要靠裁切核验，整图 SSIM 会掩盖局部劣化。

## 不留备份的口径

用户明确说「不留原图备份」时才原地覆盖。若用户没说明，先确认是保留原图（输出到新目录）还是覆盖。覆盖不可逆，原图无法恢复。

## 常见问题

**工具报「无可行组合」**：目标体积太小或最小长边设太高，放宽 `--min-edge` 或提高 `--limit-mb`。

**二维码报「码=0/0」但图里明明有**：先确认码是否太小。用 `--dry-run` 看检出数，若与肉眼不符，把码区裁切出来单独验证。

**无法覆盖文件（PermissionError）**：多为看图软件或资源管理器持有文件句柄，关闭预览后重试。Windows 下同一进程内未关闭的图片句柄也会锁文件。
