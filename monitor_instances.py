"""Periodic OCI instance monitor.

Runs as a single-shot script per GitHub Actions cron invocation. Lists all
RUNNING instances in the tenancy root compartment and compares against the
EXPECTED set (your two keepers). Sends urgent ntfy alerts for:

  - Any RUNNING instance not in EXPECTED  (a rogue/extra launch)
  - Any EXPECTED instance that is NOT RUNNING (a keeper was terminated)

Also writes a JSON snapshot of all RUNNING instances to snapshot.json for
upload as a GitHub Actions artifact (your continuous backup record).

Stays silent (no ntfy) when state matches expectations exactly.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

import oci


def clean(s: str) -> str:
    return re.sub(r"\s", "", s)


# --- Auth ---
TENANCY     = clean(os.environ["OCI_TENANCY_OCID"])
USER        = clean(os.environ["OCI_USER_OCID"])
FINGERPRINT = clean(os.environ["OCI_FINGERPRINT"])
REGION      = clean(os.environ.get("OCI_REGION", "uk-london-1"))
PRIVATE_KEY = os.environ["OCI_PRIVATE_KEY"]
NTFY_URL    = os.environ.get("NTFY_URL", "").strip()

# --- Expected ongoing instances. Edit this dict if you intentionally add or
# remove keepers in the future. The OCIDs below are your post-hunt steady
# state (codex-vm-1ocpu and codex-vm-2ocpu). ---
EXPECTED = {
    # codex-vm-1ocpu was terminated; only the 2/12 remains as a keeper.
    # If/when you resize the 2/12 the OCID stays the same, so no update needed.
    "ocid1.instance.oc1.uk-london-1.anwgiljrqr3zcfacnwqqg266kql5adpmrjbrpdaqnwfnvwz6r4x2ydiztfsq": "codex-vm-2ocpu",
}


def ntfy(title: str, msg: str, priority: str = "default", tags: str = "") -> None:
    if not NTFY_URL:
        print(f"(no NTFY_URL set; skipping push: {title})")
        return
    last_exc = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                NTFY_URL, data=msg.encode(),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            return
        except Exception as e:
            last_exc = e
    print(f"ntfy FAILED after 3 attempts: {last_exc}", file=sys.stderr)


def main() -> int:
    key_path = "/tmp/oci_api_key.pem"
    with open(key_path, "w") as f:
        f.write(PRIVATE_KEY.strip() + "\n")
    os.chmod(key_path, 0o600)

    config = {
        "user": USER,
        "fingerprint": FINGERPRINT,
        "tenancy": TENANCY,
        "region": REGION,
        "key_file": key_path,
    }

    client = oci.core.ComputeClient(config)
    client.base_client.timeout = (30, 30)
    insts = client.list_instances(compartment_id=TENANCY).data
    running = [i for i in insts if i.lifecycle_state == "RUNNING"]
    running_by_id = {i.id: i for i in running}

    print(f"=== OCI Instance Monitor @ {datetime.now(timezone.utc).isoformat()} ===")
    print(f"RUNNING instances: {len(running)}")
    for i in running:
        marker = "EXPECTED" if i.id in EXPECTED else "*** UNEXPECTED ***"
        print(f"  [{marker}] {i.display_name} | {i.shape} | "
              f"{i.shape_config.ocpus}o/{i.shape_config.memory_in_gbs}gb | "
              f"AD={i.availability_domain.split('-')[-1]} | {i.id[-12:]}")

    unexpected = [i for i in running if i.id not in EXPECTED]
    missing = [(oc, name) for oc, name in EXPECTED.items() if oc not in running_by_id]

    # Write snapshot.json - uploaded as artifact by the workflow.
    snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "running_count": len(running),
        "expected_count": len(EXPECTED),
        "unexpected_count": len(unexpected),
        "missing_count": len(missing),
        "running": [
            {
                "id": i.id,
                "display_name": i.display_name,
                "shape": i.shape,
                "ocpus": float(i.shape_config.ocpus),
                "memory_gb": float(i.shape_config.memory_in_gbs),
                "ad": i.availability_domain,
                "is_expected": i.id in EXPECTED,
                "time_created": i.time_created.isoformat() if i.time_created else None,
            }
            for i in running
        ],
        "missing_expected": [{"id": oc, "display_name": name} for oc, name in missing],
    }
    with open("snapshot.json", "w") as f:
        json.dump(snapshot, f, indent=2, default=str)
    print(f"\nSnapshot written to snapshot.json")

    if unexpected:
        msg_lines = [f"{len(unexpected)} unexpected RUNNING instance(s) detected:"]
        for i in unexpected:
            msg_lines.append(
                f"- {i.display_name} | {i.shape_config.ocpus}o/{i.shape_config.memory_in_gbs}gb | "
                f"AD={i.availability_domain.split('-')[-1]} | OCID tail: {i.id[-12:]}"
            )
        msg_lines.append("\nCheck OCI Console immediately - a hunter may have re-fired.")
        ntfy(
            "OCI EXTRA Instance Detected",
            "\n".join(msg_lines),
            "urgent", "warning,rotating_light",
        )

    if missing:
        msg_lines = [f"{len(missing)} expected instance(s) NOT RUNNING:"]
        for oc, name in missing:
            msg_lines.append(f"- {name} ({oc[-12:]})")
        msg_lines.append("\nOne of your keepers has been terminated. Check OCI Console.")
        ntfy(
            "OCI Expected Instance MISSING",
            "\n".join(msg_lines),
            "urgent", "warning,x",
        )

    if not unexpected and not missing:
        print("\nAll clear - only expected instances running.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
