"""Measuring overlap between photos with feature matching (OpenCV SIFT).

Two photos "overlap" when enough matched features agree on one geometric
relation (RANSAC on the fundamental matrix) — the same test photogrammetry
needs to connect them. The result is a graph: which photos connect, which are
isolated, and whether the set falls apart into separate groups.
"""

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_SIDE = 800
MAX_FEATURES = 1500
RATIO = 0.75
MIN_INLIERS = 30
# Every pair is compared up to this many photos (2,211 pairs for 67 photos took 20 s on a
# 6-core Intel Mac). Above it, each photo is compared with its neighbours in upload order
# and with the photos that look most similar (image retrieval with a small visual
# vocabulary, like COLMAP's vocabulary tree): upload order is often not capture order.
EXHAUSTIVE_LIMIT = 120
NEIGHBOURS = 8
RETRIEVED = 12  # most similar photos compared per photo
VOCABULARY = 256  # visual words


@dataclass(frozen=True, slots=True)
class OverlapResult:
    keys: list[str]  # caller's identifiers, in input order
    pairs_checked: int
    partners: dict[str, int]
    groups: list[list[str]]  # connected groups, largest first
    exhaustive: bool

    @property
    def isolated(self) -> list[str]:
        return [k for k in self.keys if self.partners[k] == 0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "keys": self.keys,
            "pairs_checked": self.pairs_checked,
            "partners": self.partners,
            "groups": self.groups,
            "exhaustive": self.exhaustive,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OverlapResult":
        return cls(
            keys=list(data["keys"]),
            pairs_checked=int(data["pairs_checked"]),
            partners={str(k): int(v) for k, v in data["partners"].items()},
            groups=[list(g) for g in data["groups"]],
            exhaustive=bool(data["exhaustive"]),
        )


def _features(path: Path, sift: Any) -> tuple[np.ndarray, np.ndarray | None]:
    with Image.open(path) as img:
        gray = ImageOps.exif_transpose(img).convert("L")
        gray.thumbnail((MAX_SIDE, MAX_SIDE))
        pixels = np.asarray(gray)
    keypoints, descriptors = sift.detectAndCompute(pixels, None)
    return np.array([k.pt for k in keypoints], dtype=np.float32).reshape(-1, 2), descriptors


def _inliers(
    a: tuple[np.ndarray, np.ndarray | None], b: tuple[np.ndarray, np.ndarray | None], matcher: Any
) -> int:
    if a[1] is None or b[1] is None or len(a[1]) < 8 or len(b[1]) < 8:
        return 0
    pairs = matcher.knnMatch(a[1], b[1], k=2)
    good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < RATIO * n.distance]
    if len(good) < 8:
        return 0
    src = a[0][[m.queryIdx for m in good]]
    dst = b[0][[m.trainIdx for m in good]]
    _, mask = cv2.findFundamentalMat(src, dst, cv2.FM_RANSAC, 2.0, 0.999)
    return int(mask.sum()) if mask is not None else 0


def _vocabulary_histograms(descriptors: list[np.ndarray | None]) -> np.ndarray:
    """TF-IDF weighted, L2-normalised visual-word histograms, one row per photo."""
    present = [d for d in descriptors if d is not None and len(d)]
    sample = np.vstack(present).astype(np.float32)
    rng = np.random.default_rng(0)
    if len(sample) > 60_000:
        sample = sample[rng.choice(len(sample), 60_000, replace=False)]
    words = min(VOCABULARY, max(2, len(sample) // 20))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1e-3)
    cv2.setRNGSeed(0)
    labels = np.zeros((len(sample), 1), dtype=np.int32)
    _, _, centres = cv2.kmeans(sample, words, labels, criteria, 1, cv2.KMEANS_PP_CENTERS)
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    counts = np.zeros((len(descriptors), words), dtype=np.float64)
    for row, desc in enumerate(descriptors):
        if desc is None or not len(desc):
            continue
        for match in matcher.match(desc.astype(np.float32), centres):
            counts[row, match.trainIdx] += 1
    document_frequency = (counts > 0).sum(axis=0)
    idf = np.log((len(descriptors) + 1) / (document_frequency + 1))
    weighted = counts * idf
    norms = np.linalg.norm(weighted, axis=1, keepdims=True)
    normalised: np.ndarray = np.divide(
        weighted, norms, out=np.zeros_like(weighted), where=norms > 0
    )
    return normalised


def _pairs(
    features: list[tuple[np.ndarray, np.ndarray | None]], exhaustive_limit: int = EXHAUSTIVE_LIMIT
) -> tuple[list[tuple[int, int]], bool]:
    count = len(features)
    if count <= exhaustive_limit:
        return list(itertools.combinations(range(count), 2)), True
    pairs = {(i, j) for i in range(count) for j in range(i + 1, min(count, i + NEIGHBOURS + 1))}
    similarity = _vocabulary_histograms([d for _, d in features])
    similarity = similarity @ similarity.T
    np.fill_diagonal(similarity, -1.0)
    for i in range(count):
        for j in np.argsort(-similarity[i])[:RETRIEVED]:
            pairs.add((min(i, int(j)), max(i, int(j))))
    return sorted(pairs), False


def _groups(keys: list[str], edges: list[tuple[int, int]]) -> list[list[str]]:
    parent = list(range(len(keys)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in edges:
        parent[find(i)] = find(j)
    groups: dict[int, list[str]] = {}
    for i, key in enumerate(keys):
        groups.setdefault(find(i), []).append(key)
    return sorted(groups.values(), key=len, reverse=True)


def assess_overlap(
    photos: Sequence[tuple[str, Path]], *, exhaustive_limit: int = EXHAUSTIVE_LIMIT
) -> OverlapResult:
    """``photos`` are (key, path) pairs in capture/upload order."""
    keys = [key for key, _ in photos]
    sift = cv2.SIFT.create(nfeatures=MAX_FEATURES)
    features = [_features(path, sift) for _, path in photos]
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    pairs, exhaustive = _pairs(features, exhaustive_limit)
    edges = [(i, j) for i, j in pairs if _inliers(features[i], features[j], matcher) >= MIN_INLIERS]
    partners = dict.fromkeys(keys, 0)
    for i, j in edges:
        partners[keys[i]] += 1
        partners[keys[j]] += 1
    return OverlapResult(
        keys=keys,
        pairs_checked=len(pairs),
        partners=partners,
        groups=_groups(keys, edges),
        exhaustive=exhaustive,
    )
