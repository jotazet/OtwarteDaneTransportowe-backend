# OtwarteDaneTransportowe - dokumentacja API dla frontendu

Ten dokument opisuje aktualna strukture backendu, endpointy REST API, najwazniejsze pola odpowiedzi oraz wymagane uprawnienia. Jest pisany jako kontekst do budowy aplikacji frontendowej.

## Spis Tresci

1. [Szybki Start](#1-szybki-start)
2. [Struktura Backendu](#2-struktura-backendu)
3. [Role I Uprawnienia](#3-role-i-uprawnienia)
4. [Autentykacja JWT](#4-autentykacja-jwt)
5. [Mapa Endpointow](#5-mapa-endpointow)
6. [API Bloga](#6-api-bloga)
7. [API Organizacji I Case'ow](#7-api-organizacji-i-caseow)
8. [API Feedow](#8-api-feedow)
9. [Publiczne Pobieranie Plikow](#9-publiczne-pobieranie-plikow)
10. [Modele I Slowniki](#10-modele-i-slowniki)
11. [Workflow Feedow](#11-workflow-feedow)
12. [Uwagi Dla Frontendu](#12-uwagi-dla-frontendu)

---

## 1. Szybki Start

Backend to Django 6 + Django REST Framework. API jest podzielone na trzy glowne obszary:

- `/api/blog/` - wpisy blogowe i reakcje.
- `/api/cases/` - dostawcy danych, organizacje transportowe i statusy spraw.
- `/api/data_manager/` - zglaszanie feedow statycznych i realtime oraz publiczny katalog opublikowanych feedow.

Autentykacja API uzywa JWT:

```http
POST /api/auth/token/
POST /api/auth/token/refresh/
```

Dla zapytan wymagajacych logowania frontend wysyla:

```http
Authorization: Bearer <access_token>
```

Publiczne endpointy list, odczytu bloga, odczytu organizacji, reakcji oraz pobierania opublikowanych feedow nie wymagaja tokenu.

---

## 2. Struktura Backendu

Najwazniejsze pliki:

```text
OtwarteDaneTransportowe/
├── OtwarteDaneTransportowe/
│   ├── settings_base.py       # DRF, JWT, aplikacje, baza danych
│   ├── urls.py                # glowne URL-e projektu
│   └── auth_roles.py          # role i permission classes
├── blog/
│   ├── models.py              # Post, Reaction
│   └── api/
│       ├── urls.py
│       ├── views.py
│       └── serializers.py
├── cases/
│   ├── models.py              # DataProvider, TransportOrganization, CaseStatus
│   └── api/
│       ├── urls.py
│       ├── views.py
│       └── serializers.py
└── data_manager/
    ├── models.py              # FeedSubmission, StaticFeedEntry, RealtimeSubmission, ...
    ├── tasks.py               # walidacja i pobieranie feedow
    ├── scheduler.py           # harmonogram odswiezania
    └── api/
        ├── urls.py
        ├── views.py
        └── serializers.py
```

Glowne routery DRF:

```text
/api/blog/posts/
/api/cases/data-providers/
/api/cases/case-statuses/
/api/cases/transport-organizations/
/api/data_manager/feed-submissions/
/api/data_manager/realtime-submissions/
/api/data_manager/feeds/
```

---

## 3. Role I Uprawnienia

Role sa zwyklymi grupami Django (`auth.Group`). Migracja tworzy grupy:

- `Admin`
- `Blogger`
- `DataProvider`
- `Helper`

`Admin` dziala takze dla uzytkownikow z `is_staff=True` lub `is_superuser=True`.

### Znaczenie rol

| Rola | Mozliwosci |
|------|------------|
| `Admin` | Pelny dostep administracyjny. Dziedziczy wszystkie uprawnienia rol domenowych. |
| `Blogger` | Tworzenie, edycja i usuwanie wpisow blogowych. |
| `DataProvider` | Dodawanie feedow statycznych i realtime oraz zarzadzanie wlasnymi zgloszeniami przed review. |
| `Helper` | Potwierdzanie/odrzucanie feedow, zarzadzanie dostawcami danych, organizacjami i statusami case'ow. |
| Anonim | Publiczny odczyt, reakcje na blogu, pobieranie opublikowanych feedow. |

### Macierz dostepu

| Obszar | Publiczny odczyt | Zapis |
|--------|------------------|-------|
| Blog posts | Tak | `Blogger` lub `Admin` |
| Blog reactions | Tak, `POST` bez logowania | Kazdy, limit po IP |
| Data providers | Tak | `Helper` lub `Admin` |
| Case statuses | Tak | `Helper` lub `Admin` |
| Transport organizations | Tak | `Helper` lub `Admin` |
| Static feed submissions | Nie | `DataProvider` tworzy; `Helper`/`Admin` potwierdza |
| Realtime submissions | Nie | `DataProvider` tworzy; `Helper`/`Admin` potwierdza |
| Published feeds catalog | Tak | Brak zapisu przez publiczne API |
| Public feed files | Tak, tylko opublikowane | Brak zapisu |

---

## 4. Autentykacja JWT

### `POST /api/auth/token/`

Zwraca pare tokenow JWT.

**Uprawnienia:** publiczny.

**Request:**

```json
{
  "username": "user",
  "password": "password"
}
```

**Response 200:**

```json
{
  "refresh": "eyJ...",
  "access": "eyJ..."
}
```

### `POST /api/auth/token/refresh/`

Odswieza access token na podstawie refresh tokenu.

**Uprawnienia:** publiczny, wymaga poprawnego `refresh`.

**Request:**

```json
{
  "refresh": "eyJ..."
}
```

**Response 200:**

```json
{
  "access": "eyJ..."
}
```

### `/api-auth/`

DRF browsable API session login/logout. Przydatne developersko, frontend produkcyjny powinien uzywac JWT.

---

## 5. Mapa Endpointow

### Systemowe

| Metoda | Endpoint | Uprawnienia | Opis |
|--------|----------|-------------|------|
| `GET` | `/admin/` | staff/admin Django | Panel administracyjny Django |
| `POST` | `/api/auth/token/` | publiczny | Pobranie `access` i `refresh` JWT |
| `POST` | `/api/auth/token/refresh/` | publiczny | Odswiezenie `access` JWT |
| `GET` | `/api/schema/` | publiczny | OpenAPI schema |
| `GET` | `/api/schema/swagger-ui/` | publiczny | Swagger UI |
| `GET` | `/api/schema/redoc/` | publiczny | ReDoc |
| `GET` | `/api-auth/` | publiczny/dev | Login/logout browsable API |

### Blog

| Metoda | Endpoint | Uprawnienia | Opis |
|--------|----------|-------------|------|
| `GET` | `/api/blog/posts/` | publiczny | Lista wpisow |
| `POST` | `/api/blog/posts/` | `Blogger`/`Admin` | Utworzenie wpisu |
| `GET` | `/api/blog/posts/{id}/` | publiczny | Szczegoly wpisu |
| `PUT` | `/api/blog/posts/{id}/` | `Blogger`/`Admin` | Pelna edycja wpisu |
| `PATCH` | `/api/blog/posts/{id}/` | `Blogger`/`Admin` | Czesciowa edycja wpisu |
| `DELETE` | `/api/blog/posts/{id}/` | `Blogger`/`Admin` | Usuniecie wpisu |
| `POST` | `/api/blog/reactions/{post_id}/` | publiczny | Dodanie/zmiana/usuniecie reakcji po IP |

### Cases

| Metoda | Endpoint | Uprawnienia | Opis |
|--------|----------|-------------|------|
| `GET` | `/api/cases/data-providers/` | publiczny | Lista dostawcow danych |
| `POST` | `/api/cases/data-providers/` | `Helper`/`Admin` | Dodanie dostawcy danych |
| `GET` | `/api/cases/data-providers/{id}/` | publiczny | Szczegoly dostawcy |
| `PUT/PATCH` | `/api/cases/data-providers/{id}/` | `Helper`/`Admin` | Edycja dostawcy |
| `DELETE` | `/api/cases/data-providers/{id}/` | `Helper`/`Admin` | Usuniecie dostawcy |
| `GET` | `/api/cases/case-statuses/` | publiczny | Lista statusow spraw |
| `POST` | `/api/cases/case-statuses/` | `Helper`/`Admin` | Dodanie statusu sprawy |
| `GET` | `/api/cases/case-statuses/{id}/` | publiczny | Szczegoly statusu |
| `PUT/PATCH` | `/api/cases/case-statuses/{id}/` | `Helper`/`Admin` | Edycja statusu |
| `DELETE` | `/api/cases/case-statuses/{id}/` | `Helper`/`Admin` | Usuniecie statusu |
| `GET` | `/api/cases/transport-organizations/` | publiczny | Lista organizacji transportowych |
| `POST` | `/api/cases/transport-organizations/` | `Helper`/`Admin` | Dodanie organizacji |
| `GET` | `/api/cases/transport-organizations/{id}/` | publiczny | Szczegoly organizacji z historia statusow |
| `PUT/PATCH` | `/api/cases/transport-organizations/{id}/` | `Helper`/`Admin` | Edycja organizacji |
| `DELETE` | `/api/cases/transport-organizations/{id}/` | `Helper`/`Admin` | Usuniecie organizacji |

### Data Manager

| Metoda | Endpoint | Uprawnienia | Opis |
|--------|----------|-------------|------|
| `GET` | `/api/data_manager/feed-submissions/` | `DataProvider`/`Helper`/`Admin` | Lista zgloszen statycznych: provider widzi swoje, helper/admin wszystkie |
| `POST` | `/api/data_manager/feed-submissions/` | `DataProvider`/`Admin` | Dodanie statycznego feeda |
| `GET` | `/api/data_manager/feed-submissions/{id}/` | wlasciciel lub `Helper`/`Admin` | Szczegoly statycznego zgloszenia |
| `PUT/PATCH` | `/api/data_manager/feed-submissions/{id}/` | wlasciciel przed review lub `Helper`/`Admin` | Edycja albo potwierdzenie/odrzucenie |
| `DELETE` | `/api/data_manager/feed-submissions/{id}/` | wlasciciel przed review lub `Admin` | Usuniecie zgloszenia |
| `GET` | `/api/data_manager/feed-submissions/{id}/download/static/{endpoint_pk}/` | wlasciciel lub `Helper`/`Admin` | Pobranie prywatnego pliku statycznego z danego zgloszenia |
| `GET` | `/api/data_manager/feed-submissions/{id}/download/realtime/{endpoint_pk}/` | wlasciciel lub `Helper`/`Admin` | Pobranie prywatnego cache endpointu RT powiazanego ze statycznym zgloszeniem |
| `GET` | `/api/data_manager/realtime-submissions/` | `DataProvider`/`Helper`/`Admin` | Lista zgloszen realtime: provider widzi swoje, helper/admin wszystkie |
| `POST` | `/api/data_manager/realtime-submissions/` | `DataProvider`/`Admin` | Dodanie realtime feeda |
| `GET` | `/api/data_manager/realtime-submissions/{id}/` | wlasciciel lub `Helper`/`Admin` | Szczegoly realtime zgloszenia |
| `PUT/PATCH` | `/api/data_manager/realtime-submissions/{id}/` | wlasciciel przed review lub `Helper`/`Admin` | Edycja albo potwierdzenie/odrzucenie |
| `DELETE` | `/api/data_manager/realtime-submissions/{id}/` | wlasciciel przed review lub `Admin` | Usuniecie realtime zgloszenia |
| `GET` | `/api/data_manager/feeds/` | publiczny | Publiczny katalog organizacji z opublikowanymi feedami |
| `GET` | `/api/data_manager/feeds/{id}/` | publiczny | Szczegoly organizacji i jej opublikowanych feedow |

### Publiczne pliki feedow

| Metoda | Endpoint | Uprawnienia | Opis |
|--------|----------|-------------|------|
| `GET` | `/feed/` | publiczny | Zwraca blad 400, wymagane ID feeda |
| `GET` | `/feed/{feed_submission_id}/` | publiczny | Zwraca link do opublikowanego pliku statycznego |
| `GET` | `/feed/{feed_submission_id}/{filename}` | publiczny | Pobiera opublikowany plik statyczny |
| `GET` | `/feed/rt/{realtime_submission_id}/` | publiczny | Zwraca linki do opublikowanych plikow realtime |
| `GET` | `/feed/rt/{realtime_submission_id}/{filename}` | publiczny | Pobiera opublikowany plik realtime |

---

## 6. API Bloga

### `GET /api/blog/posts/`

Lista wpisow blogowych. Bez parametru `page` backend zwraca zwykla tablice. Z parametrem `page` wlacza paginacje DRF.

**Uprawnienia:** publiczny.

**Query params:**

- `page` - wlacza paginacje.
- `page_size` - domyslnie 4, maksymalnie 50.

**Response 200 bez `page`:**

```json
[
  {
    "id": 1,
    "title": "Nowe dane GTFS",
    "author": 3,
    "author_username": "admin",
    "tags": ["gtfs", "transport"],
    "content": "Skrocona tresc do 150 znakow...",
    "image": null,
    "date": "2026-04-27T10:00:00Z",
    "updated_at": "2026-04-27T10:00:00Z",
    "reactions_summary": {
      "like": 3,
      "dislike": 0,
      "love": 1,
      "haha": 0,
      "wow": 0,
      "sad": 0,
      "angry": 0
    },
    "your_reaction": "like"
  }
]
```

### `GET /api/blog/posts/{id}/`

Szczegoly wpisu. W przeciwienstwie do listy pole `content` zawiera pelna tresc.

**Uprawnienia:** publiczny.

### `POST /api/blog/posts/`

Tworzy wpis blogowy. Autor jest ustawiany automatycznie na zalogowanego uzytkownika.

**Uprawnienia:** `Blogger` lub `Admin`.

**Request JSON lub multipart przy obrazku:**

```json
{
  "title": "Nowy wpis",
  "tags": ["gtfs", "api"],
  "content": "Pelna tresc wpisu"
}
```

**Pola:**

- `title` - wymagane, maks. 24 znaki.
- `tags` - lista maks. 5 tagow, kazdy maks. 16 znakow.
- `content` - wymagane.
- `image` - opcjonalny plik obrazu.

### `PUT/PATCH/DELETE /api/blog/posts/{id}/`

Edycja lub usuniecie wpisu.

**Uprawnienia:** `Blogger` lub `Admin`.

Aktualnie permission nie ogranicza edycji do autora wpisu. Kazdy `Blogger` moze edytowac kazdy wpis.

### `POST /api/blog/reactions/{post_id}/`

Tworzy, zmienia albo usuwa reakcje dla pary `(post, IP)`.

**Uprawnienia:** publiczny.

**Request:**

```json
{
  "reaction": "like"
}
```

Dozwolone reakcje:

```text
like, dislike, love, haha, wow, sad, angry
```

Usuniecie reakcji:

```json
{
  "reaction": null
}
```

albo:

```json
{
  "reaction": ""
}
```

**Zasady:**

- Reakcje sa limitowane po IP.
- Maksymalnie 10 aktywnych nowych reakcji na IP w ciagu 24h.
- Endpoint obsluguje tylko `POST` i `OPTIONS`; list/retrieve sa celowo wylaczone, zeby nie ujawniac IP.

---

## 7. API Organizacji I Case'ow

### DataProvider

Endpoint bazowy:

```http
/api/cases/data-providers/
```

**Uprawnienia:**

- `GET list/retrieve` - publiczny.
- `POST/PUT/PATCH/DELETE` - `Helper` lub `Admin`.

**Pola:**

```json
{
  "id": 1,
  "name": "ZTM Warszawa",
  "website": "https://www.wtp.waw.pl/",
  "contact_email": "kontakt@example.com"
}
```

### CaseStatus

Endpoint bazowy:

```http
/api/cases/case-statuses/
```

**Uprawnienia:**

- `GET list/retrieve` - publiczny.
- `POST/PUT/PATCH/DELETE` - `Helper` lub `Admin`.

**Pola:**

```json
{
  "id": 10,
  "case": 5,
  "status": "requested",
  "date": "2026-04-27T10:00:00Z",
  "description": "Wyslano wniosek o dane."
}
```

`date` jest read-only i ustawiane automatycznie przy utworzeniu.

Dozwolone `status`:

| Wartosc | Znaczenie |
|---------|-----------|
| `none` | Brak statusu |
| `requested` | Dane wymagane / wyslano prosbe |
| `denial` | Odmowa |
| `court_referral` | Skierowanie do sadu |
| `ministry_complaint` | Skarga do ministerstwa |
| `not_available` | Dane niedostepne |
| `received` | Dane otrzymane |
| `no_contract` | Brak umowy na dane z providerem |
| `reminder` | Przypomnienie |
| `phone_call` | Telefon |
| `other` | Inne |

### TransportOrganization

Endpoint bazowy:

```http
/api/cases/transport-organizations/
```

**Uprawnienia:**

- `GET list/retrieve` - publiczny.
- `POST/PUT/PATCH/DELETE` - `Helper` lub `Admin`.

**Lista (`GET /api/cases/transport-organizations/`):**

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

**Szczegoly (`GET /api/cases/transport-organizations/{id}/`):**

Zamiast `latest_status` zwracane jest pole `statuses` z cala historia statusow.

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

**Request create/update:**

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

`data_provider_ids` jest write-only. W odpowiedzi frontend dostaje `data_providers`.

Przy utworzeniu organizacji backend automatycznie tworzy poczatkowy `CaseStatus` ze statusem `none`.

---

## 8. API Feedow

Feedy sa podzielone na dwa osobne flow:

- `feed-submissions` - feedy statyczne: GTFS, NeTEx, other.
- `realtime-submissions` - feedy realtime: GTFS-RT, SIRI, GBFS.

Oba flow maja historie etapow (`history`) i sa publikowane dopiero po osiagnieciu etapu 4.

### 8.1 FeedSubmission - feed statyczny

Endpoint bazowy:

```http
/api/data_manager/feed-submissions/
```

**Uprawnienia:**

- `GET list` - `DataProvider` widzi wlasne zgloszenia; `Helper`/`Admin` widzi wszystkie.
- `POST` - `DataProvider` lub `Admin`.
- `GET detail` - wlasciciel lub `Helper`/`Admin`.
- `PUT/PATCH` - wlasciciel tylko przed review; `Helper`/`Admin` moze potwierdzac/odrzucac.
- `DELETE` - wlasciciel tylko przed review; `Admin` moze usuwac dowolne.

**Query params listy:**

- `data_type` - np. `gtfs`.
- `transport_organization` - ID organizacji.

**Lista:**

```json
[
  {
    "id": 37,
    "transport_organization": 5,
    "data_type": "gtfs",
    "current_stage": 3,
    "current_stage_label": "Step 3: Admin confirmation",
    "is_rejected": false,
    "published_at": null,
    "created_at": "2026-04-27T10:00:00Z",
    "updated_at": "2026-04-27T10:00:00Z",
    "has_rejection_cause": false
  }
]
```

**Szczegoly:**

```json
{
  "id": 37,
  "transport_organization": 5,
  "submitted_by": 12,
  "data_type": "gtfs",
  "name": "Rozklad ZTM",
  "note": "Aktualizacja kwiecien",
  "created_at": "2026-04-27T10:00:00Z",
  "updated_at": "2026-04-27T10:00:00Z",
  "current_stage": 3,
  "current_stage_label": "Step 3: Admin confirmation",
  "is_rejected": false,
  "rejection_cause": null,
  "published_at": null,
  "static_entry": {
    "id": 9,
    "url": "https://example.com/gtfs.zip",
    "file": null,
    "is_original": true,
    "hide_original": false,
    "auth_type": null,
    "download_time_1": "03:00:00",
    "download_time_2": null,
    "license": "CC BY 4.0",
    "cached_at": null,
    "uploaded_at": "2026-04-27T10:00:00Z"
  },
  "realtime_submissions": [],
  "history": [
    {
      "id": 100,
      "event_type": "uploaded",
      "stage_before": 1,
      "stage_after": 2,
      "actor": "provider",
      "cause": null,
      "created_at": "2026-04-27T10:00:00Z"
    }
  ]
}
```

`auth_value` jest write-only i nigdy nie wraca w odpowiedzi.

**Create - URL:**

```json
{
  "transport_organization": 5,
  "data_type": "gtfs",
  "name": "Rozklad ZTM",
  "note": "Zrodlo oficjalne",
  "static_entry": {
    "url": "https://example.com/gtfs.zip",
    "is_original": true,
    "hide_original": false,
    "auth_type": null,
    "download_time_1": "03:00:00",
    "download_time_2": null,
    "license": "CC BY 4.0"
  }
}
```

**Create - URL z autoryzacja:**

```json
{
  "transport_organization": 5,
  "data_type": "gtfs",
  "name": "Rozklad prywatny",
  "static_entry": {
    "url": "https://example.com/private/gtfs.zip",
    "is_original": true,
    "auth_type": "bearer_token",
    "auth_value": "secret-token",
    "download_time_1": "03:00:00",
    "license": "CC BY 4.0"
  }
}
```

Jesli `auth_type` jest ustawione, backend automatycznie ustawia `hide_original=True`.

**Create - upload pliku:**

```http
POST /api/data_manager/feed-submissions/
Content-Type: multipart/form-data
Authorization: Bearer <token>

transport_organization=5
data_type=gtfs
name=Rozklad ZTM
static_entry.file=<plik zip>
static_entry.is_original=true
static_entry.license=CC BY 4.0
```

**Potwierdzenie przez Helper/Admin:**

```json
{
  "stage": 4
}
```

`stage` musi byc liczba od 1 do 4. Jesli `stage=4`, backend tworzy wpis historii `completed`, a feed staje sie publiczny.

**Odrzucenie przez Helper/Admin:**

```json
{
  "rejection_cause": "Brakuje wymaganych plikow GTFS."
}
```

### 8.2 RealtimeSubmission - feed realtime

Endpoint bazowy:

```http
/api/data_manager/realtime-submissions/
```

**Uprawnienia:**

- `GET list` - `DataProvider` widzi wlasne; `Helper`/`Admin` widzi wszystkie.
- `POST` - `DataProvider` lub `Admin`.
- `GET detail` - wlasciciel lub `Helper`/`Admin`.
- `PUT/PATCH` - wlasciciel tylko na etapie 1 lub po odrzuceniu; `Helper`/`Admin` moze potwierdzac/odrzucac.
- `DELETE` - wlasciciel tylko na etapie 1; `Admin` moze usuwac dowolne.

**Query params listy:**

- `transport_organization` - ID organizacji.

**Create GTFS-RT:**

```json
{
  "transport_organization": 5,
  "static_submission": 37,
  "protocol": "gtfs_rt",
  "name": "Realtime ZTM",
  "note": "GTFS-RT do statycznego GTFS",
  "license": "CC BY 4.0",
  "endpoints": [
    {
      "endpoint_type": "trip_update",
      "url": "https://example.com/trip-updates.pb",
      "is_original": true,
      "auth_type": null,
      "interval": 30
    },
    {
      "endpoint_type": "vehicle_position",
      "url": "https://example.com/vehicle-positions.pb",
      "is_original": true,
      "auth_type": "api_key",
      "auth_value": "secret",
      "interval": 15
    }
  ]
}
```

**Create GBFS:**

```json
{
  "transport_organization": 5,
  "protocol": "gbfs",
  "name": "Rowery miejskie",
  "license": "ODbL",
  "endpoints": [
    {
      "endpoint_type": "gbfs",
      "url": "https://example.com/gbfs.json",
      "is_original": true,
      "auth_type": null,
      "interval": 60
    }
  ]
}
```

**Zasady walidacji realtime:**

- `protocol=gtfs_rt` i `protocol=siri` wymagaja `static_submission`.
- `static_submission` musi byc opublikowanym feedem statycznym (`current_stage=4`).
- `static_submission` musi nalezec do tej samej `transport_organization`.
- `protocol=gbfs` nie moze miec `static_submission`.
- `endpoints` sa wymagane i nie moga byc puste.
- Nie mozna dodac dwoch endpointow tego samego `endpoint_type` do jednego zgloszenia.
- Endpoint type musi pasowac do protokolu.
- `auth_value` jest write-only.
- Jesli `auth_type` jest ustawione, backend automatycznie ustawia `hide_original=True`.

**Szczegoly realtime:**

```json
{
  "id": 12,
  "transport_organization": 5,
  "submitted_by": 12,
  "static_submission": 37,
  "protocol": "gtfs_rt",
  "name": "Realtime ZTM",
  "note": "GTFS-RT",
  "license": "CC BY 4.0",
  "created_at": "2026-04-27T10:00:00Z",
  "updated_at": "2026-04-27T10:00:00Z",
  "current_stage": 3,
  "current_stage_label": "Step 3: Admin confirmation",
  "is_rejected": false,
  "rejection_cause": null,
  "published_at": null,
  "endpoints": [
    {
      "id": 20,
      "endpoint_type": "trip_update",
      "url": "https://example.com/trip-updates.pb",
      "is_original": true,
      "hide_original": false,
      "auth_type": null,
      "interval": 30,
      "cached_at": null
    }
  ],
  "history": []
}
```

**Potwierdzenie/Odrzucenie realtime:**

Tak samo jak dla feedow statycznych:

```json
{ "stage": 4 }
```

albo:

```json
{ "rejection_cause": "Endpoint nie odpowiada." }
```

### 8.3 Publiczny katalog feedow

Endpoint bazowy:

```http
/api/data_manager/feeds/
```

**Uprawnienia:** publiczny.

To nie jest lista pojedynczych feedow, tylko lista organizacji transportowych z opublikowanymi feedami.

**Lista (`GET /api/data_manager/feeds/`):**

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
    "static_types": ["gtfs"],
    "dynamic_types": ["gbfs"]
  }
]
```

**Szczegoly (`GET /api/data_manager/feeds/{organization_id}/`):**

```json
{
  "id": 5,
  "region": "Mazowieckie",
  "transport_organization": "ZTM Warszawa",
  "website": "https://www.wtp.waw.pl/",
  "contact_email": "kontakt@example.com",
  "phone_number": "+48123456789",
  "is_public": true,
  "feeds": [
    {
      "id": 37,
      "name": "Rozklad ZTM",
      "data_type": "gtfs",
      "submitted_by": "provider",
      "created_at": "2026-04-27T10:00:00Z",
      "updated_at": "2026-04-27T10:00:00Z",
      "static_feed": {
        "download_url": "https://example.com/feed/37/gtfs.zip",
        "license": "CC BY 4.0",
        "cached_at": "2026-04-27T10:00:00Z",
        "is_original": true
      },
      "realtime_feed": {
        "protocol": "gtfs_rt",
        "license": "CC BY 4.0",
        "published_at": "2026-04-27T11:00:00Z",
        "endpoints": [
          {
            "endpoint_type": "trip_update",
            "interval": 30,
            "feed_url": "https://example.com/feed/rt/12/trip-updates.pb",
            "cached_at": "2026-04-27T11:00:00Z",
            "is_original": true
          }
        ]
      }
    },
    {
      "id": 14,
      "name": "Rowery miejskie",
      "data_type": "gbfs",
      "submitted_by": "provider",
      "created_at": "2026-04-27T10:00:00Z",
      "updated_at": "2026-04-27T10:00:00Z",
      "static_feed": null,
      "realtime_feed": {
        "protocol": "gbfs",
        "license": "ODbL",
        "published_at": "2026-04-27T11:00:00Z",
        "endpoints": [
          {
            "endpoint_type": "gbfs",
            "interval": 60,
            "feed_url": "https://example.com/feed/rt/14/gbfs.json",
            "cached_at": "2026-04-27T11:00:00Z"
          }
        ]
      }
    }
  ]
}
```

Publiczny katalog pokazuje tylko feedy, ktorych najnowszy wpis historii ma `stage_after=4`.

---

## 9. Publiczne Pobieranie Plikow

Te endpointy sa poza `/api/`, bo sluza jako stabilne publiczne linki do feedow.

### `GET /feed/{feed_submission_id}/`

Zwraca link do statycznego pliku feeda, jesli feed jest opublikowany.

**Uprawnienia:** publiczny.

**Response 200:**

```json
{
  "static": "https://example.com/feed/37/gtfs.zip"
}
```

Jesli feed nie istnieje albo nie jest opublikowany, backend zwraca `404`.

### `GET /feed/{feed_submission_id}/{filename}`

Zwraca plik statyczny jako `FileResponse`.

**Uprawnienia:** publiczny, tylko opublikowane feedy.

### `GET /feed/rt/{realtime_submission_id}/`

Zwraca linki do plikow realtime danego opublikowanego zgloszenia.

**Uprawnienia:** publiczny.

**Response 200:**

```json
{
  "dynamic": {
    "trip_update": "https://example.com/feed/rt/12/trip-updates.pb",
    "vehicle_position": "https://example.com/feed/rt/12/vehicle-positions.pb"
  }
}
```

### `GET /feed/rt/{realtime_submission_id}/{filename}`

Zwraca plik realtime jako `FileResponse`.

**Uprawnienia:** publiczny, tylko opublikowane realtime feedy.

---

## 10. Modele I Slowniki

### Static feed data types

| Wartosc | Znaczenie |
|---------|-----------|
| `gtfs` | GTFS |
| `netex` | NeTEx |
| `other` | Inny format statyczny |

### Realtime protocols

| Wartosc | Znaczenie |
|---------|-----------|
| `gtfs_rt` | GTFS Realtime |
| `siri` | SIRI |
| `gbfs` | General Bikeshare Feed Specification |

### Realtime endpoint types

| Protocol | Dozwolone `endpoint_type` |
|----------|---------------------------|
| `gtfs_rt` | `trip_update`, `vehicle_position`, `service_alert` |
| `siri` | `sx`, `sm`, `vm`, `et`, `gm` |
| `gbfs` | `gbfs`, `gbfs_versions`, `system_information`, `vehicle_types`, `station_information`, `station_status`, `free_bike_status`, `system_hours`, `system_alerts` |

### Auth types dla zrodel feedow

| Wartosc | Znaczenie |
|---------|-----------|
| `null` | Brak autoryzacji |
| `api_key` | Klucz API |
| `bearer_token` | Bearer token |
| `basic_auth` | Basic auth, `auth_value` w formacie `username:password` |

### Etapy feedow statycznych

| Etap | Etykieta | Znaczenie |
|------|----------|-----------|
| `1` | `Step 1: Upload data` | Dane wymagaja poprawki lub brak historii |
| `2` | `Step 2: Data verification` | Dane przeslane, trwa/wynikla walidacja |
| `3` | `Step 3: Admin confirmation` | Dane gotowe do potwierdzenia |
| `4` | `Step 4: Complete` | Feed opublikowany |

### Etapy feedow realtime

| Etap | Etykieta | Znaczenie |
|------|----------|-----------|
| `1` | `Step 1: Endpoints` | Endpointy dodane / do poprawy |
| `2` | `Step 2: Validated` | Endpointy zwalidowane |
| `3` | `Step 3: Admin confirmation` | Gotowe do potwierdzenia |
| `4` | `Step 4: Published` | Realtime feed opublikowany |

### Historia

Feed statyczny ma `FeedSubmissionHistory`, realtime ma `RealtimeSubmissionHistory`.

| `event_type` | Znaczenie |
|--------------|-----------|
| `uploaded` | Utworzenie lub ponowny upload danych |
| `stage_advanced` | Przejscie na kolejny etap |
| `rejected` | Odrzucenie; `cause` zawiera powod |
| `completed` | Publikacja, etap 4 |

---

## 11. Workflow Feedow

### Feed statyczny

```text
DataProvider
  -> POST /api/data_manager/feed-submissions/
  -> backend zapisuje FeedSubmission + StaticFeedEntry
  -> backend tworzy historie uploaded, zwykle stage_after=2
  -> walidacja/fetch moze przesunac etap albo odrzucic
  -> Helper/Admin PATCH { "stage": 4 }
  -> feed pojawia sie publicznie w /api/data_manager/feeds/ i /feed/{id}/
```

Jesli feed zostanie odrzucony:

```text
Helper/Admin PATCH { "rejection_cause": "..." }
  -> is_rejected=true
  -> current_stage_label="Rejected"
  -> DataProvider widzi powod w szczegolach
```

### Feed realtime

```text
DataProvider
  -> POST /api/data_manager/realtime-submissions/
  -> backend zapisuje RealtimeSubmission + RealtimeEndpointRT[]
  -> task walidacji realtime
  -> Helper/Admin PATCH { "stage": 4 }
  -> realtime pojawia sie publicznie w katalogu feedow i /feed/rt/{id}/
```

### Kto co widzi

| Uzytkownik | Lista `feed-submissions` | Lista `realtime-submissions` |
|------------|--------------------------|------------------------------|
| `DataProvider` | Tylko wlasne | Tylko wlasne |
| `Helper` | Wszystkie | Wszystkie |
| `Admin` | Wszystkie | Wszystkie |
| Anonim | Brak dostepu | Brak dostepu |

---

## 12. Uwagi Dla Frontendu

### Obsluga tokenow

- Trzymaj `access` krotkozyjaco i odswiezaj przez `/api/auth/token/refresh/`.
- Po `401` sprobuj refresh, a dopiero potem wyloguj uzytkownika.
- Do endpointow publicznych nie trzeba wysylac tokenu.

### UI wedlug rol

Frontend powinien dostac informacje o rolach uzytkownika z osobnego endpointu profilu, ktorego aktualnie nie ma. Do czasu dodania takiego endpointu role musza byc znane frontendowi z procesu logowania poza API albo nalezy dodac endpoint typu `/api/me/`.

Rekomendowana logika UI:

- `Blogger`/`Admin` - pokazuj panel bloga.
- `DataProvider`/`Admin` - pokazuj formularze dodawania feedow.
- `Helper`/`Admin` - pokazuj kolejke feedow do potwierdzenia oraz panel case/organizacji.

### Statusy HTTP

Najczestsze odpowiedzi:

| Status | Znaczenie |
|--------|-----------|
| `200` | OK |
| `201` | Utworzono |
| `204` | Brak tresci, np. usuniecie reakcji |
| `400` | Niepoprawne dane requestu |
| `401` | Brak lub niewazny token |
| `403` | Token poprawny, ale brak wymaganej roli/uprawnienia |
| `404` | Obiekt nie istnieje albo nie jest dostepny publicznie |
| `429` | Limit reakcji po IP |

### Multipart nested fields

Dla uploadu statycznego pliku backend przyjmuje zagniezdzone pole pliku jako:

```text
static_entry.file
```

Analogicznie mozna przesylac:

```text
static_entry.is_original
static_entry.license
```

### Wazne ograniczenia

- `auth_value` nigdy nie jest zwracane przez API.
- Publiczne feedy sa widoczne tylko przy `current_stage=4`.
- Publiczny endpoint `/api/data_manager/feeds/` zwraca organizacje, nie surowa liste feedow.
- `Helper` moze potwierdzac feedy, ale nie tworzy feedow jako provider.
- `DataProvider` moze tworzyc feedy, ale nie moze ich potwierdzac.
- `Admin` ma dostep do wszystkiego.
- Obecnie nie ma osobnego endpointu `/api/me/`, wiec frontend nie ma natywnego API do pobrania roli zalogowanego uzytkownika.

