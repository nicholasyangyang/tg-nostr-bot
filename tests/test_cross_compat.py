"""
Cross-compatibility test: compare our shared/key_manager.py with the reference
implementation provided by the user. Run with: python tests/test_cross_compat.py
"""
import sys
from pathlib import Path

_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

# ============================================================
# Reference implementation (from user's provided code)
# ============================================================
import base64
import hashlib
import json
import secrets
import struct
import time
import random

import secp256k1
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.hmac import HMAC as _HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.backends import default_backend

_CS = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CM = {c: i for i, c in enumerate(_CS)}
_GN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]

def _pm(vals):
    c = 1
    for v in vals:
        b = c >> 25; c = (c & 0x1ffffff) << 5 ^ v
        for i in range(5): c ^= _GN[i] if (b >> i) & 1 else 0
    return c

def _hrp(h): return [ord(x) >> 5 for x in h] + [0] + [ord(x) & 31 for x in h]

def _bits(data, f, t, pad=True):
    a = bits = 0; r = []; mx = (1 << t) - 1
    for v in data:
        a = (a << f) | v; bits += f
        while bits >= t: bits -= t; r.append((a >> bits) & mx)
    if pad and bits: r.append((a << (t - bits)) & mx)
    return r

def _b32enc(hrp, data):
    d = _bits(data, 8, 5)
    p = _pm(_hrp(hrp) + d + [0] * 6) ^ 1
    return hrp + "1" + "".join(_CS[x] for x in d + [(p >> 5*(5-i)) & 31 for i in range(6)])

def _b32dec(s):
    s = s.lower(); p = s.rfind("1")
    if p < 1 or p + 7 > len(s): raise ValueError("bad bech32")
    hrp = s[:p]
    try: d = [_CM[c] for c in s[p+1:]]
    except KeyError: raise ValueError("bad char")
    if _pm(_hrp(hrp) + d) != 1: raise ValueError("bad checksum")
    return hrp, bytes(_bits(d[:-6], 5, 8, pad=False))

def to_npub(h): return _b32enc("npub", bytes.fromhex(h))
def to_nsec(h): return _b32enc("nsec", bytes.fromhex(h))
def npub2hex(s):
    hrp, b = _b32dec(s)
    if hrp != "npub": raise ValueError("not an npub")
    return b.hex()
def nsec2hex(s):
    hrp, b = _b32dec(s)
    if hrp != "nsec": raise ValueError("not an nsec")
    return b.hex()

def gen_keys():
    b = secrets.token_bytes(32)
    return b.hex(), secp256k1.PrivateKey(b).pubkey.serialize()[1:].hex()

def derive_pub(priv):
    return secp256k1.PrivateKey(bytes.fromhex(priv)).pubkey.serialize()[1:].hex()

def schnorr(eid, priv):
    return secp256k1.PrivateKey(bytes.fromhex(priv)).schnorr_sign(
        bytes.fromhex(eid), None, raw=True).hex()

def _ecdh(priv, pub):
    pk = secp256k1.PublicKey(bytes.fromhex("02" + pub), raw=True)
    return pk.tweak_mul(secp256k1.PrivateKey(bytes.fromhex(priv)).private_key
                        ).serialize(compressed=True)[1:]

def _nip44_conv_key(priv, pub):
    """HKDF-Extract(salt="nip44-v2", IKM=ecdh_x)"""
    h = _HMAC(b"nip44-v2", SHA256(), backend=default_backend())
    h.update(_ecdh(priv, pub))
    return h.finalize()

