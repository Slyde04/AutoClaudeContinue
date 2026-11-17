#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(windows)]
use std::os::windows::process::CommandExt;
use std::{
    fs::OpenOptions,
    io::Write,
    net::TcpListener,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use tauri::{
    menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, Theme, WebviewWindow, Wry,
};
use tauri_plugin_autostart::ManagerExt as AutostartExt;
use tauri_plugin_dialog::DialogExt;
use uuid::Uuid;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Clone, serde::Serialize)]
struct ApiConfig {
    port: u16,
    token: String,
}

struct AppState {
    port: u16,
    token: String,
    backend: Mutex<Option<Child>>,
    close_to_tray: AtomicBool,
    quitting: AtomicBool,
    detection_item: Mutex<Option<CheckMenuItem<Wry>>>,
}

fn data_dir() -> Option<PathBuf> {
    std::env::var_os("APPDATA").map(|base| PathBuf::from(base).join("AutoClaudeContinue"))
}

fn shell_log(message: &str) {
    let Some(directory) = data_dir() else {
        return;
    };
    if std::fs::create_dir_all(&directory).is_err() {
        return;
    }
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or(0);
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(directory.join("shell.log"))
    {
        let _ = writeln!(file, "[{stamp}] {message}");
    }
}

fn free_port() -> u16 {
    TcpListener::bind(("127.0.0.1", 0))
        .and_then(|listener| listener.local_addr())
        .map(|address| address.port())
        .unwrap_or(47823)
}

fn backend_console_log() -> Option<PathBuf> {
    let directory = data_dir()?;
    std::fs::create_dir_all(&directory).ok()?;
    Some(directory.join("backend-console.log"))
}

fn attach_console_log(command: &mut Command) {
    let Some(path) = backend_console_log() else {
        command.stdout(Stdio::null()).stderr(Stdio::null());
        return;
    };
    match OpenOptions::new().create(true).append(true).open(&path) {
        Ok(file) => {
            let error_file = file.try_clone();
            command.stdout(Stdio::from(file));
            match error_file {
                Ok(file) => {
                    command.stderr(Stdio::from(file));
                }
                Err(_) => {
                    command.stderr(Stdio::null());
                }
            }
        }
        Err(_) => {
            command.stdout(Stdio::null()).stderr(Stdio::null());
        }
    }
}

