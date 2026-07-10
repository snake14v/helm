# test_security.py — the loopback guard is the trust story; untested, it's prose (report item 2).
# Proves: a foreign-origin or DNS-rebind request to a MUTATING endpoint returns 403, and the
# legitimate same-origin path still works. Run: python test_security.py  (server must be on :8799).
import json, sys, urllib.request, urllib.error

BASE = "http://127.0.0.1:8799"
MUTATING = ["/api/launch", "/api/agent/act", "/api/operator/start", "/api/job",
            "/api/models/setkey", "/api/runner", "/api/mode", "/api/mem/reclaim"]


def _post(path, headers):
    req = urllib.request.Request(BASE + path, data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"ERR {e}"


def main():
    fails = []

    # 1. A malicious web page (foreign Origin) must be blocked on every mutating endpoint.
    for p in MUTATING:
        code = _post(p, {"Origin": "https://evil.example.com", "Host": "127.0.0.1:8799"})
        if code != 403:
            fails.append(f"cross-origin POST {p} -> {code}, expected 403")

    # 2. A DNS-rebind (attacker domain in Host) must be blocked.
    code = _post("/api/agent/act", {"Host": "evil.example.com"})
    if code != 403:
        fails.append(f"rebind Host POST -> {code}, expected 403")

    # 3. The legitimate same-origin UI path must STILL WORK (guard breaks nothing).
    code = _post("/api/agent/act", {"Origin": "http://localhost:8799", "Host": "localhost:8799"})
    if code not in (200, 400):   # 400 = handler ran and rejected the empty body; NOT a guard block
        fails.append(f"same-origin POST -> {code}, expected 200/400 (guard must let it through)")

    # 4. A local tool with no Origin (guardian, curl, the runner) must work.
    code = _post("/api/agent/act", {"Host": "127.0.0.1:8799"})
    if code not in (200, 400):
        fails.append(f"no-origin loopback POST -> {code}, expected 200/400")

    if fails:
        print("SECURITY TEST FAILED:")
        for f in fails:
            print("  ✗", f)
        sys.exit(1)
    print(f"SECURITY TEST PASSED — {len(MUTATING)} mutating endpoints reject cross-origin (403); "
          "same-origin + loopback tools still work.")


if __name__ == "__main__":
    main()
