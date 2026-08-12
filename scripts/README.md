# ansible/scripts

Operator scripts for cluster access and disaster recovery.

| Script                    | Purpose                                                                                                                                    |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `get-kubeconfig.sh`       | Fetch the kubeconfig from a control-plane node over SSH and patch the server IP.                                                           |
| `i915_compat.py`          | Validate an i915-sriov-dkms host/guest combination (driver ↔ kernel ranges + PF/VF IOV ABI) against upstream data. Fails closed. Shared verbatim with the packer repo; tests in `tests/`. |
| `backup-secrets.sh`       | Bundle the git-ignored crown-jewel secrets, encrypt with an age passphrase, write a timestamped archive to the NAS, prune to the newest N. |
| `restore-secrets.sh`      | Decrypt and extract a secrets archive into a staging dir (never in-place).                                                                 |
| `backup-secrets.manifest` | Paths (never contents) that `backup-secrets.sh` backs up.                                                                                  |

## Secrets DR backup

### Prerequisites

- `age` — `sudo apt install age`. (`tar`/coreutils are built in.)
- A backup destination reachable as a filesystem path — typically a NAS share mounted on this workstation. Set the
  `DEFAULT_DEST` constant near the top of `backup-secrets.sh`, or pass `--dest <dir>` each run.

### Passphrase

`age` encrypts with a passphrase you type at each run. **Store this passphrase in your password manager.** It is never
written to the repo, the manifest, or the archive filename. If you lose it, the backups are unrecoverable — that is the
point.

### Back up

```bash
ansible/scripts/backup-secrets.sh            # bundle + encrypt + copy + prune
ansible/scripts/backup-secrets.sh --list     # show what would be backed up
ansible/scripts/backup-secrets.sh --dest /mnt/nas/...   # override destination
ansible/scripts/backup-secrets.sh --no-prune # keep all archives
```

Run it after changing any secret (vault password, tfvars, node key). The script only reads your local files; pruning
only ever deletes old archives in the destination, never anything local.

### Restore

```bash
ansible/scripts/restore-secrets.sh <archive>.tar.gz.age --list          # inspect
ansible/scripts/restore-secrets.sh <archive>.tar.gz.age                 # -> ./restored-secrets-<ts>/
ansible/scripts/restore-secrets.sh <archive>.tar.gz.age --into /tmp/r   # custom dir
```

Restores into a staging directory; review, then copy files into place.

### What's backed up, and why it matters

| Entry                        | Why                                                                                                                                                                                                                     |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id_rsa`                     | SSH key to the nodes. With it you can re-run `get-kubeconfig.sh` to regenerate `~/.kube/config`, so the kubeconfig itself is not backed up.                                                                             |
| `ansible/.vault_pass`        | **Crown jewel.** The Ansible Vault password that decrypts the sealed-secrets master key (pre-seeded at bootstrap from a vault-encrypted value). Without it, a rebuilt cluster cannot decrypt any `SealedSecret` in git. |
| `terraform/terraform.tfvars` | Provider + S3 state-backend credentials.                                                                                                                                                                                |

### Bare-metal recovery order

1. Restore `id_rsa` and `ansible/.vault_pass` from the latest archive.
2. Run the ansible cluster bootstrap — it seeds the sealed-secrets master key from the vault-encrypted value, so
   committed `SealedSecret`s decrypt again.
3. Let ArgoCD reconcile.
4. Run `get-kubeconfig.sh` to regenerate `~/.kube/config`.

### Verify the backups (repeatable)

The committed harness does a full backup → restore → `diff -r` round-trip using a stubbed cipher (no TTY needed):

```bash
bash ansible/scripts/tests/test-secrets-backup.sh   # expect: ALL TESTS PASSED
```

For an end-to-end check with real encryption, run a manual round-trip into a temp dir and diff against your originals
(see the script headers).
