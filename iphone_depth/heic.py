"""HEIC auxiliary/depth image extraction via pillow-heif (libheif).

Apple stores Portrait-mode depth as:
  - a first-class HEIF depth image (exposed as ``info["depth_images"]``,
    with metric metadata such as d_min/d_max/z_near/z_far when present), or
  - auxiliary images identified by URN, e.g.
      urn:com:apple:photo:2017:aux:disparity
      urn:com:apple:photo:2018:aux:portraiteffectsmatte
      urn:com:apple:photo:2019:aux:semantic*matte
      urn:com:apple:photo:2020:aux:hdrgainmap
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()


@dataclass
class AuxResult:
    kind: str                 # "depth" | "aux"
    label: str                # URN suffix or "depth"
    urn: str
    image: Image.Image
    metadata: dict[str, Any] = field(default_factory=dict)


def _label_from_urn(urn: str) -> str:
    return urn.rsplit(":", 1)[-1] if ":" in urn else urn


def open_heic(path: str) -> pillow_heif.HeifFile:
    return pillow_heif.open_heif(path)


def list_aux(heif: pillow_heif.HeifFile) -> dict[str, list[int]]:
    """Map of aux-type URN -> aux image ids for the primary image."""
    primary = heif[heif.primary_index]
    return dict(primary.info.get("aux") or {})


def extract_all(heif: pillow_heif.HeifFile) -> list[AuxResult]:
    results: list[AuxResult] = []
    primary = heif[heif.primary_index]

    for depth_img in primary.info.get("depth_images") or []:
        meta = dict(depth_img.info.get("metadata") or {})
        results.append(AuxResult("depth", "depth", "heif:depth-image",
                                 depth_img.to_pillow(), meta))

    for urn, ids in list_aux(heif).items():
        for aux_id in ids:
            aux = primary.get_aux_image(aux_id)
            results.append(AuxResult("aux", _label_from_urn(urn), urn, aux.to_pillow()))
    return results