#[cfg(windows)]
fn hide_console(command: &mut Command) {
    command.creation_flags(CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn hide_console(_command: &mut Command) {}

fn python_backend_command(port: u16) -> Command {
    let python = std::env::var("ACC_PYTHON").unwrap_or_else(|_| "python".to_string());
    let mut command = Command::new(python);
    command
        .arg("-m")
        .arg("autoclaude_continue.server")
        .arg("--port")
        .arg(port.to_string());
    if let Some(root) = Path::new(env!("CARGO_MANIFEST_DIR")).parent() {
        command.current_dir(root);
    }
    command
}

fn spawn_backend(port: u16, token: &str) -> Result<Child, String> {
    let mut command;

    if cfg!(debug_assertions) {
        command = python_backend_command(port);
    } else {
        let executable = std::env::current_exe().map_err(|error| error.to_string())?;
        let directory = executable.parent().unwrap_or_else(|| Path::new("."));
        let sidecar = directory.join("acc-backend.exe");
        if sidecar.exists() {
            command = Command::new(&sidecar);
            command
                .arg("--port")
                .arg(port.to_string())
                .current_dir(directory);
        } else {
            shell_log(&format!(
                "Sidecar {} is missing; falling back to Python.",
                sidecar.display()
            ));
            command = python_backend_command(port);
        }
    }

    command
        .env("ACC_PORT", port.to_string())
        .env("ACC_API_TOKEN", token)
        .env("ACC_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null());
    attach_console_log(&mut command);
    hide_console(&mut command);

    command.spawn().map_err(|error| {
        let message = format!("Could not start the automation backend: {error}");
        shell_log(&message);
        message
    })
}

fn request_shutdown(port: u16, token: &str) {
    use std::io::{Read, Write};
    let address = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = std::net::TcpStream::connect_timeout(&address, Duration::from_millis(400))
    else {
        return;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(400)));
    let request = format!(
        "POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-ACC-Token: {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    );
    let _ = stream.write_all(request.as_bytes());
    let mut buffer = [0_u8; 64];
    let _ = stream.read(&mut buffer);
}

fn stop_backend(app: &tauri::AppHandle) {
    let state = app.state::<AppState>();
    let Ok(mut guard) = state.backend.lock() else {
        return;
    };
    let Some(mut child) = guard.take() else {
        return;
    };
    drop(guard);

    request_shutdown(state.port, &state.token);
    for _ in 0..30 {
        match child.try_wait() {
            Ok(Some(_)) => return,
            Ok(None) => std::thread::sleep(Duration::from_millis(50)),
            Err(_) => break,
        }
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn show_main_window<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[cfg(windows)]
fn remove_legacy_startup_shortcut() {
    let Some(appdata) = std::env::var_os("APPDATA") else {
        return;
    };
    let shortcut = PathBuf::from(appdata)
        .join("Microsoft")
        .join("Windows")
        .join("Start Menu")
        .join("Programs")
        .join("Startup")
        .join("AutoClaudeContinue.lnk");
    if shortcut.exists() {
        match std::fs::remove_file(&shortcut) {
            Ok(()) => shell_log(&format!(
                "Removed the legacy startup shortcut {}",
                shortcut.display()
            )),
            Err(error) => shell_log(&format!(
                "Could not remove the legacy startup shortcut {}: {error}",
                shortcut.display()
            )),
        }
    }
}

#[cfg(not(windows))]
fn remove_legacy_startup_shortcut() {}

#[cfg(windows)]
fn remove_legacy_run_entry() {
    // Older builds wrote a HKCU Run value that launched pythonw.exe.  Keep
    // the entry created by the autostart plugin for this executable and
    // remove only stale targets left behind by those builds.
    use winreg::enums::{HKEY_CURRENT_USER, KEY_READ, KEY_WRITE};
    use winreg::RegKey;

    let Ok(exe) = std::env::current_exe() else {
        return;
    };
    let exe = exe.to_string_lossy().to_lowercase();
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let path = r"Software\Microsoft\Windows\CurrentVersion\Run";
    let Ok(key) = hkcu.open_subkey_with_flags(path, KEY_READ | KEY_WRITE) else {
        return;
    };
    let Ok(value) = key.get_value::<String, _>("AutoClaudeContinue") else {
        return;
    };
    if value.to_lowercase().contains(&exe) {
        return;
    }
    match key.delete_value("AutoClaudeContinue") {
        Ok(()) => shell_log(&format!("Removed the legacy startup entry {value}")),
        Err(error) => shell_log(&format!(
            "Could not remove the legacy startup entry: {error}"
        )),
    }
}

#[cfg(not(windows))]
fn remove_legacy_run_entry() {}

#[tauri::command]
fn api_config(state: tauri::State<'_, AppState>) -> ApiConfig {
    ApiConfig {
        port: state.port,
        token: state.token.clone(),
    }
}

#[tauri::command]
fn set_close_to_tray(state: tauri::State<'_, AppState>, enabled: bool) {
    state.close_to_tray.store(enabled, Ordering::SeqCst);
}

#[tauri::command]
fn set_tray_detection(state: tauri::State<'_, AppState>, enabled: bool) {
    if let Ok(item) = state.detection_item.lock() {
        if let Some(item) = item.as_ref() {
            let _ = item.set_checked(enabled);
        }
    }
}

#[tauri::command]
fn apply_ui_scale(window: WebviewWindow, scale: u16) -> Result<(), String> {
    // Native WebView2 zoom keeps 100vw/100vh aligned with the visible
    // viewport, so the layout never overflows at 110% or 125%.
    let factor = f64::from(scale.clamp(50, 200)) / 100.0;
    window.set_zoom(factor).map_err(|error| error.to_string())
}

#[tauri::command]
fn set_window_theme(window: WebviewWindow, theme: String) -> Result<(), String> {
    let theme = if theme == "light" {
        Theme::Light
    } else {
        Theme::Dark
    };
    window
        .set_theme(Some(theme))
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn startup_enabled(app: tauri::AppHandle) -> Result<bool, String> {
    app.autolaunch()
        .is_enabled()
        .map_err(|error| error.to_string())
}

#[tauri::command]
fn set_startup(app: tauri::AppHandle, enabled: bool) -> Result<bool, String> {
    let manager = app.autolaunch();
    if enabled {
        manager.enable().map_err(|error| error.to_string())?;
    } else {
        manager.disable().map_err(|error| error.to_string())?;
    }
    manager.is_enabled().map_err(|error| error.to_string())
}

#[tauri::command]
async fn pick_images(app: tauri::AppHandle) -> Result<Vec<String>, String> {
    let (sender, receiver) = std::sync::mpsc::channel();
    app.dialog()
        .file()
        .add_filter("Images", &["png", "jpg", "jpeg", "webp", "gif", "bmp"])
        .pick_files(move |files| {
            let _ = sender.send(files);
        });

    let files = tauri::async_runtime::spawn_blocking(move || receiver.recv().ok().flatten())
        .await
        .map_err(|error| error.to_string())?
        .unwrap_or_default();

    Ok(files
        .into_iter()
        .filter_map(|file| file.into_path().ok())
        .map(|path| path.display().to_string())
        .collect())
}

fn main() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            show_main_window(app);
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            Some(vec!["--autostart"]),
        ))
        .invoke_handler(tauri::generate_handler![
            api_config,
            set_close_to_tray,
            set_tray_detection,
            apply_ui_scale,
            set_window_theme,
            startup_enabled,
            set_startup,
            pick_images
        ])
        .setup(|app| {
            remove_legacy_startup_shortcut();
            remove_legacy_run_entry();

            let port = free_port();
            let token = format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple());
            let backend = match spawn_backend(port, &token) {
                Ok(child) => {
                    shell_log(&format!(
                        "Started the automation backend on 127.0.0.1:{port}"
                    ));
                    Some(child)
                }
                Err(error) => {
                    shell_log(&error);
                    None
                }
            };

            app.manage(AppState {
                port,
                token,
                backend: Mutex::new(backend),
                close_to_tray: AtomicBool::new(true),
                quitting: AtomicBool::new(false),
                detection_item: Mutex::new(None),
            });

            let show_item =
                MenuItem::with_id(app, "show", "Open AutoClaudeContinue", true, None::<&str>)?;
            let send_item = MenuItem::with_id(app, "send", "Send now", true, None::<&str>)?;
            let detection_item = CheckMenuItem::with_id(
                app,
                "detection",
                "Detection enabled",
                true,
                true,
                None::<&str>,
            )?;
            let first_separator = PredefinedMenuItem::separator(app)?;
            let second_separator = PredefinedMenuItem::separator(app)?;
            let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(
                app,
                &[
                    &show_item,
                    &send_item,
                    &first_separator,
                    &detection_item,
                    &second_separator,
                    &quit_item,
                ],
            )?;

            let detection_click = detection_item.clone();
            let mut tray_builder = TrayIconBuilder::with_id("autoclaudecontinue-tray")
                .menu(&menu)
                .tooltip("AutoClaudeContinue")
                .show_menu_on_left_click(false)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "show" => show_main_window(app),
                    "send" => {
                        let _ = app.emit("tray-send-now", ());
                    }
                    "detection" => {
                        let next = !detection_click.is_checked().unwrap_or(true);
                        let _ = detection_click.set_checked(next);
                        let _ = app.emit("tray-toggle-detection", next);
                    }
                    "quit" => {
                        app.state::<AppState>()
                            .quitting
                            .store(true, Ordering::SeqCst);
                        stop_backend(app);
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                });
            if let Some(icon) = app.default_window_icon() {
                tray_builder = tray_builder.icon(icon.clone());
            }
            tray_builder.build(app)?;

            if let Ok(mut slot) = app.state::<AppState>().detection_item.lock() {
                *slot = Some(detection_item);
            }

            Ok(())
        })
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                let state = window.app_handle().state::<AppState>();
                if window.label() == "main"
                    && state.close_to_tray.load(Ordering::SeqCst)
                    && !state.quitting.load(Ordering::SeqCst)
                {
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        });

    match builder.build(tauri::generate_context!()) {
        Ok(app) => {
            app.run(|app_handle, event| {
                if let tauri::RunEvent::Exit = event {
                    stop_backend(app_handle);
                }
            });
        }
        Err(error) => {
            shell_log(&format!("Could not start the desktop shell: {error}"));
            eprintln!("Could not start the desktop shell: {error}");
        }
    }
}
