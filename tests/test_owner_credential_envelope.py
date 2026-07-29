import json
import subprocess
from typing import cast

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
import pytest
from pydantic import ValidationError

from agent_org_network.owner_credential_envelope import (
    CredentialEnvelopeAad,
    OwnerCredentialEnvelope,
    OwnerCredentialEnvelopeUnavailable,
    X25519PublicJwk,
    decrypt_owner_credential,
    device_key_thumbprint,
    encrypt_owner_credential,
    parse_owner_credential_envelope,
    public_jwk,
    serialize_owner_credential_envelope,
)


def _private(byte: int) -> X25519PrivateKey:
    return X25519PrivateKey.from_private_bytes(bytes([byte]) * 32)


def _aad(device: X25519PrivateKey) -> CredentialEnvelopeAad:
    return CredentialEnvelopeAad(
        agent_card_id="support",
        credential_generation=1,
        credential_id="credential-1",
        device_key_thumbprint=device_key_thumbprint(public_jwk(device.public_key())),
        expires_at="2026-08-27T00:00:00Z",
        issued_at="2026-07-28T00:00:00Z",
        org_id="acme",
        owner_user_id="owner",
        scope=("author.read", "author.write"),
    )


def _vector() -> tuple[X25519PrivateKey, OwnerCredentialEnvelope]:
    device = _private(0x11)
    values = iter((b"\x33" * 32, b"\x44" * 12, b"\x55" * 32))
    envelope = encrypt_owner_credential(
        _aad(device),
        public_jwk(device.public_key()),
        ephemeral_private_factory=lambda: _private(0x22),
        random_bytes=lambda _size: next(values),
    )
    return device, envelope


