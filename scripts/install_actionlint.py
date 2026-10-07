"""安装固定版本的官方 actionlint；校验 SHA-256 后仅提取可执行文件。"""

import hashlib
import io
import platform
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "1.7.12"
CHECKSUMS = {
    ("Linux", "x86_64"): (
        "linux_amd64",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
    ("Linux", "aarch64"): (
        "linux_arm64",
        "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
    ),
}


def main():
    target, expected = CHECKSUMS[(platform.system(), platform.machine())]
    directory = Path(__file__).resolve().parents[1] / ".tools"
    directory.mkdir(exist_ok=True)
    name = f"actionlint_{VERSION}_{target}.tar.gz"
    archive = directory / name
    if not archive.exists():
        url = f"https://github.com/rhysd/actionlint/releases/download/v{VERSION}/{name}"
        with urlopen(url, timeout=30) as response:
            content = response.read(16 * 1024 * 1024)
    else:
        content = archive.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise RuntimeError("actionlint archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as bundle:
        member = bundle.getmember("actionlint")
        if not member.isfile():
            raise RuntimeError("invalid actionlint archive")
        executable = directory / "actionlint"
        executable.write_bytes(bundle.extractfile(member).read())
        executable.chmod(0o755)
    print(f"actionlint {VERSION}: SHA-256 verified")


if __name__ == "__main__":
    main()
