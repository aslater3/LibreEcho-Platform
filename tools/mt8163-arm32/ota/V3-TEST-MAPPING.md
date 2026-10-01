# V2 → V3 safety-property mapping

V3 release states are complete and immutable; v1/v2 manifests are refused.
This mapping was recorded before deleting obsolete runtime test classes.
Offline v2 serialization/bundle tests remain as historical tooling tests, not device support.

| Retired class | Replacement / reason |
|---|---|
| AuthenticationBoundaryTests | test_ota_v3_manifest wrong signature; integration legacy refusal and channel mismatch; transport signed TLS bootstrap |
| LoopMountIdentityTests | test_ota_v3_activation loop backing mismatch/read-only options |
| LoopBindMountIdentityTests | Retired: overlays are forbidden; activation no-overlay-history test |
| TransactionFixtureTests | activation commit SIGKILL at each durable boundary and commit resume; convergence space policy/insufficient space |
| InitRebootResumeTests | activation commit resume, rollback, current activation; failure_policy init v3 routing |
| RuntimeHarnessTests | integration signed inspect/install/confirm ordering; convergence generation exactness; multi_target cross-target v3 refusal |
| PreConfirmAcceptanceTests | activation wrong-slot/unconfirmed BCB/boot identity/daemon hash; ota/test_service_identity remains maintained; init OTA health tests remain maintained |
| FreshInstallActivationTests | test_ota_v3_recovery fresh-install tree equals OTA; test_ota_v3_failure_policy boot activation |
| CommittedRuntimeLifecycleTests | convergence zero-download hash-checked hardlinks and corrupt-payload replacement; activation committed rehash, GC, rollback; overlays/authority inheritance retired |
| MultiFeatureRuntimeAuthorityTests | Retired composition; manifest exact five features, convergence full exact tree, activation all five signed payloads |
| CleanupCompatibilityTests | Retired v2 per-file backup cleanup; activation whole-generation GC retains current/previous/pending and never config |

`DevDiscoveryTests.test_dev_candidate_identity_includes_boot_and_v2_manifest`
is replaced by legacy-record refusal plus the signed-generation current/installed
identity tests in v3 convergence. Both dev and stable require whole-manifest
identity, never only matching boot bytes or a shared version string.
`RollbackFinalizationSourceContracts.test_lock_order_matches_the_real_update_flow`
now asserts fetch/install ordering plus the generation engine's shared mutation
lock before assembly; obsolete staging-per-feature assertions are removed.

## Authority and provenance
`ota/test_feature_provenance.py` now exercises signed current-generation observations,
per-feature observed versus target hashes, stable payload/manifest mismatch codes,
signature/COMPLETE/pointer identity failures, pending refusal and no writes.
The v2 installed record is not authoritative: `current` + signed transaction ID +
COMPLETE replace it; wrong directory IDs and stale bytes fail verification.

## Retired policy source assertions
`PolicyTests.test_preserve_installer_fails_before_boot_write_on_identity_mismatch`,
`test_preserve_installer_uses_installed_profile_for_transitions`, and
`test_preserve_pending_transaction_revalidates_after_staging` are replaced by
integration generation verification before boot write and activation rehash before
confirmation. No running-image daemon is required before reboot; daemon hashes
are checked from the candidate after reboot, with existing init health gates retained.

## Explicit safety gates
- Assembly at each durable boundary: real SIGKILL after manifest/signature/COMPLETE/mode sync/publish; rebuild and verify exact bytes.
- Crash at each commit durable boundary (previous/current/installed/pending-clear): real SIGKILL fixture, then retry.
- Stale installed diagnostic records cannot claim a current target; full signed generation identity is revalidated.
- Wrong running/selected slot and unconfirmed commit: activation negative tests.
- Signature/authentication: manifest wrong-signature and transport generation TLS tests.
- Space: convergence bad policy/overflow/insufficient space before downloading.
- Feature set: grammar exact list and required fields; generation exact tree test.
- Cross-target: runtime target tests use signed v3 and unchanged immutable loader.
- Channel mismatch: integration channel mismatch before install.
- Reboot resume: commit boundary retry/current rename and boot routing fixtures.
- No preserve/base/runtime-authority machinery: integration physical-absence test.

This evidence is host-only; no hardware, hosted CI or release qualification is claimed.
