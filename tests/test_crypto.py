import pytest

from drivemesh import crypto


def test_chunk_roundtrip_and_overhead():
    dek = crypto.new_dek()
    aad = crypto.chunk_aad("f1", 1, 0, True)
    obj = crypto.encrypt_chunk(dek, b"hello" * 1000, aad)
    assert len(obj) == 5000 + crypto.CHUNK_OVERHEAD  # W-25: 32 B instead of +33 %
    assert crypto.decrypt_chunk(dek, obj, aad) == b"hello" * 1000


@pytest.mark.parametrize(
    "other",
    [
        ("f2", 1, 0, True),  # replayed from another file
        ("f1", 2, 0, True),  # from another version
        ("f1", 1, 1, True),  # moved to another position (W-10 swap)
        ("f1", 1, 0, False),  # last-flag stripped (truncation)
    ],
)
def test_aad_binds_chunk_position(other):
    dek = crypto.new_dek()
    obj = crypto.encrypt_chunk(dek, b"data", crypto.chunk_aad("f1", 1, 0, True))
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_chunk(dek, obj, crypto.chunk_aad(*other))


def test_tampered_chunk_rejected():
    dek = crypto.new_dek()
    aad = crypto.chunk_aad("f", 1, 0, True)
    obj = bytearray(crypto.encrypt_chunk(dek, b"x" * 100, aad))
    obj[40] ^= 1
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt_chunk(dek, bytes(obj), aad)


def test_wrap_unwrap_and_rotation():
    ring1 = crypto.KeyRing.parse(crypto.generate_key_spec("k1"))
    dek = crypto.new_dek()
    blob, kid = crypto.wrap_dek(ring1, dek, "file-1")
    assert kid == "k1"
    spec2 = ",".join(
        [
            f"k1:{__import__('base64').urlsafe_b64encode(ring1.keys['k1']).decode()}",
            crypto.generate_key_spec("k2"),
        ]
    )
    ring2 = crypto.KeyRing.parse(spec2, "k2")
    assert crypto.unwrap_dek(ring2, blob, kid, "file-1") == dek  # old key still readable
    _blob2, kid2 = crypto.wrap_dek(ring2, dek, "file-1")
    assert kid2 == "k2"
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_dek(ring2, blob, kid, "file-2")  # bound to file id


def test_seal_is_self_describing():
    ring = crypto.KeyRing.parse(crypto.generate_key_spec("main"))
    blob = crypto.seal(ring, b"token", "account|a1")
    assert crypto.sealed_key_id(blob) == "main"
    assert crypto.unseal(ring, blob, "account|a1") == b"token"
    with pytest.raises(crypto.CryptoError):
        crypto.unseal(ring, blob, "account|a2")


@pytest.mark.parametrize("spec", ["", "nocolon", "k1:!!!", "k1:YWJj"])
def test_keyring_rejects_bad_specs(spec):
    with pytest.raises(crypto.KeyRingError):
        crypto.KeyRing.parse(spec)


def test_missing_key_id():
    ring = crypto.KeyRing.parse(crypto.generate_key_spec("k1"))
    with pytest.raises(crypto.KeyRingError):
        ring.get("k9")


def test_password_hashing():
    h = crypto.hash_password("s3cret")
    assert crypto.verify_password("s3cret", h)
    assert not crypto.verify_password("wrong", h)
    assert not crypto.verify_password("s3cret", "garbage")
