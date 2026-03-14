# OtwarteDaneTransportowe – Dokumentacja systemu zarządzania feedami

## Spis treści

1. [Przegląd architektury](#1-przegląd-architektury)
2. [Modele danych](#2-modele-danych)
3. [API – endpointy](#3-api--endpointy)
4. [Architektura widoków (ViewSety)](#4-architektura-widoków-viewsety)
5. [Workflow dodawania feeda](#5-workflow-dodawania-feeda)
6. [Przechowywanie plików](#6-przechowywanie-plików)
7. [Mechanizm odświeżania feedów (proxy/cache)](#7-mechanizm-odświeżania-feedów-proxycache)
8. [Udostępnianie feedów użytkownikom końcowym](#8-udostępnianie-feedów-użytkownikom-końcowym)
9. [Bezpieczeństwo i kontrola dostępu](#9-bezpieczeństwo-i-kontrola-dostępu)
10. [Panel administracyjny](#10-panel-administracyjny)
11. [Typy danych i protokoły](#11-typy-danych-i-protokoły)

---

## 1. Przegląd architektury

System dzieli się na dwa główne obszary funkcjonalne:

```
┌─────────────────────────────────────────────────────────────────┐
│  PRYWATNE (wymaga logowania)                                     │
│                                                                  │
│  Użytkownik → POST /api/data_manager/feed-submissions/          │
│             → GET  /api/data_manager/feed-submissions/          │
│             → GET  /api/data_manager/feed-submissions/{id}/     │
│             → GET  /api/data_manager/feed-submissions/{id}/     │
│                    history/                                      │
│             → GET  /api/data_manager/feed-submissions/{id}/     │
│                    fetch-errors/                                 │
│                                                                  │
│  Admin      → GET  /api/data_manager/admin/feed-submissions/    │
│             → GET  /api/data_manager/admin/feed-submissions/{id}/│
│             → POST /api/data_manager/admin/feed-submissions/    │
│                    {id}/advance-stage/                           │
│             → POST /api/data_manager/admin/feed-submissions/    │
│                    {id}/reject/                                  │
│             → GET  /api/data_manager/admin/feed-submissions/    │
│                    {id}/fetch-errors/                            │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  PUBLICZNE (tylko zatwierdzone feedy, etap 4)                   │
│                                                                  │
│  Każdy      → GET /api/data_manager/feeds/                      │
│             → GET /api/data_manager/feeds/{id}/                 │
│             → GET /api/data_manager/feeds/{id}/download/static/ │
│             → GET /api/data_manager/feeds/download/realtime/{pk}│
└─────────────────────────────────────────────────────────────────┘
```

Kluczowa zasada: **feed jest widoczny publicznie tylko po przejściu wszystkich 4 etapów zatwierdzenia**, w tym ręcznego potwierdzenia przez admina.

---

## 2. Modele danych

### 2.1 `FeedSubmission` — główny rekord zgłoszenia

Każde zgłoszenie feeda przez użytkownika tworzy jeden rekord `FeedSubmission`. **Model ten nie przechowuje żadnych pól etapów** — cała historia etapów i zdarzeń żyje wyłącznie w `FeedSubmissionHistory`. Aktualny etap i status są zawsze obliczane na podstawie ostatniego wpisu w historii.

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

**Właściwości obliczane (na podstawie `FeedSubmissionHistory`):**

- `current_stage` → liczba `0–4` — wyznaczana z `stage_after` ostatniego wpisu historii
- `current_stage_label` → czytelna etykieta etapu (np. `"Rejected"` gdy ostatni wpis ma `event_type='rejected'`)
- `is_rejected` → `True` jeśli ostatni wpis historii ma `event_type='rejected'`
- `rejection_cause` → wartość pola `cause` z ostatniego wpisu `rejected` w historii (lub `null`)
- `published_at` → `created_at` z wpisu historii o `event_type='completed'`

**Automatyczna logika:**
- `feed_kind` jest **zawsze** ustawiane przez `clean()` na podstawie `data_type`:
  - `gtfs_rt`, `siri`, `gbfs` → `dynamic`
  - wszystkie inne → `static`
- Po utworzeniu zgłoszenia system automatycznie tworzy pierwszy wpis `FeedSubmissionHistory` z `event_type='uploaded'`
- Odrzucone zgłoszenie jest **zamknięte** — użytkownik tworzy nowe zgłoszenie przez `POST /feed-submissions/`

---

### 2.2 `FeedSubmissionHistory` — historia zdarzeń zgłoszenia

Każde zdarzenie dotyczące zgłoszenia (przesłanie, zatwierdzenie kroku, odrzucenie, ukończenie) jest rejestrowane jako osobny, niezmienny rekord historii. Jest to **jedyne źródło prawdy** o aktualnym etapie i statusie zgłoszenia.

Dzięki tej strukturze pełna ścieżka audytu jest zawsze zachowana — nawet jeśli użytkownik po odrzuceniu złoży nowe zgłoszenie, historia poprzednich prób jest dostępna i niezmieniona.

| Pole | Typ | Opis |
|------|-----|------|
| `submission` | FK → `FeedSubmission` | Powiązane zgłoszenie |
| `event_type` | `CharField` | Typ zdarzenia: `uploaded`, `stage_advanced`, `rejected`, `completed` |
| `stage_before` | `IntegerField` | Etap zgłoszenia przed zdarzeniem (`0–4`) |
| `stage_after` | `IntegerField` | Etap zgłoszenia po zdarzeniu (`0–4`) |
| `actor` | FK → `User` | Kto wywołał zdarzenie (użytkownik lub admin) |
| `cause` | `TextField` | Powód odrzucenia — wypełniany tylko przez admina przy `rejected`; `null` w pozostałych przypadkach |
| `created_at` | `DateTimeField` | Data i czas zdarzenia (automatyczne, niezmienne) |

**Typy zdarzeń (`event_type`):**

| Wartość | Kto tworzy | Opis |
|---------|-----------|------|
| `uploaded` | System | Pierwsze przesłanie zgłoszenia przez użytkownika; `stage_before=0`, `stage_after=1` |
| `stage_advanced` | Admin | Admin zatwierdził krok — przesunięcie do kolejnego etapu (1→2, 2→3) |
| `rejected` | Admin | Admin odrzucił zgłoszenie; `cause` zawiera powód; `stage_before=stage_after`; zgłoszenie jest zamknięte |
| `completed` | Admin | Admin zatwierdził ostatni krok — feed staje się publiczny; `stage_before=3`, `stage_after=4` |

**Ważne zasady:**
- Rekordy historii są **tylko do zapisu** — raz utworzone nie mogą być modyfikowane ani usuwane
- Aktualny etap zgłoszenia = `stage_after` z **najnowszego** rekordu historii
- Zgłoszenie jest uznawane za odrzucone jeśli najnowszy rekord ma `event_type='rejected'`
- Zgłoszenie jest uznawane za publiczne jeśli istnieje rekord z `event_type='completed'`

---

### 2.3 `StaticFeedEntry` — dane statyczne (GTFS, NeTEx, inne)

Powiązany relacją `OneToOne` z `FeedSubmission`. Jeden feed statyczny = jeden rekord.

| Pole | Typ | Opis |
|------|-----|------|
| `submission` | `OneToOneField` → `FeedSubmission` | Powiązane zgłoszenie |
| `url` | `URLField` | URL źródłowy feeda (wyklucza się z `file`) |
| `file` | `FileField` | Plik przesłany ręcznie przez użytkownika (wyklucza się z `url`) |
| `is_original` | `BooleanField` | Czy plik pochodzi bezpośrednio od użytkownika (domyślnie `False`; auto `True` gdy użytkownik przesyła `file`) |
| `cached_file` | `FileField` | Kopia pobrana automatycznie przez serwer (wypełniana przez scheduler) |
| `cached_at` | `DateTimeField` | Kiedy serwer ostatnio pobrał kopię |
| `hide_original` | `BooleanField` | Czy ukrywać oryginalny URL (serwer działa jako proxy). Automatycznie ustawiane na `True` gdy `auth_type != none`. |
| `auth_type` | `CharField` | Typ autentykacji: `none`, `api_key`, `bearer_token`, `basic_auth` |
| `auth_value` | `CharField` | Wartość klucza/tokenu/hasła (nigdy nie zwracana w API) |
| `download_time_1` | `TimeField` | Godzina pierwszego dziennego pobierania (UTC) |
| `download_time_2` | `TimeField` | Godzina drugiego dziennego pobierania (opcjonalnie, dla feedów 2x dziennie) |
| `license` | `CharField(255)` | Licencja danych (np. `CC BY 4.0`, `ODbL`) — opcjonalna |
| `uploaded_at` | `DateTimeField` | Data dodania wpisu |

**Zasady walidacji (`clean()`):**
- `url` XOR `file` — dokładnie jedno z dwóch musi być wypełnione, nie oba
- Jeśli `file`: pola `hide_original`, `download_time_*`, `cached_*` są niedozwolone
- Jeśli `url` + `hide_original=True`: `auth_type` musi być różny od `none`
- Jeśli `auth_type != none`: `hide_original` jest automatycznie ustawiane na `True`
- `cached_file` jest zarządzany wyłącznie przez serwer, nigdy przez użytkownika
- `is_original` jest ustawiane automatycznie na `True` gdy pole `file` jest wypełnione przez użytkownika

**Ścieżki plików:**
```
MEDIA_ROOT/uploaded_data/{user_id}/{org_id}/static/{original_filename}
```
`OverwriteStorage` gwarantuje, że nowe pobranie nadpisuje stary plik, zachowując oryginalną nazwę.

---

### 2.4 `RealtimeFeedEntry` — kontener danych real-time (GTFS-RT, SIRI, GBFS)

Powiązany relacją `OneToOne` z `FeedSubmission`. Łączy wszystkie protokoły real-time w jednej klasie.

| Pole | Typ | Opis |
|------|-----|------|
| `submission` | `OneToOneField` → `FeedSubmission` | Powiązane zgłoszenie |
| `protocol` | `CharField` | `gtfs_rt`, `siri`, `gbfs` |
| `license` | `CharField(255)` | Licencja danych (np. `CC BY 4.0`, `ODbL`) — opcjonalna |
| `uploaded_at` | `DateTimeField` | Data dodania |

**Walidacja:** `protocol` musi być zgodny z `data_type` powiązanego `FeedSubmission`.

---

### 2.5 `RealtimeEndpoint` — pojedynczy endpoint real-time

Powiązany relacją `ForeignKey` z `RealtimeFeedEntry`. Jeden endpoint = jeden URL jednego typu.

| Pole | Typ | Opis |
|------|-----|------|
| `entry` | FK → `RealtimeFeedEntry` | Kontener nadrzędny |
| `endpoint_type` | `CharField` | Typ endpointu (patrz tabela niżej) |
| `url` | `URLField` | URL endpointu |
| `hide_original` | `BooleanField` | Czy ukrywać oryginalny URL. Automatycznie ustawiane na `True` gdy `auth_type != none`. |
| `cached_file` | `FileField` | Lokalnie zapisana kopia feeda (dla proxy) |
| `cached_at` | `DateTimeField` | Kiedy ostatnio pobrano |
| `is_original` | `BooleanField` | Czy dane są oryginalne (domyślnie `False`) |
| `interval` | `PositiveIntegerField` | Czas w sekundach co ile odświeżają się dane realtime (np. `30`, `60`). Wymagane. |
| `auth_type` | `CharField` | Typ autentykacji: `none`, `api_key`, `bearer_token`, `basic_auth` |
| `auth_value` | `CharField` | Wartość klucza/tokenu/hasła (write-only w API) |

**Dozwolone typy endpointów per protokół:**

| Protokół | `endpoint_type` | Opis |
|----------|----------------|------|
| GTFS-RT  | `trip_update` | Aktualizacje podróży (opóźnienia, zmiany trasy) |
| GTFS-RT  | `vehicle_position` | Pozycje pojazdów w czasie rzeczywistym |
| GTFS-RT  | `service_alert` | Alerty serwisowe (zakłócenia, komunikaty) |
| SIRI     | `sx` | Situation Exchange – zakłócenia i alerty |
| SIRI     | `sm` | Stop Monitoring – odjazdy real-time na przystankach |
| SIRI     | `vm` | Vehicle Monitoring – pozycje pojazdów |
| SIRI     | `et` | Estimated Timetable – przewidywane rozkłady |
| SIRI     | `gm` | General Message – wiadomości ogólne |
| GBFS     | `gbfs` | Główny plik GBFS |
| GBFS     | `gbfs_versions` | Wersje GBFS |
| GBFS     | `system_information` | Informacje o systemie |
| GBFS     | `vehicle_types` | Typy pojazdów |
| GBFS     | `station_information` | Informacje o stacjach |
| GBFS     | `station_status` | Status stacji |
| GBFS     | `free_bike_status` | Status wolnych pojazdów |
| GBFS     | `system_hours` | Godziny pracy systemu |
| GBFS     | `system_alerts` | Alerty systemowe |

**Ograniczenia:**
- `unique_together = (entry, endpoint_type)` — jeden typ endpointu na entry
- Jeśli `hide_original=True`: `auth_type` musi być różny od `none`
- Jeśli `auth_type != none`: `hide_original` jest automatycznie ustawiane na `True`

**Ścieżka pliku cached:**
```
MEDIA_ROOT/uploaded_data/{user_id}/{org_id}/realtime/{endpoint_type}/{original_filename}
```

---

### 2.6 `FeedFetchError` — błędy pobierania feedu

Każda nieudana próba pobrania pliku z URL — zarówno podczas pierwszej weryfikacji feeda przez scheduler, jak i podczas późniejszych cyklicznych odświeżeń — jest rejestrowana jako osobny, niezmienny rekord błędu. Błędy są powiązane bezpośrednio z `StaticFeedEntry` lub `RealtimeEndpoint`, których dotyczyło pobieranie.

| Pole | Typ | Opis |
|------|-----|------|
| `static_entry` | FK → `StaticFeedEntry` (nullable) | Statyczny wpis, którego dotyczy błąd (wyklucza się z `endpoint`) |
| `endpoint` | FK → `RealtimeEndpoint` (nullable) | Endpoint real-time, którego dotyczy błąd (wyklucza się z `static_entry`) |
| `error_type` | `CharField` | Kategoria błędu: `http_error`, `timeout`, `connection_error`, `invalid_content`, `auth_error` |
| `http_status_code` | `IntegerField` (nullable) | Kod HTTP odpowiedzi — wypełniany tylko przy `error_type='http_error'` |
| `message` | `TextField` | Szczegółowy komunikat błędu (np. treść wyjątku, opis) |
| `url_attempted` | `URLField` | URL, z którego próbowano pobrać plik (dla audytu) |
| `occurred_at` | `DateTimeField` | Data i czas wystąpienia błędu (automatyczne, niezmienne) |

**Kategorie błędów (`error_type`):**

| Wartość | Opis |
|---------|------|
| `http_error` | Serwer zwrócił kod błędu HTTP (4xx, 5xx); `http_status_code` jest wypełniony |
| `timeout` | Przekroczono limit czasu połączenia lub odpowiedzi |
| `connection_error` | Nie można nawiązać połączenia z serwerem (DNS, sieć) |
| `invalid_content` | Pobrano plik, ale jego zawartość jest nieprawidłowa (np. nie jest poprawnym ZIP/GTFS) |
| `auth_error` | Błąd autentykacji — nieprawidłowy klucz API, token lub dane Basic Auth |

**Ważne zasady:**
- Rekordy są **tylko do zapisu** — system tworzy je automatycznie, nigdy nie są edytowane ani usuwane przez API
- Dokładnie jedno z pól `static_entry` lub `endpoint` musi być wypełnione — nigdy oba jednocześnie
- Dostęp do listy błędów dla danego feeda mają: **właściciel zgłoszenia** oraz **admin**
- Błędy są widoczne niezależnie od etapu zgłoszenia — także przed zatwierdzeniem

---

## 3. API – endpointy

### 3.1 Prywatne użytkownika (wymagają uwierzytelnienia)

#### `GET /api/data_manager/feed-submissions/`
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
    "current_stage_label": "Step 2: Data verification",
    "is_rejected": false,
    "rejection_cause": null
  }
]
```

Jeśli feed zostanie odrzucony:
```json
{
  "id": 1,
  "current_stage": 2,
  "current_stage_label": "Rejected",
  "is_rejected": true,
  "rejection_cause": "Plik nie przeszedł walidacji formatu GTFS – brakuje pliku stops.txt."
}
```

> Po odrzuceniu zgłoszenie jest zamknięte. Użytkownik tworzy **nowe** zgłoszenie przez `POST /feed-submissions/`.

---

#### `POST /api/data_manager/feed-submissions/`
Stworzenie nowego zgłoszenia feeda.

**Uwierzytelnienie:** wymagane (sesja lub token)

**Ciało żądania — feed statyczny (URL z proxy):**
```json
{
  "transport_organization": 42,
  "data_type": "gtfs",
  "name": "Rozkład MPK Kraków",
  "static_entry": {
    "url": "https://mpk.krakow.pl/gtfs.zip",
    "auth_type": "api_key",
    "auth_value": "secret-key-123",
    "download_time_1": "03:00:00",
    "download_time_2": "15:00:00",
    "license": "CC BY 4.0"
  }
}
```

> Jeśli `auth_type != none`, pole `hide_original` jest automatycznie ustawiane na `True`.

**Ciało żądania — feed statyczny (upload pliku):**
```
Content-Type: multipart/form-data

transport_organization: 42
data_type: gtfs
static_entry.file: [plik .zip]
static_entry.license: CC BY 4.0
```

**Ciało żądania — GTFS-RT:**
```json
{
  "transport_organization": 42,
  "data_type": "gtfs_rt",
  "realtime_entry": {
    "protocol": "gtfs_rt",
    "license": "ODbL",
    "endpoints": [
      {
        "endpoint_type": "trip_update",
        "url": "https://rt.mpk.krakow.pl/TripUpdates",
        "auth_type": "none",
        "interval": 30
      },
      {
        "endpoint_type": "vehicle_position",
        "url": "https://rt.mpk.krakow.pl/VehiclePositions",
        "auth_type": "bearer_token",
        "auth_value": "Bearer eyJ...",
        "interval": 15
      }
    ]
  }
}
```

**Ciało żądania — GBFS:**
```json
{
  "transport_organization": 42,
  "data_type": "gbfs",
  "realtime_entry": {
    "protocol": "gbfs",
    "license": "CC BY 4.0",
    "endpoints": [
      {
        "endpoint_type": "gbfs",
        "url": "https://gbfs.nextbike.net/maps/gbfs/v2/nextbike_kk/gbfs.json",
        "auth_type": "none",
        "interval": 60
      }
    ]
  }
}
```

**Reguły walidacji:**
- `data_type = gtfs_rt`, `siri` lub `gbfs` → wymagane `realtime_entry`, zakazane `static_entry`
- wszystkie inne `data_type` → wymagane `static_entry`, zakazane `realtime_entry`
- pole `interval` jest **wymagane** dla każdego endpointu realtime

---

#### `GET /api/data_manager/feed-submissions/{id}/`
Szczegóły konkretnego zgłoszenia zalogowanego użytkownika.

**Błędy:**
- `403 Forbidden` — próba dostępu do zgłoszenia innego użytkownika
- `404 Not Found` — zgłoszenie nie istnieje

---

#### `PUT/PATCH /api/data_manager/feed-submissions/{id}/`
Edycja zgłoszenia — dostępna tylko gdy `current_stage = 1`.

**Błędy:** `403 Forbidden` — zgłoszenie jest na etapie > 1 lub odrzucone.

---

#### `DELETE /api/data_manager/feed-submissions/{id}/`
Usunięcie zgłoszenia — dostępne tylko gdy `current_stage = 1`.

**Błędy:** `403 Forbidden` — zgłoszenie jest na etapie > 1 lub odrzucone.

---

#### `GET /api/data_manager/feed-submissions/{id}/history/`
Pełna historia zdarzeń dla danego zgłoszenia.

**Odpowiedź (200):**
```json
[
  {
    "event_type": "uploaded",
    "stage_before": 0,
    "stage_after": 1,
    "actor": "jan.kowalski",
    "cause": null,
    "created_at": "2026-03-01T10:00:00Z"
  },
  {
    "event_type": "rejected",
    "stage_before": 2,
    "stage_after": 2,
    "actor": "admin",
    "cause": "Plik nie przeszedł walidacji formatu GTFS – brakuje pliku stops.txt.",
    "created_at": "2026-03-03T14:22:00Z"
  }
]
```

---

#### `GET /api/data_manager/feed-submissions/{id}/fetch-errors/`
Lista błędów pobierania pliku dla danego zgłoszenia. Dostępna dla **właściciela zgłoszenia** i **admina**.

Zwraca błędy ze wszystkich powiązanych źródeł: `StaticFeedEntry` i wszystkich `RealtimeEndpoint` należących do zgłoszenia.

**Parametry query:**
- `?error_type=http_error` — filtr po kategorii błędu
- `?source=static` lub `?source=realtime` — filtr po typie źródła

**Odpowiedź (200):**
```json
[
  {
    "id": 12,
    "source": "static",
    "static_entry": 7,
    "endpoint": null,
    "endpoint_type": null,
    "error_type": "http_error",
    "http_status_code": 403,
    "message": "403 Forbidden – serwer odrzucił żądanie.",
    "url_attempted": "https://mpk.krakow.pl/gtfs.zip",
    "occurred_at": "2026-03-10T03:01:22Z"
  },
  {
    "id": 15,
    "source": "realtime",
    "static_entry": null,
    "endpoint": 3,
    "endpoint_type": "vehicle_position",
    "error_type": "timeout",
    "http_status_code": null,
    "message": "ReadTimeout: timed out after 30s waiting for response.",
    "url_attempted": "https://rt.mpk.krakow.pl/VehiclePositions",
    "occurred_at": "2026-03-10T03:02:05Z"
  }
]
```

**Błędy:**
- `403 Forbidden` — próba dostępu do błędów cudzego zgłoszenia
- `404 Not Found` — zgłoszenie nie istnieje

---

### 3.2 Endpointy administracyjne (tylko admin)

#### `GET /api/data_manager/admin/feed-submissions/`
Lista **wszystkich** zgłoszeń w systemie.

**Parametry query:**
- `?data_type=gtfs`, `?feed_kind=static`, `?stage=2`, `?is_rejected=true`, `?transport_organization=42`

---

#### `GET /api/data_manager/admin/feed-submissions/{id}/`
Szczegóły dowolnego zgłoszenia.

---

#### `PUT/PATCH /api/data_manager/admin/feed-submissions/{id}/`
Edycja dowolnego zgłoszenia przez admina.

---

#### `DELETE /api/data_manager/admin/feed-submissions/{id}/`
Usunięcie dowolnego zgłoszenia przez admina.

---

#### `POST /api/data_manager/admin/feed-submissions/{id}/advance-stage/`
Przesuwa zgłoszenie do następnego etapu. Tworzy nowy wpis `FeedSubmissionHistory`.

| Aktualny etap | Nowy etap | Tworzony wpis historii |
|--------------|-----------|------------------------|
| 1 | 2 | `event_type='stage_advanced'`, `stage_before=1`, `stage_after=2` |
| 2 | 3 | `event_type='stage_advanced'`, `stage_before=2`, `stage_after=3` |
| 3 | 4 | `event_type='completed'`, `stage_before=3`, `stage_after=4` → **feed staje się publiczny** |

**Błędy:** `400` — etap 4 lub jest odrzucone; `403` — nie jest adminem.

---

#### `POST /api/data_manager/admin/feed-submissions/{id}/reject/`
Odrzuca zgłoszenie i zapisuje powód. Zamyka zgłoszenie.

**Ciało żądania:**
```json
{ "cause": "Plik nie przeszedł walidacji formatu GTFS – brakuje pliku stops.txt." }
```

**Odpowiedź (200):**
```json
{ "detail": "Submission rejected.", "cause": "Plik nie przeszedł..." }
```

---

#### `GET /api/data_manager/admin/feed-submissions/{id}/history/`
Historia zdarzeń dowolnego zgłoszenia.

---

#### `GET /api/data_manager/admin/feed-submissions/{id}/fetch-errors/`
Lista błędów pobierania dla dowolnego zgłoszenia (widok admina — bez ograniczeń własnościowych).

---

### 3.3 Publiczne (bez uwierzytelnienia, tylko etap 4)

#### `GET /api/data_manager/feeds/`
Lista wszystkich w pełni zatwierdzonych feedów.

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
    "published_at": "2026-03-05T12:00:00Z",
    "static_feed": {
      "download_url": "https://example.com/api/data_manager/feeds/1/download/static/",
      "license": "CC BY 4.0",
      "cached_at": "2026-03-10T03:01:22Z"
    },
    "realtime_feed": null
  }
]
```

**Pola nigdy nie zwracane publicznie:** `auth_value`, `auth_type`, `url` (gdy `hide_original=True`), `submitted_by`, `rejection_cause`.

---

#### `GET /api/data_manager/feeds/{id}/download/static/`
Bezpieczne pobieranie pliku feeda statycznego. Weryfikuje wpis `completed` w historii.

---

#### `GET /api/data_manager/feeds/download/realtime/{pk}/`
Bezpieczne pobieranie pliku z cachowanego endpointu realtime (`{pk}` = ID `RealtimeEndpoint`).

---

## 4. Architektura widoków (ViewSety)

Wszystkie widoki API są oparte na **DRF ViewSetach** rejestrowanych przez `DefaultRouter`. Niestandardowe akcje są dodawane dekoratorami `@action`.

### 4.1 Przegląd ViewSetów

| ViewSet | Klasa bazowa | Router prefix | Dla kogo |
|---------|-------------|---------------|----------|
| `FeedSubmissionViewSet` | `ModelViewSet` | `feed-submissions` | Zalogowany użytkownik (własne zgłoszenia) |
| `AdminFeedSubmissionViewSet` | `ModelViewSet` | `admin/feed-submissions` | Tylko admin (wszystkie zgłoszenia) |
| `PublicFeedViewSet` | `ReadOnlyModelViewSet` | `feeds` | Wszyscy (tylko etap 4) |

> `FeedFetchError` nie ma własnego ViewSetu z prefixem routera — błędy są dostępne wyłącznie jako akcja `fetch-errors` zagnieżdżona w `FeedSubmissionViewSet` i `AdminFeedSubmissionViewSet`.

---

### 4.2 `FeedSubmissionViewSet` — widok użytkownika

**Klasa bazowa:** `ModelViewSet` | **Uprawnienia:** `IsAuthenticated` | **Queryset:** `submitted_by=request.user`

| Metoda HTTP | Akcja | URL | Opis |
|-------------|-------|-----|------|
| `GET` | `list` | `/feed-submissions/` | Lista własnych zgłoszeń |
| `POST` | `create` | `/feed-submissions/` | Utwórz nowe zgłoszenie |
| `GET` | `retrieve` | `/feed-submissions/{id}/` | Szczegóły zgłoszenia |
| `PUT` | `update` | `/feed-submissions/{id}/` | Pełna edycja (tylko etap 1) |
| `PATCH` | `partial_update` | `/feed-submissions/{id}/` | Częściowa edycja (tylko etap 1) |
| `DELETE` | `destroy` | `/feed-submissions/{id}/` | Usuń zgłoszenie (tylko etap 1) |
| `GET` | `history` | `/feed-submissions/{id}/history/` | Historia zdarzeń |
| `GET` | `fetch_errors` | `/feed-submissions/{id}/fetch-errors/` | Błędy pobierania pliku |

```python
class FeedSubmissionViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = FeedSubmissionSerializer

    def get_queryset(self):
        return FeedSubmission.objects.filter(submitted_by=self.request.user)

    def perform_create(self, serializer):
        submission = serializer.save(submitted_by=self.request.user)
        FeedSubmissionHistory.objects.create(
            submission=submission, event_type='uploaded',
            stage_before=0, stage_after=1, actor=self.request.user,
        )

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.current_stage > 1:
            return Response({"detail": "Cannot delete a reviewed submission."},
                            status=status.HTTP_403_FORBIDDEN)
        return super().destroy(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.current_stage > 1:
            return Response({"detail": "Cannot edit a reviewed submission."},
                            status=status.HTTP_403_FORBIDDEN)
        return super().update(request, *args, **kwargs)

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        submission = self.get_object()
        entries = submission.history.order_by('created_at')
        return Response(FeedSubmissionHistorySerializer(entries, many=True).data)

    @action(detail=True, methods=['get'], url_path='fetch-errors')
    def fetch_errors(self, request, pk=None):
        submission = self.get_object()  # sprawdza własność przez get_queryset
        errors = FeedFetchError.objects.filter(
            models.Q(static_entry__submission=submission) |
            models.Q(endpoint__entry__submission=submission)
        ).order_by('-occurred_at')
        return Response(FeedFetchErrorSerializer(errors, many=True).data)
```

---

### 4.3 `AdminFeedSubmissionViewSet` — widok admina

**Klasa bazowa:** `ModelViewSet` | **Uprawnienia:** `IsAdminUser` | **Queryset:** wszystkie zgłoszenia

| Metoda HTTP | Akcja | URL | Opis |
|-------------|-------|-----|------|
| `GET` | `list` | `/admin/feed-submissions/` | Lista wszystkich zgłoszeń |
| `GET` | `retrieve` | `/admin/feed-submissions/{id}/` | Szczegóły |
| `PUT` | `update` | `/admin/feed-submissions/{id}/` | Edycja |
| `PATCH` | `partial_update` | `/admin/feed-submissions/{id}/` | Częściowa edycja |
| `DELETE` | `destroy` | `/admin/feed-submissions/{id}/` | Usuń |
| `POST` | `advance_stage` | `/admin/feed-submissions/{id}/advance-stage/` | Przesuń etap |
| `POST` | `reject` | `/admin/feed-submissions/{id}/reject/` | Odrzuć z powodem |
| `GET` | `history` | `/admin/feed-submissions/{id}/history/` | Historia zdarzeń |
| `GET` | `fetch_errors` | `/admin/feed-submissions/{id}/fetch-errors/` | Błędy pobierania |

```python
class AdminFeedSubmissionViewSet(ModelViewSet):
    permission_classes = [IsAdminUser]
    serializer_class = AdminFeedSubmissionSerializer
    queryset = FeedSubmission.objects.all()
    filterset_fields = ['data_type', 'feed_kind', 'transport_organization']

    @action(detail=True, methods=['post'], url_path='advance-stage')
    def advance_stage(self, request, pk=None):
        submission = self.get_object()
        current = submission.current_stage
        if current >= 4 or submission.is_rejected:
            return Response({"detail": "Cannot advance."}, status=400)
        next_stage = current + 1
        event_type = 'completed' if next_stage == 4 else 'stage_advanced'
        FeedSubmissionHistory.objects.create(
            submission=submission, event_type=event_type,
            stage_before=current, stage_after=next_stage, actor=request.user,
        )
        return Response({"detail": f"Advanced to stage {next_stage}."})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        submission = self.get_object()
        cause = request.data.get('cause')
        if not cause:
            return Response({"detail": "Field 'cause' is required."}, status=400)
        if submission.is_rejected or submission.current_stage == 4:
            return Response({"detail": "Cannot reject."}, status=400)
        current = submission.current_stage
        FeedSubmissionHistory.objects.create(
            submission=submission, event_type='rejected',
            stage_before=current, stage_after=current,
            actor=request.user, cause=cause,
        )
        return Response({"detail": "Submission rejected.", "cause": cause})

    @action(detail=True, methods=['get'])
    def history(self, request, pk=None):
        submission = self.get_object()
        entries = submission.history.order_by('created_at')
        return Response(FeedSubmissionHistorySerializer(entries, many=True).data)

    @action(detail=True, methods=['get'], url_path='fetch-errors')
    def fetch_errors(self, request, pk=None):
        submission = self.get_object()
        errors = FeedFetchError.objects.filter(
            models.Q(static_entry__submission=submission) |
            models.Q(endpoint__entry__submission=submission)
        ).order_by('-occurred_at')
        return Response(FeedFetchErrorSerializer(errors, many=True).data)
```

---

### 4.4 `PublicFeedViewSet` — widok publiczny

**Klasa bazowa:** `ReadOnlyModelViewSet` | **Uprawnienia:** `AllowAny`

| Metoda HTTP | Akcja | URL | Opis |
|-------------|-------|-----|------|
| `GET` | `list` | `/feeds/` | Lista zatwierdzonych feedów |
| `GET` | `retrieve` | `/feeds/{id}/` | Szczegóły feeda |
| `GET` | `download_static` | `/feeds/{id}/download/static/` | Pobierz plik statyczny |
| `GET` | `download_realtime` | `/feeds/download/realtime/{pk}/` | Pobierz plik realtime |

```python
class PublicFeedViewSet(ReadOnlyModelViewSet):
    permission_classes = [AllowAny]
    serializer_class = PublicFeedSerializer

    def get_queryset(self):
        completed_ids = FeedSubmissionHistory.objects.filter(
            event_type='completed'
        ).values_list('submission_id', flat=True)
        return FeedSubmission.objects.filter(id__in=completed_ids)

    @action(detail=True, methods=['get'], url_path='download/static')
    def download_static(self, request, pk=None):
        submission = self.get_object()
        # serwuje cached_file lub file przez FileResponse
        ...

    @action(detail=False, methods=['get'],
            url_path='download/realtime/(?P<endpoint_pk>[^/.]+)')
    def download_realtime(self, request, endpoint_pk=None):
        endpoint = get_object_or_404(RealtimeEndpoint, pk=endpoint_pk)
        # weryfikuje completed i serwuje cached_file
        ...
```

---

### 4.5 `FeedFetchErrorSerializer`

```python
class FeedFetchErrorSerializer(serializers.ModelSerializer):
    source = serializers.SerializerMethodField()
    endpoint_type = serializers.SerializerMethodField()

    class Meta:
        model = FeedFetchError
        fields = [
            'id', 'source', 'static_entry', 'endpoint', 'endpoint_type',
            'error_type', 'http_status_code', 'message', 'url_attempted', 'occurred_at',
        ]
        read_only_fields = fields  # tylko do odczytu, nigdy do zapisu przez API

    def get_source(self, obj):
        return 'static' if obj.static_entry_id else 'realtime'

    def get_endpoint_type(self, obj):
        return obj.endpoint.endpoint_type if obj.endpoint else None
```

---

### 4.6 Rejestracja routera

```python
# api/urls.py
from rest_framework.routers import DefaultRouter
from .views import FeedSubmissionViewSet, AdminFeedSubmissionViewSet, PublicFeedViewSet

router = DefaultRouter()
router.register(r'feed-submissions', FeedSubmissionViewSet, basename='feed-submission')
router.register(r'admin/feed-submissions', AdminFeedSubmissionViewSet, basename='admin-feed-submission')
router.register(r'feeds', PublicFeedViewSet, basename='public-feed')

urlpatterns = router.urls
```

> Akcje `fetch-errors` są zarejestrowane jako `@action` w ViewSetach — router automatycznie generuje:
> - `/feed-submissions/{id}/fetch-errors/`
> - `/admin/feed-submissions/{id}/fetch-errors/`

---

## 5. Workflow dodawania feeda

### Krok 1 – Przesłanie danych przez użytkownika

```
Użytkownik (zalogowany)
    │
    ▼
POST /api/data_manager/feed-submissions/
    │
    ├─ Walidacja serializera
    ├─ Automatyczne hide_original=True jeśli auth_type != none
    ├─ Zapis FeedSubmission + StaticFeedEntry / RealtimeFeedEntry + RealtimeEndpoint(s)
    └─ FeedSubmissionHistory: event_type='uploaded', stage 0→1
            │
            ▼
    current_stage = 1 ("Step 1: Data uploaded")
```

---

### Krok 2 – Weryfikacja danych (scheduler + admin)

Po przesłaniu scheduler próbuje pobrać plik z URL i zweryfikować jego zawartość.

```
Scheduler
    │
    ├─ Próba pobrania z URL
    │
    ├─ SUKCES → zapis cached_file, cached_at
    │
    └─ BŁĄD → FeedFetchError.objects.create(
                  static_entry=entry / endpoint=endpoint,
                  error_type='http_error' | 'timeout' | 'connection_error' | ...,
                  message="...", url_attempted="...", http_status_code=403
              )
              Właściciel widzi błąd w GET /feed-submissions/{id}/fetch-errors/
              Admin widzi błąd w GET /admin/feed-submissions/{id}/fetch-errors/
```

Następnie admin ręcznie weryfikuje i przesuwa etap:

```
Admin
    │
    ▼
POST /api/data_manager/admin/feed-submissions/{id}/advance-stage/
    │
    └─ FeedSubmissionHistory: event_type='stage_advanced', stage 1→2
            │
            ▼
    current_stage = 2 ("Step 2: Data verification")
```

---

### Krok 2a – Odrzucenie zgłoszenia (admin, opcjonalnie)

```
Admin
    │
    ▼
POST /api/data_manager/admin/feed-submissions/{id}/reject/
    │
    └─ FeedSubmissionHistory: event_type='rejected', stage N→N, cause="..."
            │
            ▼
    is_rejected = True, rejection_cause widoczny dla właściciela
    Użytkownik tworzy NOWE zgłoszenie przez POST /feed-submissions/
```

---

### Krok 3 i 4 – Potwierdzenie i publikacja

```
Admin → advance-stage → FeedSubmissionHistory: stage_advanced (2→3)
Admin → advance-stage → FeedSubmissionHistory: completed (3→4)
                         Feed pojawia się w GET /feeds/
```

---

### Diagram pełnego procesu

```
[Użytkownik]     [Scheduler]     [System/Historia]     [Admin]     [Publiczny]
     │                │                  │                  │            │
     │── POST ────────────────────▶│     │                  │            │
     │                │       Historia: uploaded (0→1)      │            │
     │◀── 201 ────────────────────│     │                  │            │
     │                │            │                        │            │
     │                │─ pobierz ──▶│                       │            │
     │                │  BŁĄD       │── FeedFetchError ──▶DB│            │
     │◀── GET fetch-errors ────────│  (widzi błąd)          │            │
     │                │            │                        │            │
     │                │─ pobierz ──▶│                       │            │
     │                │  OK         │── cached_file ─────▶DB│            │
     │                │            │◀───── advance-stage ───│            │
     │                │       Historia: stage_advanced (1→2)│            │
     │                │            │◀───── advance-stage ───│            │
     │                │       Historia: stage_advanced (2→3)│            │
     │                │            │◀───── advance-stage ───│            │
     │                │       Historia: completed (3→4)     │            │
     │                │            │────────────────────────────── GET ─▶│
```

---

## 6. Przechowywanie plików

### Struktura katalogów

```
MEDIA_ROOT/
└── uploaded_data/
    └── {user_id}/
        └── {org_id}/
            ├── static/
            │   └── gtfs.zip
            └── realtime/
                ├── trip_update/feed.pb
                ├── vehicle_position/feed.pb
                ├── sx/feed.xml
                └── vm/feed.xml
```

### Zasady

| Scenariusz | Pole | Lokalizacja |
|------------|------|-------------|
| Użytkownik przesyła plik | `StaticFeedEntry.file` | `static/{original_filename}` |
| Serwer pobiera z URL (proxy) | `StaticFeedEntry.cached_file` | `static/{original_filename}` |
| Serwer pobiera RT (proxy) | `RealtimeEndpoint.cached_file` | `realtime/{endpoint_type}/{filename}` |

**`OverwriteStorage`** — przy każdym zapisie pod tą samą ścieżką stary plik jest zastępowany nowym.

### Dostęp do katalogu

- `MEDIA_URL = '/internal-media/'` — nie jest podpięty w `urlpatterns`
- Nginx **nie powinien** udostępniać `MEDIA_ROOT` bezpośrednio
- Jedyna droga do pliku: akcje `download_static` / `download_realtime` w ViewSetach

---

## 7. Mechanizm odświeżania feedów (proxy/cache)

### Kiedy jest potrzebny

Gdy `auth_type != none` (automatycznie `hide_original=True`) lub użytkownik jawnie ustawi `hide_original=True`.

### Scheduler — feedy statyczne

```python
from data_manager.models import StaticFeedEntry, FeedSubmissionHistory, FeedFetchError
import requests

def refresh_static_feeds():
    now = timezone.now().time().replace(second=0, microsecond=0)
    completed_ids = FeedSubmissionHistory.objects.filter(
        event_type='completed'
    ).values_list('submission_id', flat=True)

    entries = StaticFeedEntry.objects.filter(
        hide_original=True, url__isnull=False,
        submission_id__in=completed_ids,
    ).filter(Q(download_time_1=now) | Q(download_time_2=now))

    for entry in entries:
        headers = _build_auth_headers(entry.auth_type, entry.auth_value)
        try:
            response = requests.get(entry.url, headers=headers, timeout=60)
            response.raise_for_status()
            filename = entry.url.rstrip('/').split('/')[-1] or 'feed.zip'
            entry.cached_file.save(filename, ContentFile(response.content), save=True)
            StaticFeedEntry.objects.filter(pk=entry.pk).update(cached_at=timezone.now())
        except requests.exceptions.Timeout as e:
            FeedFetchError.objects.create(
                static_entry=entry, error_type='timeout',
                message=str(e), url_attempted=entry.url,
            )
        except requests.exceptions.HTTPError as e:
            FeedFetchError.objects.create(
                static_entry=entry, error_type='http_error',
                http_status_code=e.response.status_code,
                message=str(e), url_attempted=entry.url,
            )
        except requests.exceptions.ConnectionError as e:
            FeedFetchError.objects.create(
                static_entry=entry, error_type='connection_error',
                message=str(e), url_attempted=entry.url,
            )
```

### Scheduler — endpointy realtime

```python
def refresh_realtime_endpoints():
    now = timezone.now()
    completed_ids = FeedSubmissionHistory.objects.filter(
        event_type='completed'
    ).values_list('submission_id', flat=True)

    endpoints = RealtimeEndpoint.objects.filter(
        hide_original=True, entry__submission_id__in=completed_ids,
    )

    for endpoint in endpoints:
        last = endpoint.cached_at or datetime.min.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < endpoint.interval:
            continue
        headers = _build_auth_headers(endpoint.auth_type, endpoint.auth_value)
        try:
            response = requests.get(endpoint.url, headers=headers, timeout=30)
            response.raise_for_status()
            filename = endpoint.url.rstrip('/').split('/')[-1] or 'feed.pb'
            endpoint.cached_file.save(filename, ContentFile(response.content), save=True)
            RealtimeEndpoint.objects.filter(pk=endpoint.pk).update(cached_at=now)
        except requests.exceptions.Timeout as e:
            FeedFetchError.objects.create(
                endpoint=endpoint, error_type='timeout',
                message=str(e), url_attempted=endpoint.url,
            )
        except requests.exceptions.HTTPError as e:
            FeedFetchError.objects.create(
                endpoint=endpoint, error_type='http_error',
                http_status_code=e.response.status_code,
                message=str(e), url_attempted=endpoint.url,
            )
        except requests.exceptions.ConnectionError as e:
            FeedFetchError.objects.create(
                endpoint=endpoint, error_type='connection_error',
                message=str(e), url_attempted=endpoint.url,
            )
```

### Autentykacja przy pobieraniu

| `auth_type` | Sposób użycia |
|-------------|---------------|
| `none` | Brak nagłówków autoryzacji |
| `api_key` | Nagłówek `X-API-Key: {auth_value}` |
| `bearer_token` | Nagłówek `Authorization: Bearer {auth_value}` |
| `basic_auth` | HTTP Basic Auth, `auth_value` w formacie `username:password` |

---

## 8. Udostępnianie feedów użytkownikom końcowym

### Publiczny feed — co widzi świat

| Pole | Źródło |
|------|--------|
| `id` | `FeedSubmission.id` |
| `organization_name` | `TransportOrganization.transport_organization` |
| `organization_region` | `TransportOrganization.region` |
| `data_type` | `FeedSubmission.data_type` |
| `feed_kind` | `FeedSubmission.feed_kind` |
| `name` | `FeedSubmission.name` |
| `created_at` | `FeedSubmission.created_at` |
| `published_at` | `FeedSubmissionHistory.created_at` z wpisu `completed` |
| `static_feed.download_url` | Wygenerowany URL do bezpiecznego pobrania |
| `static_feed.license` | `StaticFeedEntry.license` |
| `static_feed.cached_at` | Kiedy plik był ostatnio odświeżony |
| `realtime_feed.license` | `RealtimeFeedEntry.license` |
| `realtime_feed.endpoints[].endpoint_type` | Typ endpointu |
| `realtime_feed.endpoints[].interval` | Czas odświeżania w sekundach |
| `realtime_feed.endpoints[].feed_url` | URL endpointu lub link do proxy |

### Logika `download_url` / `feed_url`

```
hide_original = False  →  oryginalny URL (użytkownik pobiera bezpośrednio ze źródła)
hide_original = True AND cached_file istnieje  →  /feeds/{id}/download/static/
hide_original = True AND cached_file brak  →  null
```

---

## 9. Bezpieczeństwo i kontrola dostępu

### Warstwy ochrony

| Warstwa | Mechanizm |
|---------|-----------|
| Uwierzytelnienie API | `IsAuthenticated` (sesja lub token DRF) |
| Izolacja danych | `get_queryset()` filtruje po `submitted_by=request.user` |
| Ochrona obiektów | `get_object()` sprawdza właściciela |
| Brak wycieku przez URL | `MEDIA_URL` nie jest podpięty w `urlpatterns` |
| Ochrona plików | Akcje ViewSetu weryfikują wpis `completed` w historii |
| Ukryte dane wrażliwe | `auth_value`: `write_only=True` — nigdy nie zwracany |
| Ukryty URL źródłowy | `hide_original=True` → publiczny serializer zwraca tylko `download_url` |
| Auto-ochrona autentykacji | `auth_type != none` → automatyczne `hide_original=True` |
| Pole `cause` tylko dla admina | Wypełniane wyłącznie przez endpoint `reject/` |
| Niezmienność historii | `FeedSubmissionHistory` — tylko do zapisu |
| Niezmienność błędów | `FeedFetchError` — tylko do zapisu, tworzony przez scheduler |
| Izolacja błędów | `fetch-errors` weryfikuje właściciela przez `get_object()` w `FeedSubmissionViewSet` |

### Macierz uprawnień

| Akcja | Właściciel | Admin | Anonimowy |
|-------|-----------|-------|-----------|
| Utwórz zgłoszenie | ✅ | ✅ | ❌ |
| Odczytaj swoje zgłoszenie | ✅ | ✅ | ❌ |
| Odczytaj cudze zgłoszenie | ❌ | ✅ | ❌ |
| Edytuj/usuń zgłoszenie (etap 1) | ✅ (tylko własne) | ✅ | ❌ |
| Odczytaj historię swojego zgłoszenia | ✅ | ✅ | ❌ |
| Odczytaj historię cudzego zgłoszenia | ❌ | ✅ | ❌ |
| Odczytaj błędy pobierania swojego feeda | ✅ | ✅ | ❌ |
| Odczytaj błędy pobierania cudzego feeda | ❌ | ✅ | ❌ |
| Przesuń etap | ❌ | ✅ | ❌ |
| Odrzuć zgłoszenie | ❌ | ✅ | ❌ |
| Ustaw `cause` w historii | ❌ | ✅ | ❌ |
| Odczytaj listę publiczną | ✅ | ✅ | ✅ |
| Pobierz zatwierdzony plik | ✅ | ✅ | ✅ |
| Pobierz niezatwierdzony plik | ❌ | ✅ | ❌ |
| Bezpośredni dostęp do MEDIA_ROOT | ❌ | ❌ | ❌ |

---

## 10. Panel administracyjny

Dostępny pod `/admin/`.

### `FeedSubmission`
- Lista: organizacja, typ, rodzaj, aktualny etap (obliczany z historii), status odrzucenia, data utworzenia
- Filtry: `data_type`, `feed_kind`, `created_at`
- **Inline:** `StaticFeedEntryInline`, `RealtimeFeedEntryInline`
- **Inline historii:** `FeedSubmissionHistoryInline` — lista wpisów historii (tylko do odczytu)
- **Inline błędów:** `FeedFetchErrorInline` — lista błędów pobierania (tylko do odczytu), widoczna dla admina w widoku szczegółów zgłoszenia
- **Przyciski akcji:** „Przesuń do następnego etapu" i „Odrzuć"
- **Akcja masowa:** `Advance selected submissions to next stage`

### `RealtimeFeedEntry`
- **Inline:** `RealtimeEndpointInline` z polem `interval`

### `FeedSubmissionHistory` (tylko do odczytu)
- Lista: zgłoszenie, typ zdarzenia, etap przed/po, aktor, powód, data
- Rekordy nie mogą być edytowane ani usuwane

### `FeedFetchError` (tylko do odczytu)
- Lista: źródło (`static` / `realtime`), typ błędu, kod HTTP, URL, data
- Filtry: `error_type`, `occurred_at`
- Wyszukiwanie: po URL, komunikacie błędu
- Rekordy nie mogą być edytowane ani usuwane — tworzone wyłącznie przez scheduler

---

## 11. Typy danych i protokoły

### Typy statyczne (`feed_kind = static`)

| `data_type` | Nazwa | Opis |
|-------------|-------|------|
| `gtfs` | GTFS | General Transit Feed Specification – rozkłady jazdy |
| `netex` | NeTEx | Network Timetable Exchange – europejski standard rozkładów |
| `other` | Other | Inne formaty statyczne |

### Typy dynamiczne (`feed_kind = dynamic`)

| `data_type` | Nazwa | Opis |
|-------------|-------|------|
| `gtfs_rt` | GTFS-RT | GTFS Realtime – dane w czasie rzeczywistym do GTFS |
| `siri` | SIRI | Service Interface for Real-time Information – europejski standard RT |
| `gbfs` | GBFS | General Bikeshare Feed Specification – rowery i hulajnogi (realtime) |

### Relacja modeli (diagram)

```
TransportOrganization
        │ (1)
        │ (*)
  FeedSubmission ──────────────── submitted_by ──── User
        │   │
        │   └── (1..*) FeedSubmissionHistory     ← jedyne źródło prawdy o etapach
        │                  ├── event_type  (uploaded|stage_advanced|rejected|completed)
        │                  ├── stage_before / stage_after  (0–4)
        │                  ├── actor       (FK → User)
        │                  ├── cause       (TextField, nullable — tylko przy rejected)
        │                  └── created_at  (auto, niezmienne)
        │
        ├── (OneToOne) StaticFeedEntry
        │       ├── url / file / cached_file / cached_at
        │       ├── is_original       (BooleanField, default=False)
        │       ├── hide_original     (auto True gdy auth_type != none)
        │       ├── auth_type / auth_value
        │       ├── download_time_1 / download_time_2
        │       ├── license           (CharField(255))
        │       └── (1..*) FeedFetchError
        │                  ├── error_type  (http_error|timeout|connection_error|
        │                  │               invalid_content|auth_error)
        │                  ├── http_status_code  (nullable)
        │                  ├── message     (TextField)
        │                  ├── url_attempted
        │                  └── occurred_at (auto, niezmienne)
        │
        └── (OneToOne) RealtimeFeedEntry
                ├── protocol  (gtfs_rt|siri|gbfs)
                ├── license   (CharField(255))
                └── (FK, 1..N) RealtimeEndpoint
                        ├── url / endpoint_type / interval
                        ├── is_original   (BooleanField, default=False)
                        ├── hide_original (auto True gdy auth_type != none)
                        ├── auth_type / auth_value
                        ├── cached_file / cached_at
                        └── (1..*) FeedFetchError
                                   ├── error_type
                                   ├── http_status_code  (nullable)
                                   ├── message
                                   ├── url_attempted
                                   └── occurred_at (auto, niezmienne)
```

