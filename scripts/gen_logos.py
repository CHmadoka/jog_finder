# 生成 web/ 下三家平台徽标:官方应用图标(App Store 1024px PNG 源图)→ 1x/2x/3x
# 徽标在左栏平台按钮里以 30px CSS 尺寸显示,三档分别对应 1/2/3 倍屏,
# 浏览器按屏幕像素比直接取像素对齐的那张,不做运行时缩放,避免缩放发虚。
# 另:源图四周留白很大(图案只占 52~83%),先按图案做中心缩放裁剪,只裁掉外围
# 真实背景像素(不生成合成填充,避免抹边),让图案占到约 88% —— 同样尺寸下
# 字形明显更大、更清楚。
# 用法:python scripts/gen_logos.py(需要联网,产物已入库,平时无需重跑)
import io
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "web"

# CSS 显示尺寸(须与 web/index.html 的 .plogo 宽高保持一致)
CSS_SIZE = 30
SCALES = (1, 2, 3)
# 裁剪后图案占徽标边长的比例(其余为背景留白)
CONTENT_FILL = 0.88

# 三家官方应用图标:App Store 数字资产 1024px PNG(无损,非 JPEG rendition,
# 避免白色字形边缘的 JPEG 振铃被固化进 PNG)
LOGO_URLS = {
    "boss": "https://is1-ssl.mzstatic.com/image/thumb/Purple221/v4/fe/3b/3d/"
            "fe3b3db3-5e35-6280-26c4-571ac2563621/AppIcon-0-0-1x_U007emarketing-0-8-0-85-220.png"
            "/1024x1024bb.png",
    "51job": "https://is1-ssl.mzstatic.com/image/thumb/Purple211/v4/4e/5b/c7/"
             "4e5bc719-0f21-044e-58f2-dd728e3d06af/AppIcon-0-1x_U007epad-0-1-0-85-220-0.png"
             "/1024x1024bb.png",
    "liepin": "https://is1-ssl.mzstatic.com/image/thumb/Purple211/v4/98/f2/42/"
              "98f24243-aabf-3d17-25f1-e8515267905b/AppIcon-0-0-1x_U007emarketing-0-8-0-85-220.png"
              "/1024x1024bb.png",
}

# 图案检测方式:corner = 与左上角背景色有差异;white = 白色图案(背景为渐变时用)
CONTENT_MODE = {"boss": "corner", "51job": "white", "liepin": "corner"}


def fetch(url: str) -> Image.Image:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=30).read()
    return Image.open(io.BytesIO(data)).convert("RGB")


def content_box(img: Image.Image, mode: str) -> tuple[int, int, int, int]:
    """返回图案(非背景)的包围盒 (l, t, r, b)。"""
    if mode == "white":
        # 图案是纯白:三通道都亮 → 转灰度后阈值化
        box = img.convert("L").point(lambda v: 255 if v > 200 else 0).getbbox()
    else:
        flat = Image.new("RGB", img.size, img.getpixel((0, 0)))
        diff = ImageChops.difference(img, flat).convert("L").point(lambda v: 255 if v > 30 else 0)
        box = diff.getbbox()
    return box or (0, 0, img.width, img.height)


def zoom_square(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """以图案为中心裁正方形,使图案占边长 CONTENT_FILL;只裁不补(边界超原图时退回原图)。"""
    l, t, r, b = box
    side = round(max(r - l, b - t) / CONTENT_FILL)
    if side > min(img.width, img.height):
        return img  # 源图留白本就不多,原样保真
    left = round((l + r) / 2 - side / 2)
    top = round((t + b) / 2 - side / 2)
    # 夹到图内,保证取到真实像素
    left = min(max(0, left), img.width - side)
    top = min(max(0, top), img.height - side)
    return img.crop((left, top, left + side, top + side))


def main() -> None:
    for name, url in LOGO_URLS.items():
        src_img = fetch(url)
        src = zoom_square(src_img, content_box(src_img, CONTENT_MODE[name]))
        for scale in SCALES:
            size = CSS_SIZE * scale
            img = src.resize((size, size), Image.LANCZOS)
            suffix = "" if scale == 1 else f"@{scale}x"
            path = OUT / f"logo_{name}{suffix}.png"
            img.save(path, "PNG", optimize=True)
            print(f"{path.name}: {size}x{size} ({path.stat().st_size} B)")


if __name__ == "__main__":
    main()
