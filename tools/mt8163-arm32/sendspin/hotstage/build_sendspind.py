#!/usr/bin/env python3
"""Development ARM build of the UI repo's sendspind companion for hot-staging.

Stages the pinned SDK and its dependencies from the verified archives (the same
path the UI host lane uses), then cross-builds sendspind statically with the
rootless GCC toolchain. Not a release artifact.

  build_sendspind.py --ui UI_CHECKOUT --archives ARCHIVE_DIR --out OUT_DIR
"""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile

TOOLS = Path(__file__).resolve().parents[1]
PLATFORM = TOOLS.parents[2]
DEFAULT_PREFIX = Path.home() / '.cache/libreecho/toolchains/armhf-gcc15/bin/arm-linux-gnueabihf-'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(cmd, timeout):
    print('+', ' '.join(map(str, cmd)), flush=True)
    subprocess.run(list(map(str, cmd)), check=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ui', type=Path, required=True)
    parser.add_argument('--archives', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cross-prefix', default=os.environ.get('CROSS_PREFIX', str(DEFAULT_PREFIX)))
    args = parser.parse_args()
    work = Path(tempfile.mkdtemp(prefix='le-sendspind-', dir=os.environ.get('TMPDIR', Path.home() / '.cache')))
    source = load('verify_sendspin_sources', TOOLS / 'verify_sendspin_sources.py')
    lock = source.load_lock(TOOLS / 'SOURCE.lock')
    patches = source.load_patch_inventory(lock, TOOLS / 'SOURCE.lock', TOOLS / 'patches')
    stage, receipt = work / 'stage', work / 'receipt.json'
    source.stage(lock, args.archives.resolve(), stage, receipt, patches)
    source.verify_receipt(lock, stage, receipt, args.archives.resolve(), patch_map=patches)
    prefix = args.cross_prefix
    toolchain = work / 'armhf.cmake'
    toolchain.write_text(f'''set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)
set(CMAKE_C_COMPILER {prefix}gcc)
set(CMAKE_CXX_COMPILER {prefix}g++)
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_EXE_LINKER_FLAGS_INIT "-static")
''')
    build = work / 'build'
    cmd = ['cmake', '-S', args.ui.resolve() / 'src/adapter/sendspin', '-B', build,
           '-DCMAKE_TOOLCHAIN_FILE=' + str(toolchain), '-DCMAKE_BUILD_TYPE=Release',
           '-DSENDSPIN_SDK_DIR=' + str(stage / lock['identity::sdk']['path'])]
    for name, variable in [('ArduinoJson', 'ARDUINOJSON'), ('micro-flac', 'MICRO_FLAC'),
                           ('IXWebSocket', 'IXWEBSOCKET'), ('noise-c', 'NOISE_C')]:
        cmd.append(f'-DSENDSPIN_{variable}_DIR=' + str(stage / lock[name]['path']))
    run(cmd, 300)
    run(['cmake', '--build', build, '--target', 'sendspind', '-j', str(os.cpu_count() or 4)], 900)
    args.out.mkdir(parents=True, exist_ok=True)
    binary = args.out / 'sendspind'
    run([prefix + 'strip', '-o', binary, build / 'sendspind'], 60)
    print(hashlib.sha256(binary.read_bytes()).hexdigest(), binary)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
