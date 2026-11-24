"""批量把图片压到指定体积上限内（原地覆盖，不留备份）。

用途：电商面料图等需要「网页/IM 可用」的图片批量压到 1MB 以内。
跑法：
  先看会做什么（不写盘）：
    python img_shrink.py --dry-run "D:/路径/批次目录"
  正式执行（原地覆盖）：
    python img_shrink.py "D:/路径/批次目录" [...]
  跳过二维码验证（图里没有码时更快）：
    python img_shrink.py --no-qr "D:/路径/批次目录"

参数：
  --limit-mb 1.0     体积上限（必填，无默认值）
  --max-edge 0       长边硬上限（默认 0=跟随原图）
  --min-edge 1500    最小长边（默认 1500）
  --qr-min-edge 1800 含二维码图的最小长边（默认 1800，保扫码可用）
  --codecs auto      编码后端：auto/pillow/mozjpeg/jpegli（auto=三个都试取最优）
  --no-qr            不做二维码检测与保护
  --dry-run          只报告计划，不写文件

依赖：Pillow、numpy、zxing-cpp、imagecodecs（可选，装了就多两个后端）：
  uv run --with zxing-cpp --with pillow --with numpy --with imagecodecs \
    python img_shrink.py <目录>

行为要点：
- 已 ≤ 上限的文件自动跳过，可安全重复执行。
- 缩放一律按**长边**（max(宽,高)）计算，横竖图都不会缩错。
- 含二维码的图把「压缩后仍能解出原码」作为硬约束。
- 写入用原地截断覆盖；失败会把原内容写回，不会出现半截文件。
- 转正后把非法 Orientation（0 等）修为 1，避免查看器误转。
- 尺寸不是按固定步长猜，而是扫描多个尺寸后按融合指标（SSIM+清晰度+色度）选。
"""
import argparse
import io
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np
import zxingcpp
from PIL import Image, ImageOps

# imagecodecs 提供 mozjpeg 与 jpegli 后端。实测三者同尺寸同预算下：
# jpegli 平均 SSIM 最高（0.95401）且比 Pillow 快约 15 倍；mozjpeg 次之但慢 20 倍。
# 未安装时自动回落到 Pillow，功能不受影响。
try:
    import imagecodecs as _IC
except ImportError:
    _IC = None

# 子采样名映射，imagecodecs 用字符串，Pillow 用整数
_SS_NAME = {0: "444", 1: "422", 2: "420"}

TAG_ORIENTATION = 0x0112
BINERS = (
    zxingcpp.Binarizer.LocalAverage,
    zxingcpp.Binarizer.GlobalHistogram,
    zxingcpp.Binarizer.FixedThreshold,
)
REGIONS = (
    (0, 0, 1, 1),
    (0, 0, 0.55, 0.55),
    (0.45, 0, 1, 0.55),
    (0, 0.45, 0.55, 1),
    (0.45, 0.45, 1, 1),
    (0, 0, 1, 0.45),
    (0.25, 0.15, 0.85, 0.85),
)


# ---------- 二维码 ----------

