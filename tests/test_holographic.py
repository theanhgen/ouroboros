import math
import numpy as np
import pytest
from ouroboros.holographic import (
    encode_atom,
    bind,
    unbind,
    bundle,
    similarity,
    encode_text,
    encode_fact,
    phases_to_bytes,
    bytes_to_phases,
    snr_estimate,
)

def test_encode_atom():
    v1 = encode_atom("hello", dim=100)
    v2 = encode_atom("hello", dim=100)
    v3 = encode_atom("world", dim=100)
    
    assert v1.shape == (100,)
    assert np.all(v1 >= 0) and np.all(v1 < 2.0 * np.pi)
    assert np.array_equal(v1, v2)
    assert not np.array_equal(v1, v3)

@pytest.mark.parametrize("dim", [0, -1, -1024])
def test_encode_atom_rejects_non_positive_dim(dim):
    with pytest.raises(ValueError, match="dim must be positive"):
        encode_atom("hello", dim=dim)

def test_encode_atom_accepts_smallest_valid_dim():
    assert encode_atom("hello", dim=1).shape == (1,)

@pytest.mark.parametrize("dim", [0, -1])
def test_encode_text_rejects_non_positive_dim(dim):
    with pytest.raises(ValueError, match="dim must be positive"):
        encode_text("the cat", dim=dim)

@pytest.mark.parametrize("dim", [0, -1])
def test_encode_text_rejects_non_positive_dim_when_no_tokens(dim):
    # The empty-token path takes a different branch to encode_atom.
    with pytest.raises(ValueError, match="dim must be positive"):
        encode_text("...", dim=dim)

@pytest.mark.parametrize("dim", [0, -1])
def test_encode_fact_rejects_non_positive_dim(dim):
    with pytest.raises(ValueError, match="dim must be positive"):
        encode_fact("content", ["entity"], dim=dim)

def test_bind_unbind():
    a = encode_atom("key", dim=64)
    b = encode_atom("val", dim=64)
    bound = bind(a, b)
    
    assert bound.shape == (64,)
    # Check that binding is associative/symmetric
    assert np.allclose(bound, bind(b, a))
    
    # Check unbinding retrieves the original key or value
    retrieved_b = unbind(bound, a)
    assert np.allclose(retrieved_b, b)
    
    retrieved_a = unbind(bound, b)
    assert np.allclose(retrieved_a, a)

def test_bundle():
    a = encode_atom("apple", dim=128)
    b = encode_atom("banana", dim=128)
    c = encode_atom("cherry", dim=128)
    
    bundled = bundle(a, b, c)
    assert bundled.shape == (128,)
    
    # Similarity with components should be high
    assert similarity(bundled, a) > 0.1
    assert similarity(bundled, b) > 0.1
    assert similarity(bundled, c) > 0.1
    
    # Similarity with unrelated atom should be low
    d = encode_atom("dog", dim=128)
    assert similarity(bundled, d) < similarity(bundled, a)

def test_bundle_empty():
    with pytest.raises(ValueError, match="At least one vector must be provided to bundle"):
        bundle()

def test_similarity():
    a = encode_atom("cat", dim=64)
    b = encode_atom("cat", dim=64)
    c = encode_atom("dog", dim=64)
    
    assert math.isclose(similarity(a, b), 1.0, abs_tol=1e-6)
    assert similarity(a, c) < 0.5
    assert -1.0 <= similarity(a, c) <= 1.0

def test_encode_text():
    text = "The quick brown fox."
    v = encode_text(text, dim=128)
    assert v.shape == (128,)
    
    # Empty text fallback
    v_empty = encode_text("", dim=128)
    v_ref = encode_atom("__hrr_empty__", dim=128)
    assert np.array_equal(v_empty, v_ref)

def test_encode_fact():
    content = "likes cheese"
    entities = ["Alice", "Bob"]
    fact = encode_fact(content, entities, dim=128)
    assert fact.shape == (128,)
    
    role_content = encode_atom("__hrr_role_content__", 128)
    role_entity = encode_atom("__hrr_role_entity__", 128)
    
    content_vec = encode_text(content, 128)
    alice_vec = encode_atom("alice", 128)
    
    # Unbinding role should give content
    unbound_content = unbind(fact, role_content)
    assert similarity(unbound_content, content_vec) > 0.1
    
    unbound_entity = unbind(fact, role_entity)
    assert similarity(unbound_entity, alice_vec) > 0.1

def test_serialization():
    v = encode_atom("serialize", dim=128)
    data = phases_to_bytes(v)
    v_rec = bytes_to_phases(data)
    assert np.array_equal(v, v_rec)

def test_snr_estimate(caplog):
    assert snr_estimate(100, 0) == float("inf")
    assert snr_estimate(1024, 4) == 16.0
    
    # Check warning when SNR is low
    with caplog.at_level("WARNING"):
        snr = snr_estimate(16, 8)
        assert snr < 2.0
        assert any("HRR storage near capacity" in record.message for record in caplog.records)
