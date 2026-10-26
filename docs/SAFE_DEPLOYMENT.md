# Coexistence with a live 3x-ui installation

Farstar uses the panel's authenticated API; it does not edit the panel database, Xray configuration, existing inbound definitions or existing customer identities during setup. A production installation should first record the panel/Xray PIDs, installed version and counts/fingerprints of client and inbound configuration. Back up the actual panel database, including PostgreSQL when configured; an unused SQLite file is not a backup of a PostgreSQL installation.

Use an independent API token name. In 3x-ui 3.11, `setting -getApiToken` rotates the token with the selected name, so never invoke it against an existing integration's name. The database environment must match the running panel. No panel restart is needed to create a separately named API token.

The isolated layout used by `docs/farstar-robot.service` is:

| Path | Purpose |
|---|---|
| `/opt/farstar-robot/current` | Current release, root-owned code |
| `/opt/farstar-robot/releases/` | Versioned releases for rollback |
| `/opt/farstar-robot/venv` | Application-only Python environment |
| `/etc/farstar-robot/bot.env` | Root-only runtime credentials loaded by systemd |
| `/var/lib/farstar-robot` | Application database/backups, owned by farstarbot |

Run only one instance/polling consumer. Reserve a free loopback port, e.g. `HOST=127.0.0.1`, `PORT=18080`, and do not change panel, subscription or Xray ports. The unit sets CPU/memory limits and a lower CPU scheduling priority. No firewall or existing proxy changes are needed for a private dashboard.

For a server whose SSH port is 2022, access the dashboard through an SSH tunnel:

```sh
ssh -p 2022 -L 18080:127.0.0.1:18080 your-user@your-server
```

Then open `http://localhost:18080`. Use the separately generated dashboard credentials. A public deployment should use its own HTTPS endpoint and secure cookies.

For card-to-card operations, configure `CURRENCY=IRT` and `MANUAL_PAYMENTS=true` before the first transaction, then enter the card number/account holder in the authenticated dashboard's settings. Create and price plans yourself. Avoid sample paid plans or automatically issuing trial accounts on a customer server before the owner configures them.

Verification should use read-only panel API calls and a separate Farstar test database, not create/delete existing panel clients as a smoke test. Check panel/Xray PIDs, API health, client/inbound configuration fingerprints and service listeners again afterward. Panel traffic counters may naturally change while users remain online; compare stable configuration fields rather than byte-for-byte database dumps.

To roll back only the application, stop/disable `farstar-robot.service` and keep its data and release directories. This leaves 3x-ui and Xray untouched. Do not restore the whole panel database merely to remove the bot: doing so could roll back legitimate customer activity after the backup.
