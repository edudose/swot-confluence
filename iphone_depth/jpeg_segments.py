"""JPEG container inspection: APPn segments, MPF (Multi-Picture Format) and
embedded trailing images.

iPhone JPEG portraits that keep their depth data carry it as extra JPEG
streams appended after the primary image, indexed by an APP2 "MPF" segment
(CIPA DC-007). If that segment is missing there is no depth in the file.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

# CIPA DC-007 MP Entry type codes (bits 23..0 of the attribute word)
MP_TYPE_CODES = {
    0x030000: "Baseline MP Primary Image",
    0x010001: "Large Thumbnail (VGA)",
    0x010002: "Large Thumbnail (Full HD)",
    0x020001: "Multi-Frame Panorama",
    0x020002: "Multi-Frame Disparity",
    0x020003: "Multi-Angle",
    0x000000: "Undefined",
}


@dataclass
class Segment:
    marker: int
    offset: int
    length: int  # declared segment length (payload + 2)
    ident: str   # printable identifier prefix (e.g. "Exif", "MPF", "ICC_PROFILE")


@dataclass
class MPEntry:
    attribute: int
    size: int
    offset: int  # absolute offset within the file
    type_name: str


@dataclass
class JpegReport:
    segments: list[Segment] = field(default_factory=list)
    has_mpf: bool = False
    mp_entries: list[MPEntry] = field(default_factory=list)
    embedded: list[tuple[int, int]] = field(default_factory=list)  # (offset, size)


def _ident(payload: bytes) -> str:
    head = payload[:24].split(b"\x00")[0]
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        return ""
    return text if text.isprintable() else ""


def scan_segments(data: bytes) -> list[Segment]:
    """Walk the primary JPEG's marker segments until SOS."""
    segs: list[Segment] = []
    i = 2  # skip SOI
    n = len(data)
    while i + 4 <= n and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xD8:
            i += 2
            continue
        if marker in (0xDA, 0xD9):  # SOS / EOI: entropy-coded data follows
            break
        seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
        segs.append(Segment(marker, i, seglen, _ident(data[i + 4:i + 4 + 24])))
        i += 2 + seglen
    return segs


def _parse_mpf(data: bytes, seg: Segment) -> list[MPEntry]:
    """Parse the MP Index IFD of an APP2 MPF segment (CIPA DC-007)."""
    base = seg.offset + 8  # start of the MP endian header ("II*\0" / "MM\0*")
    endian = data[base:base + 4]
    if endian[:2] == b"II":
        fmt = "<"
    elif endian[:2] == b"MM":
        fmt = ">"
    else:
        return []
    (first_ifd,) = struct.unpack(fmt + "I", data[base + 4:base + 8])
    ifd = base + first_ifd
    (count,) = struct.unpack(fmt + "H", data[ifd:ifd + 2])
    num_images, entry_off = 0, None
    for k in range(count):
        tag_off = ifd + 2 + 12 * k
        tag, _typ, _cnt = struct.unpack(fmt + "HHI", data[tag_off:tag_off + 8])
        value = data[tag_off + 8:tag_off + 12]
        if tag == 0xB001:
            (num_images,) = struct.unpack(fmt + "I", value)
        elif tag == 0xB002:
            (entry_off,) = struct.unpack(fmt + "I", value)
    if not num_images or entry_off is None:
        return []
    entries = []
    for k in range(num_images):
        e = base + entry_off + 16 * k
        attr, size, off = struct.unpack(fmt + "III", data[e:e + 12])
        abs_off = 0 if off == 0 else base + off  # first image's offset is 0 by spec
        entries.append(MPEntry(attr, size, abs_off,
                               MP_TYPE_CODES.get(attr & 0xFFFFFF, hex(attr & 0xFFFFFF))))
    return entries


def _find_trailing_jpegs(data: bytes) -> list[tuple[int, int]]:
    """Fallback: locate concatenated JPEG streams after the primary EOI."""
    found = []
    end = data.find(EOI, 2)
    while end != -1:
        nxt = data.find(SOI + b"\xff", end + 2)
        if nxt == -1:
            break
        close = data.find(EOI, nxt + 2)
        size = (close + 2 - nxt) if close != -1 else len(data) - nxt
        found.append((nxt, size))
        end = close if close != -1 else -1
    return found


def inspect_jpeg(data: bytes) -> JpegReport:
    report = JpegReport(segments=scan_segments(data))
    for seg in report.segments:
        if seg.marker == 0xE2 and seg.ident == "MPF":
            report.has_mpf = True
            report.mp_entries = _parse_mpf(data, seg)
    report.embedded = _find_trailing_jpegs(data)
    return report


def extract_embedded(data: bytes, report: JpegReport) -> list[bytes]:
    """Return the byte streams of every non-primary embedded image."""
    blobs = []
    if report.mp_entries:
        for e in report.mp_entries:
            if e.offset:  # 0 = primary image
                blobs.append(data[e.offset:e.offset + e.size])
    else:
        for off, size in report.embedded:
            blobs.append(data[off:off + size])
    return blobs
