import json, subprocess
raw = subprocess.run(
    ["docker", "exec", "saas-cost-dashboard", "cat", "/app/state/noc_state.json"],
    capture_output=True, text=True).stdout
s = json.loads(raw)
print("locks:", json.dumps(s.get("locks")))
print("lock_meta:", json.dumps(s.get("lock_meta")))
print("quarantined:", list(s.get("quarantined", {}).keys()))
print("recent incidents:")
for i in s.get("incidents", [])[-12:]:
    print(" ", i["ts"][:16], "|", i["agent"], "|", i["event"], "|", i.get("outcome", ""))
