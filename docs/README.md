# CamPiX documentation

![CamPiX wordmark](../pictures/campix-wordmark-transparent.png)

CamPiX is an authenticated camera-control, recording, and streaming application for Raspberry Pi and Linux, with network-source profiles on Windows and macOS. Start with the [project README](../README.md) for an overview and one-command installation; use the [deployment guide](DEPLOYMENT.md) for platform requirements, operation, and recovery.

| Document                                       | Purpose                                                                                   |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------- |
| [Architecture](ARCHITECTURE.md)                | Implemented components, worker boundaries, capabilities, and design trade-offs.           |
| [Deployment](DEPLOYMENT.md)                    | One-command installation, platform profiles, packages, Docker, TLS, backup, and recovery. |
| [Installer design](INSTALLER-DESIGN.md)        | Installer boundaries, failure/recovery contract, acceptance evidence, and rollback.       |
| [Security](SECURITY.md)                        | Authentication, permissions, trust boundaries, and outstanding hardening.                 |
| [Release readiness](RELEASE-READINESS.md)      | Dated verification evidence, report coverage, and remaining production gates.             |
| [Repository analysis](REPOSITORY-ANALYSIS.md)  | Upstream branches, commit history, provenance, and migration lessons.                     |
| [Research report](deep-research-report.md)     | Ecosystem comparison and long-term target architecture, not a release feature list.       |
| [Contributing](../CONTRIBUTING.md)             | Development setup, verification, and English documentation/source-note conventions.       |
| [Engineering standard](ENGINEERING.md)         | Shared implementation, review, verification, and release requirements.                    |
| [Graphics and branding](../pictures/README.md) | Supplied banners, logos, badges, and design iterations.                                   |

Documentation and source comments are maintained in English. Interface localization is a separate feature; the optional German UI translation is retained.

Implementation claims belong in the architecture and release-readiness documents. Proposed features belong in research or explicitly labeled plans. Hardware compatibility requires recorded tests on an actual board/OS/camera combination.
