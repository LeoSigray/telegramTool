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

// Бэкенд (api/auth.py) сам генерирует токен при первом запуске и кладёт
// в ~/.telegramtool/token, если API_TOKEN не задан явно в .env. Читаем тот
// же файл напрямую с диска — токен по-прежнему обязателен (см. auth.py),
// просто человеку не нужно копировать его руками между бэкендом и приложением.
#[tauri::command]
fn read_local_token() -> Result<String, String> {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE")) // Windows
        .map_err(|_| "не удалось определить домашнюю директорию".to_string())?;
    let path = std::path::Path::new(&home).join(".telegramtool").join("token");
    std::fs::read_to_string(&path)
        .map(|s| s.trim().to_string())
        .map_err(|e| format!("не удалось прочитать {}: {}", path.display(), e))
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![read_local_token])
        .run(tauri::generate_context!())
        .expect("error while running telegramtool-dashboard");
}