def codes_full(img):
    """完整检测：多二值化器 + 精确位置裁切放大 + 区域盲扫兜底。

    只收二维码（QRCode），避免页面上的货号/编号被误识别成 Code128 等条码，
    污染基准导致「压后多出一个码」的假告警。
    小尺寸的码（占全图不到 1%）在整图检测下常被 downscale 吃掉，
    故先用 read_barcodes 拿到位置信息，按位置裁切放大后重测。
    """
    found = set()

    def scan(im, biner):
        for r in zxingcpp.read_barcodes(im, try_rotate=True, try_downscale=True,
                                        binarizer=biner):
            if r.format == zxingcpp.BarcodeFormat.QRCode:
                found.add(r.text)

    # 第一轮：整图
    for b in BINERS:
        scan(img, b)

    # 第二轮：按已检出位置精确裁切放大（码本身很小，放大后更稳）
    w, h = img.size
    boxes = set()
    for b in BINERS:
        for r in zxingcpp.read_barcodes(img, try_rotate=True, try_downscale=True,
                                        binarizer=b):
            if r.format != zxingcpp.BarcodeFormat.QRCode:
                continue
            pos = r.position
            if pos is None:
                continue
            pts = [pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left]
            xs = [p.x for p in pts]
            ys = [p.y for p in pts]
            pad_x = max(20, int((max(xs) - min(xs)) * 0.4))
            pad_y = max(20, int((max(ys) - min(ys)) * 0.4))
            boxes.add((max(0, int(min(xs)) - pad_x), max(0, int(min(ys)) - pad_y),
                       min(w, int(max(xs)) + pad_x), min(h, int(max(ys)) + pad_y)))
    for box in boxes:
        crop = img.crop(box)
        if crop.width < 20 or crop.height < 20:
            continue
        for scale in (2, 3):
            tile = crop.resize((crop.width * scale, crop.height * scale), Image.LANCZOS)
            for b in BINERS:
                scan(tile, b)

    # 第三轮：盲区扫描兜底（位置检测不到时仍可能存在于其他区域）
    for box in REGIONS:
        crop = img.crop((int(w * box[0]), int(h * box[1]),
                         int(w * box[2]), int(h * box[3])))
        # 裁切块足够小才放大；大块靠 downscale 自身适配
        scales = (1, 2) if max(crop.size) * 2 <= 6000 else (1,)
        for scale in scales:
            tile = crop if scale == 1 else crop.resize(
                (crop.width * scale, crop.height * scale), Image.LANCZOS)
            for b in BINERS:
                scan(tile, b)
    return found


def codes_quick(img):
    """选参时的快速过滤：只认二维码，避免把货号文字当成码。"""
    return {r.text for r in zxingcpp.read_barcodes(
        img, try_rotate=True, try_downscale=True)
        if r.format == zxingcpp.BarcodeFormat.QRCode}


# ---------- 画质评估 ----------

