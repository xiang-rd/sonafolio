use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs::OpenOptions,
    io::{BufRead, BufReader, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{Arc, Mutex},
    thread,
    time::{Duration, Instant},
};
use tauri::{Emitter, Manager, State};
use tokio::sync::oneshot;

type Pending = Arc<Mutex<HashMap<String, oneshot::Sender<Result<Value, String>>>>>;
struct Backend {
    workspace: PathBuf,
    child: Child,
    input: Option<ChildStdin>,
    pending: Pending,
}
#[derive(Default)]
struct Host {
    backend: Mutex<Option<Backend>>,
    serial: std::sync::atomic::AtomicU64,
}

impl Backend {
    fn stop(mut self) {
        self.input.take();
        let started = Instant::now();
        let mut terminated = false;
        loop {
            if self.child.try_wait().ok().flatten().is_some() {
                break;
            }
            if started.elapsed() > Duration::from_secs(15) {
                let _ = self.child.kill();
                let _ = self.child.wait();
                break;
            }
            if !terminated && started.elapsed() > Duration::from_secs(8) {
                terminated = true;
                unsafe {
                    libc::kill(self.child.id() as i32, libc::SIGTERM);
                }
            }
            thread::sleep(Duration::from_millis(50));
        }
        for (_, sender) in self.pending.lock().unwrap().drain() {
            let _ = sender.send(Err("后端已关闭".into()));
        }
    }
}

#[tauri::command]
fn connect_workspace(
    app: tauri::AppHandle,
    host: State<'_, Host>,
    workspace: String,
) -> Result<Value, String> {
    let workspace = PathBuf::from(workspace)
        .canonicalize()
        .map_err(|_| "工作区不存在")?;
    if !workspace.is_dir() {
        return Err("请选择文件夹".into());
    }
    let mut state = host.backend.lock().map_err(|_| "后端状态不可用")?;
    if let Some(previous) = state.take() {
        previous.stop();
    }
    let runtime = app
        .path()
        .resource_dir()
        .map_err(|_| "运行资源不可用")?
        .join("runtime");
    let python = runtime.join("python/bin/python3");
    if !python.is_file() {
        return Err("未找到内置 Python 运行包，请重新构建应用".into());
    }
    let logdir = workspace.join(".runtime/logs");
    std::fs::create_dir_all(&logdir).map_err(|_| "工作区不可写")?;
    if !logdir
        .canonicalize()
        .map_err(|_| "日志目录不可用")?
        .starts_with(&workspace)
    {
        return Err("日志目录超出工作区".into());
    }
    let logpath = logdir.join("desktop-backend.log");
    if logpath
        .symlink_metadata()
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err("日志文件不能是符号链接".into());
    }
    if logpath
        .metadata()
        .map(|m| m.len() > 1024 * 1024)
        .unwrap_or(false)
    {
        let _ = std::fs::rename(&logpath, logdir.join("desktop-backend.previous.log"));
    }
    let mut log = OpenOptions::new()
        .create(true)
        .append(true)
        .open(logpath)
        .map_err(|_| "无法写入工作区诊断日志")?;
    let mut child = Command::new(&python)
        .args(["-I", "-B"])
        .arg(runtime.join("launch.py"))
        .env("SONAFOLIO_WORKSPACE", &workspace)
        .current_dir(&workspace)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|_| "无法启动内置处理进程")?;
    let input = child.stdin.take();
    let stdout = child.stdout.take().ok_or("后端输出不可用")?;
    let stderr = child.stderr.take().ok_or("后端诊断不可用")?;
    let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
    let connection_id = host
        .serial
        .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let responders = pending.clone();
    let events = app.clone();
    thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            let Ok(line) = line else {
                break;
            };
            let Ok(mut frame) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            if frame["type"] == "response" {
                if let Some(id) = frame["id"].as_str() {
                    if let Some(sender) = responders.lock().unwrap().remove(id) {
                        let result = if frame["ok"] == true {
                            Ok(frame["result"].clone())
                        } else {
                            Err(frame["error"]["message"]
                                .as_str()
                                .unwrap_or("请求失败")
                                .to_string())
                        };
                        let _ = sender.send(result);
                    }
                }
            } else {
                frame["connection_id"] = json!(connection_id);
                let _ = events.emit("backend-event", &frame);
            }
        }
        for (_, sender) in responders.lock().unwrap().drain() {
            let _ = sender.send(Err("后端连接已断开".into()));
        }
        let _ = events.emit(
            "backend-event",
            json!({"type":"event", "event":"backend.disconnected","connection_id":connection_id}),
        );
    });
    // Diagnostics are drained without rendering request bodies or credentials in UI.
    thread::spawn(move || {
        for line in BufReader::new(stderr).lines() {
            match line {
                Ok(line) => {
                    let _ = writeln!(log, "{}", line);
                }
                Err(_) => break,
            }
        }
    });
    *state = Some(Backend {
        workspace: workspace.clone(),
        child,
        input,
        pending,
    });
    Ok(json!({"workspace":workspace.to_string_lossy(),"connection_id":connection_id}))
}

