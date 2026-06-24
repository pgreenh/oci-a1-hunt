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
import re
import sys
import urllib.request

import oci


def clean(s: str) -> str:
    """Strip ALL whitespace (including embedded). OCI OCIDs and fingerprints
    contain no legitimate whitespace, so this is safe and defends against
    invisible characters introduced by paste mishaps in the GitHub Secrets UI."""
    return re.sub(r"\s", "", s)


# --- Auth from env (GitHub Actions secrets) ---
# clean() strips ALL whitespace (including embedded). OCI OCIDs and fingerprints
# never contain legitimate whitespace, so this is safe and defends against
# trailing newlines or hidden characters from the GitHub Secrets paste UI.
TENANCY     = clean(os.environ["OCI_TENANCY_OCID"])
USER        = clean(os.environ["OCI_USER_OCID"])
FINGERPRINT = clean(os.environ["OCI_FINGERPRINT"])
REGION      = clean(os.environ.get("OCI_REGION", "uk-london-1"))
PRIVATE_KEY = os.environ["OCI_PRIVATE_KEY"]

# Diagnostic - log lengths so we can spot a secret that's the wrong size
# without leaking the actual values.
print(f"diag: TENANCY len={len(TENANCY)}, USER len={len(USER)}, "
      f"FINGERPRINT len={len(FINGERPRINT)}, REGION='{REGION}'")
# Sanity-check: an OCI fingerprint is exactly 47 chars (16 hex pairs + 15 colons).
# A user/tenancy OCID is typically 90-110+ chars.
if len(FINGERPRINT) != 47:
    print(f"WARNING: FINGERPRINT is {len(FINGERPRINT)} chars, expected 47. "
          "Re-paste the secret without trailing whitespace.", file=sys.stderr)

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


def find_latest_arm_image(client, compartment_id):
    """Look up the most recent available ARM Oracle Linux 8 image for
    VM.Standard.A1.Flex. More resilient than the hardcoded IMAGE constant -
    if Oracle retires the specific image OCID we have hardcoded, this finds
    its successor automatically. Falls back to the hardcoded value on any
    lookup failure so transient list_images() errors don't break the hunt."""
    try:
        resp = client.list_images(
            compartment_id=compartment_id,
            shape="VM.Standard.A1.Flex",
            operating_system="Oracle Linux",
            operating_system_version="8",
            sort_by="TIMECREATED",
            sort_order="DESC",
            lifecycle_state="AVAILABLE",
        )
        if resp.data:
            chosen = resp.data[0]
            print(f"image lookup: using {chosen.display_name} ({chosen.id[-12:]})")
            return chosen.id
    except Exception as e:
        print(f"image lookup failed ({e}); falling back to hardcoded IMAGE", file=sys.stderr)
    return None


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

    # Change C: dynamic image lookup. Survives Oracle rotating image OCIDs.
    image_id = find_latest_arm_image(client, TENANCY) or IMAGE

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
                source_type="image", image_id=image_id,
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
            # Change A: verify lifecycle_state is one OCI uses for a fresh
            # launch (PROVISIONING / STARTING / RUNNING). Anything else is
            # suspicious - log loudly but still treat as success since the
            # API returned data without raising.
            if instance.lifecycle_state not in ("PROVISIONING", "STARTING", "RUNNING"):
                print(f"WARNING: unexpected lifecycle_state '{instance.lifecycle_state}'", file=sys.stderr)
            print(f"SUCCESS! id={instance.id} state={instance.lifecycle_state}")
            ntfy(
                f"OCI VM Created! ({OCPUS}/{MEMORY})",
                f"AD: {ad}\nOCID: {instance.id}\nState: {instance.lifecycle_state}\n\n"
                "DISABLE THE WORKFLOW NOW to stop further attempts.",
                "urgent", "tada,white_check_mark",
            )
            return 0
        except oci.exceptions.ServiceError as e:
            text = str(e)
            # Change B: broader capacity detection. OCI wording has varied
            # historically ("Out of host capacity", "Out of capacity",
            # "Capacity unavailable"). The substring "capacity" (case-
            # insensitive) catches all known variants. Also covers
            # 500 InternalError where the body still mentions capacity.
            if "capacity" in text.lower():
                print(f"  {ad}: OutOfCapacity (expected)")
                continue
            # Explicit 500 InternalError without a capacity word - rare,
            # but treat as transient. Empirically these correlate with
            # the same allocation-cycle gap and clear themselves.
            if e.status == 500 and "InternalError" in text:
                print(f"  {ad}: 500 InternalError (treating as transient): {text[:120]}")
                continue
            if e.status == 429 or "TooManyRequests" in text:
                print(f"  {ad}: throttled")
                continue
            if "LimitExceeded" in text:
                # LimitExceeded has two possible meanings:
                #   (a) Transient quota-checker glitch (no instance actually exists)
                #   (b) An A1 instance is genuinely running and consuming the budget
                # Disambiguate by querying running instances. Only ntfy if we
                # actually find an A1 instance, otherwise stay silent so cron
                # noise doesn\'t flood the phone.
                print(f"  {ad}: LimitExceeded - checking for existing A1 instance")
                try:
                    insts = client.list_instances(
                        compartment_id=TENANCY,
                        lifecycle_state="RUNNING",
                    ).data
                    a1 = [i for i in insts if i.shape == "VM.Standard.A1.Flex"]
                    if a1:
                        inst = a1[0]
                        msg = (
                            f"Discovered: {inst.display_name}\n"
                            f"OCID: {inst.id}\n"
                            f"State: {inst.lifecycle_state}\n\n"
                            "DISABLE BOTH WORKFLOWS AND THE CLOUDFLARE TRIGGER NOW."
                        )
                        print(f"  FOUND running A1 instance: {inst.id}")
                        ntfy(
                            "OCI A1 Instance Already Running",
                            msg, "urgent", "tada,white_check_mark",
                        )
                        return 0
                    print(f"  no running A1 found - treating LimitExceeded as transient (silent)")
                except Exception as e:
                    print(f"  instance list check failed ({e}) - treating LimitExceeded as transient (silent)")
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
