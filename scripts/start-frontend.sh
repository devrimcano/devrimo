#!/bin/sh
# Start whichever kind of frontend release is currently linked.
#
# A standalone build is its own server: `server.js` next to a `.next` holding
# only what that server imports, started by running it, configured by PORT and
# HOSTNAME in the environment. A release built the old way is a working tree
# with the whole toolchain in node_modules, started through Next's CLI.
#
# systemd points at this script rather than at either one directly, because the
# host holds both shapes at once: the release being activated is standalone,
# and the release retained behind it - the one a failed activation rolls back
# to - may not be. A unit hard-coded to either shape turns that rollback into a
# frontend that will not start, which is the one moment it must.
set -eu

root="${FRONTEND_ROOT:-/opt/devrimo/current-frontend}"
port="${PORT:-3000}"
# Loopback by default. The unit sets HOSTNAME explicitly; this default is what
# a hand-run of the script uses, and 0.0.0.0 here is what published the server
# directly on the public interface.
host="${HOSTNAME:-127.0.0.1}"
cd "$root"

if [ -f server.js ]; then
  PORT="$port" HOSTNAME="$host" exec node server.js
fi

exec node node_modules/next/dist/bin/next start --port "$port" --hostname "$host"