def _nip44_pad_len(l):
    if l <= 32: return 32
    np = 1 << (l - 1).bit_length()
    chunk = max(np // 8, 32)
    return chunk * ((l - 1) // chunk + 1)

def ref_nip44_enc(text, priv, pub):
    ck = _nip44_conv_key(priv, pub)
    nonce = secrets.token_bytes(32)
    keys = HKDFExpand(SHA256(), 76, nonce, default_backend()).derive(ck)
    ck2, cn, hk = keys[:32], keys[32:44], keys[44:]
    plain = text.encode()
    pl = _nip44_pad_len(len(plain))
    padded = struct.pack('>H', len(plain)) + plain + b'\x00' * (pl - len(plain))
    enc = Cipher(algorithms.ChaCha20(ck2, b'\x00\x00\x00\x00' + cn),
                 None, default_backend()).encryptor()
    ct = enc.update(padded) + enc.finalize()
    hm = _HMAC(hk, SHA256(), backend=default_backend())
    hm.update(nonce + ct)
    return base64.b64encode(b'\x02' + nonce + ct + hm.finalize()).decode()

def ref_nip44_dec(payload, priv, pub):
    try:
        raw = base64.b64decode(payload)
        if raw[0] != 2: return "[unsupported nip44 version]"
        nonce, ct, mac = raw[1:33], raw[33:-32], raw[-32:]
        ck = _nip44_conv_key(priv, pub)
        keys = HKDFExpand(SHA256(), 76, nonce, default_backend()).derive(ck)
        ck2, cn, hk = keys[:32], keys[32:44], keys[44:]
        hm = _HMAC(hk, SHA256(), backend=default_backend())
        hm.update(nonce + ct)
        if not secrets.compare_digest(mac, hm.finalize()): return "[nip44 bad mac]"
        dec = Cipher(algorithms.ChaCha20(ck2, b'\x00\x00\x00\x00' + cn),
                     None, default_backend()).decryptor()
        padded = dec.update(ct) + dec.finalize()
        l = struct.unpack('>H', padded[:2])[0]
        return padded[2:2+l].decode()
    except Exception as e: return f"[nip44 error: {e}]"

def _rand_ts(): return int(time.time()) - random.randint(0, 172800)

def _ev_id(ev):
    s = json.dumps([0, ev["pubkey"], ev["created_at"], ev["kind"],
                    ev["tags"], ev["content"]], separators=(",",":"), ensure_ascii=False)
    return hashlib.sha256(s.encode()).hexdigest()

def ref_nip17_wrap(text, sender_priv, sender_pub, recipient_pub):
    rumor = {"pubkey": sender_pub, "created_at": int(time.time()),
             "kind": 14, "tags": [["p", recipient_pub]], "content": text}
    rumor["id"] = _ev_id(rumor)

    seal = {"pubkey": sender_pub, "created_at": _rand_ts(),
            "kind": 13, "tags": [], "content": ref_nip44_enc(json.dumps(rumor), sender_priv, recipient_pub)}
    seal["id"] = _ev_id(seal); seal["sig"] = schnorr(seal["id"], sender_priv)

    eph_priv, eph_pub = gen_keys()
    wrap = {"pubkey": eph_pub, "created_at": _rand_ts(),
            "kind": 1059, "tags": [["p", recipient_pub]],
            "content": ref_nip44_enc(json.dumps(seal), eph_priv, recipient_pub)}
    wrap["id"] = _ev_id(wrap); wrap["sig"] = schnorr(wrap["id"], eph_priv)
    return wrap

def ref_nip17_unwrap(wrap_ev, my_priv):
    seal_json = ref_nip44_dec(wrap_ev["content"], my_priv, wrap_ev["pubkey"])
    seal = json.loads(seal_json)
    if seal.get("kind") != 13: raise ValueError(f"expected kind:13, got {seal.get('kind')}")
    rumor_json = ref_nip44_dec(seal["content"], my_priv, seal["pubkey"])
    rumor = json.loads(rumor_json)
    if rumor.get("kind") != 14: raise ValueError(f"expected kind:14, got {rumor.get('kind')}")
    return rumor, seal["pubkey"]

# ============================================================
# Our implementation
# ============================================================
from shared.key_manager import (
    generate_keys as our_generate_keys,
    npub_to_hex as our_npub_to_hex,
    nsec_to_hex as our_nsec_to_hex,
    nip44_encrypt as our_nip44_encrypt,
    nip44_decrypt as our_nip44_decrypt,
    nip17_wrap_message as our_nip17_wrap,
    nip17_unwrap as our_nip17_unwrap,
)

# ============================================================
# Tests
# ============================================================

def test_shared_secret_equivalence():
    """Test that both implementations produce the same shared secret."""
    print("\n=== Test: Shared Secret Equivalence ===")
    alice_priv = secrets.token_bytes(32).hex()
    bob_priv = secrets.token_bytes(32).hex()

    # Derive pubkeys
    bob_pub = derive_pub(bob_priv)  # reference (x-only hex)
    alice_pub = secp256k1.PrivateKey(bytes.fromhex(alice_priv)).pubkey.serialize()[1:].hex()

    # Reference ECDH
    ref_ss = _ecdh(alice_priv, bob_pub)
    print(f"  Reference ECDH shared secret: {ref_ss.hex()[:32]}...")

    # Our ECDH (need to import the internal function)
    from shared.key_manager import _nip44_conv_key as _nip44_shared_secret
    our_ss = _nip44_shared_secret(alice_priv, bob_pub)
    print(f"  Our shared secret:            {our_ss.hex()[:32]}...")

    if ref_ss == our_ss:
        print("  PASS: Shared secrets match!")
    else:
        print("  FAIL: Shared secrets DIFFER!")
        print(f"    Reference length: {len(ref_ss)}, Our length: {len(our_ss)}")


def test_nip44_cross_compat():
    """Test NIP-44: encrypt with one impl, decrypt with the other."""
    print("\n=== Test: NIP-44 Cross-Compatibility ===")

    sender = gen_keys()
    recipient = gen_keys()

    sender_priv = sender[0]  # hex
    sender_pub = sender[1]    # x-only hex
    recipient_priv = recipient[0]
    recipient_pub = recipient[1]

    plaintext = "Hello, NIP-44 world!"

    # --- Encrypt with reference, decrypt with ours ---
    print("  [A] Encrypt (reference) → Decrypt (ours)")
    ref_ct = ref_nip44_enc(plaintext, sender_priv, recipient_pub)
    print(f"    Ciphertext (ref): {ref_ct[:40]}...")
    try:
        our_dec = our_nip44_decrypt(ref_ct, recipient_priv, sender_pub)
        print(f"    Our decrypted:    {our_dec}")
        if our_dec == plaintext:
            print("    PASS: Reference→Our works!")
        else:
            print(f"    FAIL: Expected '{plaintext}', got '{our_dec}'")
    except Exception as e:
        print(f"    FAIL: Our decryption threw: {e}")

    # --- Encrypt with ours, decrypt with reference ---
    print("  [B] Encrypt (ours) → Decrypt (reference)")
    our_ct = our_nip44_encrypt(plaintext, sender_priv, recipient_pub)
    print(f"    Ciphertext (our): {our_ct[:40]}...")
    ref_dec = ref_nip44_dec(our_ct, recipient_priv, sender_pub)
    print(f"    Reference decrypted: {ref_dec}")
    if ref_dec == plaintext:
        print("    PASS: Our→Reference works!")
    else:
        print(f"    FAIL: Expected '{plaintext}', got '{ref_dec}'")


def test_nip17_cross_compat():
    """Test NIP-17: wrap with one impl, unwrap with the other."""
    print("\n=== Test: NIP-17 Cross-Compatibility ===")

    sender = gen_keys()
    recipient = gen_keys()

    sender_priv = sender[0]
    sender_pub = sender[1]  # x-only hex
    recipient_priv = recipient[0]
    recipient_pub = derive_pub(recipient_priv)  # x-only hex

    plaintext = "Secret DM via gift wrap!"

    # --- Wrap with reference, unwrap with ours ---
    print("  [A] Wrap (reference) → Unwrap (ours)")
    ref_wrap = ref_nip17_wrap(plaintext, sender_priv, sender_pub, recipient_pub)
    print(f"    Gift wrap kind: {ref_wrap['kind']}")
    try:
        our_rumor = our_nip17_unwrap(ref_wrap, recipient_priv, recipient_pub)
        if our_rumor:
            print(f"    Our rumor plaintext: {our_rumor.get('plaintext')}")
            if our_rumor.get("plaintext") == plaintext:
                print("    PASS: Reference wrap → Our unwrap works!")
            else:
                print(f"    FAIL: Got '{our_rumor.get('plaintext')}', expected '{plaintext}'")
        else:
            print("    FAIL: Our unwrap returned None")
    except Exception as e:
        print(f"    FAIL: Our unwrap threw: {e}")

    # --- Wrap with ours, unwrap with reference ---
    print("  [B] Wrap (ours) → Unwrap (reference)")
    our_wrap = our_nip17_wrap(
        plaintext=plaintext,
        sender_seckey=sender_priv,
        sender_pubkey=sender_pub,
        recipient_pubkey=recipient_pub,
    )
    try:
        ref_rumor, ref_sender = ref_nip17_unwrap(our_wrap, recipient_priv)
        print(f"    Reference rumor plaintext: {ref_rumor.get('content')}")
        if ref_rumor.get("content") == plaintext:
            print("    PASS: Our wrap → Reference unwrap works!")
        else:
            print(f"    FAIL: Got '{ref_rumor.get('content')}', expected '{plaintext}'")
    except Exception as e:
        print(f"    FAIL: Reference unwrap threw: {e}")


if __name__ == "__main__":
    test_shared_secret_equivalence()
    test_nip44_cross_compat()
    test_nip17_cross_compat()
    print("\n=== Done ===")
