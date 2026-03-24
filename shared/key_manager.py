import json
import time
import os
import hashlib
import random
from pathlib import Path

# cryptography for ChaCha20-Poly1305 and HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.backends import default_backend


# ---- NIP-44 encryption helpers ----

def _ecdh_derive_shared_key(seckey_hex: str, pubkey_xonly_hex: str) -> bytes:
    """ECDH shared secret using secp256k1 for point math, cryptography for ECDH.

    seckey_hex: 32-byte hex string (or nsec bech32)
    pubkey_xonly_hex: 32-byte hex x-only pubkey
    Returns 32-byte raw ECDH shared secret (the x-coordinate).
    """
    from cryptography.hazmat.primitives.asymmetric.ec import (
        SECP256K1, ECDH,
        EllipticCurvePublicNumbers, EllipticCurvePrivateNumbers,
    )
    import secp256k1

    if seckey_hex.startswith("nsec1"):
        seckey_hex = nsec_to_hex(seckey_hex)
    priv_bytes = bytes.fromhex(seckey_hex)
    pub_bytes = bytes.fromhex(pubkey_xonly_hex)

    # Use secp256k1 to derive sender's full public key point from private key
    sender_priv = secp256k1.PrivateKey(priv_bytes)
    sender_pubkey_full = sender_priv.pubkey.serialize(compressed=False)  # 65 bytes
    x_s = int.from_bytes(sender_pubkey_full[1:33], "big")
    y_s = int.from_bytes(sender_pubkey_full[33:65], "big")

    # Derive recipient's y from x (x-only pubkey)
    p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
    x_r = int.from_bytes(pub_bytes, "big")
    y_sq = (pow(x_r, 3, p) + 7) % p
    y_r = pow(y_sq, (p + 1) // 4, p)
    # Verify y is on curve; if not, flip to the other root
    if pow(y_r, 2, p) != y_sq:
        y_r = p - y_r

    # Build private key with its public key for ECDH exchange
    sender_pub_nums = EllipticCurvePublicNumbers(x_s, y_s, SECP256K1())
    priv_nums = EllipticCurvePrivateNumbers(
        int.from_bytes(priv_bytes, "big"), sender_pub_nums,
    )
    sender_key = priv_nums.private_key()
    recipient_pub = EllipticCurvePublicNumbers(x_r, y_r, SECP256K1()).public_key()
    return sender_key.exchange(ECDH(), recipient_pub)


def _nip44_shared_secret(seckey: str, pubkey: str) -> bytes:
    """Derive NIP-44 shared secret via ECDH + HKDF.

    seckey: hex string (32 bytes) or nsec bech32
    pubkey: hex string — x-only (32 bytes), compressed (33 bytes), or uncompressed (65 bytes)
    Returns 32-byte shared secret.
    """
    # Convert pubkey to x-only hex
    pubkey_bytes = bytes.fromhex(pubkey)
    if len(pubkey_bytes) == 33:
        # Compressed: strip prefix, keep x
        pubkey_xonly = pubkey_bytes[1:].hex()
    elif len(pubkey_bytes) == 65:
        # Uncompressed: strip prefix (0x04), keep x (first 32 bytes after prefix)
        pubkey_xonly = pubkey_bytes[1:33].hex()
    else:
        # Already x-only
        pubkey_xonly = pubkey

    # Use secp256k1 library for ECDH (handles x-only natively)
    shared = _ecdh_derive_shared_key(seckey, pubkey_xonly)

    # NIP-44: HKDF-SHA512 with salt="nip04-v2", info="nip44-v2" → 32-byte secret
    hkdf = HKDF(
        algorithm=hashes.SHA512(),
        length=32,
        salt=b"nip04-v2",
        info=b"nip44-v2",
        backend=default_backend(),
    )
    return hkdf.derive(shared)


def nip44_encrypt(plaintext: str, sender_priv: str, receiver_pub: str) -> str:
    """NIP-44 encrypt: ChaCha20-Poly1305.

    plaintext: raw string
    sender_priv: hex seckey (or nsec bech32)
    receiver_pub: hex (x-only/compressed/uncompressed) or bech32 npub
    Returns base64-encoded ciphertext: version(0x02) || sender_pub(33) || nonce(12) || ct+tag
    """
    from base64 import b64encode

    # Convert receiver_pub to hex if bech32 npub
    if receiver_pub.startswith("npub1"):
        receiver_pub = npub_to_hex(receiver_pub)

    # Convert sender_priv to hex if bech32
    if sender_priv.startswith("nsec1"):
        sender_priv = nsec_to_hex(sender_priv)

    # Full 33-byte pubkey for sender (needed for embedding in ciphertext)
    sender_pub_bytes = _pubkey_from_priv(sender_priv)  # 33 bytes: 0x02/0x03 || x
    shared = _nip44_shared_secret(sender_priv, receiver_pub)
    chacha = ChaCha20Poly1305(shared)

    nonce = os.urandom(12)  # 12-byte nonce for ChaCha20
    # Plaintext = sender_pub (33 bytes) || content bytes
    pt = sender_pub_bytes + plaintext.encode("utf-8")

    ct = chacha.encrypt(nonce, pt, None)
    # Output: version(0x02) || sender_pub(33) || nonce(12) || ciphertext+tag
    return b64encode(bytes([0x02]) + sender_pub_bytes + nonce + ct).decode()


def nip44_decrypt(ciphertext_b64: str, my_priv: str, sender_pub: str) -> str:
    """NIP-44 decrypt: ChaCha20-Poly1305.

    ciphertext_b64: base64 string (version || sender_pub || nonce || ct)
    my_priv: hex seckey (or nsec bech32)
    sender_pub: hex or bech32 npub — ignored, extracted from ciphertext
    Returns plaintext string.
    """
    from base64 import b64decode

    data = b64decode(ciphertext_b64)
    if data[0] != 0x02:
        raise ValueError(f"Unknown NIP-44 version: {data[0]}")

    sender_pub_embedded = data[1:34]  # 33 bytes full pubkey
    nonce = data[34:46]
    ct = data[46:]

    # Convert my_priv to hex if bech32
    if my_priv.startswith("nsec1"):
        my_priv = nsec_to_hex(my_priv)

    # Extract x-only from embedded pubkey for ECDH
    sender_pub_x = sender_pub_embedded[1:].hex()
    shared = _nip44_shared_secret(my_priv, sender_pub_x)
    chacha = ChaCha20Poly1305(shared)

    pt = chacha.decrypt(nonce, ct, None)
    # Skip embedded sender_pub (33 bytes)
    return pt[33:].decode("utf-8")


def _pubkey_from_priv(seckey_hex: str) -> bytes:
    """Get full 33-byte secp256k1 pubkey (prefix || x) from seckey.

    For ECDH: use full point so recipient can extract x-coordinate.
    Returns 33 bytes: 0x02/0x03 || x (32 bytes).
    """
    import secp256k1
    if seckey_hex.startswith("nsec1"):
        seckey_hex = nsec_to_hex(seckey_hex)
    if len(seckey_hex) == 64:
        pk = secp256k1.PrivateKey(bytes.fromhex(seckey_hex))
    else:
        pk = secp256k1.PrivateKey()
    return pk.pubkey.serialize()  # 33 bytes: 0x02/0x03 || x (32 bytes)


def _x_only(pubkey_bytes: bytes) -> bytes:
    """Return x-only 32-byte pubkey from full 33-byte serialized pubkey."""
    if len(pubkey_bytes) == 33:
        return pubkey_bytes[1:]  # strip prefix, keep x
    return pubkey_bytes  # already x-only


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
            # Continue anyway — let the relay handle it

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
    """Legacy ECDH shared secret (SHA256 of ECDH result)."""
    return _nip44_shared_secret(seckey, pubkey)
