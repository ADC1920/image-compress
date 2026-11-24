# image-compress

把批量图片压缩到用户指定的体积上限以内的命令行工具，也可作为智能体技能使用。

面向需要「图片必须小于某个体积」的场景：表单上传限制、IM 传递、档案归档、素材整理。

体积上限通过 `--limit-mb` 指定，**没有默认值**——压到多少取决于具体场景的余量要求，由使用者决定。

## 特点

- **先保尺寸再降质量**：从原尺寸起，用预算能撑住的最高质量；质量跌破 75 才按 0.85 缩一档。保住分辨率，不靠无脑降采样换体积。
- **三后端自动选优**：装了 `imagecodecs` 时在 jpegli / mozjpeg / pillow 之间实测比较，按同尺寸的 SSIM + 色度挑最优，不预设哪个赢。
- **色度子采样按内容自适应**：先试 4:4:4 作基准，再试 4:2:0；若色度 PSNR 相对降幅超过 3dB 就弃用 4:2:0。彩色细线、小字不会因为省体积而糊掉。
- **质量下限惩罚**：低于 q40 的候选按比例折损评分，避免出现「大尺寸 + 极低质量」这种块效应明显的结果。
- **最差块 SSIM 闸门**：按 96px 分块取最低块，低于 0.75 直接否决。整图均值会把局部劣化平均掉，这条堵的是这个漏洞。
- **二维码保护**：检测到二维码的图，压缩后必须仍能解出原码，否则降质量重试，降到头仍解不出就换更小尺寸重试。检测走三段式（整图粗检 → 按位置裁切放大精检 → 区域盲扫），只认 QRCode 格式，避免页面上的数字文字被误判。
- **原地写入带回滚**：用 `r+b` 覆盖后截断，失败把原内容写回，不产生半截文件。
- **Orientation 干净**：像素经 `exif_transpose` 转正后无条件删除 Orientation 标签。留着会让查看器再转一次，值为 0 这类非法值更会被各种软件解释成随机角度。
- **并行处理**：多进程按文件并行。
- **可重复执行**：已达标文件自动跳过。

## 依赖

Python 3.9+。

```bash
uv run --with zxing-cpp --with pillow --with numpy --with imagecodecs \
  python scripts/img_shrink.py --help
```

- `pillow`：图片读写与 libjpeg-turbo 编码
- `numpy`：画质评估
- `zxing-cpp`：二维码检测（`--no-qr` 时不需要）
- `imagecodecs`：提供 mozjpeg 与 jpegli 两个额外编码后端（Windows x64 有 cp312 abi3 wheel，约 19MB，依赖 numpy>=2.1）。**不装也能跑**，只是 `--codec auto` 退化为只用 pillow。

常规安装：

```bash
pip install pillow numpy zxing-cpp imagecodecs
```

## 用法

```bash
# 预演，不写盘
python scripts/img_shrink.py --limit-mb 1 --dry-run "/path/to/photos"

# 正式执行，原地覆盖
python scripts/img_shrink.py --limit-mb 1 "/path/to/photos"

# 多个目录一次处理
python scripts/img_shrink.py --limit-mb 1 "/path/batch-a" "/path/batch-b"

# 指定体积上限
python scripts/img_shrink.py --limit-mb 0.5 "/path/photos"

# 确有平台长边限制时才传 max-edge（默认跟随原图）
python scripts/img_shrink.py --limit-mb 0.5 --max-edge 2400 "/path/photos"
```

标准流程是先 `--dry-run` 看计划，确认后再正式执行。已达标（≤ 上限）的文件自动跳过，可安全重复执行。

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit-mb` | **必填** | 体积上限（MB，按 1024×1024 字节计），无默认值 |
| `--max-edge` | 0 | 长边硬上限，0=跟随原图。确有平台限制再传 |
| `--min-edge` | 1500 | 最小长边，撑不住就报「无可行组合」 |
| `--qr-min-edge` | 1800 | 含二维码图的最小长边，保扫码可用 |
| `--codec` | auto | `auto` / `pillow` / `mozjpeg` / `jpegli` |
| `--no-qr` | 关 | 关闭二维码检测（图里确定没码时更快） |
| `--dry-run` | 关 | 只报告，不写盘 |
| `--report` | 空 | 结果写成 JSON 的路径 |

## 输出示例

```
待处理 5 张（原地覆盖），编码后端 jpegli, mozjpeg, pillow
[OK] IMG_0001.jpg: 11.04->0.91MB 4416x5888->2250x3000 长边3000 q82 420 jpegli SSIM 0.9851 码=1/1
[OK] IMG_0002.jpg: 8.72->0.94MB 4416x5888->4416x5888 长边4416 q77 mozjpeg SSIM 0.9768
[OK] IMG_0003.jpg: 9.71->0.93MB 5888x4416->3000x2250 长边3000 q79 420 mozjpeg SSIM 0.9707

