import json
import time
import os
import hashlib
import random
import secrets
import struct
import base64
from pathlib import Path

# cryptography for ChaCha20 and HKDF
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.hmac import HMAC as _HMAC
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from cryptography.hazmat.backends import default_backend


# ---- ECDH shared secret (secp256k1-native) ----

def _ecdh(priv_hex: str, pub_xonly_hex: str) -> bytes:
    """ECDH shared secret using secp256k1 tweak_mul (same as reference).

    priv_hex: 32-byte hex private key
    pub_xonly_hex: 32-byte x-only OR 33-byte compressed hex public key
    Returns 32-byte raw ECDH shared secret x-coordinate.
    """
    import secp256k1
    pub_bytes = bytes.fromhex(pub_xonly_hex)
    if len(pub_bytes) == 33:
        pub_formatted = pub_bytes  # already has 0x02/0x03 prefix
    else:
        pub_formatted = bytes.fromhex("02" + pub_xonly_hex)  # add prefix for x-only
    pk = secp256k1.PublicKey(pub_formatted, raw=True)
    return pk.tweak_mul(
        secp256k1.PrivateKey(bytes.fromhex(priv_hex)).private_key
    ).serialize(compressed=True)[1:]


# ---- NIP-44 encryption (reference implementation) ----

def _nip44_conv_key(priv: str, pub: str) -> bytes:
    """HKDF-Extract(salt="nip44-v2", IKM=ecdh_x) — matches reference."""
    h = _HMAC(b"nip44-v2", SHA256(), backend=default_backend())
    h.update(_ecdh(priv, pub))
    return h.finalize()


