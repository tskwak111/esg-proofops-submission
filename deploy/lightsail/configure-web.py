"""Prepare a local Vercel rewrite. Does not call Vercel, push, or deploy."""

import argparse
import ipaddress
import json
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--checkout", required=True, type=Path)
p.add_argument("--static-ip", required=True)
p.add_argument("--apply", action="store_true")
a = p.parse_args()
ip = ipaddress.ip_address(a.static_ip)
if ip.version != 4 or not ip.is_global:
    raise SystemExit("A public static IPv4 is required")
path = a.checkout / "vercel.json"
config = json.loads(path.read_text())
matches = [r for r in config["rewrites"] if r["source"] == "/hosted-api/:path*"]
if len(matches) != 1:
    raise SystemExit("Expected exactly one existing hosted-api rewrite; inspect config")
matches[0]["destination"] = f"https://{ip}.sslip.io/:path*"
if a.apply:
    backup = path.with_name("vercel.json.p3-backup")
    if backup.exists():
        raise SystemExit("Backup already exists; inspect before another patch")
    backup.write_bytes(path.read_bytes())
    path.write_text(json.dumps(config, indent=2) + "\n")
    print(
        "Updated local rewrite; original saved in vercel.json.p3-backup. "
        "Review diff before deployment."
    )
else:
    print(json.dumps(matches[0], indent=2))
print(
    "Set Vercel production build variable VITE_ANALYSIS_BACKEND=hosted and redeploy; "
    "APP_ORIGIN must be the exact browser production domain."
)
