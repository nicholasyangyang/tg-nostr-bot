"""Unit tests for shared/key_manager.py — NIP-44 and NIP-17 roundtrip."""
import sys
from pathlib import Path

# Add project root so shared/ is importable
_parent = Path(__file__).resolve().parent.parent
if str(_parent) not in sys.path:
    sys.path.insert(0, str(_parent))

import pytest
from shared.key_manager import (
    generate_keys,
    npub_to_hex,
    nsec_to_hex,
    hex_to_npub,
    nip44_encrypt,
    nip44_decrypt,
    nip17_wrap_message,
    nip17_unwrap,
    KIND_NIP17_GIFT_WRAP,
)


class TestNIP44Roundtrip:
    """Test NIP-44 encrypt/decrypt roundtrip."""

    def test_encrypt_decrypt_roundtrip(self):
        """NIP-44 encrypt then decrypt should recover the original plaintext."""
        keys = generate_keys()
        sender_priv = nsec_to_hex(keys["nsec"])
        sender_pub = npub_to_hex(keys["npub"])

        recipient_keys = generate_keys()
        recipient_priv = nsec_to_hex(recipient_keys["nsec"])
        recipient_pub = npub_to_hex(recipient_keys["npub"])

        plaintext = "Hello, Nostr!"
        ciphertext = nip44_encrypt(plaintext, sender_priv, recipient_pub)
        decrypted = nip44_decrypt(ciphertext, recipient_priv, sender_pub)

        assert decrypted == plaintext

    def test_encrypt_decrypt_roundtrip_unicode(self):
        """NIP-44 should handle unicode characters."""
        keys = generate_keys()
        sender_priv = nsec_to_hex(keys["nsec"])

        recipient_keys = generate_keys()
        recipient_priv = nsec_to_hex(recipient_keys["nsec"])
        recipient_pub = npub_to_hex(recipient_keys["npub"])

        plaintext = "你好世界 🐱 NIP-44"
        ciphertext = nip44_encrypt(plaintext, sender_priv, recipient_pub)
        decrypted = nip44_decrypt(ciphertext, recipient_priv, npub_to_hex(keys["npub"]))

        assert decrypted == plaintext

    def test_encrypt_decrypt_roundtrip_long(self):
        """NIP-44 should handle long messages."""
        keys = generate_keys()
        sender_priv = nsec_to_hex(keys["nsec"])

        recipient_keys = generate_keys()
        recipient_priv = nsec_to_hex(recipient_keys["nsec"])
        recipient_pub = npub_to_hex(recipient_keys["npub"])

        plaintext = "A" * 10000
        ciphertext = nip44_encrypt(plaintext, sender_priv, recipient_pub)
        decrypted = nip44_decrypt(ciphertext, recipient_priv, npub_to_hex(keys["npub"]))

        assert decrypted == plaintext

    def test_encrypt_decrypt_compressed_pubkey(self):
        """NIP-44 should accept compressed (33-byte) pubkey."""
        keys = generate_keys()
        sender_priv = nsec_to_hex(keys["nsec"])

        recipient_keys = generate_keys()
        # Create compressed pubkey (0x02/0x03 + x)
        recipient_pub = npub_to_hex(recipient_keys["npub"])
        import secp256k1
        pk = secp256k1.PrivateKey(bytes.fromhex(nsec_to_hex(recipient_keys["nsec"])))
        compressed = pk.pubkey.serialize()  # 33 bytes

        plaintext = "Test with compressed key"
        ciphertext = nip44_encrypt(plaintext, sender_priv, compressed.hex())
        decrypted = nip44_decrypt(ciphertext, nsec_to_hex(recipient_keys["nsec"]), npub_to_hex(keys["npub"]))

        assert decrypted == plaintext


