"""Package an existing macOS development app with ad-hoc signatures, never publish it."""
from pathlib import Path
import argparse
import hashlib
import json
import platform
import plistlib
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MACHO = {bytes.fromhex(x) for x in ('feedface', 'feedfacf', 'cefaedfe', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}
OPENING = """声笺 · Sonafolio

要求：Apple Silicon（M 系列），macOS 14 或更高版本。
此包使用 ad-hoc 临时签名，没有 Developer ID 身份，也没有 Apple 公证。
首次打开可能被 macOS 拦截，不能保证只有普通的下载确认提示。

1. 打开 DMG，将 Sonafolio.app 拖入 Applications（应用程序），
   完成复制后推出磁盘映像，再从“应用程序”打开声笺。
2. 如果提示无法验证开发者或无法检查恶意软件，关闭提示。
3. 打开“系统设置 → 隐私与安全”，找到 Sonafolio，点击“仍要打开”，
   再按系统要求确认。仅在确认下载来源可信时这样操作。
   管理设备可能不允许手动放行，具体提示取决于 macOS 版本。
   Apple 说明：https://support.apple.com/102445
4. 选择一个专用工作区；在“设置 → 本地模型”下载模型，按需配置 API。
   运行包不包含用户录音、ASR／说话人模型或 API 凭据。
   依赖自身的公共样例与资源可能随包附带。

同目录 SHA256SUMS.txt 可用于检查 DMG 是否传输完整：
  shasum -a 256 -c SHA256SUMS.txt
校验值不代表 Apple 认证；请从项目可信发布渠道同时获取文件和校验值。
本测试版不代表 Windows／Intel Mac 支持或全新机器验收已经完成。
"""


def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def audit(app):
    root = app.resolve()
    binaries = []
    for path in app.rglob('*'):
        relative = path.relative_to(app)
        if path.is_symlink():
            if not path.resolve().is_relative_to(root):
                raise ValueError(f'External symlink: {relative}')
            continue
        if not path.is_file():
            continue
        if (set(relative.parts) & {'.models', '.cache', '.tmp', '.git', '.runtime', '.venv', 'secrets'}
                or path.name in {'sonafolio.toml', '.env'}
                or path.name.startswith('.env.')
                or path.suffix.lower() in {'.m4a', '.safetensors', '.gguf'}
                or relative.parts[:3] == ('Contents', 'Resources', 'data')):
            raise ValueError(f'Unexpected private/runtime data: {relative}')
        with path.open('rb') as source:
            if source.read(4) in MACHO:
                binaries.append(path)
    if not binaries:
        raise ValueError('No Mach-O executables found')
    return sorted(binaries, key=lambda p: (-len(p.parts), str(p)))


def package(source):
    if platform.system() != 'Darwin':
        raise ValueError('Packaging requires macOS codesign, ditto and hdiutil')
    source = source.resolve(strict=True)
    if not source.is_relative_to(ROOT) or source.suffix != '.app':
        raise ValueError('Source must be an .app inside this workspace')
    with (source/'Contents/Info.plist').open('rb') as f:
        info = plistlib.load(f)
    if info.get('CFBundleIdentifier') != 'org.sonafolio.desktop':
        raise ValueError('Source is not the Sonafolio desktop app')
    executable = source/'Contents/MacOS'/info['CFBundleExecutable']
    if not executable.resolve().is_relative_to(source):
        raise ValueError('Main executable escaped app')
    architectures = subprocess.check_output(['/usr/bin/lipo', '-archs', str(executable)], text=True).split()
    if architectures != ['arm64']:
        raise ValueError('This package currently supports only an arm64 app')
    version = info['CFBundleShortVersionString']
    if not all(c.isalnum() or c in '.-' for c in version):
        raise ValueError('Invalid version')
    name = f'Sonafolio-{version}-macos-arm64'
    output = ROOT/'dist/releases'
    output.mkdir(parents=True, exist_ok=True)
    if not output.resolve().is_relative_to(ROOT):
        raise ValueError('Release directory escaped workspace')
    destination = output/name
    if destination.exists():
        raise ValueError(f'Package already exists; preserve it before rebuilding: {destination}')
    # Work only on a disposable copy; preserve the source .app and existing assets.
    with tempfile.TemporaryDirectory(prefix='.package-', dir=output) as temp:
        stage = Path(temp)
        payload = stage/name
        payload.mkdir()
        app = payload/'Sonafolio.app'
        run('/usr/bin/ditto', '--noextattr', '--norsrc', str(source), str(app))
        binaries = audit(app)
        for binary in binaries:
            run('/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none', str(binary))
        run('/usr/bin/codesign', '--force', '--sign', '-', '--timestamp=none',
            '--identifier', info['CFBundleIdentifier'], str(app))
        run('/usr/bin/codesign', '--verify', '--deep', '--strict', str(app))
        # This install shortcut is intentionally outside the audited app bundle.
        (payload/'Applications').symlink_to('/Applications', target_is_directory=True)
        archive = stage/(name+'.dmg')
        run('/usr/bin/hdiutil', 'create', '-srcfolder', str(payload), '-volname',
            'Sonafolio', '-format', 'UDZO', str(archive))
        run('/usr/bin/hdiutil', 'verify', str(archive))
        # Inspect the final read-only filesystem, not only the source directory.
        mounted = stage/'mounted'
        mounted.mkdir()
        run('/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse',
            '-mountpoint', str(mounted), str(archive))
        try:
            restored = mounted/'Sonafolio.app'
            audit(restored)
            run('/usr/bin/codesign', '--verify', '--deep', '--strict', str(restored))
            if not (mounted/'Applications').is_symlink() or (mounted/'Applications').readlink() != Path('/Applications'):
                raise ValueError('Missing Applications install shortcut')
        finally:
            run('/usr/bin/hdiutil', 'detach', str(mounted))
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        assets = stage/'assets'
        assets.mkdir()
        shutil.move(str(archive), assets/archive.name)
        (assets/'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n')
        (assets/'FIRST-OPEN.txt').write_text(OPENING, encoding='utf-8')
        (assets/'release.json').write_text(json.dumps({
            'version': version, 'channel': 'development', 'platform': 'macos-arm64',
            'minimum_macos': info.get('LSMinimumSystemVersion', '14.0'),
            'signature': 'ad-hoc', 'notarized': False, 'macho_files_signed': len(binaries),
            'archive': archive.name, 'sha256': digest,
        }, ensure_ascii=False, indent=2)+'\n')
        assets.rename(destination)
    print(f'Created local development assets: {destination}')
    print(f'Ad-hoc signed {len(binaries)} Mach-O files; read-only disk image and app signature verified.')
    print('Not notarized. Gatekeeper may block first launch. Nothing uploaded.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=ROOT/'dist/macos/Sonafolio.app')
    args = parser.parse_args()
    try:
        package(args.app)
    except ValueError as error:
        raise SystemExit(str(error))
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr.decode(errors='replace') if error.stderr else str(error))
