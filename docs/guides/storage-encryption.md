# Storage encryption

Pincer can encrypt everything it stores in cloud object storage with **one
AES-256 key that you hold and the provider never keeps**. The provider does the
encryption, with the key Pincer sends on every request:

| Provider | Mechanism |
|---|---|
| Amazon S3 (and S3-compatible servers) | SSE-C (server-side encryption with customer-provided keys) |
| Azure Blob Storage, including hierarchical-namespace (ADLS Gen2) accounts | CPK (customer-provided keys) |
| Google Cloud Storage | CSEK (customer-supplied encryption keys) |

Without the key, nobody can read the data: not someone with bucket access, not
the provider's support staff, and not Pincer.

It covers every cloud location Pincer uses: the storage root
(`PINCER_STORAGE_ROOT`) with everything under it, `workspace/` and `skills/`
included, and a cloud `PINCER_SKILLS_DIR` outside it. Local storage roots are
not affected.

## Turning it on

1. Make sure every storage endpoint is TLS (see the warning below).
2. Generate a key and keep a copy somewhere safe, outside the server:

    ```bash
    openssl rand -base64 32
    ```

3. Set it and restart Pincer:

    ```bash
    PINCER_STORAGE_ENCRYPTION_KEY=<the base64 key>
    ```

4. Check it with `pincer doctor` (the `storage_encryption` check) or
   `pincer storage encryption-status`.

Encryption is enabled, and becomes permanent, on the first start of
`pincer run` / `pincer serve` (or the first `pincer storage encrypt`) with the
key set. `pincer doctor`, `pincer storage encryption-status` and
`pincer storage encrypt --dry-run` only check: they never enable it, so
trying a key with them is safe. Until then they report "enabled on the next
start".

