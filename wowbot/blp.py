"""BLP-Texturen lesen (World of Warcraft 3.3.5a).

Der Client speichert seine Bilder als BLP. Fuer die Weltkarten brauchen wir
daraus nur die erste Mipmap-Stufe als RGBA-Bild. Unterstuetzt werden die
Varianten, die im 3.3.5a-Client vorkommen:

* BLP1: JPEG (Kanal 4 = Alpha) und palettiert
* BLP2: DXT1/DXT3/DXT5, palettiert (mit Alpha-Tiefe 0/1/4/8) und roh BGRA

Alles ohne native Abhaengigkeiten - die DXT-Bloecke werden in Python
entpackt, das reicht fuer Kartenkacheln (256 px) in wenigen Millisekunden.
"""

from __future__ import annotations

import struct

from PIL import Image

__all__ = ["is_blp", "decode_blp", "BLPError"]


class BLPError(Exception):
    """Datei ist kein lesbares BLP."""


# --------------------------------------------------------------------------
# DXT
# --------------------------------------------------------------------------


def _rgb565(c: int) -> tuple[int, int, int]:
    r = (c >> 11) & 0x1F
    g = (c >> 5) & 0x3F
    b = c & 0x1F
    # 5/6 Bit auf 8 Bit spreizen, damit die Helligkeit stimmt
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def _decode_dxt1(
    data: bytes, width: int, height: int, offset: int = 0, stride: int = 8
) -> tuple[bytearray, bytearray]:
    """DXT1 -> (RGB-Bytes, Alpha-Bytes). 1-Bit-Alpha wird mitgenommen.

    Bei DXT3/DXT5 stehen vor jedem Farbblock die 8 Alpha-Bytes: dann beginnt
    der Farbteil bei offset=8 und jeder Block ist 16 statt 8 Bytes lang.
    """
    rgb = bytearray(width * height * 3)
    alpha = bytearray(b"\xff" * (width * height))
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    pos = offset
    for by in range(bh):
        for bx in range(bw):
            if pos + 8 > len(data):
                return rgb, alpha
            c0, c1, bits = struct.unpack_from("<HHI", data, pos)
            pos += stride
            r0, g0, b0 = _rgb565(c0)
            r1, g1, b1 = _rgb565(c1)
            if c0 > c1:
                palette = (
                    (r0, g0, b0, 255),
                    (r1, g1, b1, 255),
                    ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3, 255),
                    ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3, 255),
                )
            else:
                palette = (
                    (r0, g0, b0, 255),
                    (r1, g1, b1, 255),
                    ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255),
                    (0, 0, 0, 0),
                )
            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    break
                row = y * width
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        break
                    idx = (bits >> (2 * (py * 4 + px))) & 3
                    cr, cg, cb, ca = palette[idx]
                    o = (row + x) * 3
                    rgb[o] = cr
                    rgb[o + 1] = cg
                    rgb[o + 2] = cb
                    alpha[row + x] = ca
    return rgb, alpha


def _decode_dxt3_alpha(
    data: bytes, width: int, height: int, offset: int = 0, stride: int = 8
) -> bytearray:
    alpha = bytearray(width * height)
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    pos = offset
    for by in range(bh):
        for bx in range(bw):
            if pos + 8 > len(data):
                return alpha
            block = struct.unpack_from("<Q", data, pos)[0]
            pos += stride
            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    break
                row = y * width
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        break
                    nib = (block >> (4 * (py * 4 + px))) & 0xF
                    alpha[row + x] = nib * 17
    return alpha


def _decode_dxt5_alpha(
    data: bytes, width: int, height: int, offset: int = 0, stride: int = 8
) -> bytearray:
    alpha = bytearray(width * height)
    bw = (width + 3) // 4
    bh = (height + 3) // 4
    pos = offset
    for by in range(bh):
        for bx in range(bw):
            if pos + 8 > len(data):
                return alpha
            a0, a1 = data[pos], data[pos + 1]
            bits = int.from_bytes(data[pos + 2:pos + 8], "little")
            pos += stride
            # DXT5: die sechs Zwischenwerte sind (6-i)*a0 + (1+i)*a1)/7 bzw.
            # (4-i)*a0 + (1+i)*a1)/5 - die Gewichte summieren sich auf 7 bzw. 5.
            # Mit 8 als Summe entstehen Werte > 255 und das Entpacken bricht ab.
            if a0 > a1:
                table = [a0, a1] + [
                    ((6 - i) * a0 + (1 + i) * a1) // 7 for i in range(6)
                ]
            else:
                table = [a0, a1] + [
                    ((4 - i) * a0 + (1 + i) * a1) // 5 for i in range(4)
                ] + [0, 255]
            for py in range(4):
                y = by * 4 + py
                if y >= height:
                    break
                row = y * width
                for px in range(4):
                    x = bx * 4 + px
                    if x >= width:
                        break
                    idx = (bits >> (3 * (py * 4 + px))) & 7
                    alpha[row + x] = table[idx]
    return alpha


def _decode_dxt(data: bytes, width: int, height: int, alpha_bits: int) -> Image.Image:
    """DXT1/3/5 entpacken. alpha_bits: 0 = DXT1, 1 = DXT3, 8 = DXT5."""
    if alpha_bits == 1:
        alpha = _decode_dxt3_alpha(data, width, height, 0, 16)
        rgb, _ = _decode_dxt1(data, width, height, 8, 16)
        img = Image.frombytes("RGB", (width, height), bytes(rgb))
        return _with_alpha(img, alpha)
    if alpha_bits == 8:
        alpha = _decode_dxt5_alpha(data, width, height, 0, 16)
        rgb, _ = _decode_dxt1(data, width, height, 8, 16)
        return _with_alpha(Image.frombytes("RGB", (width, height), bytes(rgb)), alpha)
    # DXT1: 1-Bit-Alpha steckt schon in den Farbblöcken (c0 <= c1)
    rgb, alpha = _decode_dxt1(data, width, height)
    img = Image.frombytes("RGB", (width, height), bytes(rgb))
    if min(alpha) < 255:
        return _with_alpha(img, alpha)
    return img


