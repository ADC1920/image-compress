# image-compress

把批量图片压缩到指定体积上限（默认 1MB）以内的命令行工具，也可作为智能体技能使用。

面向需要「图片必须小于某个体积」的场景：表单上传限制、IM 传递、档案归档、素材整理。

## 特点

- **按长边统一缩放**：横竖图混批不会缩错。落盘前后双重断言输出长边等于计划值。
- **质量二分搜索**：给定体积上限，自动找能达到的最高质量，不做低效的全网格枚举。
- **二维码保护**：检测到二维码的图，压缩后必须仍能解出原码，否则降质量重试。检测走三段式（整图粗检 → 按位置裁切放大精检 → 区域盲扫），只认 QRCode 格式，避免页面上的数字文字被误判。
- **原地写入带回滚**：用 `r+b` 覆盖后截断，失败把原内容写回，不产生半截文件。
- **Orientation 修正**：转正后把非法值（0 等）字节级改为 1，不重编码像素。
- **并行处理**：多进程按文件并行，8 张 12MP 图约 26 秒。
- **可重复执行**：已达标文件自动跳过。

## 依赖

Python 3.9+，三个包：

```bash
uv run --with zxing-cpp --with pillow --with numpy python scripts/img_shrink.py --help
```

或常规安装：

```bash
pip install pillow numpy zxing-cpp
```

## 用法

```bash
# 预演，不写盘
python scripts/img_shrink.py --dry-run "/path/to/photos"

# 正式执行，原地覆盖
python scripts/img_shrink.py "/path/to/photos"

# 多个目录一次处理
python scripts/img_shrink.py "/path/batch-a" "/path/batch-b"

# 指定体积上限与最大长边
python scripts/img_shrink.py --limit-mb 0.5 --max-edge 2400 "/path/photos"
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit-mb` | 1.0 | 体积上限（MB） |
| `--max-edge` | 3000 | 最大长边像素，避免无意义超采样 |
| `--min-edge` | 1500 | 最小长边像素 |
| `--qr-min-edge` | 1800 | 含二维码图的最小长边，保扫码可用 |
| `--codec` | pillow | `pillow` 或 `jpegli` |
| `--no-qr` | 关 | 关闭二维码检测（图里确定没码时更快） |
| `--dry-run` | 关 | 只报告，不写盘 |
| `--report` | 空 | 结果写成 JSON 的路径 |

## 输出示例

```
待处理 5 张（原地覆盖）
[OK] IMG_0001.jpg: 11.04->0.91MB 4416x5888->2250x3000 长边3000 q71 SSIM 0.9957 码=1/1
[OK] IMG_0002.jpg: 8.72->0.94MB 4416x5888->2250x3000 长边3000 q84 SSIM 0.9973
[OK] IMG_0003.jpg: 9.71->0.93MB 5888x4416->3000x2250 长边3000 q80 SSIM 0.9973

合计 2.78MiB | 处理 3 张 | 失败 0
```

## 编码器说明

默认 `pillow`（libjpeg-turbo，4:4:4 全色度采样 + 渐进式 + optimize）。

`--codec jpegli` 用 Google 的 [jpegli](https://github.com/google/jpegli) 编码器（需 `pip install pyjpegli`）。在 12 张同批语料上按同体积对比，jpegli 画质略差（亮度 SSIM 胜 0/12，色度 PSNR 低 1.29dB），原因是两套编码器的 quality 刻度不同义。它的质量档范围更宽（q70–96），仅建议在 Pillow 压不进目标体积时尝试。

网上「jpegli 同画质省 20% 体积」的结论来自手机照片语料，换成大色块、少渐变的画面且用「塞进固定体积」的用法时，优势不成立。横向对比编码器必须固定在同一个轴上。

## 作为智能体技能使用

`SKILL.md` 是可被支持技能机制的客户端识别的技能描述，含触发词、流程、核验清单。把它放进技能目录即可让智能体在用户提到压图需求时自动调用本工具。

## 工作方式

1. 读图，转正（按 EXIF Orientation 旋转），清掉失效的 Orientation 标签
2. 检测二维码（可关闭），记录原码作为压缩后的校验基准
3. 长边从 `--max-edge` 起，对每个尺寸二分质量找到能塞进上限的最高档
4. 含码图在候选上验证二维码，不过则降质量重试
5. 体积利用不足 85% 且有放大空间时，按面积比例放大后重试
6. 落盘（原地覆盖），回读校验尺寸、体积、二维码
7. 修正非法 Orientation

## 已知限制

- 只处理 JPEG（`.jpg` / `.jpeg`）。PNG、WebP、AVIF 不在此工具范围。
- 原地覆盖不留备份，覆盖后原图无法恢复。首次在重要素材上使用前，建议先 `--dry-run` 看计划。
- 视觉无损不等于数学无损。2600 万像素手机照片全分辨率压不进 1MB，必须降分辨率，这是体积上限的物理约束。

## 许可

MIT，见 [LICENSE](LICENSE)。
