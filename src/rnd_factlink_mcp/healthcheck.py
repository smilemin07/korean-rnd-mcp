"""Container health probe — `python -m rnd_factlink_mcp.healthcheck`.

Used by the Dockerfile HEALTHCHECK. Kept as a module instead of an inline
`python -c` one-liner so it can be read, reviewed and tested like the rest of
the code, and so Docker's exec-form quoting can never mangle it.

Standard library only: the runtime image is python:3.12-slim and has no
curl/wget. http.client rather than urllib.request, because urllib honours proxy
environment variables and follows redirects — either could turn "some other
server answered 200" into a healthy verdict. The address is the literal
127.0.0.1: `localhost` can resolve to ::1 first, where nothing is listening.

Exit 0 only on HTTP 200 from /readyz, i.e. the local retrieval path is warm.
A degraded or failed warm-up answers 503 and is reported unhealthy — that is an
operator signal, not a restart trigger: compose's restart policy reacts to
process exit, not to health status.
"""

import http.client
import os
import sys


def main() -> int:
    port = int(os.environ.get("PORT", "8081"))
    con = None
    try:
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        con.request("GET", "/readyz")
        status = con.getresponse().status
    except Exception:  # noqa: BLE001 — any failure to reach /readyz is unhealthy
        return 1
    finally:
        if con is not None:
            con.close()
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
