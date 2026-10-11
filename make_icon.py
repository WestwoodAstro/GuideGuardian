#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Loran Hughes
# SPDX-License-Identifier: GPL-3.0-only
"""Generate assets/phd2guard.ico (16-256 px) from the icon drawn in phd2_guard.render_icon().

    python make_icon.py                  # writes assets/phd2guard.ico
    python make_icon.py --preview p.png  # also writes a contact sheet of all sizes (for a quick look)

build_exe.bat runs this before PyInstaller and passes the .ico to --icon. Sizes below 256 px are stored as classic
32-bit BMP entries (the most compatible form); 256 px is stored as PNG.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, Qt                    # noqa: E402
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter              # noqa: E402

import phd2_guard as g                                                          # noqa: E402


def png_bytes(img: QImage) -> bytes:
    data, buf = QByteArray(), QBuffer()
    buf.setBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def dib_bytes(img: QImage) -> bytes:
    """32-bit BGRA DIB (bottom-up) + empty AND mask, as stored in an ICO entry."""
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    s = img.width()
    ptr = img.constBits()
    ptr.setsize(img.sizeInBytes())
    raw = bytes(ptr)
    rows = [raw[y * s * 4:(y + 1) * s * 4] for y in range(s)]
    header = struct.pack("<IiiHHIIiiII", 40, s, 2 * s, 1, 32, 0, 0, 0, 0, 0, 0)
    return header + b"".join(reversed(rows)) + bytes(((s + 31) // 32 * 4) * s)


def ico_bytes(sizes) -> bytes:
    entries = []
    for s in sizes:
        img = g.render_icon(s)
        entries.append((s, png_bytes(img) if s >= 256 else dib_bytes(img)))
    out = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    body = b""
    for s, payload in entries:
        dim = 0 if s >= 256 else s                                   # 0 means 256 in an ICO directory
        out += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(payload), offset + len(body))
        body += payload
    return out + body


def preview(path: str) -> None:
    sizes = g.ICON_SIZES
    pad, scale = 16, 2
    width = sum(s * scale if s <= 64 else s for s in sizes) + pad * (len(sizes) + 1)
    sheet = QImage(width, 256 + 2 * pad, QImage.Format.Format_ARGB32)
    sheet.fill(QColor("#d9dde6"))
    p = QPainter(sheet)
    x = pad
    for s in sizes:
        img = g.render_icon(s)
        if s <= 64:
            img = img.scaled(s * scale, s * scale, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        p.drawImage(x, pad, img)
        x += img.width() + pad
    p.end()
    sheet.save(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "phd2guard.ico"))
    ap.add_argument("--preview", help="also write a contact sheet PNG")
    a = ap.parse_args()
    app = QGuiApplication(sys.argv[:1])
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "wb") as fh:
        fh.write(ico_bytes(g.ICON_SIZES))
    print(f"wrote {a.out} ({os.path.getsize(a.out) // 1024} KB, sizes {', '.join(map(str, g.ICON_SIZES))})")
    if a.preview:
        preview(a.preview)
        print(f"wrote {a.preview}")
    app.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