合计 2.78MiB | 处理 3 张 | 失败 0
```

每行给出：原体积→新体积、原尺寸→新尺寸、长边、quality、子采样（444/422/420）、实际用的后端、SSIM、二维码检出一致性。

## 编码器选择

`--codec auto`（默认）会依次试 jpegli / mozjpeg / pillow，按同尺寸下的 SSIM + 色度挑最优。

三者在 33 张同批语料上的实测（2400px、600KB 预算、4:2:0）：

| 后端 | 平均 SSIM | 强边保持率 | 单次编码耗时 |
|---|---|---|---|
| mozjpeg | 0.95814 | 0.8395 | 约 2.1s |
| pillow | 0.95730 | **0.8720** | 约 0.3s |
| jpegli | 0.95337 | 0.8577 | 约 0.1s |

**没有单一赢家**：mozjpeg 的 SSIM 略高，pillow 的细节保留最好，jpegli 居中但最快。该批语料含大量自相似高频纹理，jpegli 的自适应量化会把它们当噪声抹掉，所以在合成图上的领先优势消失了——这也是 auto 模式存在的理由，不要预设哪个更好。

在固定体积口径下（各自二分到自己能塞进上限的最高 q），mozjpeg 通常能撑到更低 q 而不劣化，效率最高。

两个容易踩的坑：

- `imagecodecs` 的 `subsampling` 要传字符串 `'444'/'422'/'420'`，传整数会报 `TypeError: 'int' object is not iterable`。
- jpegli 默认输出 baseline，必须显式传 `progressive=True`，实测再省约 7% 体积。

横向对比编码器必须固定在同一个轴上（同体积比画质，或同画质比体积），不能直接比 quality 数字。

## 作为智能体技能使用

`SKILL.md` 是可被支持技能机制的客户端识别的技能描述，含触发词、流程、核验清单。把它和 `scripts/` 一起放进技能目录即可让智能体在用户提到压图需求时自动调用本工具。

```bash
mkdir -p ~/.agents/skills/image-compress
cp SKILL.md README.md ~/.agents/skills/image-compress/
cp -r scripts ~/.agents/skills/image-compress/
```

## 工作方式

1. 读图，像素按 EXIF Orientation 转正，删除 Orientation 标签
2. 检测二维码（可关闭），记录原码作为压缩后的校验基准
3. 从原图长边起，对该尺寸二分 quality 找能塞进上限的最高档
   - 每个尺寸内比较各后端与子采样组合，取融合分最高者
   - 4:2:0 相对 4:4:4 的色度降幅超 3dB 则弃用
   - 最差块 SSIM 低于 0.75 直接否决
4. 质量低于 75 则按 0.85 缩一档重试，最多缩到 `--min-edge`
5. 含码图在候选上验证二维码，不过则降质量重试
6. 落盘（原地覆盖），回读校验尺寸、体积、二维码
7. 输出结果与可选 JSON 报告

## 已知限制

- 只处理 JPEG（`.jpg` / `.jpeg`）。PNG、WebP、AVIF 不在此工具范围。
- 原地覆盖不留备份，覆盖后原图无法恢复。首次在重要素材上使用前，建议先 `--dry-run` 看计划。
- 视觉无损不等于数学无损。2600 万像素照片全分辨率压不进 1MB，必须降分辨率，这是体积上限的物理约束。
- `auto` 模式在单图上要跑 3 个后端 × 2 种子采样 × 每次 7 轮二分编码，实测约 5–14 秒/张。赶时间用 `--codec pillow` 或 `--codec jpegli` 锁单后端。

## 许可

MIT，见 [LICENSE](LICENSE)。
