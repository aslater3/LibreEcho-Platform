#!/usr/bin/env python3
"""Build-only companion UI adapter; never edits the caller's source tree.
Fail closed if the pinned status/overview integration anchors drift.
"""
from pathlib import Path
import argparse
import shutil

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

def prepare(source, output):
    source, output = Path(source), Path(output)
    api = adapt_api((source / 'src/api.c').read_text())
    js = adapt_js((source / 'web/js/app.js').read_text())
    # Copy only build inputs, never Git metadata, generated binaries or secrets.
    shutil.copytree(source, output, ignore=shutil.ignore_patterns('.git', 'build', '__pycache__'))
    (output / 'src/api.c').write_text(api)
    (output / 'web/js/app.js').write_text(js)
    shutil.copyfile(Path(__file__).with_suffix('.h'), output / 'src/ota_v3_health.h')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    prepare(args.source, args.output)
