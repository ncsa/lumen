# Models

The **Models** page (`/models`) is a dashboard showing every model configured in `config.yaml`, their health status, and pricing.

![Models dashboard](/help/img/models.png)

## Table Columns

| Column | Description |
|--------|------------|
| **Model** | Clickable name — links to the model detail page |
| **Coins / 1M tokens (input)** | Cost per million input tokens |
| **Coins / 1M tokens (output)** | Cost per million output tokens |
| **Total Endpoints** | Number of backend endpoints configured |
| **Healthy** | Number of healthy endpoints out of total |
| **Last Checked** | Timestamp of the most recent health check |
| **Status** | Current availability status |

## Status Badges

| Badge | Meaning |
|-------|---------|
| **disabled** (gray) | Model is set to `active: false` in config |
| **no endpoints** (gray) | No backend endpoints configured |
| **ok** (green) | All endpoints are healthy and responding |
| **degraded** (yellow) | Some endpoints are down, but at least one is healthy |
| **down** (red) | All endpoints are unreachable |

## Health Checks

A background service periodically pings each model endpoint using the OpenAI client. Health status determines:

- Whether the endpoint is used in round-robin balancing (only healthy ones)
- What status badge is shown on this dashboard and the model detail page
