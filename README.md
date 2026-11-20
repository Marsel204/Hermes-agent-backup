# Hermes agent backup

Encrypted snapshots of the Hermes installation and personal setup on the owner's server. This repository is public; private content is stored only in encrypted snapshot parts. The recovery key and GitHub token must NEVER be committed here.

## Automatic backups

A user systemd timer runs `backup.py` every minute and after boot. Source and personal setup are tracked separately to avoid re-uploading the full source for small configuration changes. Changed snapshots are encrypted using GnuPG AES-256 with integrity protection, committed and pushed to `main`. Git history retains older snapshots. Unchanged snapshots do not create commits. Failed pushes are retried next run. Rapid changes between checks are captured as the latest state; this is not an event-by-event audit log.

Snapshots include the installed Hermes source (including local edits), source version, sanitized config, SOUL, memories, skills, plugins, hooks, scripts, cron jobs, consistent SQLite database copies, operator notes, and Hermes systemd units with environment values removed.

Excluded: API/login credentials (`.env`, OAuth tokens, auth files, WhatsApp sessions and pairing keys/QRs), service environment overrides, logs, caches, media, dependency environments, build scratch directories and tool binaries. Config keys containing token/secret/password/API key/credential are replaced with `REENTER_AFTER_RESTORE`. Private conversation/task state can be present in the encrypted databases. Reinstall dependencies and reconnect services after restoration.

## Recovery

Download the recovery key from the original machine (`~/.config/hermes-backup/recovery-key.txt`) and store a copy somewhere independent of the server. Losing it makes the encrypted backup unrecoverable. Never upload it to this repository.

On a trusted machine with Git, GnuPG, tar and Python:

```sh
git clone https://github.com/Marsel204/Hermes-agent-backup.git
cd Hermes-agent-backup
# Optionally git checkout an older backup commit first.
mkdir -m 700 restored
for group in source setup; do
  cat "$group".tar.gpg.part-* > /tmp/hermes-snapshot.tar.gpg
  # Enter the contents of recovery-key.txt when GnuPG asks.
  gpg --output /tmp/hermes-snapshot.tar --decrypt /tmp/hermes-snapshot.tar.gpg
  tar -xf /tmp/hermes-snapshot.tar -C restored
  rm /tmp/hermes-snapshot.tar /tmp/hermes-snapshot.tar.gpg
done
```

Inspect `restored/source-version.json` and the extracted files before installing. Stop Hermes before restoring `hermes-home` into `~/.hermes`; restore the source separately, reinstall its dependencies, re-enter `.env` values using `.env.example`, and reconnect OAuth/WhatsApp. Do not overwrite running SQLite databases or reuse stale `-wal`/`-shm` files. Review scheduled jobs before re-enabling them to avoid duplicate deliveries. Unit files must be reviewed for installation paths and supplied with service environment values before enabling.

## Server operation

Requires `python3`, PyYAML, Git and GnuPG. The checkout is `~/hermes-backup`.

```sh
systemctl --user status hermes-backup.timer
journalctl --user -u hermes-backup.service -n 30
systemctl --user start hermes-backup.service
systemctl --user disable --now hermes-backup.timer  # pause backups
```

The timer uses user lingering so it runs after logout and reboot. The GitHub credential is local in `~/.config/hermes-backup/github-token`, mode 600. Renew it when it expires. No force pushes are used: remote branch conflicts fail visibly for manual resolution. The local last successful push is recorded in `~/.local/state/hermes-backup/last-success`.
