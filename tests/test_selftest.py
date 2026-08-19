"""Self-test sintético: corre con `python tests/test_selftest.py` o pytest."""

from __future__ import annotations

import io
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from iphone_depth import colormap, depthmap, heic, jpeg_segments, pointcloud  # noqa: E402


def _tiny_jpeg(color=(200, 30, 30), size=(16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def _jpeg_with_mpf(primary: bytes, second: bytes) -> bytes:
    """Craft a minimal CIPA DC-007 MPF file: primary JPEG + appended second JPEG."""
    # MP header (II) + IFD with 3 tags + 2 MP entries of 16 bytes
    n_entries = 2
    ifd = struct.pack("<H", 3)
    entries_offset = 8 + 2 + 3 * 12 + 4  # after endian hdr + count + tags + next-IFD
    ifd += struct.pack("<HHI4s", 0xB000, 7, 4, b"0100")
    ifd += struct.pack("<HHII", 0xB001, 4, 1, n_entries)
    ifd += struct.pack("<HHII", 0xB002, 7, 16 * n_entries, entries_offset)
    ifd += struct.pack("<I", 0)  # next IFD
    mp_header = b"II*\x00" + struct.pack("<I", 8) + ifd

    payload_len = len(mp_header) + 16 * n_entries
    seg_len = 2 + 4 + payload_len
    app2_prefix = b"\xff\xe2" + struct.pack(">H", seg_len) + b"MPF\x00"

    out_prefix = primary[:2] + app2_prefix
    # offset of second image relative to MP endian header start
    mp_base = len(out_prefix)  # header starts right after "MPF\0"
    file_second_off = 2 + 2 + seg_len + len(primary[2:])
    rel_second = file_second_off - mp_base

    entries = struct.pack("<IIIHH", 0x030000, len(primary) + seg_len + 2, 0, 0, 0)
    entries += struct.pack("<IIIHH", 0x020002, len(second), rel_second, 0, 0)
    return out_prefix + mp_header + entries + primary[2:] + second


def test_colormap():
    x = np.linspace(0, 1, 256)
    rgb = colormap.turbo(x)
    assert rgb.shape == (256, 3) and rgb.dtype == np.uint8
    assert rgb[0, 2] > rgb[0, 0]      # low end is blue-ish
    assert rgb[-1, 0] > rgb[-1, 2]    # high end is red-ish


def test_depth_and_pointcloud(tmp_path: Path):
    h, w = 60, 40
    norm = np.tile(np.linspace(0.1, 0.9, w, dtype=np.float32), (h, 1))
    depth, metric = depthmap.disparity_to_depth(norm, d_min=0.5, d_max=4.0)
    assert metric and depth.max() <= 2.0 + 1e-5  # 1/0.5 m
    products = depthmap.save_products(norm, tmp_path, "syn")
    assert Path(products["turbo_png"]).exists()

    rgb = Image.fromarray(np.full((h, w, 3), 128, np.uint8))
    ply = tmp_path / "syn.ply"
    n = pointcloud.depth_to_ply(depth, ply, f35=26.0, photo=rgb, stride=1)
    header = ply.read_bytes()[:200].decode("ascii", "replace")
    assert f"element vertex {n}" in header and n == h * w


def test_jpeg_no_depth():
    rep = jpeg_segments.inspect_jpeg(_tiny_jpeg())
    assert not rep.has_mpf and not jpeg_segments.extract_embedded(_tiny_jpeg(), rep)


def test_jpeg_mpf_roundtrip():
    second = _tiny_jpeg((10, 10, 10), (8, 8))
    combo = _jpeg_with_mpf(_tiny_jpeg(), second)
    rep = jpeg_segments.inspect_jpeg(combo)
    assert rep.has_mpf and len(rep.mp_entries) == 2
    assert rep.mp_entries[1].type_name == "Multi-Frame Disparity"
    blobs = jpeg_segments.extract_embedded(combo, rep)
    assert len(blobs) == 1 and blobs[0] == second
    Image.open(io.BytesIO(blobs[0])).load()  # embedded image decodes


def test_heic_without_aux(tmp_path: Path):
    p = tmp_path / "plain.heic"
    Image.new("RGB", (32, 32), (0, 120, 200)).save(p, "HEIF")
    hf = heic.open_heic(str(p))
    assert heic.extract_all(hf) == [] and heic.list_aux(hf) == {}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_colormap()
        test_depth_and_pointcloud(tmp)
        test_jpeg_no_depth()
        test_jpeg_mpf_roundtrip()
        test_heic_without_aux(tmp)
    print("OK: 5/5 tests sintéticos pasaron")
    return 0


if __name__ == "__main__":
    sys.exit(main())
