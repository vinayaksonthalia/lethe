# Incident Post-Mortem — 2026-03-12

**Summary:** The **eu-west-redis** cache ran out of memory and evicted session keys. Logins failed across the board, and because payments require a valid login token, checkout failed too.

**Root cause:** the Redis `maxmemory` limit was set too low for peak traffic, triggering key eviction.

**Resolution:** the owning team raised `maxmemory` and added memory-pressure alerts.
