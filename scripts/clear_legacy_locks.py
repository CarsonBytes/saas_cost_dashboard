import json, subprocess, time

CLEAR = (
    "import noc, datetime as dt"
    "; names=list(noc._load_state().get('locks',{}))"
    "; [noc.clear_lock(n) for n in names]"
    "; print('cleared', names)"
)

def locks():
    raw = subprocess.run(
        ["docker", "exec", "saas-cost-dashboard", "cat", "/app/state/noc_state.json"],
        capture_output=True, text=True).stdout
    return list(json.loads(raw).get("locks", {}))

for attempt in range(6):
    remaining = locks()
    if not remaining:
        print(f"attempt {attempt}: no locks -- converged")
        break
    print(f"attempt {attempt}: clearing {remaining}")
    out = subprocess.run(["docker", "exec", "saas-cost-dashboard",
                          "python", "-c", CLEAR],
                         capture_output=True, text=True)
    print("  ", out.stdout.strip() or out.stderr.strip()[-200:])
    time.sleep(10)

# decisive check: survive one full 120s health cycle without resurrection
time.sleep(130)
final = locks()
print("after one full cycle:", final if final else "LOCKS CLEARED, STABLE")
