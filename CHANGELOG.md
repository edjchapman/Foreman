# Changelog

## [0.13.6](https://github.com/edjchapman/Foreman/compare/v0.13.5...v0.13.6) (2026-07-08)


### Documentation

* **claude:** extend the milestone roadmap with M6 and M7 ([#113](https://github.com/edjchapman/Foreman/issues/113)) ([6195623](https://github.com/edjchapman/Foreman/commit/6195623e3170e0a61e7f053b700663c36250abdf))
* **readme:** extend the milestone note with M6 (load) and M7 (tracing) ([#112](https://github.com/edjchapman/Foreman/issues/112)) ([24e8bd1](https://github.com/edjchapman/Foreman/commit/24e8bd10a2b080c8b23d1c7ad4699e6f2112fec5))

## [0.13.5](https://github.com/edjchapman/Foreman/compare/v0.13.4...v0.13.5) (2026-07-08)


### Documentation

* **case-study:** add distributed tracing, gate-placement, and buildless-demo notes ([#110](https://github.com/edjchapman/Foreman/issues/110)) ([36e721d](https://github.com/edjchapman/Foreman/commit/36e721d80bd4e63bf1098402c8525a37e574b6a9))

## [0.13.4](https://github.com/edjchapman/Foreman/compare/v0.13.3...v0.13.4) (2026-07-08)


### Code Refactoring

* **demo:** consolidate the page on Alpine, one-row controls, refresh README ([#108](https://github.com/edjchapman/Foreman/issues/108)) ([75f7d8e](https://github.com/edjchapman/Foreman/commit/75f7d8e92b84ace1d26c97c07b7f92cf35dbe820))

## [0.13.3](https://github.com/edjchapman/Foreman/compare/v0.13.2...v0.13.3) (2026-07-08)


### Code Refactoring

* **demo:** put live activity centre stage with a compact control bar ([#106](https://github.com/edjchapman/Foreman/issues/106)) ([a280c0c](https://github.com/edjchapman/Foreman/commit/a280c0c6c72fed776f0bf85d4b86edb4cd81d64b))

## [0.13.2](https://github.com/edjchapman/Foreman/compare/v0.13.1...v0.13.2) (2026-07-08)


### Bug Fixes

* **demo:** cap queue-board lane height so it scrolls instead of growing the page ([#104](https://github.com/edjchapman/Foreman/issues/104)) ([e3dcd95](https://github.com/edjchapman/Foreman/commit/e3dcd95b93d58c3412e51a86d076177dcbfbad80))

## [0.13.1](https://github.com/edjchapman/Foreman/compare/v0.13.0...v0.13.1) (2026-07-07)


### Bug Fixes

* **build:** pin runtime image back to Python 3.12 to match the venv ([#101](https://github.com/edjchapman/Foreman/issues/101)) ([93a1ed0](https://github.com/edjchapman/Foreman/commit/93a1ed09538b352ccad0525f936fac75525f1305))

## [0.13.0](https://github.com/edjchapman/Foreman/compare/v0.12.0...v0.13.0) (2026-07-07)


### Features

* **observability:** OpenTelemetry distributed tracing across the outbox ([#99](https://github.com/edjchapman/Foreman/issues/99)) ([617ae48](https://github.com/edjchapman/Foreman/commit/617ae4836d26be7e5b13895aae2ff5b828e50e11))

## [0.12.0](https://github.com/edjchapman/Foreman/compare/v0.11.4...v0.12.0) (2026-07-07)


### Features

* **demo:** add a live queue board to the demo page ([#97](https://github.com/edjchapman/Foreman/issues/97)) ([b4932f8](https://github.com/edjchapman/Foreman/commit/b4932f845d2783b7a7430c7387335d1a0822b1fd))

## [0.11.4](https://github.com/edjchapman/Foreman/compare/v0.11.3...v0.11.4) (2026-07-06)


### Documentation

* lead the README with a successful run and document the agentic workflow ([#93](https://github.com/edjchapman/Foreman/issues/93)) ([e5f45c3](https://github.com/edjchapman/Foreman/commit/e5f45c37960a79a7c8c2154802d5d8d1b78839eb))

## [0.11.3](https://github.com/edjchapman/Foreman/compare/v0.11.2...v0.11.3) (2026-07-06)


### Build System

* **deps:** bump python from 3.12-slim-bookworm to 3.14-slim-bookworm ([#90](https://github.com/edjchapman/Foreman/issues/90)) ([15dd0bb](https://github.com/edjchapman/Foreman/commit/15dd0bb8fd1f6c8908631566c15162f299e21ef2))

## [0.11.2](https://github.com/edjchapman/Foreman/compare/v0.11.1...v0.11.2) (2026-07-03)


### Documentation

* refresh demo imagery for the restyled page ([#87](https://github.com/edjchapman/Foreman/issues/87)) ([33a428a](https://github.com/edjchapman/Foreman/commit/33a428a418f356f911af65d9510aaac774024a2a))

## [0.11.1](https://github.com/edjchapman/Foreman/compare/v0.11.0...v0.11.1) (2026-07-03)


### Documentation

* restructure README around signposts to the docs ([#85](https://github.com/edjchapman/Foreman/issues/85)) ([e91d442](https://github.com/edjchapman/Foreman/commit/e91d44208b9b340240cad94231f7c7a5c2db7955))

## [0.11.0](https://github.com/edjchapman/Foreman/compare/v0.10.3...v0.11.0) (2026-07-03)


### Features

* **demo:** show retries, dead-letter and redrive live ([#77](https://github.com/edjchapman/Foreman/issues/77)) ([9869b11](https://github.com/edjchapman/Foreman/commit/9869b11c733524e6fd3f33ab4c57130d3a8bd5ee))
* **dispatch:** LISTEN/NOTIFY push dispatch with Beat fallback ([#81](https://github.com/edjchapman/Foreman/issues/81)) ([a3353b6](https://github.com/edjchapman/Foreman/commit/a3353b60075553bbbe5cf8ba08df0235be41981b))


### Documentation

* add architecture diagrams and demo screenshots ([#78](https://github.com/edjchapman/Foreman/issues/78)) ([a59e15b](https://github.com/edjchapman/Foreman/commit/a59e15bea5a908f4886d5d2db339fc462eb38e2c))

## [0.10.3](https://github.com/edjchapman/Foreman/compare/v0.10.2...v0.10.3) (2026-07-03)


### Build System

* multi-stage image + migrate-gated compose ([#79](https://github.com/edjchapman/Foreman/issues/79)) ([adea78e](https://github.com/edjchapman/Foreman/commit/adea78e38a17721b2ecf0e393f60a25319ff76bd))

## [0.10.2](https://github.com/edjchapman/Foreman/compare/v0.10.1...v0.10.2) (2026-07-03)


### Documentation

* surface load-test results prominently in the README ([#73](https://github.com/edjchapman/Foreman/issues/73)) ([8a6f9a5](https://github.com/edjchapman/Foreman/commit/8a6f9a5b87219a09a0048e6a564802eb81504caa))

## [0.10.1](https://github.com/edjchapman/Foreman/compare/v0.10.0...v0.10.1) (2026-07-03)


### Documentation

* capture load-test baseline numbers ([#71](https://github.com/edjchapman/Foreman/issues/71)) ([290ae89](https://github.com/edjchapman/Foreman/commit/290ae89922eaa4aebf6e792778d1075aa025eadc))

## [0.10.0](https://github.com/edjchapman/Foreman/compare/v0.9.5...v0.10.0) (2026-07-03)


### Features

* load testing + event-rate and latency metrics ([#69](https://github.com/edjchapman/Foreman/issues/69)) ([aa0dd86](https://github.com/edjchapman/Foreman/commit/aa0dd869df198fa4bfba7c6ec674780221050e71))

## [0.9.5](https://github.com/edjchapman/foreman/compare/v0.9.4...v0.9.5) (2026-07-02)


### Documentation

* add the reliability case study — completes M5 ([#65](https://github.com/edjchapman/foreman/issues/65)) ([2e90506](https://github.com/edjchapman/foreman/commit/2e90506e30c9f88d176ce371825e6ea3ea6aa7e7))

## [0.9.4](https://github.com/edjchapman/foreman/compare/v0.9.3...v0.9.4) (2026-07-02)


### Documentation

* fix broken ci.md pipeline diagram; tighten overclaims ([#61](https://github.com/edjchapman/foreman/issues/61)) ([95671f4](https://github.com/edjchapman/foreman/commit/95671f4a5ee9d3cb57db07edb76cefc0de95323c))

## [0.9.3](https://github.com/edjchapman/foreman/compare/v0.9.2...v0.9.3) (2026-07-02)


### Code Refactoring

* **scripts:** extract shared Railway lib; fix stale-branch output ([#57](https://github.com/edjchapman/foreman/issues/57)) ([b4aa1e6](https://github.com/edjchapman/foreman/commit/b4aa1e68ad30bd514d0c48bcd8f31666305070ac))


### Documentation

* showcase the live demo, add CI pipeline overview + code of conduct ([#58](https://github.com/edjchapman/foreman/issues/58)) ([11f6e82](https://github.com/edjchapman/foreman/commit/11f6e82afd8fddef68906b8271148e0f7aaf61b4))

## [0.9.2](https://github.com/edjchapman/foreman/compare/v0.9.1...v0.9.2) (2026-07-02)


### Bug Fixes

* **deploy:** poll rollouts by deployment id; replace vanished bitnami image ([#53](https://github.com/edjchapman/foreman/issues/53)) ([5f2f96a](https://github.com/edjchapman/foreman/commit/5f2f96af883369ae576a697edf84d7e58d54a50f))

## [0.9.1](https://github.com/edjchapman/foreman/compare/v0.9.0...v0.9.1) (2026-07-02)


### Bug Fixes

* **deploy:** unblock Railway healthchecks and drop the redis volume ([#51](https://github.com/edjchapman/foreman/issues/51)) ([5a41141](https://github.com/edjchapman/foreman/commit/5a41141498be5aa7e40ff086f993737e036a3f87))

## [0.9.0](https://github.com/edjchapman/foreman/compare/v0.8.0...v0.9.0) (2026-07-02)


### Features

* **deploy:** railway-configure script closes the IaC provider gaps ([#49](https://github.com/edjchapman/foreman/issues/49)) ([daa9b93](https://github.com/edjchapman/foreman/commit/daa9b933d2718a7557405da6076264f66a7c48ad))

## [0.8.0](https://github.com/edjchapman/foreman/compare/v0.7.0...v0.8.0) (2026-07-02)


### Features

* **deploy:** Railway platform — Terraform IaC, semver-pinned CD, release pipeline ([#46](https://github.com/edjchapman/foreman/issues/46)) ([cca9722](https://github.com/edjchapman/foreman/commit/cca972284967a12a31f7efd2b3f8fc61b6fce2de))
* **deploy:** verify worker/beat rollout + GHCR tag pre-flight ([#48](https://github.com/edjchapman/foreman/issues/48)) ([7360039](https://github.com/edjchapman/foreman/commit/7360039bb7e0d04246ccd9aa0998d2f50f97fb6c))

## [0.7.0](https://github.com/edjchapman/foreman/compare/v0.6.0...v0.7.0) (2026-07-02)


### Features

* downloadable CSV report for succeeded jobs ([#42](https://github.com/edjchapman/foreman/issues/42)) ([35f93ad](https://github.com/edjchapman/foreman/commit/35f93adc1e848fa533b93565bce30cfe6838aefe))

## [0.6.0](https://github.com/edjchapman/foreman/compare/v0.5.0...v0.6.0) (2026-07-01)


### Features

* **deploy:** production-harden settings, WhiteNoise static, non-root Docker ([#40](https://github.com/edjchapman/foreman/issues/40)) ([f8a46f6](https://github.com/edjchapman/foreman/commit/f8a46f69bf059839cdcdf601904d70ab9743f5e2))

## [0.5.0](https://github.com/edjchapman/foreman/compare/v0.4.0...v0.5.0) (2026-07-01)


### Features

* **ui:** minimal live job-status demo page ([#38](https://github.com/edjchapman/foreman/issues/38)) ([bc285c0](https://github.com/edjchapman/foreman/commit/bc285c0363d56e1318d03c8cc6b6f82994c1a537))

## [0.4.0](https://github.com/edjchapman/foreman/compare/v0.3.0...v0.4.0) (2026-07-01)


### Features

* **realtime:** stream live job status over WebSockets (Channels) ([#36](https://github.com/edjchapman/foreman/issues/36)) ([ef738e2](https://github.com/edjchapman/foreman/commit/ef738e2684928359d62484b823172f469e3012cb))

## [0.3.0](https://github.com/edjchapman/foreman/compare/v0.2.3...v0.3.0) (2026-07-01)


### Features

* **observability:** structured logging, DB-derived metrics, liveness/readiness, runbook + ADR 0003 ([#34](https://github.com/edjchapman/foreman/issues/34)) ([6174638](https://github.com/edjchapman/foreman/commit/61746380a8178431f0d1b03f383dd351d0886752))

## [0.2.3](https://github.com/edjchapman/foreman/compare/v0.2.2...v0.2.3) (2026-07-01)


### Build System

* **deps-dev:** bump mypy from 1.19.1 to 2.1.0 in the python group across 1 directory ([#30](https://github.com/edjchapman/foreman/issues/30)) ([43106e3](https://github.com/edjchapman/foreman/commit/43106e3ebe68975a84748f1513dd75efac240356))

## [0.2.2](https://github.com/edjchapman/foreman/compare/v0.2.1...v0.2.2) (2026-07-01)


### Build System

* **security:** pin base-image digest, docker Dependabot, provenance ([#27](https://github.com/edjchapman/foreman/issues/27)) ([b2fe7c1](https://github.com/edjchapman/foreman/commit/b2fe7c1219828869b758cce84b02d7878cb67fd4))


### Documentation

* **readme:** best-practice portfolio README with table of contents ([#28](https://github.com/edjchapman/foreman/issues/28)) ([f4b141d](https://github.com/edjchapman/foreman/commit/f4b141d729913bb4d8c51024935ce5c9130123fe))

## [0.2.1](https://github.com/edjchapman/foreman/compare/v0.2.0...v0.2.1) (2026-07-01)


### Documentation

* **readme:** badges, architecture diagram, platform docs ([#24](https://github.com/edjchapman/foreman/issues/24)) ([bd6a04d](https://github.com/edjchapman/foreman/commit/bd6a04d7ed4b63d587b95ccd31d3f80897ff20dc))

## [0.2.0](https://github.com/edjchapman/foreman/compare/v0.1.0...v0.2.0) (2026-06-30)


### Features

* **release:** release-please + GHCR image publishing ([#22](https://github.com/edjchapman/foreman/issues/22)) ([af69950](https://github.com/edjchapman/foreman/commit/af6995027ad7af9435312edc61b51af6bc770c02))
