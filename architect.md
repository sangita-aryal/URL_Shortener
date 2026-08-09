# OPTIMAL URL SHORTENER ARCHITECTURE DESIGN SPECIFICATION

## 1. System Objective and Scope
The primary objective of the URL Shortener service is to achieve high-performance global redirection with ultra-low latency and high throughput. The architecture ensures modularity, security, and horizontal scalability, transitioning to a production-grade distributed system capable of handling millions of concurrent users.

**Platform Scale Parameters:**
*   **Daily Write Volume:** 100 Million URL creations/day (approx. 2,320 peak Write QPS).
*   **Daily Read Volume:** 1 Billion redirects/day (approx. 11,600 average Read QPS).
*   **Key Length:** Exactly 7 characters (Base 62) to support 3.52 Trillion unique keys.

## 2. Networking and Ingress Architecture
Exposing Python ASGI servers (FastAPI/Uvicorn) directly to the public internet is a severe anti-pattern due to vulnerability to slow-client I/O starvation.

*   **L7 API Gateway (Nginx):** Nginx acts as the primary reverse proxy and is the ONLY component exposed to the public internet (Port 80/443). It terminates SSL, buffers slow connections to protect backend threads, and applies IP-based rate limiting.
*   **Stateless Application Tier (FastAPI):** Python FastAPI workers execute business logic on an isolated private virtual network. They are 100% stateless; all state is fetched from the shared NoSQL store or cache.

## 3. Distributed Unique ID Generator
Standard auto-incrementing IDs create enumeration vulnerabilities, while UUIDs bloat the URL length. Centralized lock coordinators introduce severe network contention.

*   **Range Pre-Allocation (Segment Leasing):** To achieve sub-microsecond ID generation, each FastAPI container contacts MongoDB to atomically increment a centralized sequence counter by 1,000,000 using `find_one_and_update`. The server stores this range locally and increments its counter in lock-free $O(1)$ memory.
*   **42-Bit Feistel Cipher:** To prevent sequential crawling, IDs are scrambled using a custom in-memory 42-Bit Feistel Cipher (4 rounds). This bijective function mathematically guarantees collision-free permutation without requiring expensive database "read-before-write" checks. A 42-bit depth aligns perfectly with our 7-character Base 62 encoding maximum.

## 4. Security: TOCTOU SSRF Shield
To prevent Server-Side Request Forgery (SSRF) attacks targeting loopback or private subnets, incoming domains must be strictly validated.

*   **Asynchronous SSRF Validation & 302 Pass-Through:** During the URL creation (write path), the async SSRF shield resolves the domain once via non-blocking `aiodns`. If it resolves to a private IP (e.g., 127.0.0.1 or 10.0.0.x), the write is rejected.
*   **Redirection Path:** If safe, the original URL string is saved to the database. For the read path, the server simply returns the stored URL in an HTTP 302 `Location` header. This preserves Server Name Indication (SNI) and CDN compatibility for the client's browser, bypassing client-side TOCTOU rebinding vulnerabilities.

## 5. Database High-Availability and Caching
Storing massive volumes of unstructured redirection data requires horizontally scalable persistence.

*   **MongoDB Replica Set:** The data tier utilizes a horizontally sharded MongoDB Cluster with a Replica Set topology. Automated elections handle primary node failovers.
*   **Strict Write Concerns:** To prevent sequence lease rollbacks during a primary failover, the PyMongo driver must connect using a strict write concern of `w="majority"` and `j=True` (journaling). No ID lease is granted until permanently journaled across a majority of nodes.
*   **Redis Read-Through Cache:** Redirection requests first hit a Redis cluster. Cache hits resolve instantly in $O(1)$ time. Cache misses query MongoDB via a `secondaryPreferred` read preference to offload the master database, updating the cache lazily.

## 6. Telemetry and Analytics Pipeline
Buffering click analytics directly in Redis memory is a system anti-pattern that exhausts RAM and forces LRU evictions of hot redirection mappings. 

*   **Asynchronous Local Logging:** FastAPI web nodes track Daily Active Users (DAU) and clicks by writing telemetry asynchronously to structured, append-only local log files (e.g., `/var/log/url_shortener_analytics.log`).
*   **Stream-Processing Consumer:** An offline Python script acts as the analytics consumer. It streams the logs line-by-line via a generator, extracting the `user_id`, and feeds them into a deduplicated Python Set to calculate DAU. This maintains an $O(U)$ space complexity bounded strictly by unique users, keeping memory usage minimal.