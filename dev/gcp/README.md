# Local Google Cloud service account (development only)

`service-account-dev.json` is a service account key for the **local Google Cloud stand-in only**
(`fakes`, at `/gcp`). It reads the `acme-prod` project's fixture logs, errors and traces. The
stand-in verifies the signed token requests with `sa-dev-public-key.pem` (passed to it as
`GCP_SA_PUBLIC_KEY`), as Google does with the keys it holds.

The key protects nothing outside the stand-in. Never use it, or this pattern of committing a key,
anywhere else: real service account keys are uploaded by an admin in the console and kept by the
vault (decision log, phase 11).
