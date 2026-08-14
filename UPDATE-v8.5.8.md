# v8.5.8 server update

This incremental package is installed over v8.5.7.

The portal now uses server-side sessions. Existing browser sessions become
invalid after the portal service restarts. New sessions expire after 30 minutes
without portal activity or after eight hours total; signing in again revokes a
previous session for the same user.

Copy the update files to `/opt/gns3-lab-portal`, keep the default session
values in `.env` unless your policy requires different values, and restart the
service. Every user will then need to sign in again.
