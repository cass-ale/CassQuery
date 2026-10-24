"""Bounded PNG decoding independent of Pillow, for portable release evidence."""
import struct
import zlib
import binascii


def dimensions(data):
    if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data) > 32 * 1024**2:
        raise ValueError('invalid or excessive PNG')
    offset, header, packed, ended = 8, None, bytearray(), False
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError('truncated PNG chunk')
        length = struct.unpack('>I', data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(data):
            raise ValueError('truncated PNG data')
        content = data[offset + 8:offset + 8 + length]
        if binascii.crc32(kind + content) & 0xffffffff != struct.unpack('>I', data[end - 4:end])[0]:
            raise ValueError('PNG checksum mismatch')
        if header is None and kind != b'IHDR':
            raise ValueError('PNG missing initial header')
        if kind == b'IHDR':
            if header is not None or length != 13:
                raise ValueError('invalid PNG header')
            header = struct.unpack('>IIBBBBB', content)
        elif kind == b'IDAT':
            packed.extend(content)
        elif kind == b'IEND':
            if length or end != len(data):
                raise ValueError('invalid PNG end')
            ended = True
            break
        offset = end
    if not header or not packed or not ended:
        raise ValueError('incomplete PNG')
    width, height, depth, color, compression, filtering, interlace = header
    channels = {2: 3, 6: 4}.get(color)
    if depth != 8 or not channels or compression or filtering or interlace or not 0 < width <= 16384 or not 0 < height <= 16384:
        raise ValueError('unsupported PNG capture format')
    stride = width * channels
    expected = (stride + 1) * height
    if expected > 128 * 1024**2:
        raise ValueError('PNG decoded size exceeds bound')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(packed), expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('PNG deflate length mismatch')
    if any(raw[row * (stride + 1)] > 4 for row in range(height)):
        raise ValueError('invalid PNG row filter')
    return width, height