def _nip44_pad_len(l: int) -> int:
    """Pad length to 32-byte chunk boundaries."""
    if l <= 32:
        return 32
    np = 1 << (l - 1).bit_length()
    chunk = max(np // 8, 32)
    return chunk * ((l - 1) // chunk + 1)


def nip44_encrypt(plaintext: str, sender_priv: str, receiver_pub: str) -> str:
    """NIP-44 encrypt: ChaCha20 + HMAC-SHA256 (reference algorithm).

    Matches: ref_nip44_enc() in the reference implementation.
    """
    # Normalize keys
    if sender_priv.startswith("nsec1"):
        sender_priv = nsec_to_hex(sender_priv)
    if receiver_pub.startswith("npub1"):
        receiver_pub = npub_to_hex(receiver_pub)

    ck = _nip44_conv_key(sender_priv, receiver_pub)
    nonce = secrets.token_bytes(32)
    keys = HKDFExpand(SHA256(), 76, nonce, default_backend()).derive(ck)
    ck2, cn, hk = keys[:32], keys[32:44], keys[44:]

    plain = plaintext.encode()
    pl = _nip44_pad_len(len(plain))
    padded = struct.pack('>H', len(plain)) + plain + b'\x00' * (pl - len(plain))

    enc = Cipher(algorithms.ChaCha20(ck2, b'\x00\x00\x00\x00' + cn),
                 None, default_backend()).encryptor()
    ct = enc.update(padded) + enc.finalize()

    hm = _HMAC(hk, SHA256(), backend=default_backend())
    hm.update(nonce + ct)
    return base64.b64encode(b'\x02' + nonce + ct + hm.finalize()).decode()


def nip44_decrypt(ciphertext_b64: str, my_priv: str, sender_pub: str) -> str:
    """NIP-44 decrypt: ChaCha20 + HMAC-SHA256 (reference algorithm).

    Matches: ref_nip44_dec() in the reference implementation.
    Raises ValueError on bad MAC or version.
    """
    if my_priv.startswith("nsec1"):
        my_priv = nsec_to_hex(my_priv)
    if sender_pub.startswith("npub1"):
        sender_pub = npub_to_hex(sender_pub)

    raw = base64.b64decode(ciphertext_b64)
    if raw[0] != 2:
        raise ValueError(f"Unsupported NIP-44 version: {raw[0]}")
    nonce, ct, mac = raw[1:33], raw[33:-32], raw[-32:]

    ck = _nip44_conv_key(my_priv, sender_pub)
    keys = HKDFExpand(SHA256(), 76, nonce, default_backend()).derive(ck)
    ck2, cn, hk = keys[:32], keys[32:44], keys[44:]

    hm = _HMAC(hk, SHA256(), backend=default_backend())
    hm.update(nonce + ct)
    if not secrets.compare_digest(mac, hm.finalize()):
        raise ValueError("NIP-44 bad MAC")

    dec = Cipher(algorithms.ChaCha20(ck2, b'\x00\x00\x00\x00' + cn),
                 None, default_backend()).decryptor()
    padded = dec.update(ct) + dec.finalize()
    l = struct.unpack('>H', padded[:2])[0]
    return padded[2:2 + l].decode()


# ---- Nostr key management ----

def get_keys(key_path: str) -> dict:
    """Get or generate Nostr keys."""
    path = Path(key_path)

    if path.exists():
        with open(path, "r") as f:
            keys = json.load(f)
        if "npub" in keys and "nsec" in keys:
            return keys

    keys = generate_keys()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(keys, f, indent=2)
    return keys


def generate_keys() -> dict:
    """Generate a new Nostr key pair using secp256k1."""
    import secp256k1
    import bech32

    pk = secp256k1.PrivateKey()
    privkey_hex = pk.serialize()
    # Encode x-only pubkey (no prefix byte) so npub_to_hex / hex_to_npub are inverses
    pubkey_bytes = pk.pubkey.serialize()[1:]  # strip 0x02/0x03 prefix → x-only

    witdata = bech32.convertbits(pubkey_bytes, 8, 5)
    npub = bech32.bech32_encode("npub", witdata)

    privkey_bytes = bytes.fromhex(privkey_hex)
    witdata_sec = bech32.convertbits(privkey_bytes, 8, 5)
    nsec = bech32.bech32_encode("nsec", witdata_sec)

    return {"npub": npub, "nsec": nsec}


def get_public_key(key_path: str) -> str:
    keys = get_keys(key_path)
    return keys.get("npub", "")


def npub_to_hex(npub: str) -> str:
    """Decode bech32 npub to 32-byte hex pubkey (x-only).

    Accepts:
    - Raw 64-char hex string (already hex) → returned as-is
    - bech32 npub1... → decoded to 32-byte x-only pubkey
    """
    import bech32
    if not npub or not npub.startswith("npub1"):
        # Validate raw hex: must be exactly 64 hex chars
        if len(npub) == 64 and all(c in "0123456789abcdef" for c in npub.lower()):
            return npub
        return npub
    try:
        hrp, data = bech32.bech32_decode(npub)
        if data is None:
            return npub
        decoded = bech32.convertbits(data, 5, 8, True)
        if decoded is None:
            return npub
        # Take exactly 32 bytes (64 hex chars) — the x-only pubkey
        return bytes(decoded).hex()[:64]
    except Exception:
        return npub


def nsec_to_hex(nsec: str) -> str:
    """Decode bech32 nsec to 32-byte hex seckey."""
    import bech32
    if not nsec or not nsec.startswith("nsec1"):
        return nsec
    try:
        hrp, data = bech32.bech32_decode(nsec)
        if data is None:
            return nsec
        decoded = bech32.convertbits(data, 5, 8, True)
        if decoded is None:
            return nsec
        return bytes(decoded).hex()[:64]
    except Exception:
        return nsec


def hex_to_npub(hex_pubkey: str) -> str:
    """Encode pubkey hex as bech32 npub.

    Accepts either 32-byte x-only hex or 33-byte full (parity || x) hex.
    """
    import bech32
    if not hex_pubkey or hex_pubkey.startswith("npub1"):
        return hex_pubkey
    try:
        pubkey_bytes = bytes.fromhex(hex_pubkey)
        # If 33 bytes (parity || x), strip parity byte to get x-only
        if len(pubkey_bytes) == 33:
            pubkey_bytes = pubkey_bytes[1:]
        # 32 bytes -> convertbits -> bech32
        witdata = bech32.convertbits(pubkey_bytes, 8, 5)
        return bech32.bech32_encode("npub", witdata)
    except Exception:
        return hex_pubkey


def get_private_key(key_path: str) -> str:
    keys = get_keys(key_path)
    nsec = keys.get("nsec", "")
    if not nsec:
        return ""
    return nsec_to_hex(nsec)


# ---- NIP-17 constants ----

KIND_NIP17_GIFT_WRAP = 1059
KIND_NIP17_SEAL = 13
KIND_NIP17_TEXT_MSG = 14
KIND_NIP17_FILE_MSG = 15


# ---- Event signing ----

def _serialize_for_id(event: dict) -> str:
    """Serialize event for id computation per NIP-01.

    Array format: [0, pubkey, created_at, kind, tags, content]
    """
    arr = [0, event["pubkey"], event["created_at"], event["kind"],
           event["tags"], event["content"]]
    return json.dumps(arr, separators=(",", ":"), ensure_ascii=False)


def _event_id(event: dict) -> str:
    return hashlib.sha256(_serialize_for_id(event).encode("utf-8")).hexdigest()


def sign_event(event: dict, seckey_hex: str) -> str:
    """Sign an event with BIP340 Schnorr.

    seckey_hex: 32-byte hex string (or nsec bech32)
    Returns 64-byte hex signature (R.x || s).

    BIP340 signing: hash the serialized event → 32-byte event_id,
    then sign with raw=True (message used as-is, no extra hash).
    """
    import secp256k1

    event_id_hex = _event_id(event)

    if seckey_hex.startswith("nsec1"):
        seckey_hex = nsec_to_hex(seckey_hex)
    if len(seckey_hex) == 64:
        pk = secp256k1.PrivateKey(bytes.fromhex(seckey_hex))
    else:
        pk = secp256k1.PrivateKey()

    # raw=True: 32-byte event_id used as-is (no internal hash)
    sig = pk.schnorr_sign(bytes.fromhex(event_id_hex), b"", raw=True)
    return sig.hex()


def generate_keypair() -> tuple[str, str]:
    """Generate a new (privkey_hex, pubkey_hex) keypair."""
    import secp256k1
    pk = secp256k1.PrivateKey()
    priv_hex = pk.serialize()
    pub_bytes = pk.pubkey.serialize()[1:]  # x-only
    return priv_hex, pub_bytes.hex()


def random_past_timestamp(max_days_back: int = 2) -> int:
    """Random past timestamp for anti-timing-attack."""
    now = int(time.time())
    max_back = max_days_back * 86400
    return now - random.randint(0, max_back)


# ---- NIP-17 Gift Wrap ----

def nip17_wrap_message(
    plaintext: str,
    sender_seckey: str,
    sender_pubkey: str,
    recipient_pubkey: str,
    recipient_npub: str = "",
    reply_to_event_id: str = None,
    subject: str = None,
    kind: int = KIND_NIP17_TEXT_MSG,
) -> dict:
    """Create a NIP-17 gift-wrapped DM event (kind 1059).

    Structure: rumor (kind 14, UNSIGNED) → seal (kind 13, SIGNED) → gift wrap (kind 1059, SIGNED)
    Encryption: NIP-44 (ChaCha20-Poly1305) throughout.

    Returns the gift_wrap dict ready to publish.
    """
    recipient_hex = npub_to_hex(recipient_npub) if recipient_npub else recipient_pubkey

    # --- Layer 1: Rumor (kind 14, UNSIGNED) ---
    rumor_tags = [["p", recipient_hex]]
    if reply_to_event_id:
        rumor_tags.append(["e", reply_to_event_id])
    if subject:
        rumor_tags.append(["subject", subject])

    rumor = {
        "pubkey": sender_pubkey,
        "created_at": int(time.time()),
        "kind": kind,
        "tags": rumor_tags,
        "content": plaintext,
        # No id/sig — rumor is never signed
    }

    # --- Layer 2: Seal (kind 13, SIGNED) ---
    # Encrypt rumor JSON to recipient using sender key
    encrypted_rumor = nip44_encrypt(json.dumps(rumor), sender_seckey, recipient_hex)

    seal = {
        "pubkey": sender_pubkey,
        "created_at": random_past_timestamp(),
        "kind": KIND_NIP17_SEAL,
        "tags": [],
        "content": encrypted_rumor,
    }
    seal["id"] = _event_id(seal)
    seal["sig"] = sign_event(seal, sender_seckey)

    # --- Layer 3: Gift Wrap (kind 1059, SIGNED with ephemeral key) ---
    # Encrypt seal JSON to recipient using ephemeral key
    ephem_priv, ephem_pub = generate_keypair()
    encrypted_seal = nip44_encrypt(json.dumps(seal), ephem_priv, recipient_hex)

    relay_url = ""  # could be passed in if needed
    gift_wrap_tags = [["p", recipient_hex]]
    if relay_url:
        gift_wrap_tags.append(["relay", relay_url])

    gift_wrap = {
        "pubkey": ephem_pub,
        "created_at": random_past_timestamp(),
        "kind": KIND_NIP17_GIFT_WRAP,
        "tags": gift_wrap_tags,
        "content": encrypted_seal,
    }
    gift_wrap["id"] = _event_id(gift_wrap)
    gift_wrap["sig"] = sign_event(gift_wrap, ephem_priv)

    return gift_wrap


def nip17_unwrap(event: dict, recipient_seckey: str, recipient_pubkey: str) -> dict | None:
    """Unwrap a NIP-17 gift-wrapped DM event (kind 1059).

    Returns the inner rumor dict with 'plaintext' field added, or None on failure.
    """
    try:
        kind = event.get("kind")
        if kind != KIND_NIP17_GIFT_WRAP:
            return None

        # Step 1: Decrypt gift wrap → seal
        # Encryption: nip44_encrypt(ephem_priv, recipient_pub)
        # Decryption must use the SAME pubkey: recipient_pub
        gift_pubkey = event.get("pubkey", "")
        encrypted_seal = event.get("content", "")

        seal_json_str = nip44_decrypt(encrypted_seal, recipient_seckey, gift_pubkey)
        seal = json.loads(seal_json_str)

        if seal.get("kind") != KIND_NIP17_SEAL:
            return None

        # Verify seal signature (optional but recommended)
        seal_sender_pub = seal.get("pubkey", "")
        seal_id = seal.get("id", "")
        seal_tags = seal.get("tags", [])
        seal_created = seal.get("created_at", 0)
        seal_content = seal.get("content", "")

        # Re-compute expected id
        seal_arr = [0, seal_sender_pub, seal_created, KIND_NIP17_SEAL, seal_tags, seal_content]
        expected_id = hashlib.sha256(
            json.dumps(seal_arr, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        if seal_id != expected_id:
            import logging
            logging.getLogger("relay").warning("NIP-17 seal id mismatch")
            return None  # reject forged seals

        # Step 2: Decrypt seal → rumor
        # ECDH(recipient_priv, seal.pubkey) = ECDH(sender_priv, recipient_pubkey)
        sender_pub = seal_sender_pub
        encrypted_rumor = seal.get("content", "")

        rumor_json_str = nip44_decrypt(encrypted_rumor, recipient_seckey, sender_pub)
        rumor = json.loads(rumor_json_str)

        # Rumor must have matching pubkey to sender
        if rumor.get("pubkey") != sender_pub:
            import logging
            logging.getLogger("relay").warning("NIP-17 rumor pubkey mismatch")
            return None

        # Extract plaintext from rumor
        rumor["plaintext"] = rumor.get("content", "")
        return rumor

    except Exception as e:
        import logging
        logging.getLogger("relay").warning(f"NIP-17 unwrap failed: {e}")
        return None


# ---- Legacy helpers (keep for compatibility) ----

def encrypt_nip17(plaintext: str, seckey: str, pubkey: str) -> str:
    """Legacy alias for nip44_encrypt."""
    return nip44_encrypt(plaintext, seckey, pubkey)


def decrypt_nip17(ciphertext_b64: str, seckey: str, pubkey: str) -> str:
    """Legacy alias for nip44_decrypt."""
    return nip44_decrypt(ciphertext_b64, seckey, pubkey)


def _shared_key(seckey: str, pubkey: str) -> bytes:
    """Legacy alias for _nip44_conv_key."""
    return _nip44_conv_key(seckey, pubkey)

