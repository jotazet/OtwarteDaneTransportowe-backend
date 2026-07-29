# Zmiany w API — przewodnik migracyjny dla frontendu

Dotyczy wdrożenia backendu z lipca 2026 (hardening + ujednolicenie DRF).
Zmiany **łamiące** oznaczone ⚠️. Po naniesieniu poprawek frontend powinien
przejść pełny smoke: logowanie → listy → szczegóły → formularze zgłoszeń →
pobieranie feedów.

---

## 1. ⚠️ Paginacja: KAŻDA lista zwraca teraz kopertę DRF

Wszystkie endpointy listowe zwracają jednolity obiekt zamiast gołej tablicy:

```json
{
  "count": 123,
  "next": "https://api.example.org/api/...?page=2",
  "previous": null,
  "results": [ /* pozycje bieżącej strony */ ]
}
```

**Migracja w kodzie:**

```js
// PRZED:
const items = await (await fetch(url)).json();
items.map(...)

// PO:
const data = await (await fetch(url)).json();
data.results.map(...)          // pozycje strony
// data.count  – łączna liczba
// data.next   – URL następnej strony lub null
```

Pomocnik do pobrania wszystkich stron (używać oszczędnie — listy potrafią być długie):

```js
async function fetchAllPages(url, init) {
  const results = [];
  let next = url;
  while (next) {
    const data = await (await fetch(next, init)).json();
    results.push(...data.results);
    next = data.next;
  }
  return results;
}
```

**Parametry:** `?page=N` (numer strony, od 1), `?page_size=M`.

### Endpointy objęte zmianą (dotąd zwracały gołą tablicę)

| Endpoint | page_size domyślny | max |
|---|---|---|
| `GET /api/blog/posts/` | 4 | 50 |
| `GET /api/data_manager/feeds/` (publiczny katalog organizacji) | 50 | 200 |
| `GET /api/data_manager/feed-submissions/` | 50 | 200 |
| `GET /api/data_manager/realtime-submissions/` | 50 | 200 |
| `GET /api/data_manager/realtime-submissions/eligible-static-submissions/{org_id}/` (i wariant `/{data_type}/`) | 50 | 200 |
| `GET /api/data_manager/static-feed-entries/` | 50 | 200 |
| `GET /api/data_manager/realtime-endpoints/` | 50 | 200 |
| `GET /api/cases/transport-organizations/` | 50 | 200 |
| `GET /api/cases/case-statuses/` | 50 | 200 |
| `GET /api/cases/data-providers/` | 50 | 200 |
| `GET /api/users/` | 50 | 200 |

### Bez zmian (już wcześniej były paginowane — ten sam kształt co dziś)

- `GET /api/data_manager/fetch-errors/` (25 / max 100)
- `GET /api/data_manager/proxy-feeds/` (25 / max 100)
- `GET /api/data_manager/feed-submissions/{id}/fetch-errors/`
- `GET /api/data_manager/realtime-submissions/{id}/fetch-errors/`

### Nadal BEZ paginacji (celowo — kształt niezmieniony)

- `GET /api/data_manager/submissions/` → `{ "user": <id>, "static": [...], "realtime": [...] }`
- `GET /api/users/me/`, szczegóły (`/{id}/`), payloady `/feed/...`

### Usunięta hybryda w blogu ⚠️

`GET /api/blog/posts/` **bez** `?page` zwracał dotąd gołą tablicę wszystkich
wpisów, a **z** `?page` — kopertę. Teraz **zawsze** zwraca kopertę
(4 wpisy/strona). Jeśli frontend pobierał „wszystko" bez `?page`, musi przejść
na stronicowanie (lub `?page_size=50`).

---

## 2. ⚠️ Pobieranie plików realtime: nowa trasa kanoniczna po `endpoint_type`

Stara trasa po nazwie pliku była niedeterministyczna (dwa endpointy mogły mieć
tę samą nazwę bazową pliku). Nowa trasa:

```
GET /feed/rt/{realtime_submission_id}/{endpoint_type}/
```

