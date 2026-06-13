# OCI A1.Flex Capacity Hunt via GitHub Actions

Hunts for `VM.Standard.A1.Flex` Always Free capacity in OCI uk-london-1 by
attempting a launch every 30 minutes via GitHub Actions cron. Sends a ntfy
alert on success.

## Required GitHub Secrets

Settings -> Secrets and variables -> Actions -> New repository secret:

- `OCI_TENANCY_OCID` - your tenancy OCID
- `OCI_USER_OCID` - the user OCID that owns the API key
- `OCI_FINGERPRINT` - API key fingerprint shown in OCI Console
- `OCI_PRIVATE_KEY` - full contents of the .pem file (including BEGIN/END lines)
- `NTFY_URL` - e.g. `https://ntfy.sh/ociinstance-4269-github`

## Customize before pushing

Edit `launch_a1.py` constants at the top to match your tenancy:

- `SUBNET` - your VCN subnet OCID in uk-london-1
- `IMAGE`  - ARM Oracle Linux 8 image OCID for uk-london-1
- `SSH_KEY` - your public SSH key

To target a different size, set `OCPUS` / `MEMORY` env vars in
`.github/workflows/hunt.yml` (defaults: 2 OCPU / 12 GB - the Always Free max).

## On success

You will get a phone push notification via ntfy. The new instance OCID is
in the alert message. **Disable the workflow immediately** to stop further
attempts:

- Actions tab -> A1 Capacity Hunt -> ... menu -> Disable workflow

If you don't disable, the next scheduled run will try to launch another
A1, which will fail with `LimitExceeded` (you've used your Always Free
budget). Harmless but noisy.

## Schedule frequency vs Actions minutes

GitHub free tier allows 2000 Actions minutes/month on private repos. Each
run is ~30s but billed at the per-minute minimum (1 min/run).

- `*/30 * * * *` -> ~48 runs/day -> ~1440 min/month
- `*/15 * * * *` -> ~96 runs/day -> ~2880 min/month (over free tier)
- `*/10 * * * *` -> ~144 runs/day -> ~4320 min/month
- `*/5 * * * *`  -> ~288 runs/day -> ~8640 min/month

Default is `*/30` to stay safely inside free tier. Bump up only if you have
paid Actions minutes or a public repo (unlimited).
 
