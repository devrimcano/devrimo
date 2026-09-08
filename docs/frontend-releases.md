# Frontend deployment and infrastructure errors

The deployment lock at `/opt/devrimo/.deploy.lock` covers preparation through
health checks. Use `scripts/deploy-vps.sh` for both manual and CI releases;
never rebuild `.next` or run `npm ci` in `/opt/devrimo/frontend` while serving.

Each frontend is built at its permanent path beneath `.frontend-releases`,
with its own dependencies, environment and build. Before switching, the helper
checks page modules, client-reference manifests and the static error page,
starts an isolated loopback instance, renders `/login`, and fetches its static
assets. A redirect from `/` alone is insufficient evidence of a healthy build.
Build failure leaves the serving frontend untouched.

After migration, the frontend is stopped briefly and its symlink is replaced.
The previous directory remains available; failed activation or health checks
restore that frontend and restart it. This is frontend rollback, not database
or backend rollback. Review migration compatibility separately. Do not prune
the active or previous release, or run a different deployment path concurrently.
Database backup credentials remain a preflight requirement of full releases.
The deploy script retains the active release and the newest complete inactive
release for rollback. Older script-named release directories and incomplete
builds are removed before the next build so their private `node_modules` and
`.next` trees cannot fill the host.

## Independent error reporting

`devrimo-web-observer.service` reads the frontend's systemd journal independently
of Next.js. It forwards missing build/manifests/error-page and service-process
failures to PostHog Error Tracking using fixed descriptions. It does not forward
raw journal bodies, request content, database credentials or historical release
guesses. Original timestamps and deterministic event UUIDs preserve provenance.
Repeated categories are bounded to one report per minute. Delivery failure
restarts from the last acknowledged journal cursor.

Install the observer script at `/opt/devrimo/scripts/web_error_observer.py` and
the unit in `/etc/systemd/system`. As root, run
`/opt/devrimo/backend/.venv/bin/python scripts/install_web_observer.py`, then
`systemctl daemon-reload` and `systemctl enable --now devrimo-web-observer`.
The installer creates a root-owned mode-0600 environment containing only PostHog
ingestion settings. Systemd grants journal read access and a private cursor
directory; the observer has no database credentials. Repeat provisioning when
rotating the ingestion key. The first start scans one hour of retained journal
history; later starts resume its cursor.

Run `python3 -m unittest discover -s scripts/tests -v` for release/observer
regressions. `frontend_release.py smoke --release /absolute/frontend/path
--node-bin /path/to/node/bin` exercises a real build without modifying the
serving symlink or starting production traffic.

## Incident repair on 2026-09-08

The recovered application version `4b3367f` was retained while the frontend was
moved into `.frontend-releases` and the observer was installed. The admin usage
query also required a targeted repair: it now uses the existing assistant-owned
Agno connection instead of attempting to read `ai.agno_runs` with the API role.
No database grants or schema changes were needed for this repair. PostgreSQL
regressions verify that the API remains unable to access `ai` directly while the
usage read succeeds through the proper identity.
