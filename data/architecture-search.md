# Architecture — Search path (distractor)

- The **SearchService** reads from the **search-index-primary** index.
- The **SearchService** is independent of the AuthService and the payments path.
- Search degradation does not affect login or checkout.
