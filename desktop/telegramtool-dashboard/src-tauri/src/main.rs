// Фаза A (сейчас): десктоп — тонкий клиент. Бэкенд (uvicorn api.server:app)
// запускается пользователем вручную через run_api.sh, приложение просто
// открывает окно с src/index.html, который ходит на http://127.0.0.1:9000.
//
// Фаза B (позже): здесь же, в setup(), поднять python-бэкенд как sidecar-процесс
// (см. tauri-plugin-shell + `bundle.externalBin` в tauri.conf.json) — тогда
// пользователь просто открывает .app/.exe и ничего руками не запускает.
// Для этого бэкенд нужно предварительно "заморозить" в один исполняемый файл
// на каждую ОС (например через PyInstaller) — отдельная задача, не блокирует Фазу A.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running telegramtool-dashboard");
}
