import sqlite3, json, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

conn = sqlite3.connect("pipeline.db")
rows = conn.execute(
    "SELECT run_id, payload FROM pipeline_state WHERE step='script_writer' AND status='success' ORDER BY id DESC LIMIT 1"
).fetchall()
for run_id, payload in rows:
    print(f"run_id: {run_id}")
    data = json.loads(payload) if payload else {}
    print("=== HOOK ===")
    print(data.get("hook", ""))
    print()
    print("=== BODY ===")
    print(data.get("body", ""))
    print()
    print("=== CTA ===")
    print(data.get("cta", ""))
    print()
    print("=== FULL SCRIPT ===")
    print(data.get("full_script", ""))
    words = len(data.get("full_script", "").split())
    print(f"\nСлов: {words}")
conn.close()
