# OtwarteDaneTransportowe

Projekt Django uruchamiany lokalnie przez Docker Compose (Django + Postgres).

## Wymagania

- Docker + Docker Compose (plugin `docker compose`)

## Szybki start (development)

1. Zbuduj i uruchom wszystko:

```bash
docker compose up --build
```

Albo w tle:

```bash
docker compose up -d --build
```

2. Wejdź w przeglądarce:

- aplikacja: `http://localhost:8000/`
- panel admina: `http://localhost:8000/admin/`

### Co dzieje się przy starcie?

Kontener `web` startuje komendą z `Dockerfile`:

- `python manage.py migrate`
- `python manage.py runserver 0.0.0.0:8000`

Dzięki temu migracje odpalają się automatycznie przy każdym starcie kontenera.

## Pierwsza konfiguracja

### Utworzenie konta administratora

```bash
docker compose exec web python manage.py createsuperuser
```

### Ręczne migracje (gdy potrzebujesz)

```bash
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate
```

## Jak aktualizować działający kontener po zmianach

### 1) Zwykłe zmiany w kodzie (Python/Django)

W `docker-compose.yml` jest podmontowany katalog projektu (`.:/app`), więc większość zmian w kodzie jest widoczna od razu, a `runserver` zwykle przeładuje się automatycznie.

Jeśli z jakiegoś powodu serwer nie przeładuje się sam, zrestartuj usługę:

```bash
docker compose restart web
```

### 2) Zmiany w zależnościach lub Dockerfile

Gdy zmienisz `requirements.txt` albo `Dockerfile`, musisz przebudować obraz:

```bash
docker compose up -d --build
```

Jeśli chcesz mieć pewność, że kontener zostanie odtworzony:

```bash
docker compose up -d --build --force-recreate
```

### 3) Zmiany w modelach (migracje)

Po zmianach w `models.py`:

```bash
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate
```

## Przydatne komendy

### Logi

```bash
docker compose logs -f web
```

```bash
docker compose logs -f postgres
```

### Wejście do kontenera

Shell w kontenerze `web`:

```bash
docker compose exec web bash
```

Django shell:

```bash
docker compose exec web python manage.py shell
```

### Sprawdzenie statusu usług

```bash
docker compose ps
```

## Baza danych

- W Dockerze aplikacja łączy się po nazwie serwisu: `POSTGRES_HOST=postgres` i port `5432`.
- Na hoście Postgres jest wystawiony jako `localhost:5420` (mapowanie `5420:5432`).

Jeśli uruchamiasz Django poza Dockerem, ustaw:

- `POSTGRES_HOST=127.0.0.1`
- `POSTGRES_PORT=5420`

(albo użyj SQLite — patrz niżej).

## Reset bazy (UWAGA: usuwa dane)

Jeśli chcesz wyczyścić bazę (usuwa volume `odt_pgdata`):

```bash
docker compose down -v
```

Potem uruchom projekt ponownie:

```bash
docker compose up -d --build
```

## Uruchomienie bez Dockera (opcjonalnie)

Projekt ma opcję SQLite przez zmienną środowiskową `USE_SQLITE` (zob. `OtwarteDaneTransportowe/settings.py`). Przykładowo:

```bash
USE_SQLITE=1 python manage.py migrate
USE_SQLITE=1 python manage.py runserver
```

## Troubleshooting

### Port 8000 lub 5420 jest zajęty

Zmień mapowanie portów w `docker-compose.yml` albo zatrzymaj proces, który używa danego portu.

### Aplikacja nie może połączyć się z bazą

1. Sprawdź czy Postgres żyje:

```bash
docker compose ps
```

2. Podejrzyj logi:

```bash
docker compose logs -f postgres
```

3. Jeśli to dev i nie szkoda danych, zrób reset bazy (`docker compose down -v`).