`endpoint_type` to np. `trip_update`, `vehicle_position`, `service_alert`,
`gbfs`, `station_status`… (unikalny w ramach zgłoszenia — pobranie zawsze
trafia we właściwy plik). **Uwaga na końcowy slash** — jest częścią trasy.

### Co zwracają teraz endpointy informacyjne

`GET /feed/rt/{id}/`:

```json
{
  "dynamic": {
    "trip_update": "https://api.example.org/feed/rt/12/trip_update/",
    "vehicle_position": "https://api.example.org/feed/rt/12/vehicle_position/"
  }
}
```

Publiczny katalog `GET /api/data_manager/feeds/{org_id}/` — pole `feed_url`
proxowanych endpointów RT ma teraz format trasy kanonicznej:

```json
{ "endpoint_type": "trip_update", "feed_url": "https://api.example.org/feed/rt/12/trip_update/", ... }
```

**Migracja:** jeśli frontend budował URL-e plików samodzielnie albo parsował
nazwę pliku z `feed_url` — przestać; zawsze używać `feed_url` / `dynamic[...]`
z odpowiedzi API. Stara trasa `/feed/rt/{id}/{filename}` nadal działa
(kompatybilność), ale jest legacy i nazwy plików mogą się zmieniać.

Pobieranie statyczne **bez zmian**: `GET /feed/{id}/` → `{"static": "...url..."}`,
`GET /feed/{id}/{filename}`.

---

## 3. ⚠️ Usuwanie zgłoszeń: tylko Admin

`DELETE /api/data_manager/feed-submissions/{id}/` oraz
`DELETE /api/data_manager/realtime-submissions/{id}/`:

- rola `Admin` → `204 No Content`,
- każdy inny (także właściciel zgłoszenia) → `403` z body
  `{"detail": "Only Admin can delete submissions."}`.

Wcześniej właściciel mógł usunąć zgłoszenie realtime na etapie 1 — już nie.
**Migracja:** ukryć przycisk „Usuń" przy zgłoszeniach dla użytkowników bez
roli `Admin` (claim `roles` w JWT).

---

## 4. Odpowiedzi POST/PATCH mają ten sam kształt co GET

Dotąd operator (`Helper`/`Admin`) dostawał w odpowiedzi na POST/PATCH inny
(uboższy) obiekt niż z GET. Teraz kształt jest spójny per rola — dla
operatorów odpowiedzi zapisu zawierają dodatkowo `submitted_by_username`,
tak jak GET. Nic nie trzeba zmieniać, chyba że frontend zakładał **brak**
tego pola po zapisie.

---

## 5. Nowe/zmienione odpowiedzi błędów (dotąd częściowo 500)

| Sytuacja | Status | Body |
|---|---|---|
| `PATCH` zgłoszenia z `stage` niebędącym liczbą | **400** (było 500) | `{"stage": ["Stage must be an integer."]}` |
| `PATCH` ze `stage` poza 1–4 | **400** (było 500) | `{"stage": ["Stage must be between 1 and 4."]}` |
| `PATCH` realtime z nieznanym `endpoint_type` (tryb ograniczony — zmiana tylko `interval`) | **400** (było 500) | `{"endpoints": ["Unknown endpoint_type '...' ..."]}` |
| Upload pliku GTFS, który nie jest ZIP-em | **400** | `{"static_entry": {"file": ["Uploaded GTFS file is not a ZIP archive."]}}` |
| Dezaktywacja (`is_active:false`) ostatniego aktywnego Admina | **400** | `["Cannot remove the Admin role from, or deactivate, the last admin user."]` |

Dodatkowo: błędny `stage` jest teraz walidowany **przed** zapisem treści —
odpowiedź 400 oznacza, że **nic** się nie zapisało (wcześniej treść mogła
zostać zapisana mimo błędu).

---

## 6. Zmiany zachowań, o których warto wiedzieć (bez zmian w kodzie frontu)

- **Edycja endpointów RT przez Admina nie cofa już publikacji** — opublikowany
  feed (etap 4) pozostaje na etapie 4 po podmianie endpointów; walidacja
  przebiega w tle i dopiero jej błąd odrzuca zgłoszenie (z wpisem w historii).