#[tauri::command]
async fn backend_request(
    host: State<'_, Host>,
    method: String,
    params: Value,
) -> Result<Value, String> {
    let id = format!(
        "desktop-{}",
        host.serial
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    );
    let (tx, rx) = oneshot::channel();
    {
        let mut state = host.backend.lock().map_err(|_| "后端状态不可用")?;
        let backend = state.as_mut().ok_or("请先选择工作区")?;
        let line = serde_json::to_string(
            &json!({"protocol_version":1,"id":id,"method":method,"params":params}),
        )
        .map_err(|_| "请求序列化失败")?;
        if line.len() > 1024 * 1024 - 1 {
            return Err("请求过大".into());
        }
        backend.pending.lock().unwrap().insert(id.clone(), tx);
        if backend
            .input
            .as_mut()
            .ok_or("后端已关闭")?
            .write_all((line + "\n").as_bytes())
            .is_err()
        {
            backend.pending.lock().unwrap().remove(&id);
            return Err("后端写入失败".into());
        }
    }
    match tokio::time::timeout(Duration::from_secs(30), rx).await {
        Ok(Ok(result)) => result,
        _ => {
            if let Some(backend) = host.backend.lock().unwrap().as_mut() {
                backend.pending.lock().unwrap().remove(&id);
            }
            Err("后端响应超时，请查看任务状态后再试".into())
        }
    }
}

#[tauri::command]
fn disconnect_workspace(host: State<'_, Host>) {
    if let Some(backend) = host.backend.lock().unwrap().take() {
        backend.stop();
    }
}

#[tauri::command]
fn reveal_artifact(host: State<'_, Host>, path: String) -> Result<(), String> {
    let state = host.backend.lock().map_err(|_| "后端不可用")?;
    let backend = state.as_ref().ok_or("请先连接工作区")?;
    let target = backend
        .workspace
        .join(path)
        .canonicalize()
        .map_err(|_| "文件尚未生成")?;
    if !target.starts_with(&backend.workspace) {
        return Err("路径超出工作区".into());
    }
    Command::new("/usr/bin/open")
        .arg("-R")
        .arg(target)
        .status()
        .map_err(|_| "无法打开 Finder")?;
    Ok(())
}

