# API Spec (OpenAPI)

> **Developer note:** This page is for people writing code against Lumen. See the [API Reference](api-reference.md) for examples and details.

Lumen describes its official `/v1/` API in an OpenAPI 3.1 spec. Every Lumen instance serves its own copy, with its own URL as the server, at:

```
https://lumen.example.com/v1/openapi.yaml
```

That URL needs no API key. Point a client generator or an API tool at it.

## Access classes

Each operation lists the credentials that may call it, in its description and in the `x-access` field of the spec:

| Class | Credential |
|-------|------------|
| `public` | No key needed. |
| `normal` | A normal API key (`sk_...`). Generation requests are billed. |
| `monitor` | A monitoring key (`sk_monitor_...`), or the deprecated `api.monitoring.token` from the config file. |
| `admin` | An admin key (`sk_admin_...`). |

On an operation that needs a key, a valid key whose type is not in the operation's list gets `403`. Public operations ignore any key sent. See [Key types and access](api-reference.md#key-types-and-access).

## Endpoints

The spec below is read-only: "Try it out" is turned off. Use `curl` or an SDK against your own Lumen instance to send requests.

<swagger-ui src="../swagger.yaml" supportedSubmitMethods="[]"/>
