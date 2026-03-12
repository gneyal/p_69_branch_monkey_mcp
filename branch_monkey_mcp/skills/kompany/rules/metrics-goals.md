# Metrics & Goals

How to set up system types, metrics, and goal tracking.

## System types

Each system has a `machine_type` that describes its role in the business:

| Type | Description | Example |
|------|-------------|---------|
| generator | Creates new items/leads | Lead generation, Content creation |
| processor | Transforms inputs to outputs | Order fulfillment, Onboarding |
| funnel | Filters/qualifies items | Sales pipeline, Application screening |
| monitor | Watches and alerts | Dashboard, Health checks |
| router | Directs items to destinations | Lead routing, Ticket triage |
| aggregator | Combines multiple inputs | Reporting, Data consolidation |
| syncer | Keeps systems in sync | CRM sync, Inventory sync |
| nurture | Nurtures over time | Email sequences, Customer success |

## Metrics

Each system can have multiple metrics tracking its performance.

### Creating metrics
When creating a system, use these fields for auto-seeded metrics:
- `metric_unit` — Output/result metric name (e.g., "leads", "deals", "revenue")
- `leading_metric_name` — Input/activity metric name (e.g., "calls made", "emails sent")

Both are optional. If provided, they auto-create metrics when the system is created.

### Adding metrics manually
```
kompany_metric_add(
  machine_id="<uuid>",
  metric_name="revenue",
  value=0,
  target=10000,
  period="monthly"
)
```

### Metric periods
- `weekly` — Reset/tracked per week (default)
- `monthly` — Reset/tracked per month
- `daily` — Reset/tracked per day

## Examples by business type

### SaaS
| System | metric_unit | leading_metric_name |
|---------|-------------|-------------------|
| Content Marketing | leads | posts published |
| Sales Pipeline | deals closed | demos booked |
| Onboarding | activated users | onboarding calls |
| Billing | MRR | invoices sent |

### E-Commerce
| System | metric_unit | leading_metric_name |
|---------|-------------|-------------------|
| Product Catalog | page views | products listed |
| Cart & Checkout | orders | cart additions |
| Shipping | deliveries | packages shipped |

### Agency
| System | metric_unit | leading_metric_name |
|---------|-------------|-------------------|
| Proposals | proposals sent | meetings held |
| Project Delivery | projects completed | hours logged |
| Renewals | renewals | review meetings |

## Best practices
- Every system should have at least one metric
- Use `target` to set goals — the dashboard shows progress toward targets
- Leading metrics (activities) predict lagging metrics (results)
- Start with weekly periods, switch to monthly for slower-moving metrics
