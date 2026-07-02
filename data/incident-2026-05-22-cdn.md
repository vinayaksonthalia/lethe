# Incident Post-Mortem — 2026-05-22 (distractor)

**Summary:** Product images failed to load in Asia after an **image-cdn-edge** misconfiguration.

**Root cause:** an incorrect cache region was set during a CDN config update.

**Resolution:** the owning team rolled back the CDN configuration and added a staging check.