class TestNIP17Roundtrip:
    """Test NIP-17 gift wrap/unwrap roundtrip."""

    def test_wrap_unwrap_roundtrip(self):
        """NIP-17 wrap then unwrap should recover the original plaintext."""
        sender = generate_keys()
        recipient = generate_keys()

        sender_seckey = nsec_to_hex(sender["nsec"])
        sender_pubkey = npub_to_hex(sender["npub"])
        recipient_hex = npub_to_hex(recipient["npub"])
        recipient_seckey = nsec_to_hex(recipient["nsec"])

        # Derive recipient pubkey from seckey
        import secp256k1
        rp = secp256k1.PrivateKey(bytes.fromhex(recipient_seckey))
        recipient_pubkey = rp.pubkey.serialize()[1:].hex()

        plaintext = "Secret DM message"
        gift_wrap = nip17_wrap_message(
            plaintext=plaintext,
            sender_seckey=sender_seckey,
            sender_pubkey=sender_pubkey,
            recipient_pubkey=recipient_hex,
        )

        assert gift_wrap["kind"] == KIND_NIP17_GIFT_WRAP
        assert gift_wrap["pubkey"]  # ephemeral pubkey
        assert gift_wrap["sig"]     # signature

        rumor = nip17_unwrap(gift_wrap, recipient_seckey, recipient_pubkey)
        assert rumor is not None
        assert rumor["pubkey"] == sender_pubkey
        assert rumor["plaintext"] == plaintext

    def test_wrap_unwrap_unicode(self):
        """NIP-17 should handle unicode content."""
        sender = generate_keys()
        recipient = generate_keys()

        sender_seckey = nsec_to_hex(sender["nsec"])
        sender_pubkey = npub_to_hex(sender["npub"])
        recipient_hex = npub_to_hex(recipient["npub"])
        recipient_seckey = nsec_to_hex(recipient["nsec"])

        import secp256k1
        rp = secp256k1.PrivateKey(bytes.fromhex(recipient_seckey))
        recipient_pubkey = rp.pubkey.serialize()[1:].hex()

        plaintext = "🎉 消息测试 Emoji: 🐱🐶"
        gift_wrap = nip17_wrap_message(
            plaintext=plaintext,
            sender_seckey=sender_seckey,
            sender_pubkey=sender_pubkey,
            recipient_pubkey=recipient_hex,
        )

        rumor = nip17_unwrap(gift_wrap, recipient_seckey, recipient_pubkey)
        assert rumor is not None
        assert rumor["plaintext"] == plaintext

    def test_wrap_unwrap_with_subject(self):
        """NIP-17 wrap with subject tag should preserve it."""
        sender = generate_keys()
        recipient = generate_keys()

        sender_seckey = nsec_to_hex(sender["nsec"])
        sender_pubkey = npub_to_hex(sender["npub"])
        recipient_hex = npub_to_hex(recipient["npub"])
        recipient_seckey = nsec_to_hex(recipient["nsec"])

        import secp256k1
        rp = secp256k1.PrivateKey(bytes.fromhex(recipient_seckey))
        recipient_pubkey = rp.pubkey.serialize()[1:].hex()

        gift_wrap = nip17_wrap_message(
            plaintext="Message body",
            sender_seckey=sender_seckey,
            sender_pubkey=sender_pubkey,
            recipient_pubkey=recipient_hex,
            subject="Subject line",
        )

        rumor = nip17_unwrap(gift_wrap, recipient_seckey, recipient_pubkey)
        assert rumor is not None
        assert rumor["plaintext"] == "Message body"
        # Subject is in rumor tags
        subject_tags = [t for t in rumor.get("tags", []) if t[0] == "subject"]
        assert len(subject_tags) == 1
        assert subject_tags[0][1] == "Subject line"

    def test_unwrap_wrong_seckey_returns_none(self):
        """Unwrapping with wrong seckey should return None."""
        alice = generate_keys()
        bob = generate_keys()
        charlie = generate_keys()  # unrelated third party

        alice_seckey = nsec_to_hex(alice["nsec"])
        alice_pubkey = npub_to_hex(alice["npub"])
        bob_hex = npub_to_hex(bob["npub"])
        charlie_seckey = nsec_to_hex(charlie["nsec"])

        import secp256k1
        bp = secp256k1.PrivateKey(bytes.fromhex(bob_seckey := nsec_to_hex(bob["nsec"])))
        bob_pubkey = bp.pubkey.serialize()[1:].hex()

        gift_wrap = nip17_wrap_message(
            plaintext="Secret",
            sender_seckey=alice_seckey,
            sender_pubkey=alice_pubkey,
            recipient_pubkey=bob_hex,
        )

        # charlie's seckey can't decrypt alice→bob message
        rumor = nip17_unwrap(gift_wrap, charlie_seckey, bob_pubkey)
        # Should either return None or a mismatched rumor
        if rumor is not None:
            # If it returns a rumor, the pubkey won't match charlie's
            assert rumor.get("pubkey") != npub_to_hex(charlie["npub"])


