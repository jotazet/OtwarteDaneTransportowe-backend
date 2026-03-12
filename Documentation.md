# OtwarteDaneTransportowe – Dokumentacja systemu zarządzania feedami

## Spis treści

1. [Przegląd architektury](#1-przegląd-architektury)
2. [Modele danych](#2-modele-danych)
3. [API – endpointy](#3-api--endpointy)
4. [Workflow dodawania feeda](#4-workflow-dodawania-feeda)
5. [Przechowywanie plików](#5-przechowywanie-plików)
6. [Mechanizm odświeżania feedów (proxy/cache)](#6-mechanizm-odświeżania-feedów-proxycache)
7. [Udostępnianie feedów użytkownikom końcowym](#7-udostępnianie-feedów-użytkownikom-końcowym)
8. [Bezpieczeństwo i kontrola dostępu](#8-bezpieczeństwo-i-kontrola-dostępu)
9. [Panel administracyjny](#9-panel-administracyjny)
10. [Typy danych i protokoły](#10-typy-danych-i-protokoły)

---

## 1. Przegląd architektury

System dzieli się na dwa główne obszary funkcjonalne:

```
┌─────────────────────────────────────────────────────────────────┐
│  PRYWATNE (wymaga logowania)                                     │
│                                                                  │
│  Użytkownik → POST /api/data_manager/my-feed-submissions/       │
│             → GET  /api/data_manager/my-feed-submissions/       │
│             → GET  /api/data_manager/my-feed-submissions/{id}/  │
│                                                                  │
│  Admin      → POST /api/data_manager/my-feed-submissions/{id}/  │
│                    advance-stage/                                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  PUBLICZNE (tylko zatwierdzone feedy, etap 4)                   │
│                                                                  │
│  Każdy      → GET /api/data_manager/feeds/                      │
│             → GET /api/data_manager/feeds/{id}/                 │
│             → GET /api/data_manager/feeds/download/static/{pk}/ │
│             → GET /api/data_manager/feeds/download/realtime/{pk}│
└─────────────────────────────────────────────────────────────────┘
```

Kluczowa zasada: **feed jest widoczny publicznie tylko po przejściu wszystkich 4 etapów zatwierdzenia**, w tym ręcznego potwierdzenia przez admina.

---

## 2. Modele danych

### 2.1 `FeedSubmission` — główny rekord zgłoszenia

Każde zgłoszenie feeda przez użytkownika tworzy jeden rekord `FeedSubmission`.

| Pole | Typ | Opis |
|------|-----|------|
| `transport_organization` | FK → `TransportOrganization` | Organizacja transportowa, której dotyczy feed |
| `submitted_by` | FK → `User` | Użytkownik, który dodał feed |
| `data_type` | `CharField` | Typ danych: `gtfs`, `netex`, `gbfs`, `siri`, `gtfs_rt`, `other` |
| `feed_kind` | `CharField` | Wypełniany automatycznie: `static` lub `dynamic` |
| `name` | `CharField` | Opcjonalna nazwa własna feeda |
| `note` | `TextField` | Opcjonalna notatka |
| `created_at` | `DateTimeField` | Data utworzenia zgłoszenia |
| `updated_at` | `DateTimeField` | Data ostatniej modyfikacji |
| `stage_upload_at` | `DateTimeField` | Etap 1: data przesłania danych (ustawiana automatycznie przy tworzeniu) |
| `stage_verification_at` | `DateTimeField` | Etap 2: data weryfikacji danych |
| `stage_confirmation_at` | `DateTimeField` | Etap 3: data potwierdzenia przez admina |
| `stage_complete_at` | `DateTimeField` | Etap 4: data zakończenia procesu (feed staje się publiczny) |

**Właściwości obliczane:**

- `current_stage` → liczba `0–4` oznaczająca aktualny etap
- `current_stage_label` → czytelna etykieta etapu

**Automatyczna logika:**
- `feed_kind` jest **zawsze** ustawiane przez `clean()` na podstawie `data_type`:
  - `gtfs_rt`, `siri` → `dynamic`
  - wszystkie inne → `static`
- `stage_upload_at` jest ustawiane automatycznie przy tworzeniu zgłoszenia (w serializerze)

---

### 2.2 `StaticFeedEntry` — dane statyczne (GTFS, NeTEx, GBFS, inne)

Powiązany relacją `OneToOne` z `FeedSubmission`. Jeden feed statyczny = jeden rekord.

| Pole | Typ | Opis |
|------|-----|------|
| `submission` | `OneToOneField` → `FeedSubmission` | Powiązane zgłoszenie |
| `url` | `URLField` | URL źródłowy feeda (wyklucza się z `file`) |
| `file` | `FileField` | Plik przesłany ręcznie przez użytkownika (wyklucza się z `url`) |
| `cached_file` | `FileField` | Kopia pobrana automatycznie przez serwer (wypełniana przez scheduler) |
| `cached_at` | `DateTimeField` | Kiedy serwer ostatnio pobrał kopię |
| `hide_original` | `BooleanField` | Czy ukrywać oryginalny URL (serwer działa jako proxy) |
| `auth_type` | `CharField` | Typ autentykacji: `none`, `api_key`, `bearer_token`, `basic_auth` |
| `auth_value` | `CharField` | Wartość klucza/tokenu/hasła (nigdy nie zwracana w API) |
| `download_time_1` | `TimeField` | Godzina pierwszego dziennego pobierania (UTC) |
| `download_time_2` | `TimeField` | Godzina drugiego dziennego pobierania (opcjonalnie, dla feedów 2x dziennie) |
| `uploaded_at` | `DateTimeField` | Data dodania wpisu |

**Zasady walidacji (`clean()`):**
- `url` XOR `file` — dokładnie jedno z dwóch musi być wypełnione, nie oba
- Jeśli `file`: pola `hide_original`, `download_time_*`, `cached_*` są niedozwolone
- Jeśli `url` + `hide_original=True`: `auth_type` musi być różny od `none`
- `cached_file` jest zarządzany wyłącznie przez serwer, nigdy przez użytkownika

**Ścieżki plików:**
```
# Plik ręcznie przesłany przez użytkownika:
MEDIA_ROOT/uploaded_data/{user_id}/{org_id}/static/{original_filename}

# Plik pobrany automatycznie przez serwer (proxy):
MEDIA_ROOT/uploaded_data/{user_id}/{org_id}/static/{original_filename}
```
Oba typy pliku lądują w tym samym katalogu. `OverwriteStorage` gwarantuje, że nowe pobranie nadpisuje stary plik, zachowując oryginalną nazwę.

---

### 2.3 `RealtimeFeedEntry` — kontener danych real-time (GTFS-RT, SIRI)

Powiązany relacją `OneToOne` z `FeedSubmission`. Łączy oba protokoły w jednej klasie.

| Pole | Typ | Opis |
|------|-----|------|
| `submission` | `OneToOneField` → `FeedSubmission` | Powiązane zgłoszenie |
| `protocol` | `CharField` | `gtfs_rt` lub `siri` |
| `uploaded_at` | `DateTimeField` | Data dodania |

**Walidacja:** `protocol` musi być zgodny z `data_type` powiązanego `FeedSubmission`.

---

### 2.4 `RealtimeEndpoint` — pojedynczy endpoint real-time

Powiązany relacją `ForeignKey` z `RealtimeFeedEntry`. Jeden endpoint = jeden URL jednego typu.

| Pole | Typ | Opis |
|------|-----|------|
| `entry` | FK → `RealtimeFeedEntry` | Kontener nadrzędny |
| `endpoint_type` | `CharField` | Typ endpointu (patrz tabela niżej) |
| `url` | `URLField` | URL endpointu |
| `hide_original` | `BooleanField` | Czy ukrywać oryginalny URL |
| `cached_file` | `FileField` | Lokalnie zapisana kopia feeda (dla proxy) |
| `cached_at` | `DateTimeField` | Kiedy ostatnio pobrano |
| `auth_type` | `CharField` | Typ autentykacji |
| `auth_value` | `CharField` | Wartość klucza/tokenu/hasła (write-only w API) |

**Dozwolone typy endpointów per protokół:**

| Protokół | `endpoint_type` | Opis |
|----------|----------------|------|
| GTFS-RT | `trip_update` | Aktualizacje podróży (opóźnienia, zmiany trasy) |
| GTFS-RT | `vehicle_position` | Pozycje pojazdów w czasie rzeczywistym |
| GTFS-RT | `service_alert` | Alerty serwisowe (zakłócenia, komunikaty) |
| SIRI | `sx` | Situation Exchange – zakłócenia i alerty |
| SIRI | `sm` | Stop Monitoring – odjazdyreal-time na przystankach |
| SIRI | `vm` | Vehicle Monitoring – pozycje pojazdów |
| SIRI | `et` | Estimated Timetable – przewidywane rozkłady |
| SIRI | `gm` | General Message – wiadomości ogólne |

**Ograniczenia:**
- `unique_together = (entry, endpoint_type)` — jeden typ endpointu na entry
- Jeśli `hide_original=True`: `auth_type` musi być różny od `none`

**Ścieżka pliku cached:**
```
MEDIA_ROOT/uploaded_data/{user_id}/{org_id}/realtime/{endpoint_type}/{original_filename}
```

---

## 3. API – endpointy

### 3.1 Prywatne (wymagają uwierzytelnienia)

#### `GET /api/data_manager/my-feed-submissions/`
Lista zgłoszeń **zalogowanego użytkownika** (tylko jego własne, wszystkie etapy).

**Parametry query:**
- `?data_type=gtfs` — filtr po typie danych
- `?feed_kind=static` — filtr po rodzaju feeda

**Odpowiedź (200):**
```json
[
  {
    "id": 1,
    "transport_organization": 42,
    "data_type": "gtfs",
    "feed_kind": "static",
    "name": "Rozkład MPK Kraków",
    "created_at": "2026-03-01T10:00:00Z",
    "updated_at": "2026-03-01T10:00:00Z",
    "current_stage": 2,
    "current_stage_label": "Step 2: Data verification"
  }
]
```

---

#### `POST /api/data_manager/my-feed-submissions/`
Stworzenie nowego zgłoszenia feeda.

**Uwierzytelnienie:** wymagane (sesja lub token)

**Ciało żądania — feed statyczny (URL z proxy):**
```json
{
  "transport_organization": 42,
  "data_type": "gtfs",
  "name": "Rozkład MPK Kraków",
  "note": "Aktualizowany codziennie o 3:00 i 15:00 UTC",
  "static_entry": {
    "url": "https://mpk.krakow.pl/gtfs.zip",
    "hide_original": true,
    "auth_type": "api_key",
    "auth_value": "secret-key-123",
    "download_time_1": "03:00:00",
    "download_time_2": "15:00:00"
  }
}
```

**Ciało żądania — feed statyczny (upload pliku):**
```
Content-Type: multipart/form-data

transport_organization: 42
data_type: gtfs
name: Rozkład MPK Kraków
static_entry.file: [plik .zip]
```

**Ciało żądania — GTFS-RT:**
```json
{
  "transport_organization": 42,
  "data_type": "gtfs_rt",
  "name": "RT MPK Kraków",
  "realtime_entry": {
    "protocol": "gtfs_rt",
    "endpoints": [
      {
        "endpoint_type": "trip_update",
        "url": "https://rt.mpk.krakow.pl/TripUpdates",
        "hide_original": false,
        "auth_type": "none"
      },
      {
        "endpoint_type": "vehicle_position",
        "url": "https://rt.mpk.krakow.pl/VehiclePositions",
        "hide_original": true,
        "auth_type": "bearer_token",
        "auth_value": "Bearer eyJ..."
      }
    ]
  }
}
```

**Ciało żądania — SIRI:**
```json
{
  "transport_organization": 42,
  "data_type": "siri",
  "name": "SIRI MPK Kraków",
  "realtime_entry": {
    "protocol": "siri",
    "endpoints": [
      {
        "endpoint_type": "vm",
        "url": "https://siri.mpk.krakow.pl/vm",
        "hide_original": false,
        "auth_type": "none"
      },
      {
        "endpoint_type": "sx",
        "url": "https://siri.mpk.krakow.pl/sx",
        "hide_original": true,
        "auth_type": "api_key",
        "auth_value": "my-key"
      }
    ]
  }
}
```

**Odpowiedź (201):** pełny obiekt `FeedSubmission` z zagnieżdżonym entry.

**Reguły walidacji:**
- `data_type = gtfs_rt` lub `siri` → wymagane `realtime_entry`, zakazane `static_entry`
- wszystkie inne `data_type` → wymagane `static_entry`, zakazane `realtime_entry`
- dla GTFS-RT: dozwolone typy endpointów: `trip_update`, `vehicle_position`, `service_alert`
- dla SIRI: dozwolone typy endpointów: `sx`, `sm`, `vm`, `et`, `gm`
- brak duplikatów `endpoint_type` w jednym zgłoszeniu

---

#### `GET /api/data_manager/my-feed-submissions/{id}/`
Szczegóły konkretnego zgłoszenia zalogowanego użytkownika.

Zwraca pełne dane: wszystkie etapy, dane entry, `auth_type` (ale **nie** `auth_value`).

**Błędy:**
- `403 Forbidden` — próba dostępu do zgłoszenia innego użytkownika
- `404 Not Found` — zgłoszenie nie istnieje

---

#### `POST /api/data_manager/my-feed-submissions/{id}/advance-stage/`
**Tylko admin.** Przesuwa zgłoszenie do następnego etapu.

Ustawia odpowiedni znacznik czasowy w zależności od aktualnego etapu:

| Aktualny etap | Ustawiane pole | Nowy etap |
|--------------|---------------|-----------|
| 0 | `stage_upload_at` | 1 |
| 1 | `stage_verification_at` | 2 |
| 2 | `stage_confirmation_at` | 3 |
| 3 | `stage_complete_at` | 4 → **feed staje się publiczny** |

**Błędy:**
- `400 Bad Request` — zgłoszenie już na etapie 4
- `403 Forbidden` — użytkownik nie jest adminem

---

### 3.2 Publiczne (bez uwierzytelnienia, tylko etap 4)

#### `GET /api/data_manager/feeds/`
Lista wszystkich w pełni zatwierdzonych feedów.

**Parametry query:**
- `?transport_organization=42`
- `?data_type=gtfs`
- `?feed_kind=static`

**Odpowiedź (200):**
```json
[
  {
    "id": 1,
    "organization_name": "MPK Kraków",
    "organization_region": "małopolskie",
    "data_type": "gtfs",
    "feed_kind": "static",
    "name": "Rozkład MPK Kraków",
    "created_at": "2026-03-01T10:00:00Z",
    "updated_at": "2026-03-10T03:01:22Z",
    "published_at": "2026-03-05T12:00:00Z",
    "static_feed": {
      "download_url": "https://example.com/api/data_manager/feeds/download/static/7/",
      "uploaded_at": "2026-03-01T10:00:00Z",
      "cached_at": "2026-03-10T03:01:22Z"
    },
    "realtime_feed": null
  }
]
```

**Pola nigdy nie zwracane publicznie:** `auth_value`, `auth_type`, `url` (gdy `hide_original=True`), `stage_upload_at`, `stage_verification_at`, `stage_confirmation_at`, `submitted_by`.

---

#### `GET /api/data_manager/feeds/{id}/`
Szczegóły konkretnego opublikowanego feeda. Identyczna struktura jak w liście.

---

#### `GET /api/data_manager/feeds/download/static/{pk}/`
Bezpieczne pobieranie pliku feeda statycznego.

- Sprawdza czy `stage_complete_at` jest ustawione — jeśli nie: `404`
- Jeśli `hide_original=True`: serwuje `cached_file` (kopia pobrana przez serwer)
- Jeśli `hide_original=False`: nie ma pliku do serwowania (URL jest zwracany bezpośrednio w `download_url`)
- Jeśli feed pochodzi z ręcznego uploadu: serwuje `file`
- Odpowiedź: `FileResponse` z nagłówkiem `Content-Disposition: attachment`

---

#### `GET /api/data_manager/feeds/download/realtime/{pk}/`
Bezpieczne pobieranie pliku z cachowanego endpointu realtime.

- `{pk}` to ID `RealtimeEndpoint`, nie `FeedSubmission`
- Sprawdza `stage_complete_at` na `endpoint.entry.submission`
- Serwuje `cached_file` endpointu
- Jeśli brak pliku: `404`

---

## 4. Workflow dodawania nowego feeda

### Krok 1 – Przesłanie danych przez użytkownika

```
Użytkownik (zalogowany)
    │
    ▼
POST /api/data_manager/my-feed-submissions/
    │
    ├─ Walidacja serializera (typy, wymagane pola, protokoły)
    ├─ Walidacja modelu StaticFeedEntry.clean() lub RealtimeEndpoint.clean()
    ├─ Zapis FeedSubmission do bazy
    ├─ Zapis StaticFeedEntry lub RealtimeFeedEntry + RealtimeEndpoint(s) do bazy
    └─ Automatyczne ustawienie stage_upload_at = now()
            │
            ▼
    current_stage = 1 ("Step 1: Data uploaded")
```

W tym momencie feed jest **widoczny tylko dla właściciela** w `/my-feed-submissions/`.

---

### Krok 2 – Weryfikacja danych (admin)

Admin w panelu Django lub przez API wywołuje `advance-stage` dla danego zgłoszenia.

```
Admin
    │
    ▼
POST /api/data_manager/my-feed-submissions/{id}/advance-stage/
    │
    └─ stage_verification_at = now()
            │
            ▼
    current_stage = 2 ("Step 2: Data verification")
```

---

### Krok 3 – Potwierdzenie przez admina

```
Admin
    │
    ▼
POST /api/data_manager/my-feed-submissions/{id}/advance-stage/  (ponownie)
    │
    └─ stage_confirmation_at = now()
            │
            ▼
    current_stage = 3 ("Step 3: Admin confirmation")
```

---

### Krok 4 – Zakończenie procesu (feed staje się publiczny)

```
Admin
    │
    ▼
POST /api/data_manager/my-feed-submissions/{id}/advance-stage/  (ponownie)
    │
    └─ stage_complete_at = now()
            │
            ▼
    current_stage = 4 ("Step 4: Complete")
            │
            ▼
    Feed pojawia się w GET /api/data_manager/feeds/
    Feed jest dostępny do pobrania przez /feeds/download/...
```

---

### Diagram pełnego procesu

```
[Użytkownik]         [System]              [Admin]         [Publiczny dostęp]
     │                   │                    │                    │
     │── POST /my-... ──▶│                    │                    │
     │                   │── zapisz do bazy   │                    │
     │                   │── stage_upload_at  │                    │
     │◀── 201 Created ───│                    │                    │
     │                   │                    │                    │
     │                   │◀── advance-stage ──│                    │
     │                   │── stage_verification_at                 │
     │                   │                    │                    │
     │                   │◀── advance-stage ──│                    │
     │                   │── stage_confirmation_at                 │
     │                   │                    │                    │
     │                   │◀── advance-stage ──│                    │
     │                   │── stage_complete_at│                    │
     │                   │                    │        ◀── GET /feeds/ ──│
     │                   │────────────────────────────── 200 OK ──▶│
```

---

## 5. Przechowywanie plików

### Struktura katalogów

```
MEDIA_ROOT/                          ← /app/uploaded_data/
└── uploaded_data/
    └── {user_id}/                   ← ID użytkownika Django
        └── {org_id}/                ← ID TransportOrganization
            ├── static/
            │   └── gtfs.zip         ← plik użytkownika LUB kopia serwera
            └── realtime/
                ├── trip_update/
                │   └── feed.pb      ← kopia serwera (GTFS-RT)
                ├── vehicle_position/
                │   └── feed.pb
                ├── sx/
                │   └── feed.xml     ← kopia serwera (SIRI SX)
                └── vm/
                    └── feed.xml
```

### Zasady

| Scenariusz | Pole | Lokalizacja |
|------------|------|-------------|
| Użytkownik przesyła plik | `StaticFeedEntry.file` | `static/{original_filename}` |
| Serwer pobiera z URL (proxy) | `StaticFeedEntry.cached_file` | `static/{original_filename}` |
| Serwer pobiera RT (proxy) | `RealtimeEndpoint.cached_file` | `realtime/{endpoint_type}/{filename}` |

**`OverwriteStorage`** — przy każdym zapisie pod tą samą ścieżką stary plik jest usuwany i zastępowany nowym. Dzięki temu:
- oryginalna nazwa pliku jest zawsze zachowana
- nie ma duplikatów z suffiksami (`gtfs_1.zip`, `gtfs_2.zip` itp.)
- scheduler zawsze nadpisuje najnowszą wersją

### Dostęp do katalogu

- `MEDIA_URL = '/internal-media/'` — nie ma żadnej reguły URL w `urlpatterns` serwującej ten prefiks
- Nginx/serwer webowy **nie powinien** udostępniać katalogu `MEDIA_ROOT` bezpośrednio
- Jedyna droga do pliku: widoki `StaticFeedDownloadView` / `RealtimeFeedDownloadView`, które weryfikują `stage_complete_at`

---

## 6. Mechanizm odświeżania feedów (proxy/cache)

### Kiedy jest potrzebny

Gdy użytkownik przy dodawaniu feeda ustawi `hide_original = True`. System wtedy:
1. Nie udostępnia oryginalnego URL publicznie
2. Sam pobiera plik z URL (z opcjonalną autentykacją)
3. Zapisuje go lokalnie w `MEDIA_ROOT`
4. Udostępnia własny URL do pobrania

### Harmonogram pobierania (do zaimplementowania — scheduler)

Pola `download_time_1` i `download_time_2` w `StaticFeedEntry` przechowują godziny pobierania (UTC). Scheduler (np. Celery Beat lub cron) powinien:

```python
# Pseudokod schedulera

from data_manager.models import StaticFeedEntry, RealtimeEndpoint
from django.utils import timezone
from datetime import time
import requests

def refresh_static_feeds():
    """Uruchamiać codziennie co godzinę lub w minutach pasujących do download_time_1/2."""
    now = timezone.now().time().replace(second=0, microsecond=0)

    entries = StaticFeedEntry.objects.filter(
        hide_original=True,
        url__isnull=False,
        submission__stage_complete_at__isnull=False  # tylko zatwierdzone
    ).filter(
        models.Q(download_time_1=now) | models.Q(download_time_2=now)
    )

    for entry in entries:
        headers = _build_auth_headers(entry.auth_type, entry.auth_value)
        response = requests.get(entry.url, headers=headers, timeout=60)
        if response.ok:
            filename = entry.url.rstrip('/').split('/')[-1] or 'feed.zip'
            entry.cached_file.save(filename, ContentFile(response.content), save=False)
            entry.cached_at = timezone.now()
            StaticFeedEntry.objects.filter(pk=entry.pk).update(
                cached_file=entry.cached_file.name,
                cached_at=entry.cached_at
            )

def _build_auth_headers(auth_type, auth_value):
    if auth_type == 'api_key':
        return {'X-API-Key': auth_value}
    if auth_type == 'bearer_token':
        return {'Authorization': f'Bearer {auth_value}'}
    if auth_type == 'basic_auth':
        user, pwd = auth_value.split(':', 1)
        # użyj requests.auth.HTTPBasicAuth(user, pwd)
    return {}
```

Dla `RealtimeEndpoint` z `hide_original=True` logika jest analogiczna, ale harmonogram jest ciągły (GTFS-RT odświeżany co kilkanaście sekund/minut), bez pól `download_time_*` — do konfiguracji osobno w schedulerze.

### Autentykacja przy pobieraniu

| `auth_type` | Sposób użycia |
|-------------|---------------|
| `none` | Brak nagłówków autoryzacji |
| `api_key` | Nagłówek `X-API-Key: {auth_value}` |
| `bearer_token` | Nagłówek `Authorization: Bearer {auth_value}` |
| `basic_auth` | HTTP Basic Auth, `auth_value` w formacie `username:password` |

---

## 7. Udostępnianie feedów użytkownikom końcowym

### Publiczny feed — co widzi świat

Po osiągnięciu etapu 4 feed pojawia się w `/api/data_manager/feeds/`. Publicznie dostępne są **wyłącznie**:

| Pole | Źródło |
|------|--------|
| `id` | `FeedSubmission.id` |
| `organization_name` | `TransportOrganization.transport_organization` |
| `organization_region` | `TransportOrganization.region` |
| `data_type` | `FeedSubmission.data_type` |
| `feed_kind` | `FeedSubmission.feed_kind` |
| `name` | `FeedSubmission.name` |
| `created_at` | `FeedSubmission.created_at` |
| `updated_at` | `FeedSubmission.updated_at` |
| `published_at` | `FeedSubmission.stage_complete_at` |
| `static_feed.download_url` | Wygenerowany URL do bezpiecznego pobrania |
| `static_feed.cached_at` | Kiedy plik był ostatnio odświeżony |
| `realtime_feed.endpoints[].endpoint_type` | Typ endpointu |
| `realtime_feed.endpoints[].feed_url` | URL endpointu lub link do proxy |

### Logika `download_url` / `feed_url`

```
Jeśli hide_original = False:
    → zwracany jest oryginalny URL bezpośrednio (np. https://mpk.krakow.pl/gtfs.zip)
    → użytkownik pobiera plik bezpośrednio ze źródła

Jeśli hide_original = True AND cached_file istnieje:
    → zwracany jest URL do naszego widoku:
       https://example.com/api/data_manager/feeds/download/static/{pk}/
    → plik serwowany przez Django z MEDIA_ROOT

Jeśli hide_original = True AND cached_file nie istnieje (jeszcze nie pobrano):
    → zwracane jest null
```

---

## 8. Bezpieczeństwo i kontrola dostępu

### Warstwy ochrony

| Warstwa | Mechanizm |
|---------|-----------|
| Uwierzytelnienie API | `IsAuthenticated` (sesja lub token DRF) |
| Izolacja danych | `queryset` filtruje po `submitted_by=request.user` |
| Ochrona obiektów | `get_object()` sprawdza właściciela + podwójny guard przed admin bypass |
| Brak wycieku przez URL | `MEDIA_URL` nie jest podpięty w `urlpatterns` |
| Ochrona przed enumeracją plików | Pliki serwowane przez widok Django, który sprawdza `stage_complete_at` |
| Ukryte dane wrażliwe | `auth_value` w serializers: `write_only=True` — nigdy nie zwracany w odpowiedzi |
| Ukryty URL źródłowy | `hide_original=True` → publiczny serializer zwraca tylko `download_url` (nasz proxy), nigdy oryginalny URL |

### Macierz uprawnień

| Akcja | Właściciel | Admin | Anonimowy |
|-------|-----------|-------|-----------|
| Utwórz zgłoszenie | ✅ | ✅ | ❌ |
| Odczytaj swoje zgłoszenie | ✅ | ✅ | ❌ |
| Odczytaj cudze zgłoszenie | ❌ | ✅ | ❌ |
| Przesuń etap | ❌ | ✅ | ❌ |
| Odczytaj listę publiczną | ✅ | ✅ | ✅ |
| Pobierz zatwierdzony plik | ✅ | ✅ | ✅ |
| Pobierz niezatwierdzony plik | ❌ | ✅ | ❌ |
| Bezpośredni dostęp do MEDIA_ROOT przez URL | ❌ | ❌ | ❌ |

---

## 9. Panel administracyjny

Dostępny pod `/admin/`. Zarejestrowane modele:

### `FeedSubmission`
- Lista: organizacja, typ, rodzaj, etap, daty
- Filtry: `data_type`, `feed_kind`, `created_at`
- Wyszukiwanie: po nazwie, regionie, organizacji
- **Inline:** `StaticFeedEntryInline`, `RealtimeFeedEntryInline` (z linkiem do szczegółów)
- **Akcja masowa:** `Advance selected submissions to next stage` — przesuwa wybrane zgłoszenia o jeden etap

### `RealtimeFeedEntry` (osobna strona)
- Lista: protokół, zgłoszenie, data
- **Inline:** `RealtimeEndpointInline` — edycja wszystkich endpointów w jednym formularzu tabularycznym

---

## 10. Typy danych i protokoły

### Typy statyczne (`feed_kind = static`)

| `data_type` | Nazwa | Opis |
|-------------|-------|------|
| `gtfs` | GTFS | General Transit Feed Specification – rozkłady jazdy |
| `netex` | NeTEx | Network Timetable Exchange – europejski standard rozkładów |
| `gbfs` | GBFS | General Bikeshare Feed Specification – rowery i hulajnogi |
| `other` | Other | Inne formaty statyczne |

### Typy dynamiczne (`feed_kind = dynamic`)

| `data_type` | Nazwa | Opis |
|-------------|-------|------|
| `gtfs_rt` | GTFS-RT | GTFS Realtime – dane w czasie rzeczywistym do GTFS |
| `siri` | SIRI | Service Interface for Real-time Information – europejski standard RT |

### Relacja modeli (diagram)

```
TransportOrganization
        │ (1)
        │ (*)
  FeedSubmission ──── submitted_by ──── User
        │
        ├── (OneToOne) StaticFeedEntry
        │       ├── file              (FileField – upload użytkownika)
        │       ├── cached_file       (FileField – kopia serwera)
        │       └── url               (URLField – źródłowy URL)
        │
        └── (OneToOne) RealtimeFeedEntry
                └── (FK, 1..8) RealtimeEndpoint
                        ├── url           (URLField)
                        ├── endpoint_type (trip_update|vehicle_position|...|sx|sm|vm|et|gm)
                        └── cached_file   (FileField – kopia serwera)
```

