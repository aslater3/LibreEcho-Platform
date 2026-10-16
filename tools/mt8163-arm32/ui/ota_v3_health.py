#!/usr/bin/env python3
"""Build-only companion UI adapter; never edits the caller's source tree.
Fail closed if the pinned status/overview integration anchors drift.
"""
from pathlib import Path
import argparse
import shutil
import re

BANNER = 'function otaHealthBanner(s){let n=document.getElementById("ota-health-banner");if(!n){n=document.createElement("div");n.id="ota-health-banner";n.role="alert";document.body.prepend(n)}const messages=[];if(s.features_state==="degraded")messages.push("Feature services unavailable: "+(s.features_error||"target verification failed"));if(s.config_error)messages.push("Configuration needs attention: "+s.config_error);n.textContent=messages.join(" — ");n.hidden=messages.length===0;}'

def adapt_api(source):
    if '#include "ota_v3_health.h"' in source:
        raise ValueError('OTA v3 adapter already applied')
    start = source.index('static void status_json(')
    end = source.index('\n', start)
    handler = source[start:end]
    old = 'struct le_system_status s;'
    tail = r'\"device_state\":\"%s\"}'
    args = 's.device_state);'
    for token in (old, tail, args):
        if handler.count(token) != 1:
            raise ValueError('companion status handler anchor changed: ' + token)
    handler = handler.replace(old, 'char ota_health[512];le_ota_health_json(ota_health,sizeof(ota_health));' + old)
    handler = handler.replace(tail, r'\"device_state\":\"%s\"%s}')
    handler = handler.replace(args, 's.device_state,ota_health);')
    return '#include "ota_v3_health.h"\n' + source[:start] + handler + source[end:]

def adapt_js(source):
    if source.count('state.data.status=s;') != 2:
        raise ValueError('companion overview status anchors changed')
    return '"use strict";\n' + BANNER + '\n' + source.replace('state.data.status=s;', 'state.data.status=s;otaHealthBanner(s);')

def adapt_init(name, source):
    features = {'libreecho-agentd.init': 'assistant', 'libreecho-waked.init': 'wakeword',
                'libreecho-sttd.init': 'stt', 'libreecho-ttsd.init': 'tts', 'libreecho-airplayd.init': 'airplay2'}
    if name in features:
        feature = features[name]
        payload = f'PAYLOAD=${{PAYLOAD:-/data/libreecho/features/{feature}/payload.squashfs}}\n'
        if source.count(payload) != 1:
            raise ValueError('companion payload anchor changed: ' + name)
        source = source.replace(payload, '')
        if name == 'libreecho-airplayd.init':
            source = source.replace('/data/libreecho/features/airplay2/avahi-services', '$RUNTIME_ROOT/etc/avahi/services')
        pattern = r'^mount_runtime\(\) \{\n.*?^\}\n'
        matches = list(re.finditer(pattern, source, re.M | re.S))
        if len(matches) != 1:
            raise ValueError('companion mount anchor changed: ' + name)
        old = matches[0].group()
        support = ''
        if feature == 'airplay2':
            anchor = '    create_support_mounts || return 1\n'
            if old.count(anchor) != 1:
                raise ValueError('companion AirPlay support anchor changed')
            support = old[old.index(anchor):old.rindex('}')]
        new = 'mount_runtime() {\n    # Platform owns authenticated generation mounts. Never remount legacy bytes.\n    awk -v p="$RUNTIME_ROOT" \'$5==p {n++; opts=","$6","; if (opts !~ /,ro,/ || opts !~ /,nosuid,/ || opts !~ /,nodev,/) bad=1; for(i=7;i<=NF;i++) if($i=="-" && $(i+1)!="squashfs") bad=1} END {exit n!=1 || bad}\' "${MOUNTINFO_FILE:-/proc/self/mountinfo}" || return 1\n'
        source = source[:matches[0].start()] + new + support + '}\n' + source[matches[0].end():]
        # Stop may tear down AirPlay support mounts, but not the shared root.
        source = source.replace('    umount "$RUNTIME_ROOT" 2>/dev/null || true', '    : # generation root lifetime belongs to Platform')
    if '/data/libreecho/features' in source or '$PAYLOAD' in source:
        raise ValueError('legacy companion feature authority remains: ' + name)
    return source


def verify_init(directory):
    for script in Path(directory).glob('*.init'):
        if script.is_symlink() or '/data/libreecho/features' in script.read_text():
            raise ValueError('legacy packaged feature authority: ' + script.name)


def prepare(source, output):
    source, output = Path(source), Path(output)
    api = adapt_api((source / 'src/api.c').read_text())
    js = adapt_js((source / 'web/js/app.js').read_text())
    # Copy only build inputs, never Git metadata, generated binaries or secrets.
    shutil.copytree(source, output, ignore=shutil.ignore_patterns('.git', 'build', '__pycache__'))
    (output / 'src/api.c').write_text(api)
    (output / 'web/js/app.js').write_text(js)
    for script in (output / 'init').glob('*.init'):
        script.write_text(adapt_init(script.name, script.read_text()))
    verify_init(output / 'init')
    shutil.copyfile(Path(__file__).with_suffix('.h'), output / 'src/ota_v3_health.h')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source')
    parser.add_argument('--verify-init')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.verify_init:
        verify_init(args.verify_init)
    elif args.source and args.output:
        prepare(args.source, args.output)
    else:
        parser.error('--source and --output or --verify-init are required')