def test_fixed_vector_roundtrip와_byte_exact_JCS다() -> None:
    device, envelope = _vector()
    result = decrypt_owner_credential(
        envelope, device, expected_aad=_aad(device)
    )
    assert result.credential_secret.get_secret_value() == (
        "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU"
    )
    wire = serialize_owner_credential_envelope(envelope)
    assert (
        envelope.ciphertext
        == "pP5dAkuve_mHidVvWUXG1IoyDClGRVmUvAAyoNYiVmxdfScNw7ZoeMAfrgWczswm"
        "gtBIgKEOaQzymwO9Yl53U8DsU1Ibb00478T9xXDwusYHLLeECRwCkuUyn8TdJqBbw"
        "KK1JIkB7wO8hmp4rG-5PGYIYkRcrkEEUoDvHEfxVJHbpiE"
    )
    assert envelope.ephemeral_public_key.x == (
        "D6poTtKIZ7l_Smot7l34zpdOdrcBjj8iocTPJnhXDyA"
    )
    assert wire == json.dumps(
        envelope.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert b"\x11" * 32 not in wire
    assert result.credential_secret.get_secret_value() not in repr(result)


def test_RFC7638_member_order_independent와_strict_JWK다() -> None:
    device = _private(0x11)
    jwk = public_jwk(device.public_key())
    reordered = X25519PublicJwk.model_validate(
        {"x": jwk.x, "kty": "OKP", "crv": "X25519"}
    )
    assert device_key_thumbprint(jwk) == device_key_thumbprint(reordered)
    for invalid in (
        {"x": jwk.x, "kty": "OKP", "crv": "X25519", "extra": "x"},
        {"x": jwk.x + "=", "kty": "OKP", "crv": "X25519"},
        {"x": "AA", "kty": "OKP", "crv": "X25519"},
        {"x": jwk.x, "kty": "RSA", "crv": "X25519"},
    ):
        with pytest.raises((ValidationError, OwnerCredentialEnvelopeUnavailable)):
            X25519PublicJwk.model_validate(invalid)


def test_binding_downgrade_wrong_key_truncation은failclosed다() -> None:
    device, envelope = _vector()
    wrong = _private(0x66)
    with pytest.raises(OwnerCredentialEnvelopeUnavailable):
        decrypt_owner_credential(envelope, wrong, expected_aad=_aad(wrong))
    with pytest.raises(ValidationError):
        OwnerCredentialEnvelope.model_validate(
            envelope.model_dump(mode="json") | {"suite": "unknown"}
        )
    with pytest.raises((ValidationError, OwnerCredentialEnvelopeUnavailable)):
        OwnerCredentialEnvelope.model_validate(
            envelope.model_dump(mode="json") | {"ciphertext": "AA"}
        )
    with pytest.raises(ValidationError):
        CredentialEnvelopeAad.model_validate(
            _aad(device).model_dump(mode="json")
            | {"scope": ["author.write", "author.read"]}
        )
    reordered = json.dumps(
        dict(reversed(list(envelope.model_dump(mode="json").items()))),
        separators=(",", ":"),
    ).encode()
    assert parse_owner_credential_envelope(reordered) == envelope
    with pytest.raises(OwnerCredentialEnvelopeUnavailable):
        parse_owner_credential_envelope(
            b'{"suite":"x","suite":"y"}'
        )
    with pytest.raises(OwnerCredentialEnvelopeUnavailable):
        parse_owner_credential_envelope(b"{" + b"x" * 8192 + b"}")
    with pytest.raises(OwnerCredentialEnvelopeUnavailable):
        parse_owner_credential_envelope(b"[" * 9 + b"0" + b"]" * 9)
    with pytest.raises(ValidationError):
        CredentialEnvelopeAad.model_validate(
            _aad(device).model_dump(mode="json")
            | {"issued_at": "2026-07-28T00:00:00+00:00"}
        )
    with pytest.raises(ValidationError):
        CredentialEnvelopeAad.model_validate(
            _aad(device).model_dump(mode="json")
            | {"expires_at": "2026-08-26T23:59:59Z"}
        )


def test_low_order_all_zero_public_key는failclosed다() -> None:
    device = _private(0x11)
    zero = X25519PublicJwk(
        x="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    aad = _aad(device).model_copy(
        update={"device_key_thumbprint": device_key_thumbprint(zero)}
    )
    with pytest.raises(OwnerCredentialEnvelopeUnavailable):
        encrypt_owner_credential(aad, zero)


def test_key_like_forged_object는failclosed다() -> None:
    device, envelope = _vector()
    with pytest.raises(OwnerCredentialEnvelopeUnavailable):
        decrypt_owner_credential(
            envelope,
            object(),  # pyright: ignore[reportArgumentType]
            expected_aad=_aad(device),
        )
    with pytest.raises(OwnerCredentialEnvelopeUnavailable):
        encrypt_owner_credential(
            _aad(device),
            public_jwk(device.public_key()),
            ephemeral_private_factory=lambda: cast(X25519PrivateKey, object()),
        )


def test_Node_WebCrypto가_Python_fixed_vector를독립복호화한다() -> None:
    device, envelope = _vector()
    raw_private = device.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )
    private_jwk = {
        "kty": "OKP",
        "crv": "X25519",
        "x": public_jwk(device.public_key()).x,
        "d": (
            __import__("base64")
            .urlsafe_b64encode(raw_private)
            .decode()
            .rstrip("=")
        ),
        "key_ops": ["deriveBits"],
        "ext": True,
    }
    script = r"""
const [privJwk, env] = JSON.parse(process.argv[1]);
const b64 = s => Buffer.from(s.replace(/-/g,'+').replace(/_/g,'/') + '='.repeat((4-s.length%4)%4),'base64');
const jcs = x => Array.isArray(x) ? '['+x.map(jcs).join(',')+']' :
  x && typeof x === 'object' ? '{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+jcs(x[k])).join(',')+'}' :
  JSON.stringify(x);
(async()=>{
 const priv=await crypto.subtle.importKey('jwk',privJwk,{name:'X25519'},false,['deriveBits']);
 const pub=await crypto.subtle.importKey('jwk',env.ephemeral_public_key,{name:'X25519'},false,[]);
 const shared=await crypto.subtle.deriveBits({name:'X25519',public:pub},priv,256);
 const aad=Buffer.from(jcs(env.aad));
 const digest=Buffer.from(await crypto.subtle.digest('SHA-256',aad));
 const ikm=await crypto.subtle.importKey('raw',shared,'HKDF',false,['deriveKey']);
 const key=await crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt:b64(env.kdf_salt),
   info:Buffer.concat([Buffer.from('agent-org-network/owner-install/credential-envelope/v1\0'),digest])},
   ikm,{name:'AES-GCM',length:256},false,['decrypt']);
 const plain=await crypto.subtle.decrypt({name:'AES-GCM',iv:b64(env.nonce),additionalData:aad},
   key,b64(env.ciphertext));
 process.stdout.write(Buffer.from(plain));
})().catch(e=>{console.error(e);process.exit(1)});
"""
    completed = subprocess.run(
        [
            "node",
            "-e",
            script,
            json.dumps([private_jwk, envelope.model_dump(mode="json")]),
        ],
        check=True,
        capture_output=True,
    )
    assert json.loads(completed.stdout) == {
        "credential_secret": "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU",
        "credential_type": "bearer",
        "envelope_version": 1,
    }


