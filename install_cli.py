"""Build-time official CLI install. Fixed archive digest; no runtime updater."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import urllib.request

URL = "https://github.com/higgsfield-ai/cli/releases/download/v1.1.26/hf_1.1.26_linux_amd64.tar.gz"
SHA256 = "5d666fae70c99b7388690191a649d1487962250ed50820e079edfd1ffac7bf5b"


def install(target):
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read(128 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise RuntimeError("CLI archive integrity failed")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [m for m in archive.getmembers() if Path(m.name).name == "hf" and m.isfile()]
        if len(members) != 1:
            raise RuntimeError("CLI archive layout failed")
        with archive.extractfile(members[0]) as stream:
            binary = stream.read(128 * 1024 * 1024 + 1)
    if len(binary) > 128 * 1024 * 1024:
        raise RuntimeError("CLI size exceeded")
    target.write_bytes(binary)
    target.chmod(0o755)


if __name__ == "__main__":
    install(Path(sys.argv[1]))