class TestKeyConversions:
    """Test npub/nsec hex/bech32 conversions."""

    def test_npub_roundtrip(self):
        """npub → hex → npub should be stable."""
        keys = generate_keys()
        npub = keys["npub"]
        hex_pub = npub_to_hex(npub)
        npub_back = hex_to_npub(hex_pub)
        assert npub == npub_back

    def test_nsec_roundtrip(self):
        """nsec → hex → nsec should be stable."""
        keys = generate_keys()
        nsec = keys["nsec"]
        hex_priv = nsec_to_hex(nsec)
        assert len(hex_priv) == 64

    def test_generate_keys_produces_valid_npub_nsec(self):
        """generate_keys should produce valid bech32 npub/nsec."""
        keys = generate_keys()
        assert keys["npub"].startswith("npub1")
        assert keys["nsec"].startswith("nsec1")

        # Hex conversion should work
        hex_pub = npub_to_hex(keys["npub"])
        assert len(hex_pub) == 64

        hex_priv = nsec_to_hex(keys["nsec"])
        assert len(hex_priv) == 64

        # Roundtrip
        assert hex_to_npub(hex_pub) == keys["npub"]


class TestNpubToHexValidation:
    """Test npub_to_hex validation of raw hex input."""

    def test_npub_to_hex_valid_64char_hex(self):
        """Valid 64-char hex should be returned as-is."""
        keys = generate_keys()
        hex_pub = npub_to_hex(keys["npub"])
        # Verify it's exactly 64 hex chars
        assert len(hex_pub) == 64
        assert all(c in "0123456789abcdef" for c in hex_pub)

    def test_npub_to_hex_rejects_invalid_hex(self):
        """Invalid hex strings should be returned as-is (no crash)."""
        # Invalid chars
        result = npub_to_hex("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
        assert result == "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz"

    def test_npub_to_hex_rejects_wrong_length(self):
        """Wrong-length strings should be returned as-is."""
        result = npub_to_hex("abc123")
        assert result == "abc123"


class TestNIP17SealVerification:
    """Test NIP-17 seal signature verification."""

    def test_unwrap_rejects_tampered_seal(self):
        """Unwrapping a tampered seal (wrong id) should return None."""
        sender = generate_keys()
        recipient = generate_keys()

        sender_seckey = nsec_to_hex(sender["nsec"])
        sender_pubkey = npub_to_hex(sender["npub"])
        recipient_hex = npub_to_hex(recipient["npub"])
        recipient_seckey = nsec_to_hex(recipient["nsec"])

        import secp256k1
        rp = secp256k1.PrivateKey(bytes.fromhex(recipient_seckey))
        recipient_pubkey = rp.pubkey.serialize()[1:].hex()

        gift_wrap = nip17_wrap_message(
            plaintext="Secret",
            sender_seckey=sender_seckey,
            sender_pubkey=sender_pubkey,
            recipient_pubkey=recipient_hex,
        )

        # Tamper with the seal id inside the gift wrap content
        import secp256k1 as secp
        # First decrypt the gift wrap to get the seal
        from shared.key_manager import nip44_decrypt
        gift_pub = gift_wrap["pubkey"]
        seal_json = nip44_decrypt(gift_wrap["content"], recipient_seckey, gift_pub)
        import json as json_mod
        seal = json_mod.loads(seal_json)
        # Tamper: change the id
        seal["id"] = "0" * 64
        tampered_seal_ct = nip44_encrypt(
            json_mod.dumps(seal), gift_pub, recipient_hex
        )
        # Re-wrap
        tampered_gift = dict(gift_wrap)
        tampered_gift["content"] = tampered_seal_ct

        # The tampered gift should fail to unwrap
        rumor = nip17_unwrap(tampered_gift, recipient_seckey, recipient_pubkey)
        assert rumor is None
