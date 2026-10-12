# Organizations and cases API

## DataProvider

Base endpoint:

```http
/api/cases/data-providers/
```

**Permissions:**

- `GET list/retrieve` - public.
- `POST/PUT/PATCH/DELETE` - `Helper` or `Admin`.

**Fields:**

```json
{
  "id": 1,
  "name": "ZTM Warszawa",
  "website": "https://www.wtp.waw.pl/",
  "contact_email": "kontakt@example.com"
}
```

## CaseStatus

Base endpoint:

```http
/api/cases/case-statuses/
```

**Permissions:**

- `GET list/retrieve` - public.
- `POST/PUT/PATCH/DELETE` - `Helper` or `Admin`.

**Fields:**

```json
{
  "id": 10,
  "case": 5,
  "status": "requested",
  "date": "2026-04-27T10:00:00Z",
  "description": "Wyslano wniosek o dane."
}
```

`date` is read-only and set automatically on creation.

Allowed `status` values:

| Value | Meaning |
|---------|-----------|
| `none` | No status |
| `requested` | Data requested / request sent |
| `denial` | Denial |
| `court_referral` | Referral to court |
| `ministry_complaint` | Complaint to the ministry |
| `not_available` | Data not available |
| `received` | Data received |
| `no_contract` | No data contract with the provider |
| `reminder` | Reminder |
| `phone_call` | Phone call |
| `other` | Other |

## TransportOrganization

Base endpoint:

```http
/api/cases/transport-organizations/
```

**Permissions:**

- `GET list/retrieve` - public.
- `POST/PUT/PATCH/DELETE` - `Helper` or `Admin`.

**List (`GET /api/cases/transport-organizations/`):**

```json
[
  {
    "id": 5,
    "region": "Mazowieckie",
    "transport_organization": "ZTM Warszawa",
    "website": "https://www.wtp.waw.pl/",
    "contact_email": "kontakt@example.com",
    "phone_number": "+48123456789",
    "is_public": true,
    "data_providers": [
      {
        "id": 1,
        "name": "ZTM Warszawa"
      }
    ],
    "created_at": "2026-04-27T10:00:00Z",
    "updated_at": "2026-04-27T10:00:00Z",
    "latest_status": {
      "id": 10,
      "status": "requested",
      "status_display": "Data Requested",
      "date": "2026-04-27T10:00:00Z",
      "description": "Wyslano wniosek."
    }
  }
]
```

**Details (`GET /api/cases/transport-organizations/{id}/`):**

Instead of `latest_status`, the `statuses` field is returned with the full status history.

```json
{
  "id": 5,
  "region": "Mazowieckie",
  "transport_organization": "ZTM Warszawa",
  "website": "https://www.wtp.waw.pl/",
  "contact_email": "kontakt@example.com",
  "phone_number": "+48123456789",
  "is_public": true,
  "data_providers": [
    {
      "id": 1,
      "name": "ZTM Warszawa"
    }
  ],
  "created_at": "2026-04-27T10:00:00Z",
  "updated_at": "2026-04-27T10:00:00Z",
  "statuses": [
    {
      "id": 10,
      "status": "requested",
      "status_display": "Data Requested",
      "date": "2026-04-27T10:00:00Z",
      "description": "Wyslano wniosek."
    }
  ]
}
```

**Create/update request:**

```json
{
  "region": "Mazowieckie",
  "transport_organization": "ZTM Warszawa",
  "website": "https://www.wtp.waw.pl/",
  "contact_email": "kontakt@example.com",
  "phone_number": "+48123456789",
  "is_public": true,
  "data_provider_ids": [1, 2]
}
```

`data_provider_ids` is write-only. In the response the client receives `data_providers`.

When an organization is created, the backend automatically creates an initial `CaseStatus` with the status `none`.
