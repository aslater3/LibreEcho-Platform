"""Boot markers -> real companion status serializer -> safe browser banner.
Host source-integration fixture; no device or complete ARM image is implied.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from test_ota_v3_failure_policy import FailurePolicyTests
from test_ota_v3_manifest import TOOLS

class WebStatusTests(FailurePolicyTests):
    def test_packager_adapts_private_snapshot_before_compiling(self):
        source = (TOOLS / 'ui/build_ui_bundle.sh').read_text()
        self.assertIn('ota_v3_health.py', source)
        self.assertLess(source.index('ota_v3_health.py'), source.index('"$MAKE_BIN" -C "$UI_SOURCE" clean'))
        self.assertIn('ui_ota_adapter_sha256', source)

    def test_packager_publishes_snapshot_build_tree_to_caller_checkout(self):
        # Product snapshots relink objects from "$UI_SOURCE/build" of the
        # checkout it passed in, after this builder returns. The compile runs in
        # a private snapshot that is deleted on exit, so the shipped build tree
        # must be copied back to the caller's checkout once it is final.
        source = (TOOLS / 'ui/build_ui_bundle.sh').read_text()
        publish = 'cp -a -- "$UI_SOURCE/build" "$ui_input_source/build"'
        self.assertIn(publish, source)
        self.assertIn('rm -rf -- "$ui_input_source/build"', source)
        self.assertLess(source.index('--strip-unneeded "$OUTPUT/sbin/$binary"'), source.index(publish))
        self.assertLess(source.index(publish), source.index("printf 'ui_source=%s"))

    def test_private_snapshot_is_applied_without_modifying_companion(self):
        adapter = TOOLS / 'ui/ota_v3_health.py'
        spec = importlib.util.spec_from_file_location('health_copy', adapter)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        ui = Path(os.environ.get('LIBREECHO_OTA_UI_SOURCE', str(TOOLS.parents[1] / '.test-ui')))
        before = {name: (ui / name).read_bytes() for name in ('src/api.c', 'web/js/app.js')}
        output = self.root / 'snapshot'
        module.prepare(ui, output)
        self.assertEqual(before, {name: (ui / name).read_bytes() for name in before})
        self.assertIn('le_ota_health_json', (output / 'src/api.c').read_text())
        self.assertIn('otaHealthBanner(s);', (output / 'web/js/app.js').read_text())
        self.assertTrue((output / 'src/ota_v3_health.h').is_file())
        for malformed in ('', before['src/api.c'].decode().replace('s.device_state);', 'changed);')):
            with self.assertRaises((ValueError, IndexError)):
                module.adapt_api(malformed)

    def test_packaged_agentd_uses_generation_mount_without_legacy_payload(self):
        import shutil
        spec = importlib.util.spec_from_file_location('init_adapter', TOOLS / 'ui/ota_v3_health.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        ui = Path(os.environ.get('LIBREECHO_OTA_UI_SOURCE', str(TOOLS.parents[1] / '.test-ui')))
        source = self.root / 'ui-source'
        shutil.copytree(ui, source)
        if not (source / 'init/libreecho-agentd.init').is_file():
            shutil.copytree(TOOLS / 'ota/fixtures/companion-init', source / 'init', dirs_exist_ok=True)
        output = self.root / 'adapted'
        module.prepare(source, output)
        script = (output / 'init/libreecho-agentd.init').read_text()
        runtime = self.root / 'run/libreecho/features/assistant/root'
        binary = runtime / 'usr/local/sbin/libreecho-agentd'
        binary.parent.mkdir(parents=True); binary.write_bytes(b'daemon'); binary.chmod(0o755)
        mounts = self.root / 'mountinfo'
        mounts.write_text(f'1 0 7:0 / {runtime} ro,nosuid,nodev - squashfs /dev/loop0 ro\n')
        body = script[:script.rfind('case "${1:-}" in')]
        result = subprocess.run(['/bin/busybox', 'sh'], input=body + '\nmount_runtime\n',
            env=dict(os.environ, RUNTIME_ROOT=str(runtime), MOUNTINFO_FILE=str(mounts)), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('/data/libreecho/features', script)
        mounts.write_text('')
        result = subprocess.run(['/bin/busybox', 'sh'], input=body + '\nmount_runtime\n',
            env=dict(os.environ, RUNTIME_ROOT=str(runtime), MOUNTINFO_FILE=str(mounts)), text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        for script in (output / 'init').glob('*.init'):
            self.assertNotIn('/data/libreecho/features', script.read_text())
            self.assertEqual(subprocess.run(['/bin/busybox', 'sh', '-n', str(script)]).returncode, 0)

    def test_unknown_companion_feature_reference_fails_build(self):
        spec = importlib.util.spec_from_file_location('init_adapter', TOOLS / 'ui/ota_v3_health.py')
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with self.assertRaises(ValueError):
            module.adapt_init('unknown.init', 'cat /data/libreecho/features/unknown/file\n')

    def test_boot_failure_and_config_error_reach_status_json_and_banner(self):
        adapter = TOOLS / 'ui/ota_v3_health.py'
        self.assertTrue(adapter.is_file(), 'missing Platform companion UI health adapter')
        spec = importlib.util.spec_from_file_location('health_adapter', adapter)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        ui = Path(os.environ.get('LIBREECHO_OTA_UI_SOURCE', str(TOOLS.parents[1] / '.test-ui')))
        self.assertTrue((ui / 'src/api.c').is_file(), 'pinned companion API source is required')
        self.test_feature_failure_starts_web_but_not_feature_daemons()
        update = self.root / 'data/libreecho/update'
        config = self.root / 'data/libreecho/config'; config.mkdir()
        (config / 'web-config.json').write_text('{"schema":99}')
        result = subprocess.run(['/bin/busybox', 'sh', str(TOOLS / 'initramfs/libreecho-config-migrate'), '1'],
                                env=dict(self.env, ROOT=str(update), CONFIG_ROOT=str(config)), capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        api = module.adapt_api((ui / 'src/api.c').read_text())
        handler = api[api.index('static void status_json('):].split('\n', 1)[0]
        harness = self.root / 'status.c'
        harness.write_text(r'''#include <stdio.h>
#include <string.h>
#include <stdarg.h>
#include "ota_v3_health.h"
struct le_system_status {double uptime; int cpu,memory,storage,temperature,memory_used_mb,memory_total_mb,storage_used_mb,storage_total_mb,storage_available,light_lux; char storage_state[32],device_state[24];};
struct api_context {void *backend;}; struct api_response {int status; char body[4096];};
#define LE_IO 1
static int le_get_system_status(void *unused,struct le_system_status *s){(void)unused;memset(s,0,sizeof(*s));return 0;}
static int cpu_json(struct le_system_status *s,char *p,size_t n){(void)s;snprintf(p,n,"[]");return 0;}
static const char *le_backend_mode(void *b){(void)b;return "linux";}
static void err(struct api_response *r,int a,int b,const char *s){(void)b;(void)s;r->status=a;}
static void out(struct api_response *r,int status,const char *fmt,...){va_list ap;r->status=status;va_start(ap,fmt);vsnprintf(r->body,sizeof(r->body),fmt,ap);va_end(ap);}
''' + handler + '\nint main(void){struct api_context c={0};struct api_response r={0};status_json(&c,&r);puts(r.body);return r.status!=200;}\n')
        binary = self.root / 'status'
        command = ['cc', '-Wall', '-Wextra', '-Werror', '-I' + str(TOOLS / 'ui'),
                   '-DLE_OTA_STATUS_ROOT="' + str(update) + '"', str(harness), '-o', str(binary)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)['data']
        self.assertEqual(data['features_state'], 'degraded')
        self.assertEqual(data['features_error'], 'feature-stt-payload-hash')
        self.assertEqual(data['config_error'], 'unsupported_schema')
        js = module.adapt_js((ui / 'web/js/app.js').read_text())
        function = js[js.index('function otaHealthBanner('):].split('\n', 1)[0]
        script = self.root / 'banner.js'
        script.write_text('const nodes={}; const document={getElementById:id=>nodes[id]||null,createElement:()=>({}),body:{prepend:n=>{nodes[n.id]=n}}};\n' + function +
                          '\notaHealthBanner(' + json.dumps(data) + ');\nconsole.log(JSON.stringify(nodes["ota-health-banner"]));\n')
        result = subprocess.run(['node', str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        banner = json.loads(result.stdout)
        self.assertFalse(banner['hidden'])
        self.assertIn('unsupported_schema', banner['textContent'])
        self.assertIn('feature-stt-payload-hash', banner['textContent'])
        self.assertNotIn('innerHTML', function)

if __name__ == '__main__':
    unittest.main()