- **Ponowne przesłanie odrzuconego zgłoszenia zawsze restartuje weryfikację**
  — także gdy URL/plik są identyczne. Frontend może po PATCH-u od razu
  pokazywać „Step 2: Data verification".
- **`your_reaction` w blogu** jest liczone z tego samego IP co zapis reakcji
  (nagłówek `X-Forwarded-For` jest honorowany wyłącznie od zaufanego
  reverse-proxy). Uwaga wdrożeniowa: w produkcji za nginx/Caddy backend musi
  mieć ustawione `TRUSTED_PROXY_CIDRS`, inaczej wszyscy użytkownicy za proxy
  współdzielą jedno IP (limit reakcji i `your_reaction` będą wspólne).
- **Rola `Editor`**: tworzy/edytuje/usuwa dowolne wpisy bloga. `Blogger`
  edytuje wyłącznie własne wpisy. Jeśli UI pokazuje przyciski edycji — dla
  `Blogger` tylko przy własnych wpisach (`author` vs id zalogowanego),
  dla `Editor`/`Admin` przy wszystkich.

---

## 7. ⚠️ Reakcje na blogu: `your_reaction` NIE może pochodzić z SSR

**Nowy endpoint:**

```
GET /api/blog/reactions/mine/?post_ids=1,2,3
→ { "1": "like", "3": "angry" }        // brak klucza = brak reakcji
```

Zwraca **wyłącznie reakcje wywołującego** (rozpoznawanego po jego adresie IP);
cudze reakcje ani adresy nigdy nie są ujawniane.

**Dlaczego to konieczne:** pole `your_reaction` w odpowiedzi `/api/blog/posts/`
opisuje tego, kto wykonał żądanie. Gdy stronę renderuje serwer (Next.js SSR),
żądanie wychodzi z **serwera frontendu** — więc `your_reaction` opisywało jego,
identycznie dla wszystkich odwiedzających. Objaw: każdy widział cudzą reakcję
jako swoją, a interfejs pozwalał ją nadpisać.

**Migracja:**

- ❌ nie używaj `post.your_reaction` z danych pobranych w komponencie serwerowym,
- ✅ w komponencie klienckim pobierz swój stan z `/api/blog/reactions/mine/`
  (w tym repo zrobione w `ReactionPanel`; helper: `fetchMyReactions()` w
  `lib/api/blog.ts`),
- `reactions_summary` (globalne liczniki) pozostaje poprawne z SSR — to dane
  publiczne, niezależne od tożsamości.

Pole `your_reaction` zostaje w API (jest poprawne dla żądań wysyłanych wprost
z przeglądarki), ale przy SSR musi być ignorowane.

**Wymóg wdrożeniowy:** backend za reverse-proxy musi mieć ustawione
`TRUSTED_PROXY_CIDRS`, a proxy przekazywać `X-Forwarded-For` — inaczej wszyscy
odwiedzający dzielą jedną tożsamość (jedna reakcja na post, wspólny limit
dzienny). `manage.py check` sygnalizuje brak jako `blog.W001`.

---

## 8. Checklist migracji frontendu

- [ ] Wszystkie fetche list przełączone na `data.results` (+ obsługa `next`/`count`).
- [ ] Blog: pobieranie wpisów przez paginację (nie ma już gołej tablicy bez `?page`).
- [ ] URL-e plików RT brane wprost z `feed_url` / `dynamic[...]` (nie budowane z nazw plików).
- [ ] Przycisk „Usuń zgłoszenie" widoczny tylko dla roli `Admin`.
- [ ] Obsługa nowych komunikatów 400 (stage, endpoint_type, nie-ZIP GTFS, ostatni admin).
- [ ] Przyciski edycji bloga wg ról: `Blogger` = własne, `Editor`/`Admin` = wszystkie.
- [ ] Reakcje: stan „moja reakcja" czytany z `/api/blog/reactions/mine/` w
      przeglądarce (nie z `your_reaction` z SSR).
- [ ] (Ops) `TRUSTED_PROXY_CIDRS` ustawione w produkcji za reverse-proxy +
      proxy przekazuje `X-Forwarded-For` (`manage.py check` bez `blog.W001`).