def _with_alpha(img: Image.Image, alpha: bytearray) -> Image.Image:
    img = img.convert("RGBA")
    img.putalpha(Image.frombytes("L", img.size, bytes(alpha)))
    return img





# --------------------------------------------------------------------------
# Palettiert / roh
# --------------------------------------------------------------------------


def _decode_paletted(
    data: bytes, width: int, height: int, palette: bytes, alpha_bits: int, alpha: bytes
) -> Image.Image:
    px = width * height
    if len(data) < px:
        raise BLPError("Palettendaten zu kurz")
    rgb = bytearray(px * 3)
    for i in range(px):
        o = data[i] * 4
        if o + 3 > len(palette):
            break
        b = palette[o]
        g = palette[o + 1]
        r = palette[o + 2]
        j = i * 3
        rgb[j] = r
        rgb[j + 1] = g
        rgb[j + 2] = b
    img = Image.frombytes("RGB", (width, height), bytes(rgb))
    if alpha_bits:
        img.putalpha(_palette_alpha(alpha, px, alpha_bits, width, height))
    return img


def _palette_alpha(raw: bytes, px: int, alpha_bits: int, width: int, height: int) -> Image.Image:
    out = bytearray(px)
    if alpha_bits == 8:
        out[: min(px, len(raw))] = raw[: min(px, len(raw))]
    elif alpha_bits == 4:
        for i in range(px):
            byte = raw[i // 2] if i // 2 < len(raw) else 0
            nib = (byte >> 4) if i % 2 == 0 else (byte & 0xF)
            out[i] = nib * 17
    elif alpha_bits == 1:
        for i in range(px):
            byte = raw[i // 8] if i // 8 < len(raw) else 0
            out[i] = 255 if (byte >> (7 - i % 8)) & 1 else 0
    else:
        out = bytearray(b"\xff" * px)
    return Image.frombytes("L", (width, height), bytes(out))


# --------------------------------------------------------------------------
# Oeffentliche Schnittstelle
# --------------------------------------------------------------------------


def _first_mip(data: bytes) -> tuple[int, int]:
    """Offset und Größe der ersten Mipmap-Stufe.

    Im Kopf stehen 16 Offsets (ab Byte 20) und danach 16 Größen (ab Byte 84) -
    die Größe liegt also NICHT direkt hinter dem Offset.
    """
    offset = struct.unpack_from("<I", data, 20)[0]
    size = struct.unpack_from("<I", data, 84)[0]
    if not offset or not size:
        size = max(0, len(data) - offset)
    return offset, min(size, max(0, len(data) - offset))


def is_blp(data: bytes) -> bool:
    return data[:4] in (b"BLP1", b"BLP2")


def decode_blp(data: bytes) -> Image.Image:
    """Erste Mipmap-Stufe als RGB(A)-Bild."""
    if len(data) < 148 or not is_blp(data):
        raise BLPError("keine BLP-Datei")

    version = data[:4]
    if version == b"BLP1":
        typ, alpha_bits, width, height = struct.unpack_from("<4I", data, 4)
        has_alpha = alpha_bits in (1, 4, 8)
        mip_off, mip_size = _first_mip(data)
        body = data[mip_off: mip_off + mip_size]
        if typ == 0:  # JPEG
            return _decode_jpeg(body, width, height, has_alpha)
        palette = data[mip_off + mip_size: mip_off + mip_size + 1024]
        alpha = data[mip_off + mip_size + 1024:]
        return _decode_paletted(body, width, height, palette, alpha_bits if has_alpha else 0, alpha)

    # BLP2
    typ = struct.unpack_from("<I", data, 4)[0]
    compression = data[8]
    alpha_depth = data[9]
    width, height = struct.unpack_from("<II", data, 12)
    mip_off, mip_size = _first_mip(data)
    body = data[mip_off: mip_off + mip_size]
    if compression == 2:  # DXT: alpha_depth 1 = DXT1, 4 = DXT3, 8 = DXT5
        if alpha_depth == 4:
            return _decode_dxt(body, width, height, 1)
        if alpha_depth == 8:
            return _decode_dxt(body, width, height, 8)
        return _decode_dxt(body, width, height, 0)
    if compression == 1:  # palettiert: Palette direkt nach dem Kopf (148 Bytes)
        palette = data[148:148 + 1024]
        alpha = data[mip_off + width * height:]
        return _decode_paletted(body, width, height, palette, alpha_depth, alpha)
    if compression == 3:  # roh BGRA
        img = Image.frombytes("RGBA", (width, height), body[: width * height * 4])
        return img
    raise BLPError(f"unbekannte Kompression {compression}")


def _decode_jpeg(body: bytes, width: int, height: int, has_alpha: bool) -> Image.Image:
    """BLP1-JPEG: 4 Kanaele, der vierte traegt das Alpha."""
    from io import BytesIO

    img = Image.open(BytesIO(body))
    img.load()
    if img.mode == "CMYK":
        # WoW legt Alpha in den Scharzkanal: C=M=Y=Bild, K=255-Alpha
        c, m, y, k = img.split()
        rgb = Image.merge("RGB", (c, m, y))
        if has_alpha:
            alpha = k.point(lambda v: 255 - v)
            rgb = rgb.convert("RGBA")
            rgb.putalpha(alpha)
        return rgb
    return img.convert("RGB")
