# System Prompt & Architectural Mandate: URL Shortener

## 1. Project Objective & Tech Stack
We are building a stateless, highly scalable distributed URL Shortener designed to handle massive concurrent read/write volumes with ultra-low redirection latency. 

**Tech Stack:**
*   **Edge Proxy / API Gateway:** Containerized Nginx (Layer 7).
*   **Application Tier:** Stateless Python FastAPI ASGI workers.
*   **Database Tier:** Horizontal Sharded MongoDB Cluster.
*   **Cache Infrastructure:** Read-Through Redis RAM Cluster.

## 2. Architectural Constraints
You must strictly enforce the following boundaries:

*   **Stateless Compute:** The FastAPI servers must remain 100% stateless. No client data, session tokens, or presentation states shall be stored in local memory.
*   **Network Isolation:** The Nginx Load Balancer is the ONLY component with a public-facing IP (exposing port 80). Communication to the FastAPI, Redis, and MongoDB containers must occur exclusively over a private internal network.
*   **Distributed ID Generation:** Design a highly available, collision-free ID generation scheme that maps to exactly 7 Base-62 characters. It must scale horizontally across all FastAPI instances without bottlenecks.
*   **Security (SSRF):** Validate incoming domains to prevent Server-Side Request Forgery (SSRF) and block private or loopback IPs.
*   **Redirection:** For the read path, use HTTP 302 Temporary Redirection.
*   **Telemetry:** Capture daily active users (DAU) and total clicks for every redirection. Ensure the telemetry processing does not bottleneck the redirection latency.

## 3. Execution Workflow: Contract-Driven (Test-First) Development
Before writing any feature code, you must first write the unit and integration tests to establish the architectural contracts.

1.  **Generate Tests First:** Use `pytest` and `pytest-asyncio`. 
    *   Write tests to prove the ID generator creates unique strings and scales.
    *   Write tests to prove the SSRF shield blocks local/private IPs.
    *   Write tests to prove cache hits bypass the database.
    *   Write tests to validate the telemetry aggregation logic.
2.  **Implement Features:** Only after generating the tests should you write the implementation code (`app.py`, `nginx.conf`, etc.) to make the tests pass.
3.  **Orchestration:** Finalize the setup by writing a `docker-compose.yml` that enforces the private network isolation.