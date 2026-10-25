"""Rasterize the project's geometric SVG design to a Windows ICO, stdlib only."""
import math
from pathlib import Path
import struct


def inside(x, y):
    # Matching vector primitives from assets/bluehead.svg.
    def rounded(x0, y0, x1, y1, radius):
        dx = max(x0 + radius - x, 0, x - (x1 - radius))
        dy = max(y0 + radius - y, 0, y - (y1 - radius))
        return dx * dx + dy * dy <= radius * radius
    if not rounded(0, 0, 64, 64, 17):
        return (0, 0, 0, 0)
    if rounded(14, 29, 24, 49, 5) or rounded(40, 29, 50, 49, 5):
        return (255, 255, 255, 255)
    distance = math.hypot(x - 32, y - 30)
    if y <= 30 and 14 <= distance <= 18:
        return (255, 255, 255, 255)
    if (14 <= x <= 18 or 46 <= x <= 50) and 29 <= y <= 34:
        return (255, 255, 255, 255)
    if rounded(27.5, 30.5, 30.5, 40.5, 1.5) or rounded(33.5, 27.5, 36.5, 43.5, 1.5):
        return (180, 242, 247, 255)
    return (7, 193, 96, 255)


def build(path):
    images = []
    sizes = (16, 32, 48, 64, 128)
    for size in sizes:
        pixels = bytearray()
        for row in range(size - 1, -1, -1):
            for col in range(size):
                samples = [inside((col + (sx + .5) / 4) * 64 / size,
                                  (row + (sy + .5) / 4) * 64 / size) for sx in range(4) for sy in range(4)]
                alpha = sum(s[3] for s in samples) / 16
                channels = [round(sum(s[c] * s[3] for s in samples) / (16 * alpha)) if alpha else 0 for c in range(3)]
                pixels.extend((*reversed(channels), round(alpha)))
        mask = bytes(((size + 31) // 32) * 4 * size)
        images.append(struct.pack('<IIIHHIIIIII', 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0) + pixels + mask)
    offset = 6 + 16 * len(images)
    result = bytearray(struct.pack('<HHH', 0, 1, len(images)))
    for size, image in zip(sizes, images):
        result.extend(struct.pack('<BBBBHHII', size, size, 0, 0, 1, 32, len(image), offset))
        offset += len(image)
    for image in images:
        result.extend(image)
    path.write_bytes(result)


if __name__ == '__main__':
    build(Path(__file__).resolve().parents[1] / 'bluehead/assets/bluehead.ico')