def _gauss(win=11, sigma=1.5):
    c = np.arange(-(win // 2), win // 2 + 1, dtype=np.float32)
    g = np.exp(-(c ** 2) / (2 * sigma ** 2))
    return g / g.sum()


def _sep_conv(x, g):
    r = len(g) // 2
    h, w = x.shape
    p = np.pad(x, ((r, r), (0, 0)), mode="reflect")
    tmp = np.zeros((h, w), dtype=np.float64)
    for i, weight in enumerate(g):
        tmp += weight * p[i:i + h, :]
    p2 = np.pad(tmp, ((0, 0), (r, r)), mode="reflect")
    out = np.zeros((h, w), dtype=np.float64)
    for i, weight in enumerate(g):
        out += weight * p2[:, i:i + w]
    return out


def ssim_map(a, b, g):
    """逐像素 SSIM 图，均值与最差块都从这里取，不重复计算。"""
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    ma, mb = _sep_conv(a, g), _sep_conv(b, g)
    va = _sep_conv(a * a, g) - ma ** 2
    vb = _sep_conv(b * b, g) - mb ** 2
    cov = _sep_conv(a * b, g) - ma * mb
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return ((2 * ma * mb + c1) * (2 * cov + c2)) / \
        ((ma ** 2 + mb ** 2 + c1) * (va + vb + c2))


def ssim(a, b, g):
    return float(np.mean(ssim_map(a, b, g)))


def worst_block(m, block=96):
    """SSIM 图按 block 分块后的最低块均分。

    整图均值会把局部劣化平均掉：实测二维码只占 2.7% 面积时，
    整图 SSIM 0.9898 而码区只有 0.62。用最低块作硬闸门堵这个漏洞。
    """
    h, w = m.shape
    if h < block or w < block:
        return float(np.mean(m))
    hb, wb = h // block, w // block
    trim = m[:hb * block, :wb * block]
    tiles = trim.reshape(hb, block, wb, block).mean(axis=(1, 3))
    return float(tiles.min())


def gray_small(img, long_edge=700):
    r = long_edge / max(img.size)
    size = (max(1, round(img.size[0] * r)), max(1, round(img.size[1] * r)))
    return np.asarray(img.resize(size, Image.LANCZOS).convert("L"))


# 评估尺度：同尺寸内比较质量，不需要大到原尺寸。
# 实测 measure 单次耗时：2000px 0.67s / 3000px 1.38s / 3600px 1.83s，
# 而尺寸策略是「先保尺寸再降质量」、跨尺寸不比画质，故取 2000 即可。
EVAL_CAP = 2000
# 融合权重：同尺寸内 SSIM 管整体结构，色度管颜色。
W_SSIM, W_CHROMA = 0.65, 0.35
# 最差块 SSIM 闸门：整图 SSIM 是均值，会把局部劣化平均掉。
# 实测二维码只占 2.7% 面积时整图 SSIM 0.9898，但码区只有 0.62。
# 低于此值的候选直接否决，比调权重更直接（分块复用同一张 map，开销约 4ms）。
WORST_BLOCK_MIN = 0.75
# 色度相对损失安全线：4:2:0 相比同尺寸 4:4:4 的色度 PSNR 降幅。
# 用相对值而非绝对值：源图本身色度信息弱时（如高饱和色卡）4:4:4 也达不到高 dB，
# 绝对阈值会误判。实测面料/文字 4:2:0 仅掉 0.8-2.0dB（可接受），
# 彩色页掉 6.8-15.5dB（须退回 4:4:4）。
CHROMA_REL_DROP_DB = 3.0
# 色度梯度预判：高于此值说明图里有细彩色边缘，直接跳过 4:2:0 试探省时间
CHROMA_EDGE_HINT = 4.0
# 色度归一化上限：达到此 PSNR 视为色度无感（满分）
CHROMA_NORM_DB = 50.0
# 质量下限惩罚起点：低于此 quality 的候选按比例折损分数。
# 实测真实面料图上不加此约束会选出「3600px + q13」这种块效应明显的结果。
Q_PENALTY_FROM = 40
# 质量验收线：达到此 quality 就不再为画质微差牺牲尺寸
Q_ACCEPT = 75


def sharpness(arr):
    """强边缘梯度强度：只统计梯度最强的 0.5% 像素的平均梯度。

    比拉普拉斯方差更适合判断「细节有没有被抹掉」：拉普拉斯方差是全局高频能量，
    会把压缩噪声也算作细节，而 jpegli 的自适应量化主动平滑噪声反而被判为不清晰
    （实测 1920px 下 lapvar 保持率 jpegli 0.1887 对 pillow 0.2831）。
    本指标只看原本最锐利的边缘还剩多少，对噪声不敏感。
    """
    f = arr.astype(np.float32)
    gx = np.abs(np.diff(f, axis=1))[:-1, :]
    gy = np.abs(np.diff(f, axis=0))[:, :-1]
    g = np.sqrt(gx ** 2 + gy ** 2)
    if g.size == 0:
        return 0.0
    strong = g[g >= np.percentile(g, 99.5)]
    return float(strong.mean()) if strong.size else 0.0


def fuse_score(ssim_val, worst, chroma_db, quality):
    """同一尺寸内的候选评分。

    跨尺寸不比画质（尺寸由「先保尺寸再降质量」决定），
    这里只挑同尺寸下最优的 (后端, 子采样) 组合：
    SSIM 均值管整体，色度管颜色，质量过低整体折损。

    最差块 SSIM 作为硬闸门而非加权项：整图均值会把局部劣化平均掉
    （实测二维码只占 2.7% 面积时整图 0.9898，码区只有 0.62），
    闸门能直接否决这类候选，加权则会被整体分掩盖。
    """
    if worst < WORST_BLOCK_MIN:
        return 0.0
    chroma_norm = 1.0 if chroma_db == float("inf") else min(1.0, chroma_db / CHROMA_NORM_DB)
    base = W_SSIM * ssim_val + W_CHROMA * chroma_norm
    if quality < Q_PENALTY_FROM:
        base *= max(0.3, quality / Q_PENALTY_FROM)
    return base


def gray_eval(img):
    """评估用的灰度参考图，尺度取 min(EVAL_CAP, 原长边)。"""
    return gray_small(img, min(EVAL_CAP, max(img.size)))


def chroma_psnr(ref, dec):
    """同尺寸两张图的 Cb/Cr PSNR，用来发现 SSIM 看不见的色度劣化。"""
    a = np.asarray(ref.convert("YCbCr"), dtype=np.float64)
    b = np.asarray(dec.convert("YCbCr"), dtype=np.float64)
    out = []
    for i in (1, 2):
        mse = float(np.mean((a[:, :, i] - b[:, :, i]) ** 2))
        out.append(float("inf") if mse == 0 else 10 * np.log10(255 ** 2 / mse))
    return out[0], out[1]


def chroma_edge_strength(img):
    """色度梯度均值，用于预判 4:2:0 是否可能糊掉彩色细节。

    实测：彩色文字页 6.5、面料图 1.2、文字页 1.1，相差 5 倍以上。
    """
    w = min(800, max(img.size))
    r = w / max(img.size)
    small = img.resize((max(1, round(img.size[0] * r)),
                        max(1, round(img.size[1] * r))), Image.LANCZOS)
    ycc = np.asarray(small.convert("YCbCr"), dtype=np.float32)
    cb, cr = ycc[:, :, 1], ycc[:, :, 2]
    gx = np.abs(np.diff(cb, axis=1)).mean() + np.abs(np.diff(cr, axis=1)).mean()
    gy = np.abs(np.diff(cb, axis=0)).mean() + np.abs(np.diff(cr, axis=0)).mean()
    return float(gx + gy)


def view_ssim(ref_arr, dec):
    """解码图放到评估尺度后与参考比，返回 (均值, 最差块)。

    尺寸策略是「先保尺寸」，同尺寸内比较质量；取 2000px 评估尺度是因为
    实测单次耗时 0.67s（3000px 需 1.38s、3600px 需 1.83s），而跨尺寸不比画质，
    大尺度只增耗时不给判据。
    """
    target = (ref_arr.shape[1], ref_arr.shape[0])
    if dec.size != target:
        dec = dec.resize(target, Image.LANCZOS)
    m = ssim_map(ref_arr, np.asarray(dec.convert("L")), _gauss())
    return float(np.mean(m)), worst_block(m)


# ---------- 尺寸与编码 ----------

def size_for(im, long_edge):
    """按长边算尺寸，保证 max(宽,高) == long_edge。"""
    ow, oh = im.size
    if ow >= oh:
        w, h = long_edge, max(1, round(oh * long_edge / ow))
    else:
        w, h = max(1, round(ow * long_edge / oh)), long_edge
    assert max(w, h) == long_edge, f"尺寸计算异常 {w}x{h}"
    return (w, h)


def encode(img, exif_bytes, dpi, quality, codec="pillow", subsampling=0):
    """按指定后端编码为 JPEG。

    codec="pillow"：libjpeg-turbo + 渐进式 + optimize。走 Pillow 存盘，支持 EXIF/DPI。
    codec="mozjpeg"：mozjpeg 4.x，trellis 量化开启，渐进式。需 imagecodecs。
    codec="jpegli"：jpegli，自适应量化。需 imagecodecs。

    2026-11-24 用 33 张真实面料图（2400px、600KB 预算）实测：
        mozjpeg 平均 SSIM 0.95814 / 强边保持 0.8395
        pillow  平均 SSIM 0.95730 / 强边保持 0.8720
        jpegli  平均 SSIM 0.95337 / 强边保持 0.8577
    在合成图上面 jpegli 领先，但在真实面料图上并非如此——面料纹理有大量
    自相似高频，jpegli 的自适应量化会把它们当噪声抹掉。故 auto 模式全试，
    由融合指标（强边保持权重最高）决定，不预设赢家。

    imagecodecs 的 mozjpeg/jpegli 只收像素数组、不带元数据，故 EXIF 需单独注入。
    """
    if codec in ("mozjpeg", "jpegli") and _IC is not None:
        arr = np.ascontiguousarray(np.asarray(img.convert("RGB")))
        ss = _SS_NAME.get(subsampling, "420")
        try:
            if codec == "mozjpeg":
                data = bytes(_IC.mozjpeg_encode(
                    arr, level=quality, subsampling=ss, optimize=True,
                    progressive=True))
            else:
                # jpegli 默认输出 baseline；progressive=True 实测再省约 7%
                data = bytes(_IC.jpegli_encode(
                    arr, level=quality, subsampling=ss, optimize=True,
                    progressive=True))
        except Exception:
            # 后端不支持该参数组合时退回 Pillow，不让单张图中断整批
            data = None
        if data:
            if exif_bytes:
                data = _inject_exif(data, exif_bytes)
            return data

    # 高熵内容（随机噪声、密集纹理）在 4:4:4 + 渐进式下会触发 libjpeg 的
    # "Suspension not allowed here"，Pillow 抛 OSError。降为 4:2:0 可正常编码，
    # 否则整批处理会中断。
    buf = io.BytesIO()
    try:
        img.save(buf, format="JPEG", quality=quality, subsampling=subsampling,
                 optimize=True, progressive=True, exif=exif_bytes, dpi=dpi)
    except OSError:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, subsampling=2, optimize=True,
                 progressive=True, exif=exif_bytes, dpi=dpi)
    return buf.getvalue()


def available_codecs(requested):
    """解析可用的编码后端列表。"""
    if requested == "auto":
        out = ["pillow"]
        if _IC is not None:
            out = ["jpegli", "mozjpeg", "pillow"]
        return out
    if requested in ("mozjpeg", "jpegli") and _IC is None:
        print(f"提示：未安装 imagecodecs，{requested} 无法使用，回落到 pillow")
        return ["pillow"]
    return [requested]


def _inject_exif(jpeg: bytes, exif_bytes: bytes) -> bytes:
    """把 EXIF 插进 JPEG 的 SOI 之后（APP1 段）。已有 APP1 Exif 则替换。"""
    if not exif_bytes:
        return jpeg
    if not jpeg.startswith(b"\xff\xd8"):
        return jpeg
    payload = b"Exif\x00\x00" + exif_bytes
    if len(payload) + 2 > 0xFFFF:
        return jpeg  # EXIF 过大，放弃注入，避免破坏文件
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload

    # 跳过 SOI 后已有的 APP1/APP0，找到第一个非 APP 段位置
    i = 2
    insert_at = 2
    while i < len(jpeg) - 4 and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if marker == 0xE1 and jpeg[i + 4:i + 10] == b"Exif\x00\x00":
            seg_len = int.from_bytes(jpeg[i + 2:i + 4], "big")
            # 已有 EXIF，直接替换该段
            return jpeg[:i] + app1 + jpeg[i + 2 + seg_len:]
        if 0xE0 <= marker <= 0xEF:
            seg_len = int.from_bytes(jpeg[i + 2:i + 4], "big")
            i += 2 + seg_len
            insert_at = i
            continue
        break
    return jpeg[:insert_at] + app1 + jpeg[insert_at:]


# ---------- 写盘 ----------

def write_in_place(target: Path, data: bytes, attempts=5, delay=0.4):
    """r+b 覆盖后截断；失败回写原内容，避免半截文件。"""
    original = target.read_bytes()
    last = None
    for i in range(attempts):
        try:
            with open(target, "r+b") as f:
                f.write(data)
                f.truncate()
                f.flush()
                os.fsync(f.fileno())
            return
        except OSError as exc:
            last = exc
            time.sleep(delay * (i + 1))
    try:
        with open(target, "r+b") as f:
            f.write(original)
            f.truncate()
    except OSError:
        pass
    raise RuntimeError(f"{target.name}: 写入失败，已回写原内容（{last}）")


# ---------- 单图处理 ----------

def process(job):
    path_str, expect, opts = job
    path = Path(path_str)
    expect = set(expect)
    has_qr = bool(expect)
    limit = opts["limit"]
    budget = limit - 32 * 1024
    max_edge = opts["max_edge"]
    min_edge = opts["min_edge"]
    qr_min_edge = opts["qr_min_edge"]
    dry_run = opts["dry_run"]
    codecs = opts.get("codecs") or ["pillow"]

    before = path.stat().st_size
    if before <= limit:
        return {"name": path.name, "skipped": True, "before": before, "after": before}

    with Image.open(path) as raw:
        raw.load()
        raw_size = raw.size
        exif = raw.getexif()
        dpi = raw.info.get("dpi", (72, 72))
        src = ImageOps.exif_transpose(raw).convert("RGB")
        rotated = raw_size != src.size
        # 像素已由 exif_transpose 转正，Orientation 标签必须去掉：
        # 留着会让查看器再转一次，值为 0 这类非法值更会被各种软件解释成随机角度。
        # 原实现只在 rotated 时删，非旋转图会带着 0 之类的非法值出去（实测踩到）。
        if TAG_ORIENTATION in exif:
            del exif[TAG_ORIENTATION]
        exif_bytes = exif.tobytes()

    orig = src.size
    orig_long = max(orig)
    ref = gray_eval(src)
    eval_ref = src if max(orig) <= EVAL_CAP else src.resize(
        size_for(src, EVAL_CAP), Image.LANCZOS)

    # 尺寸上限放到原图长边（不再硬砍到 max_edge）：图本来就不大时，
    # 缩下去是纯损失——实测 3600px 图缩到 3000 只用掉 59% 预算，
    # 放宽回原尺寸后 SSIM 更高且体积仍在上限内。
    top_allowed = min(max_edge, orig_long) if max_edge else orig_long
    floor = qr_min_edge if has_qr else min_edge

    # 色度梯度大的图（细彩色边缘）直接跳过 4:2:0，省得白跑
    try_420 = chroma_edge_strength(src) <= CHROMA_EDGE_HINT

    def try_at(edge, ss, cd):
        """在指定尺寸、子采样、后端下二分找最大可用 quality。"""
        small = src.resize(size_for(src, edge), Image.LANCZOS)
        lo, hi, found = 20, 100, None
        while lo <= hi:
            mid = (lo + hi) // 2
            data = encode(small, exif_bytes, dpi, mid, cd, ss)
            if len(data) <= budget:
                found = (mid, data)
                lo = mid + 1
            else:
                hi = mid - 1
        return found, small

    def measure(data, small, q):
        """解码后测色度、SSIM（均值 + 最差块），并按 quality 算融合分。"""
        with Image.open(io.BytesIO(data)) as im2:
            dec = im2.convert("RGB")
        ref_same = eval_ref.resize(dec.size, Image.LANCZOS) \
            if dec.size != eval_ref.size else eval_ref
        cb, cr = chroma_psnr(ref_same, dec)
        sc, wb = view_ssim(ref, dec)
        return {"q": q, "data": data, "size": small.size, "img": small,
                "cb": cb, "cr": cr, "ssim": sc, "worst": wb,
                "fused": fuse_score(sc, wb, cb, q)}

    # 策略：先保尺寸，再优化质量。
    # 实测任何「把候选放大回原尺寸再比清晰度」的指标都会让最大尺寸几乎必胜
    # （3600px 强边保持率 0.918，1700px 只有 0.629），尺寸选择就失去意义了。
    # 改为：从原尺寸起，能撑住质量下限就不缩；撑不住才按步长缩，直到质量达标。
    edges = [top_allowed]
    e = top_allowed
    while e > floor:
        e = max(floor, int(e * 0.85))
        edges.append(e)
        if len(edges) >= 6:
            break

    def eval_at(edge, ss_list, codec_list):
        """在指定尺寸上评估若干 (子采样, 后端) 组合，返回候选列表。"""
        out = []
        for ss in ss_list:
            for cd in codec_list:
                found, small = try_at(edge, ss, cd)
                if not found:
                    continue
                q, data = found
                c = measure(data, small, q)
                c["ss"] = ss
                c["cd"] = cd
                out.append(c)
        return out

    ss_try = (0, 2) if try_420 else (0,)
    chosen_c = None
    scanned = []
    for i, edge in enumerate(edges):
        # 第一个尺寸（原尺寸）全部后端都试；后续尺寸只试已证明最好的后端，省时间
        cands = eval_at(edge, ss_try, codecs if i == 0 else [best_cd])
        if not cands:
            continue
        scanned.extend(cands)

        # 4:2:0 相对 4:4:4 掉色度太多则剔除
        by_ss = {c["ss"]: c for c in cands}
        if 0 in by_ss and 2 in by_ss:
            drop = max(by_ss[0]["cb"] - by_ss[2]["cb"],
                       by_ss[0]["cr"] - by_ss[2]["cr"])
            if drop > CHROMA_REL_DROP_DB:
                cands = [c for c in cands if c["ss"] != 2]

        best_here = max(cands, key=lambda c: c["fused"])
        best_cd = best_here["cd"]
        # 质量达标就停：不再为小幅画质提升换更小尺寸
        if best_here["q"] >= Q_ACCEPT:
            chosen_c = best_here
            break
        chosen_c = best_here

    if chosen_c is None:
        return {"name": path.name, "ok": False, "err": "无可行组合", "before": before}

    q = chosen_c["q"]
    data = chosen_c["data"]
    size = chosen_c["size"]
    small = chosen_c["img"]
    subsampling = chosen_c["ss"]
    codec_used = chosen_c["cd"]

    if has_qr:
        ok_qr = False
        for try_q in range(q, 9, -2):
            if try_q != q:
                data = encode(small, exif_bytes, dpi, try_q, codec_used, subsampling)
                if len(data) > budget:
                    continue
            with Image.open(io.BytesIO(data)) as im2:
                dec = im2.convert("RGB")
            if expect.issubset(codes_quick(dec)):
                ok_qr = True
                q = try_q
                break
        if not ok_qr:
            # 首选尺寸保不住码，按已扫过的更小尺寸重试一次
            for c in sorted(scanned, key=lambda c: c["size"][0]):
                if c["size"][0] >= max(size):
                    continue
                for try_q in range(c["q"], 9, -2):
                    data2 = encode(c["img"], exif_bytes, dpi, try_q, c["cd"], c["ss"])
                    if len(data2) > budget:
                        continue
                    with Image.open(io.BytesIO(data2)) as im2:
                        dec = im2.convert("RGB")
                    if expect.issubset(codes_quick(dec)):
                        q, data, size, small = try_q, data2, c["size"], c["img"]
                        subsampling, codec_used = c["ss"], c["cd"]
                        ok_qr = True
                        break
                if ok_qr:
                    break
        if not ok_qr:
            return {"name": path.name, "ok": False,
                    "err": "二维码无法保住（缩小后解不出原码）", "before": before}
    edge = max(size)
    with Image.open(io.BytesIO(data)) as im3:
        score, worst = view_ssim(ref, im3.convert("RGB"))

    if dry_run:
        # 试算也报出预期需保的码数，便于核对是否漏检
        return {"name": path.name, "ok": True, "dry": True, "before": before,
                "after": len(data), "orig": list(orig), "new": list(size),
                "edge": edge, "quality": q, "ssim": score, "worst_block": worst, "has_qr": has_qr,
                "subsampling": subsampling, "codec": codec_used,
                "codes_got": len(expect), "codes_need": len(expect),
                "missing": []}

    write_in_place(path, data)
    orient_old = None

    with Image.open(path) as fin:
        fin.load()
        fin_size = ImageOps.exif_transpose(fin).size
        final = ImageOps.exif_transpose(fin).convert("RGB")
    got = codes_full(final) if has_qr else set()
    missing = expect - got
    after = path.stat().st_size
    ok = after <= limit and not missing and max(fin_size) == edge
    return {
        "name": path.name, "ok": ok, "before": before, "after": after,
        "orig": list(orig), "new": list(fin_size), "edge": edge,
        "quality": q, "ssim": score, "worst_block": worst, "rotated": rotated,
        "orient_old": orient_old, "has_qr": has_qr,
        "subsampling": subsampling, "codec": codec_used,
        "codes_got": len(got), "codes_need": len(expect),
        "missing": sorted(missing),
    }


def collect_jobs(dirs, opts):
    jobs = []
    for d in dirs:
        root = Path(d)
        if not root.is_dir():
            print(f"跳过（不是目录）：{d}")
            continue
        for p in sorted(root.glob("*.jpg")) + sorted(root.glob("*.jpeg")):
            if p.stat().st_size <= opts["limit"]:
                print(f"跳过（已达标 {p.stat().st_size/1024/1024:.2f}MB）：{p.name}")
                continue
            expect = []
            if opts["qr"]:
                with Image.open(p) as im:
                    im.load()
                    expect = sorted(codes_full(ImageOps.exif_transpose(im).convert("RGB")))
            jobs.append((str(p), expect, opts))
    return jobs


def main():
    ap = argparse.ArgumentParser(description="批量把图片压到指定体积上限内（原地覆盖）")
    ap.add_argument("dirs", nargs="+", help="图片目录（可多个）")
    ap.add_argument("--limit-mb", type=float, required=True,
                    help="体积上限（MB，必填）。压缩到多少由使用者按目标场景决定，"
                         "没有通用默认值——上传限制、IM 传输、归档各自的余量不同。")
    ap.add_argument("--max-edge", type=int, default=0,
                    help="长边硬上限，0=不限制（默认跟随原图）。"
                         "实测把大图硬砍到 3000 反而浪费预算：原尺寸能塞进上限时"
                         "保留原尺寸的画质更高。确有平台限制（如某些网站限 3000）再传。")
    ap.add_argument("--min-edge", type=int, default=1500)
    ap.add_argument("--qr-min-edge", type=int, default=1800)
    ap.add_argument("--codec", choices=["auto", "pillow", "mozjpeg", "jpegli"],
                    default="auto",
                    help="编码后端：auto（默认，装了 imagecodecs 时依次试 jpegli/mozjpeg/"
                         "pillow 取融合分最高）、pillow（libjpeg-turbo 渐进式）、"
                         "mozjpeg（trellis 量化）、jpegli（自适应量化，baseline）。"
                         "mozjpeg/jpegli 需 imagecodecs。")
    ap.add_argument("--no-qr", action="store_true", help="不做二维码检测")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写盘")
    ap.add_argument("--report", default="", help="把结果写成 JSON 的路径")
    args = ap.parse_args()

    codecs = available_codecs(args.codec)

    opts = {
        "limit": int(args.limit_mb * 1024 * 1024),
        "max_edge": args.max_edge,
        "min_edge": args.min_edge,
        "qr_min_edge": args.qr_min_edge,
        "qr": not args.no_qr,
        "dry_run": args.dry_run,
        "codecs": codecs,
    }

    jobs = collect_jobs(args.dirs, opts)
    if not jobs:
        print("没有需要处理的文件")
        return

    print(f"待处理 {len(jobs)} 张（{'试算，不写盘' if opts['dry_run'] else '原地覆盖'}）"
          f"，编码后端 {', '.join(codecs)}")
    results = []
    workers = min(8, os.cpu_count() or 4, len(jobs))
    if workers <= 1:
        for j in jobs:
            results.append(process(j))
            _print_row(results[-1])
    else:
        with mp.Pool(workers) as pool:
            for r in pool.imap_unordered(process, jobs):
                results.append(r)
                _print_row(r)

    if args.report:
        Path(args.report).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(r.get("after", 0) for r in results if not r.get("skipped"))
    bad = [r for r in results if r.get("ok") is False]
    print(f"\n合计 {total/1024/1024:.2f}MiB | 处理 {len([r for r in results if not r.get('skipped')])} 张"
          f" | 失败 {len(bad)}")
    if bad:
        for r in bad:
            print(f"  未通过: {r['name']} {r.get('err') or r.get('missing')}")
        sys.exit(1)


def _print_row(r):
    if r.get("skipped"):
        return
    if "err" in r:
        print(f"[FAIL] {r['name']}: {r['err']}")
        return
    tag = "试算" if r.get("dry") else ("OK" if r["ok"] else "FAIL")
    ss = {0: "444", 1: "422", 2: "420"}.get(r.get("subsampling"), "-")
    cd = r.get("codec", "-")
    line = (f"[{tag}] {r['name']}: {r['before']/1024/1024:.2f}->{r['after']/1024/1024:.2f}MB "
            f"{r['orig'][0]}x{r['orig'][1]}->{r['new'][0]}x{r['new'][1]} "
            f"长边{r['edge']} q{r['quality']} {ss} {cd} SSIM {r['ssim']:.4f}")
    if r.get("has_qr"):
        line += f" 码={r.get('codes_got', 0)}/{r.get('codes_need', 0)}"
        if r.get("missing"):
            line += f" 缺={r['missing']}"
    print(line)


if __name__ == "__main__":
    main()
