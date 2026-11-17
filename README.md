# AutoClaudeContinue

A small Windows app that watches **Claude** and **ChatGPT** for a rate-limit
banner, waits until the limit resets (waking the PC if needed), then sends the
message and images you chose.

It is local-only: no account, no cloud calls, and no browser automation. It
reads and drives the desktop apps through Windows UI Automation.

## Requirements

- Windows 10 or Windows 11 with the WebView2 runtime (included with Windows 11)
- Claude Desktop and/or the ChatGPT desktop app (the MSIX / Store builds)
- Python 3.10 or newer when running from source (tested with 3.14)
- Node.js 18 or newer and the Rust toolchain when building from source
- The packages in `requirements.txt`

## Install

Download the Windows portable ZIP from
[GitHub Releases](https://github.com/Slyde04/AutoClaudeContinue/releases), then
extract the entire archive and run `AutoClaudeContinue.exe`. Keep the `_internal`
folder beside the executable. The portable release uses the legacy
CustomTkinter interface; it is not an installer.

To run from source:

```powershell
py -m pip install -r requirements.txt
npm install
npm run dev
```

`npm run dev` starts the Tauri shell and a local Python backend. The
`pythonw run.pyw` entry point opens the legacy CustomTkinter interface included
in the portable Windows release.

## Use

Open the target app once and focus it. Windows only exposes its accessibility
tree after the first focus. Then start AutoClaudeContinue.

The interface uses the bundled **OpenDyslexic3** typeface. It is registered
privately for the app process, so Windows and other applications are not
changed.

1. On **Home**:
   - Choose **Claude**, **ChatGPT**, or **Automatic**.
   - Turn **Detection** on or off.
   - Type the message to send in the large composer.
   - Optionally paste an image with `Ctrl+V` or use **Add images…**.
   - Watch the rate-limit timer and the activity log.
   - On a small window, scroll the page to reach every card. The composer keeps
     its full height instead of shrinking.
2. On **Settings**, choose the chat, check interval (default **15 minutes**),
   startup behavior, tray behavior, developer mode, and safety options.
3. Under **Settings → Appearance**, choose the **light** or **dark** theme and
   set the **Interface scale** to **90%**, **100%**, **110%**, or **125%**.
   Both choices are saved and restored on the next launch.
4. **Send now** sends the configured message immediately, even while a rate
   limit is pending or detection is paused.

When a limit is found, the app shows the reset time and a live countdown. At
reset time it briefly focuses the app, selects the configured chat, sets the
message through UI Automation, attaches images through a guarded clipboard
paste, and invokes the app's Send button.

### Rate-limit detection

Claude can present a limit as a status-bar message, a banner, a modal, or a
compact side pop. Side pops are common when Claude developer mode is enabled,
and their text is often split across several accessibility text spans. The app
joins nearby spans in small overlay-style groups and also reads toast/alert
containers, so the reset time can still be parsed from those split messages.

In developer mode, the side pop can appear only **after** Send is invoked. The
message is then marked **Failed to send** and Claude restores the text and images
in the composer. The app treats that as an active rate limit, keeps the restored
draft and attachments intact, and does not retry or reset the draft until the
limit clears. On the retry it removes the restored attachments first, so the
configured images are not added twice. A normal send is also rechecked after 30
seconds in case a new limit appears late.

### Claude developer mode

Developer mode makes Claude show richer rate-limit messages, including the side
pop described above. Leave **Claude developer mode** enabled under **Settings >
Claude** so the app expects that layout.

When the setting is enabled, the app silently probes the open Claude window at
startup and whenever Claude opens. After a send is rejected with a restored
draft, it also turns the setting on automatically and logs the change. The probe
never opens UI or takes focus; it only reads the accessibility tree. You can turn
the setting off if you use the standard Claude layout.

### Chats

The **Chat** dropdown is filled from the app's sidebar, using the same buttons
you click yourself. The app scrolls the sidebar when the chat is out of view and
verifies the open chat from the document title before sending. It does **not**
type into the search palette, and it never sends when the requested chat cannot
be verified. Leave it on **Current chat** to use whatever is already open.

### Images

Images are copied to the clipboard and pasted into the composer. The paste is
refused unless the composer really has keyboard focus, so a stray `Ctrl+V` cannot
land in another window. Up to eight images can be attached.

## Safety behavior

- Polling only reads the accessibility tree. It never focuses, warms up, or
  launches an app.
- Focus is taken only at send time.
- While the PC is locked, or while you are actively using the keyboard and
  mouse, sending is deferred.
- Synthetic typing is not used for the message. The composer is set with
  `ValuePattern.SetValue`, and Send is invoked with `InvokePattern`.
- The Control key is released after every synthetic paste.
- If a send cannot be verified, the app treats it as submitted and waits
  instead of clicking Send again. A second click can land on the Stop button
  and interrupt the response.
- If Claude rejects a send and restores the message and images, that draft is
  preserved rather than treated as delivered or reset.
- If a limit banner is still present after a successful send, it waits for the
  banner to clear instead of sending again.

## Wake timers

The app schedules a Windows waitable timer with `fResume=TRUE` so the PC can
wake from sleep at the reset time. For that to work, enable wake timers:

1. Control Panel → Power Options → Change plan settings → Change advanced power
   settings → Sleep → **Allow wake timers** → **Enable**.
2. Some laptops also have a BIOS/UEFI setting for wake timers.

If the wake timer cannot be created, the app falls back to an ordinary timer.
That still works while the PC is awake, but cannot wake it from sleep.

## Start with Windows

The setting creates a per-user startup entry through the Tauri autostart plugin,
so Task Manager lists the app by its real name. The old Startup-folder shortcut
and the older `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` entry are
removed automatically.

## Diagnostics

```powershell
python -m autoclaude_continue --diagnose
python -m autoclaude_continue --diagnose claude
python -m autoclaude_continue --diagnose chatgpt
```

The diagnostic prints whether each window and its UIA tree are available, the
detected limit and reset time, composer/modal state, and the visible sidebar
chats. It only focuses an app for a warm-up when the tree is blank; it never
sends anything.

Logs and settings:

```
%APPDATA%\AutoClaudeContinue\app.log
%APPDATA%\AutoClaudeContinue\config.json
```

## Build

```powershell
py -m pip install -r requirements-dev.txt
powershell -ExecutionPolicy Bypass -File build.ps1
```

`build.ps1` packages the Python worker as a single executable, places it in
`src-tauri\binaries` with the Rust target-triple suffix Tauri expects, then
compiles the Rust shell and writes the NSIS installer to
`src-tauri\target\release\bundle\nsis`.

The steps are also available separately:

```powershell
powershell -ExecutionPolicy Bypass -File build-backend.ps1
npm run build
```

The portable Windows release can be rebuilt with `build-legacy.ps1`. It creates
the executable and its required `_internal` folder under `dist\AutoClaudeContinue`.
The Tauri build above produces a separate NSIS installer.

## Limitations

- Claude and ChatGPT are Electron apps. A future UI update can change their
  accessibility trees. Run the diagnostic and check the log if detection stops
  working.
- The app does not bypass limits. It waits for the reset and sends a message you
  could have sent yourself.
- Closing to the tray keeps the monitor running. Use the tray menu → **Quit** to
  stop it completely.
- The first diagnostic or manual send may briefly focus an app. Normal polling
  stays in the background.

## License

AutoClaudeContinue is source-available under the
[Noncommercial Source-Available License](./LICENSE). You may use, modify, and
redistribute it for noncommercial purposes with attribution; commercial use is
not allowed. This is not an OSI-approved open-source license. The license is
also included in `LICENSE.txt` and can be read inside the app under
**Misc → License**. The bundled OpenDyslexic fonts are third-party material
licensed under the SIL Open Font License; see
[`desktop/assets/OpenDyslexic3-OFL.txt`](./desktop/assets/OpenDyslexic3-OFL.txt).

## Uninstall

1. Quit the app from the tray menu and delete the extracted portable-app folder.
2. Turn off **Start with Windows** first, or remove the
   `AutoClaudeContinue` value under
   `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
3. If you installed a Tauri build, uninstall it from Windows Settings. Delete
   `%APPDATA%\AutoClaudeContinue` if you also want to remove your settings and
   logs.

## Files

```
run.pyw                     legacy no-console launcher
run.py                      legacy console launcher (debugging)
build.ps1                   Tauri sidecar + installer build
build-backend.ps1           packages the Python worker as a Tauri sidecar
build-legacy.ps1            legacy PyInstaller build
backend_entry.py            sidecar entry point
package.json                Tauri CLI scripts and app version
src-tauri/
  Cargo.toml                Rust shell dependencies
  src/main.rs               window, tray, backend process, startup commands
  tauri.conf.json           bundle, CSP, window, and sidecar configuration
  capabilities/default.json window permissions
  icons/                    application and installer icons
desktop/
  index.html                web UI markup
  styles.css                themes, layout, switches, scaling
  app.js                    API client, state rendering, Tauri bindings
  assets/                   logo, link icons, fonts, and the font's OFL license
version_info.txt            Windows file/version metadata
LICENSE                     Noncommercial source-available license
LICENSE.txt                 License text displayed by the app
autoclaude_continue/
  server.py                 local JSON/SSE API and backend entry point
  automator.py              quiet detect/wait/send state machine
  claude_uia.py             Claude Desktop adapter
  chatgpt_uia.py            ChatGPT desktop adapter
  apps.py                   adapter registry and launch helpers
  rate_limit.py             reset parser and limit detection
  input_safety.py           lock/idle checks, focus, clipboard, paste guard
  attachments.py            image capture and thumbnails
  autostart.py              legacy Startup-folder shortcut helpers
  resources.py              logo, generated icon, asset paths
  wake.py                   waitable timer with fResume
  config.py                 JSON config and migrations
  gui.py                    legacy CustomTkinter window
  tray.py                   legacy pystray tray icon
tests/
  test_rate_limit.py        parser and pattern tests
  test_claude_limit_detection.py  side-pop and overlay detection tests
  test_config.py            config migration tests
  test_automator_logic.py   probe, waiting, and send behavior tests
```
