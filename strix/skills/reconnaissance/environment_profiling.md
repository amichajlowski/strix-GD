---
name: environment_profiling
description: Fingerprint the target before heavy tooling — classify network location, detect WAF/CDN and stack, identify the asset type, then record it with set_target_profile and tune every scanner/fuzzer to its hints.
---

# Environment Profiling

Recon writes the target's fingerprint **once**; every specialist reads it
before composing a tool command. This stops one-size-fits-all options — nobody
fuzzes localhost slowly, nobody sprays raw payloads through a WAF.

Do this immediately after confirming the target is live, **before** any heavy
tool run.

## 1. Classify network location

Decide localhost vs internal vs internet from the host/URL alone (no DNS
needed):

- `localhost`, `*.local`, `*.localhost`, `127.0.0.0/8`, `::1` → **localhost**
- RFC1918 (`10/8`, `172.16/12`, `192.168/16`) and `fc00::/7` ULA → **internal**
- any public host/IP → **internet**

Localhost/internal ⇒ safe to raise threads/rate and drop evasion.

## 2. Detect WAF / CDN

```bash
wafw00f https://XXXX.example
```

A WAF/CDN in front ⇒ lower throughput, encode payloads, rotate headers, and
watch `traffic_health` before scaling load.

## 3. Fingerprint the stack

```bash
httpx -u https://XXXX.example -td -sc -title -server
```

`-td` gives the tech stack; `-sc/-title/-server` give status, title, and server
banner. Record open ports and scheme too.

## 4. Detect the asset type

Look for tell-tales:

- `/graphql` (or introspection) → `api_graphql`
- OpenAPI/Swagger (`/openapi.json`, `/swagger`) or JSON-only responses → `api_rest`
- a hydrated JS bundle / empty HTML shell → `spa`
- server-rendered HTML pages → `web_app`

## 5. Record the profile

```text
set_target_profile(
  target="https://XXXX.example",
  network_location="internet",
  scheme="https",
  ports=[443],
  waf="cloudflare",
  cdn="cloudflare",
  tech_stack=["nginx", "php", "laravel"],
  auth_model="bearer",
  asset_type="api_rest",
  cloud_provider="aws",
  rate_limit_observed=True,
  scope_size="single_host",
  sources=["wafw00f", "httpx"],
)
```

`set_target_profile` is an upsert with merge: only the fields you pass are
written, so call it again as new facts arrive. `sources` is appended and
deduped.

## 6. Read the hints before every heavy tool

```text
get_target_profile(target="https://XXXX.example")
```

The response carries derived `hints` — `throughput`, `evasion`,
`host_timeout`, `skills`, `notes`. Tune each scanner/fuzzer to them:

- `throughput: high` → raise threads/rate (localhost/internal, no WAF).
- `throughput: low` + `evasion: encode+rotate-headers` → slow down, encode
  payloads, rotate headers (WAF present or rate limiting observed).
- `skills: ["graphql"]` → load the GraphQL skill and introspect the schema.
- cloud provider set → treat the metadata endpoint as an SSRF target.

Re-read the profile whenever you start a new attack surface.
