# telegramTool Dashboard — десктоп (Tauri 2.0)

Тонкий клиент к локальному бэкенду (`api/server.py`). Бэкенд запускаете
отдельно (`./run_api.sh` в корне репо) — приложение сам его не поднимает
(Фаза A, см. `src-tauri/src/main.rs`).

## Установка (один раз)

```bash
# Rust-тулчейн — нужен для сборки Tauri-приложения
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# зависимости фронтенда
cd desktop/telegramtool-dashboard
npm install
```

Mac: дополнительно нужны Xcode Command Line Tools (`xcode-select --install`).
Windows: нужен WebView2 Runtime (обычно уже стоит на Win10 21H2+/Win11) и
Build Tools for Visual Studio (C++ workload) — ставит `rustup` сам подскажет, если чего-то не хватает.

## Запуск в разработке

```bash
# в одном терминале — бэкенд
cd ../..  # корень telegramTool
./run_api.sh

# в другом терминале — приложение
cd desktop/telegramtool-dashboard
npm run dev
```

Откроется нативное окно. При первом запуске впишите:
- **Backend**: `http://127.0.0.1:9000` (или другой хост/порт, если меняли в `.env`)
- **API_TOKEN**: тот же, что в `.env` бэкенда

Оба значения сохраняются в `localStorage` приложения — при следующем запуске вводить не нужно.

## Сборка установщика

```bash
npm run build
```

Результат: `src-tauri/target/release/bundle/` — `.dmg`/`.app` на Mac, `.msi`/`.nsis` на Windows.
**Кросс-сборки нет** — Mac-сборку получаете на Mac, Windows-сборку на Windows (или через GitHub Actions с матрицей `macos-latest` + `windows-latest`, если нужно собирать оба сразу без второй машины).

## Иконки

`src-tauri/icons/*` сейчас — однотонные плейсхолдеры (сгенерированы программно, просто цвет `--acc` из темы дашборда). Замените на реальный брендинг перед публичным релизом — `icon.png` (512×512), `icon.icns` (Mac), `icon.ico` (Windows).

## Архитектура

См. `src/index.html` — конфиг-driven дашборд:
- `DASHBOARDS[]` — список дашбордов (левая панель). Новый дашборд = новый объект в массиве.
- `renderWidget()` — WIDGET TYPE REGISTRY, маппинг `type` → рендерер.
- `displayByUnit()` / `displayCount()` — тумблер count/count-в-секунду (справа), применяется только к `unit: 'count'`.

## Фаза B (не сделано, следующий шаг)

Авто-старт бэкенда самим приложением (чтобы не запускать `run_api.sh` руками):
1. Заморозить `api/server.py` в один exe через PyInstaller — отдельно под Mac и Windows.
2. Прописать его в `tauri.conf.json` → `bundle.externalBin`.
3. В `src-tauri/src/main.rs`, в `setup()`, поднять его через `tauri-plugin-shell` (Command::sidecar), дождаться `GET /health`, только потом показывать окно.

Не делал сейчас, потому что бэкенд с Telethon-клиентами и SQLite per-OS — отдельная по объёму задача, а Фаза A уже даёт рабочее приложение.
