# portal-plugin

## Calendar capability probe

`GET /api/plugins/portal/capabilities?profile=<profile>` (authenticated with
the dashboard session token, `Cache-Control: no-store`) reports whether the
bound profile has opted in to phone calendar data, without reading any
calendar data. Success body (schema v1):

```json
{"schema_version":1,"plugin":{"name":"portal","version":"0.2.0-dev","api_loaded":true},"scope":{"profile":"<profile>","authoritative":true},"calendar":{"supported":true,"read_sync":true,"host_opted_in":false}}
```

- The actual profile is derived from the bound host context, never echoed
  from the query. An unverifiable scope (custom home, resolution failure)
  answers `409 {"schema_version":1,"error":"scope_unverified"}`.
- `host_opted_in` mirrors the existing owner-side flag, set on the host with
  `python -m portal.phone opt-in calendar`. The probe never mutates consent
  and never reads events, stores, or history.
- A plugin that is absent, disabled, or not loaded answers `404` for the
  probe. That is honestly "not loaded or unsupported" — never readiness.

### Install

Copy the plugin to `~/.hermes/plugins/portal/` and enable it for the profile
in that profile's `config.yaml`:

```yaml
plugins:
  enabled:
    - portal
```

### Reload

A running dashboard hot-mounts the plugin's API on its first request after
install/enable; otherwise restart the dashboard. After upgrading this
plugin's files, restart the dashboard so the mounted routes come from the
new code.
