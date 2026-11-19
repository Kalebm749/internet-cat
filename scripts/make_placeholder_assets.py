"""Generate placeholder web assets with the standard library only (no Pillow):
cat/web/static/img/cat.png, cat-asleep.png (eyes closed) and paper.png (faint texture).
Replace cat.png with a render of the real 3D model later. Usage: python scripts/make_placeholder_assets.py"""
import math
import random
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "cat" / "web" / "static" / "img"


def png(path: Path, w: int, h: int, pixel) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            raw.extend(pixel(x, y))

    def chunk(t: bytes, d: bytes) -> bytes:
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def tri(px, py, a, b, c) -> bool:
    def s(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    p = (px, py)
    d1, d2, d3 = s(p, a, b), s(p, b, c), s(p, c, a)
    return not ((d1 < 0 or d2 < 0 or d3 < 0) and (d1 > 0 or d2 > 0 or d3 > 0))


def cat_pixel(asleep: bool):
    fur, dark, pink, eye = (150, 150, 158), (95, 95, 105), (226, 164, 160), (60, 50, 40)

    def sample(x, y):  # coordinates in 0..1
        # ears
        for ear in (((0.22, 0.42), (0.27, 0.12), (0.45, 0.30)), ((0.78, 0.42), (0.73, 0.12), (0.55, 0.30))):
            if tri(x, y, *ear):
                inner = tuple(((p[0] - 0.5) * 0.55 + 0.5 + (0.5 - 0.5) * 0, (p[1] - 0.3) * 0.55 + 0.3) for p in ear)
                return pink if tri(x, y, *inner) and y > 0.2 else fur
        # head
        if ((x - 0.5) / 0.34) ** 2 + ((y - 0.56) / 0.31) ** 2 <= 1:
            for ex in (0.38, 0.62):
                if asleep:
                    if abs(y - 0.53) < 0.012 and abs(x - ex) < 0.055 and y > 0.53 - 0.03 * math.cos((x - ex) / 0.055 * math.pi / 2) - 0.004:
                        return eye
                    if abs((y - 0.53) - 0.03 * (1 - ((x - ex) / 0.055) ** 2)) < 0.008 and abs(x - ex) < 0.055:
                        return eye
                elif ((x - ex) / 0.045) ** 2 + ((y - 0.53) / 0.06) ** 2 <= 1:
                    return eye
            if ((x - 0.5) / 0.03) ** 2 + ((y - 0.63) / 0.02) ** 2 <= 1:
                return pink
            for sx in (-1, 1):  # whiskers
                for dy in (-0.02, 0.02):
                    t = (x - 0.5) * sx
                    if 0.1 < t < 0.3 and abs(y - (0.65 + dy * t * 6)) < 0.004:
                        return dark
            return fur
        return None
    SS = 3

    def pixel(px, py, size=480):
        acc, n = [0, 0, 0], 0
        for i in range(SS):
            for j in range(SS):
                c = sample((px + (i + .5) / SS) / size, (py + (j + .5) / SS) / size)
                if c:
                    n += 1
                    for k in range(3):
                        acc[k] += c[k]
        if not n:
            return (0, 0, 0, 0)
        return (acc[0] // n, acc[1] // n, acc[2] // n, 255 * n // (SS * SS))
    return pixel


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    png(OUT / "cat.png", 480, 480, cat_pixel(False))
    png(OUT / "cat-asleep.png", 480, 480, cat_pixel(True))
    rng = random.Random(1)
    tile = [[rng.randint(0, 255) for _ in range(64)] for _ in range(64)]
    png(OUT / "paper.png", 64, 64, lambda x, y: (90, 70, 40, tile[y][x] // 16))
    print("wrote", ", ".join(p.name for p in sorted(OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