fn validate_audio_path(
    workspace: &std::path::Path,
    path: &std::path::Path,
) -> Result<PathBuf, String> {
    let target = path.canonicalize().map_err(|_| "录音文件不存在")?;
    let input = target.parent().ok_or("录音路径无效")?;
    let task = input.parent().ok_or("任务路径无效")?;
    if !target.starts_with(workspace) || input.file_name().and_then(|s| s.to_str()) != Some("input")
    {
        return Err("只允许播放工作区内的受管录音".into());
    }
    let manifest_path = task
        .join("manifest.json")
        .canonicalize()
        .map_err(|_| "任务信息不存在")?;
    if !manifest_path.starts_with(task) {
        return Err("任务信息路径无效".into());
    }
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(manifest_path).map_err(|_| "任务信息不可读")?)
            .map_err(|_| "任务信息无效")?;
    let stored = manifest["input"]["path"].as_str().ok_or("缺少录音路径")?;
    let expected = task
        .join(stored)
        .canonicalize()
        .map_err(|_| "录音路径无效")?;
    if expected != target || !expected.starts_with(input) {
        return Err("录音与任务信息不匹配".into());
    }
    if !matches!(
        target.extension().and_then(|s| s.to_str()),
        Some("m4a" | "mp3" | "mp4" | "wav" | "flac" | "ogg" | "aac" | "aiff" | "aif" | "m4b")
    ) {
        return Err("此文件不是受支持的录音格式".into());
    }
    Ok(target)
}

#[tauri::command]
fn authorize_audio(
    app: tauri::AppHandle,
    host: State<'_, Host>,
    path: String,
) -> Result<String, String> {
    let state = host.backend.lock().map_err(|_| "后端不可用")?;
    let backend = state.as_ref().ok_or("请先连接工作区")?;
    let target = validate_audio_path(&backend.workspace, &PathBuf::from(path))?;
    app.asset_protocol_scope()
        .allow_file(&target)
        .map_err(|_| "无法授权录音播放")?;
    Ok(target.to_string_lossy().into())
}

#[tauri::command]
fn open_model_access() -> Result<(), String> {
    Command::new("/usr/bin/open")
        .arg("https://huggingface.co/pyannote/speaker-diarization-community-1")
        .status()
        .map_err(|_| "无法打开浏览器".to_string())?;
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(Host::default())
        .invoke_handler(tauri::generate_handler![
            connect_workspace,
            backend_request,
            disconnect_workspace,
            reveal_artifact,
            authorize_audio,
            open_model_access
        ])
        .build(tauri::generate_context!())
        .expect("Unable to start Sonafolio")
        .run(|app, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(backend) = app.state::<Host>().backend.lock().unwrap().take() {
                    backend.stop();
                }
            }
        });
}

#[cfg(test)]
mod tests {
    use super::validate_audio_path;
    use serde_json::json;
    #[test]
    fn audio_authorization_requires_a_managed_input_and_matching_manifest() {
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../.tmp")
            .join(format!("audio-scope-{}", std::process::id()));
        std::fs::create_dir_all(root.join("task/input")).unwrap();
        let root = root.canonicalize().unwrap();
        let audio = root.join("task/input/recording.m4a");
        std::fs::write(&audio, b"synthetic").unwrap();
        let manifest = root.join("task/manifest.json");
        std::fs::write(
            &manifest,
            json!({"input":{"path":"input/recording.m4a"}}).to_string(),
        )
        .unwrap();
        assert_eq!(validate_audio_path(&root, &audio).unwrap(), audio);
        let other = root.join("task/input/other.m4a");
        std::fs::write(&other, b"other").unwrap();
        assert!(validate_audio_path(&root, &other).is_err());
        assert!(validate_audio_path(&root.join("elsewhere"), &audio).is_err());
        std::fs::write(
            &manifest,
            json!({"input":{"path":"../outside.m4a"}}).to_string(),
        )
        .unwrap();
        assert!(validate_audio_path(&root, &audio).is_err());
        #[cfg(unix)]
        {
            let outside = root.join("outside.m4a");
            std::fs::write(&outside, b"outside").unwrap();
            std::fs::remove_file(&audio).unwrap();
            std::os::unix::fs::symlink(&outside, &audio).unwrap();
            assert!(validate_audio_path(&root, &audio).is_err());
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