From then on, every object Pincer writes is encrypted. Objects stored before
are encrypted by a background job that `pincer run` starts on its own (see
[Existing data](#existing-data)).

!!! danger "Losing the key means losing the data"
    The provider does not keep the key and cannot recover it. Back the key up
    the way you back up the database: a password manager, a secrets manager,
    or an offline copy. Treat it like the JWT secret: anyone who has it, and
    read access to the bucket, can read everything.

## It cannot be turned off

Once encryption has been enabled on a location, Pincer refuses that location:

- if `PINCER_STORAGE_ENCRYPTION_KEY` is unset ("encryption was enabled … and
  cannot be turned off"), and
- if the key is a different one ("the key cannot be changed").

The lock lives in the bucket itself, as two objects at the root of each
location, both written with the key. No database is involved.

- `.pincer/encryption.json`, written when encryption is enabled. It holds the
  key's fingerprint (an HMAC, never the key) and the progress of encrypting
  existing data.
- `.encrypted`, written once **every** object has been encrypted. It holds
  only the HMAC. Pincer reads it back with the key and compares, which proves
  the key is right. Without the key the provider refuses to read it, which
  proves the location is encrypted.

Either one is enough to keep encryption on. If one is deleted, the other
proves the key and Pincer writes the missing one again; `.encrypted` comes
back only if the marker says every object is encrypted. Only deleting both
removes the lock, so don't. A file named `.encrypted` at the root that isn't
Pincer's stops Pincer from using encryption there until it's moved away.

Neither object is visible through Pincer: the storage browser doesn't list
`.pincer/` or `.encrypted`, and every storage endpoint (read, download, links,
save, upload, move, delete, new folder) answers 404 for them, whether they
exist or not.

Only an answer about the key itself counts as a wrong or missing key.
Throttling, timeouts, server errors and other 400s (an expired token, a
malformed authorization header, a bad bucket name) are reported as
unreachable storage, not as a refusal; a location Pincer couldn't check gets
the full check on its next use. A refused location stays refused until Pincer
restarts, whether it was refused at startup or on a later use. A malformed key
is reported without its value, and `pincer doctor` reports it as a failed
`storage_encryption` check instead of stopping.

A refused location is not used at all: the storage browser answers 503 with
the reason, and skills and the workspace fail with the same message. The rest
of Pincer keeps running.

Rotating the key is not supported yet.

## Encrypted data can only be read over TLS

!!! warning "Encrypted data can only be read over TLS"
    S3 SSE-C, Azure CPK and GCS CSEK send the key with every request, so
    providers and S3-compatible servers (MinIO, for example) refuse those
    requests over plain `http://`. Once storage is encrypted, it **cannot be
    decrypted or read over a non-TLS connection**: not by Pincer, not by any
    other tool, even with the right key.

    So Pincer refuses an `http://` `PINCER_STORAGE_S3_ENDPOINT_URL`, an
    `http://` `PINCER_STORAGE_AZURE_ACCOUNT_URL`, and an Azure connection
    string with `DefaultEndpointsProtocol=http`, an `http://` `BlobEndpoint` or
    `UseDevelopmentStorage=true` whenever a key is set.

    Moving an encrypted bucket behind a non-TLS endpoint, or ending TLS at a
    proxy that forwards plain HTTP to the store, makes the data unreadable
    until TLS is restored. Set up TLS on the endpoint **before** enabling
    encryption. A local Azurite or MinIO needs a TLS certificate too.

## Existing data

Objects stored before the key was set stay readable: when a keyed read hits an
object stored without a key, Pincer reads it once more without the key. To
encrypt those objects:

- `pincer run` (and `pincer serve`) start a background job while they start.
  A lease object, `.pincer/encrypt.lock`, keeps two replicas from running it
  at the same time. The job renews it as it goes, stops if another process
  took it over, and only its owner removes it.
- `pincer storage encrypt` runs the same job in the foreground, with progress;
  `--dry-run` only counts what is left.

The job rewrites each plain object in place, inside the provider where it can,
and keeps its content type and metadata:

| Provider | How an object is rewritten |
|---|---|
| S3 | Copy onto itself with the key (multipart above 8 MB, so objects over 5 GB work), on condition of its ETag |
| GCS | Rewrite onto itself with the key, on condition of its generation |
| Azure | Download and upload over itself with the key, on condition of its ETag |

When an object changed during the rewrite, the job checks it again. If it is
now encrypted (Pincer wrote it meanwhile, with the key), it is left as it is.
If it is still plain (another service in a shared bucket wrote it), it is
tried once more, and counted as failed if it changes again. An object deleted
while the job runs is skipped.

Progress is saved in the marker every 100 objects. On S3 and GCS a restart
resumes after the last key, but never after an object that failed, so a
failed object is tried again. On Azure it lists again and skips what is
already encrypted. When every object is done the state is `complete`, and the
`.encrypted` check file appears. A run with failed objects leaves the state
`pending` and writes no check file. Running the job again is always safe: it
also picks up plain objects that another service dropped into the bucket.

`pincer storage encrypt` exits with status 1 unless every location ends
`complete`: a failed object, or a run stopped because another process took
the lease, is not done. `--dry-run` exits with status 1 when some objects
could not be checked.

### Versioned buckets

The job rewrites each object **in place**. On a bucket that keeps old
versions, the plain version from before stays behind, readable by anyone
with read access to the bucket, until it is deleted:

| Provider | What keeps the plain copy |
|---|---|
| S3 | Bucket versioning (enabled or suspended): the previous version |
| GCS | Object versioning: the noncurrent generation; soft delete: the overwritten generation, for its retention |
| Azure | Blob versioning: the previous version; blob soft delete: a soft-deleted snapshot, for its retention |

The job detects these settings and records them in the marker.
`pincer storage encrypt`, `pincer storage encryption-status` and
`pincer doctor` warn about them, even once the state is `complete`. The status
command and the doctor check the bucket again each time they run:

- Soft delete removes the plain copies on its own: the warning ends once its
  retention has passed since the job last rewrote an object. A run that finds
  everything encrypted already rewrites nothing and does not restart that clock.
- Versioning keeps them until they are deleted, and Pincer cannot tell which
  old versions predate encryption, so the warning stays while versioning is on
  (enabled or, on S3, suspended). Once you have deleted them in a location,
  run `pincer storage old-versions-deleted <location>` (for example
  `s3://docs`; it asks for confirmation, or pass `--yes`). It records that in
  that location's marker only, and its warning (and the doctor check) clears.
  It is refused while the job is not complete or runs in another process. The
  warning comes back if a later run rewrites another object there, because
  that leaves a new plain version.

Running `pincer storage encrypt` again also refreshes what the marker
records.

Checking needs `s3:GetBucketVersioning` on S3, `storage.buckets.get` on GCS,
and the account's service properties on Azure. Without them, Pincer logs a
warning, keeps the last answer it recorded, or reports "unknown".

After the job is complete, delete the old versions with the provider's tools,
for example:

- S3: delete the noncurrent versions (`aws s3api list-object-versions`, then
  `delete-object --version-id`), or add a lifecycle rule that expires
  noncurrent versions.
- GCS: delete the noncurrent generations (`gcloud storage ls --all-versions`,
  then `gcloud storage rm gs://bucket/object#<generation>` for each old one),
  or add a lifecycle rule on `isLive: false`; soft-deleted objects expire with
  their retention.
- Azure: delete the previous versions (`az storage blob delete --version-id`)
  or set a lifecycle rule on versions; soft-deleted snapshots expire with their
  retention.

!!! note "A shared bucket"
    Encryption covers the whole storage root, including folders that belong
    to other services sharing the bucket. After the job has run, those
    services need the key to read their objects.

## Azure hierarchical-namespace accounts

On an account with hierarchical namespace (ADLS Gen2), cloudpathlib uses the
Data Lake API for file properties, rename, mkdir, listing and folder delete.
File properties carry the key. Renames, mkdir, listing and folder delete only
change paths, not content, so they need none. Content always goes through the
Blob API, which works on these accounts too.

Every Data Lake request Pincer makes is wrapped:

- "not found" stays "not found",
- an error about the customer-provided key becomes a storage encryption error
  naming the operation, the path and the service error code (never the key),
- anything else becomes "storage unavailable" with the same details.

When Pincer starts, it probes such an account with the key, on a temporary
file under `.pincer/`. It writes the file, reads its properties through Data
Lake, reads it back, renames it and deletes it. If any step fails, Pincer
refuses the location and names the step. `pincer doctor` runs the same probe
and reports each step.

## What the storage browser shows

The storage browser always shows plain content: Pincer decrypts on the server.
A provider's presigned link cannot carry the key, so download and preview
links point at Pincer's own signed route, `/api/storage/signed/{token}`, which
streams the file decrypted, with the API's `nosniff` and sandbox headers.
`GET /api/storage` reports `"encrypted": true`.
