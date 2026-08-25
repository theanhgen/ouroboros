"""Holographic Reduced Representations (HRR) with phase encoding.

Adapted from NousResearch/hermes-agent (MIT license).

HRRs encode compositional structure into fixed-width distributed
representations using phase vectors (angles in [0, 2pi)). Operations:

  bind   -- circular convolution (phase addition)  -- associates two concepts
  unbind -- circular correlation (phase subtraction) -- retrieves a bound value
  bundle -- superposition (circular mean)           -- merges multiple concepts

Atoms are generated deterministically from SHA-256 so representations are
identical across processes and machines.
"""

import hashlib
import logging
import math
import struct
from typing import List

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

log = logging.getLogger(__name__)

_TWO_PI = 2.0 * math.pi


def _require_numpy() -> None:
    if not HAS_NUMPY:
        raise RuntimeError("numpy is required for holographic operations")


def encode_atom(word: str, dim: int = 1024) -> "np.ndarray":
    """Deterministic phase vector via SHA-256 counter blocks.

    Raises ValueError if dim is not positive. A non-positive dim would
    otherwise yield a length-0 vector that every HRR operation accepts
    silently -- similarity() on two of them returns nan, which corrupts
    ranking instead of failing.
    """
    _require_numpy()
    if dim <= 0:
        raise ValueError(f"dim must be positive, got {dim}")
    values_per_block = 16
    blocks_needed = math.ceil(dim / values_per_block)
    uint16_values: list[int] = []
    for i in range(blocks_needed):
        digest = hashlib.sha256(f"{word}:{i}".encode()).digest()
        uint16_values.extend(struct.unpack("<16H", digest))
    return np.array(uint16_values[:dim], dtype=np.float64) * (_TWO_PI / 65536.0)


def bind(a: "np.ndarray", b: "np.ndarray") -> "np.ndarray":
    """Circular convolution -- element-wise phase addition."""
    _require_numpy()
    return (a + b) % _TWO_PI


def unbind(memory: "np.ndarray", key: "np.ndarray") -> "np.ndarray":
    """Circular correlation -- element-wise phase subtraction."""
    _require_numpy()
    return (memory - key) % _TWO_PI


def bundle(*vectors: "np.ndarray") -> "np.ndarray":
    """Superposition via circular mean of complex exponentials."""
    _require_numpy()
    if not vectors:
        raise ValueError("At least one vector must be provided to bundle")
    complex_sum = np.sum([np.exp(1j * v) for v in vectors], axis=0)
    return np.angle(complex_sum) % _TWO_PI


def similarity(a: "np.ndarray", b: "np.ndarray") -> float:
    """Phase cosine similarity. Range [-1, 1]."""
    _require_numpy()
    if a.shape != b.shape:
        raise ValueError(f"Vector shapes must match: {a.shape} vs {b.shape}")
    if a.size == 0:
        return 0.0
    value = float(np.mean(np.cos(a - b)))
    if math.isnan(value):
        return 0.0
    return value


def encode_text(text: str, dim: int = 1024) -> "np.ndarray":
    """Bag-of-words: bundle of atom vectors for each token."""
    _require_numpy()
    tokens = [
        token.strip(".,!?;:\"'()[]{}")
        for token in text.lower().split()
    ]
    tokens = [t for t in tokens if t]
    if not tokens:
        return encode_atom("__hrr_empty__", dim)
    return bundle(*[encode_atom(token, dim) for token in tokens])


def encode_fact(content: str, entities: List[str], dim: int = 1024) -> "np.ndarray":
    """Structured encoding: content bound to ROLE_CONTENT, entities bound to ROLE_ENTITY, all bundled."""
    _require_numpy()
    role_content = encode_atom("__hrr_role_content__", dim)
    role_entity = encode_atom("__hrr_role_entity__", dim)
    components = [bind(encode_text(content, dim), role_content)]
    for entity in entities:
        components.append(bind(encode_atom(entity.lower(), dim), role_entity))
    return bundle(*components)


def phases_to_bytes(phases: "np.ndarray") -> bytes:
    """Serialize phase vector to bytes."""
    _require_numpy()
    return phases.tobytes()


def bytes_to_phases(data: bytes) -> "np.ndarray":
    """Deserialize bytes back to phase vector."""
    _require_numpy()
    return np.frombuffer(data, dtype=np.float64).copy()


def snr_estimate(dim: int, n_items: int) -> float:
    """Signal-to-noise ratio estimate for holographic storage."""
    if n_items <= 0:
        return float("inf")
    snr = math.sqrt(dim / n_items)
    if snr < 2.0:
        log.warning(
            "HRR storage near capacity: SNR=%.2f (dim=%d, n_items=%d)",
            snr, dim, n_items,
        )
    return snr
