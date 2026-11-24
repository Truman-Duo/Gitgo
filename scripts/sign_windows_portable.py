"""Sign a Windows Portable output using its already signed managed ZIP manifest."""
import argparse
import base64
from hashlib import file_digest
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.core.update.manifest import ManifestVerifier
from backend.core.update.portable import PORTABLE_FOOTER
from backend.core.update.tree import encode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--zip-manifest", required=True, type=Path)
    parser.add_argument("--zip-signature", required=True, type=Path)
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_pem_private_key

    key = load_pem_private_key(args.private_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey) or args.private_key.resolve().is_relative_to(ROOT):
        raise ValueError("Use the publisher's Ed25519 signing key outside the checkout")
    verifier = ManifestVerifier({args.key_id: key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)})
    archive_raw = args.zip_manifest.read_bytes()
    archive = verifier.verify(archive_raw, args.zip_signature.read_bytes())
    name = f"GitgoPortable-{archive.version}-win-x64.exe"
    if (archive.platform, archive.architecture, args.exe.name) != ("windows", "x86_64", name):
        raise ValueError("Portable executable name does not match the reviewed release")
    with args.exe.open("rb") as stream:
        stream.seek(-len(PORTABLE_FOOTER), 2)
        if stream.read() != PORTABLE_FOOTER:
            raise ValueError("Expected a portable Windows build output")
        size = stream.tell()
        stream.seek(0)
        digest = file_digest(stream, "sha256").hexdigest()
    value = json.loads(archive_raw)
    raw = encode({**value, "archive_format": "exe", "url": archive.url.rsplit("/", 1)[0] + "/" + name,
                  "bytes": size, "sha256": digest})
    signature = encode({"schema_version": 1, "algorithm": "ed25519", "key_id": args.key_id,
                        "signature": base64.b64encode(key.sign(raw)).decode("ascii")})
    verifier.verify_portable(raw, signature)
    args.output.mkdir(parents=True, exist_ok=True)
    for suffix, content in ((".manifest.json", raw), (".manifest.sig", signature)):
        with (args.output / ("windows-x86_64.portable" + suffix)).open("xb") as stream:
            stream.write(content)


if __name__ == "__main__":
    main()
