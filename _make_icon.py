"""生成程序图标 assets/app.ico（蓝色圆底 + 白色 N）。"""
from pathlib import Path

from PIL import Image, ImageDraw

sizes = [16, 24, 32, 48, 64, 128, 256]
frames = []
for s in sizes:
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(1, s // 16)
    draw.ellipse((pad, pad, s - pad, s - pad), fill=(30, 120, 220, 255))
    width = max(2, s // 10)
    x1, y1, x2, y2 = int(s * 0.32), int(s * 0.72), int(s * 0.68), int(s * 0.28)
    draw.line((x1, y1, x1, y2), fill="white", width=width)
    draw.line((x1, y2, x2, y1), fill="white", width=width)
    draw.line((x2, y1, x2, y2), fill="white", width=width)
    frames.append(img)

out = Path("assets")
out.mkdir(exist_ok=True)
frames[-1].save(out / "app.ico", sizes=[(s, s) for s in sizes])
print("icon ->", (out / "app.ico").resolve(), (out / "app.ico").stat().st_size, "bytes")
