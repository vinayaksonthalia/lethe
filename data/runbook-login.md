# Runbook — login and checkout failures

- Always review the recent change log first; most incidents follow a recent change.
- If users report failing logins or checkout, inspect the AuthService and every component it depends on.
- Escalate by paging the **on-call engineer for the owning team of the failing component**. Resolve the component's owning team from the service catalog, then resolve that team's current on-call engineer from the on-call roster. Page that person directly.
