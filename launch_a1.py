"""Single-shot A1.Flex launch attempt for GitHub Actions cron.

Reads OCI auth from env vars (provided by Actions secrets), tries to launch a
VM.Standard.A1.Flex instance in each AD until one succeeds or all return
OutOfCapacity. Exits 0 in all expected outcomes so the cron keeps firing;
exits non-zero only on unexpected errors.

On a successful launch, fires an urgent ntfy alert. The workflow itself does
not auto-disable - you stop it manually by going to the repo's Actions tab
and disabling the workflow.
"""
from __future__ import annotations

import os
import sys
import urllib.request

import oci


# --- Auth from env (GitHub Actions secrets) ---
TENANCY     = os.environ["OCI_TENANCY_OCID"]
USER        = os.environ["OCI_USER_OCID"]
FINGERPRINT = os.environ["OCI_FINGERPRINT"]
REGION      = os.environ.get("OCI_REGION", "uk-london-1")
PRIVATE_KEY = os.environ["OCI_PRIVATE_KEY"]

# --- Launch parameters (edit constants below to retarget; or pass via env) ---
# Hard-coded for now. If you want different per-workflow defaults, override
# OCPUS / MEMORY in the workflow YAML.
SUBNET   = "ocid1.subnet.oc1.uk-london-1.aaaaaaaaidtmd26w7aauxlkx4hfq7qnigrzm5symjb5y34ne7wfhvp3s55ca"
IMAGE    = "ocid1.image.oc1.uk-london-1.aaaaaaaavrfdkd4ymh4nikhd3tyslkigrx2rg2dyzj4xsqizi47gba2s7dwq"
SSH_KEY  = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJv7rITJa5AK40q+CfAXeEWTAydpus/UOp+ym+nhH8y5 paul@oci"
ADS      = ["HXzK:UK-LONDON-1-AD-1", "HXzK:UK-LONDON-1-AD-2"]

OCPUS    = int(os.environ.get("OCPUS", "2"))
MEMORY   = int(os.environ.get("MEMORY", "12"))
NTFY_URL = os.environ.get("NTFY_URL", "").strip()


def ntfy(title: str, msg: str, priority: str = "default", tags: str = "") -> None:
    if not NTFY_URL:
        return
    try:
        req = urllib.request.Request(
            NTFY_URL, data=msg.encode(),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"ntfy failed: {e}", file=sys.stderr)


def main() -> int:
    # OCI SDK wants the private key on disk. Write it to a runner-local temp.
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

    for ad in ADS:
        print(f"trying {ad} for {OCPUS} OCPU / {MEMORY} GB")
        details = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad,
            compartment_id=TENANCY,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=OCPUS, memory_in_gbs=MEMORY,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image", image_id=IMAGE,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=SUBNET, assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_KEY},
            display_name=f"codex-vm-{OCPUS}o-{MEMORY}g",
        )
        try:
            r = client.launch_instance(
                details, retry_strategy=oci.retry.NoneRetryStrategy(),
            )
            instance = r.data
            print(f"SUCCESS! id={instance.id} state={instance.lifecycle_state}")
            ntfy(
                f"OCI VM Created! ({OCPUS}/{MEMORY})",
                f"AD: {ad}\nOCID: {instance.id}\n\n"
                "DISABLE THE WORKFLOW NOW to stop further attempts.",
                "urgent", "tada,white_check_mark",
            )
            return 0
        except oci.exceptions.ServiceError as e:
            text = str(e)
            if "Out of host capacity" in text:
                print(f"  {ad}: OutOfCapacity (expected)")
                continue
            if e.status == 429 or "TooManyRequests" in text:
                print(f"  {ad}: throttled")
                continue
            if "LimitExceeded" in text:
                print(f"  {ad}: LimitExceeded - you may already have an A1 instance")
                ntfy("OCI LimitExceeded", text[:200], "high", "warning")
                return 0
            if "NotAuthorizedOrNotFound" in text and "Authorization failed" in text:
                print(f"  {ad}: NotAuthorizedOrNotFound (capacity-equivalent)")
                continue
            print(f"  {ad}: unexpected service error: {e}", file=sys.stderr)
            ntfy("OCI Unexpected Error", text[:300], "high", "warning")
            return 1
        except Exception as e:
            print(f"  {ad}: unexpected exception: {e}", file=sys.stderr)
            ntfy("OCI Hunt Exception", str(e)[:300], "high", "warning")
            return 1

    print("all ADs tried, no capacity - exiting cleanly so cron keeps trying")
    return 0


if __name__ == "__main__":
    sys.exit(main())
