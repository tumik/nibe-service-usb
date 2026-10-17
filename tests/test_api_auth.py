import base64
import hashlib
import hmac

from nibe_service_usb.api import auth

KEY = bytes(range(32))
SERVER_NONCE = bytes(range(100, 132))
CLIENT_NONCE = bytes(range(200, 232))
S_B64 = base64.b64encode(SERVER_NONCE).decode()
C_B64 = base64.b64encode(CLIENT_NONCE).decode()


def test_supported_proto():
    assert auth.SUPPORTED_PROTO == {1}


def test_make_nonce_is_32_random_bytes_base64():
    a, b = auth.make_nonce(), auth.make_nonce()
    assert a != b
    assert len(base64.b64decode(a, validate=True)) == 32


def test_proof_is_hmac_over_raw_nonce_bytes():
    expected = hmac.new(KEY, SERVER_NONCE + CLIENT_NONCE, hashlib.sha256).hexdigest()
    assert auth.compute_proof(KEY, S_B64, C_B64) == expected
    # not computed over the base64 text
    wrong = hmac.new(KEY, (S_B64 + C_B64).encode(), hashlib.sha256).hexdigest()
    assert auth.compute_proof(KEY, S_B64, C_B64) != wrong


def test_server_proof_reverses_order():
    expected = hmac.new(KEY, CLIENT_NONCE + SERVER_NONCE, hashlib.sha256).hexdigest()
    assert auth.compute_server_proof(KEY, S_B64, C_B64) == expected
    assert auth.compute_server_proof(KEY, S_B64, C_B64) != auth.compute_proof(KEY, S_B64, C_B64)


def test_verify_proof():
    proof = auth.compute_proof(KEY, S_B64, C_B64)
    assert auth.verify_proof(KEY, S_B64, C_B64, proof)
    assert auth.verify_proof(KEY, S_B64, C_B64, proof.upper())
    assert not auth.verify_proof(bytes(32), S_B64, C_B64, proof)  # wrong key
    assert not auth.verify_proof(KEY, C_B64, S_B64, proof)  # swapped nonces
    assert not auth.verify_proof(KEY, S_B64, C_B64, proof[:-1] + ("0" if proof[-1] != "0" else "1"))
    assert not auth.verify_proof(KEY, S_B64, C_B64, None)
    assert not auth.verify_proof(KEY, S_B64, C_B64, 123)
    assert not auth.verify_proof(KEY, S_B64, "***not base64***", proof)
    assert not auth.verify_proof(KEY, S_B64, C_B64, "pröof")


def test_verify_server_proof():
    sp = auth.compute_server_proof(KEY, S_B64, C_B64)
    assert auth.verify_server_proof(KEY, S_B64, C_B64, sp)
    assert not auth.verify_server_proof(bytes(32), S_B64, C_B64, sp)
    assert not auth.verify_server_proof(KEY, S_B64, C_B64, auth.compute_proof(KEY, S_B64, C_B64))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_lockout_and_expiry():
    clock = Clock()
    lim = auth.AuthLimiter(max_failures=5, window=600, lockout=600, delay=5.0, clock=clock)
    assert lim.delay == 5.0
    for _ in range(4):
        assert lim.record_failure("1.2.3.4") is False
        assert not lim.is_locked("1.2.3.4")
        clock.now += 1
    assert lim.record_failure("1.2.3.4") is True
    assert lim.is_locked("1.2.3.4")
    assert not lim.is_locked("5.6.7.8")
    clock.now += 599
    assert lim.is_locked("1.2.3.4")
    clock.now += 2
    assert not lim.is_locked("1.2.3.4")
    # counter was reset with the lockout
    assert lim.record_failure("1.2.3.4") is False


def test_limiter_window_forgets_old_failures():
    clock = Clock()
    lim = auth.AuthLimiter(max_failures=3, window=100, lockout=600, clock=clock)
    lim.record_failure("ip")
    lim.record_failure("ip")
    clock.now += 101
    assert lim.record_failure("ip") is False
    assert not lim.is_locked("ip")


def test_limiter_success_resets():
    lim = auth.AuthLimiter(max_failures=2, clock=Clock())
    lim.record_failure("ip")
    lim.record_success("ip")
    assert lim.record_failure("ip") is False