def test_Node_WebCrypto_envelope를_Python이역방향복호화한다() -> None:
    device = _private(0x11)
    aad = _aad(device)
    device_public = public_jwk(device.public_key()).model_dump(mode="json")
    script = r"""
const [devicePub,aad] = JSON.parse(process.argv[1]);
const b64 = b => Buffer.from(b).toString('base64url');
const jcs = x => Array.isArray(x) ? '['+x.map(jcs).join(',')+']' :
  x && typeof x === 'object' ? '{'+Object.keys(x).sort().map(k=>JSON.stringify(k)+':'+jcs(x[k])).join(',')+'}' :
  JSON.stringify(x);
(async()=>{
 const eph=await crypto.subtle.generateKey({name:'X25519'},true,['deriveBits']);
 const pub=await crypto.subtle.importKey('jwk',devicePub,{name:'X25519'},false,[]);
 const shared=await crypto.subtle.deriveBits({name:'X25519',public:pub},eph.privateKey,256);
 const aadBytes=Buffer.from(jcs(aad));
 const digest=Buffer.from(await crypto.subtle.digest('SHA-256',aadBytes));
 const salt=Buffer.alloc(32,0x33), nonce=Buffer.alloc(12,0x44);
 const ikm=await crypto.subtle.importKey('raw',shared,'HKDF',false,['deriveKey']);
 const key=await crypto.subtle.deriveKey({name:'HKDF',hash:'SHA-256',salt,
   info:Buffer.concat([Buffer.from('agent-org-network/owner-install/credential-envelope/v1\0'),digest])},
   ikm,{name:'AES-GCM',length:256},false,['encrypt']);
 const plain=Buffer.from(jcs({credential_secret:b64(Buffer.alloc(32,0x77)),
   credential_type:'bearer',envelope_version:1}));
 const cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce,additionalData:aadBytes},key,plain);
 const ephPub=await crypto.subtle.exportKey('jwk',eph.publicKey);
 process.stdout.write(JSON.stringify({aad,ciphertext:b64(cipher),
   ephemeral_public_key:{crv:'X25519',kty:'OKP',x:ephPub.x},
   kdf_salt:b64(salt),nonce:b64(nonce),
   suite:'AON-OWNER-PAIR-X25519-HKDF-SHA256-A256GCM-v1'}));
})().catch(e=>{console.error(e);process.exit(1)});
"""
    completed = subprocess.run(
        ["node", "-e", script, json.dumps([device_public, aad.model_dump(mode="json")])],
        check=True,
        capture_output=True,
    )
    envelope = parse_owner_credential_envelope(completed.stdout)
    result = decrypt_owner_credential(envelope, device, expected_aad=aad)
    assert result.credential_secret.get_secret_value() == (
        "d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3c"
    )


def test_production_defaults는매발급_ephemeral_salt_nonce_secret이다() -> None:
    device = _private(0x11)
    first = encrypt_owner_credential(_aad(device), public_jwk(device.public_key()))
    second = encrypt_owner_credential(_aad(device), public_jwk(device.public_key()))
    assert (
        first.ephemeral_public_key,
        first.kdf_salt,
        first.nonce,
        first.ciphertext,
    ) != (
        second.ephemeral_public_key,
        second.kdf_salt,
        second.nonce,
        second.ciphertext,
    )
